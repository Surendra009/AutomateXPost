import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import APP_PASSWORD, SECRET_KEY

serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "postpilot_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Simple in-memory rate limiting for login
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_SECONDS]
    if len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")


def record_login_attempt(request: Request) -> None:
    ip = _client_ip(request)
    _login_attempts[ip].append(time.time())


def create_session_token() -> str:
    return serializer.dumps({"authenticated": True})


def verify_session_token(token: str) -> bool:
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("authenticated") is True
    except (BadSignature, SignatureExpired):
        return False


def set_session_cookie(response: Response, token: str) -> None:
    import os

    secure = os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("HTTPS", "").lower() == "true"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        secure=bool(secure),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def authenticate(password: str) -> bool:
    return password == APP_PASSWORD


def get_session_from_request(request: Request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


def require_auth(request: Request) -> None:
    token = get_session_from_request(request)
    if not token or not verify_session_token(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
