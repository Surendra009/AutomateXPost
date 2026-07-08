import time
from collections import defaultdict
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import APP_PASSWORD, APP_PASSWORD_HASH, SECRET_KEY
from security import safe_compare_password

serializer = URLSafeTimedSerializer(SECRET_KEY)
SESSION_COOKIE = "postpilot_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Simple in-memory rate limiting for login
_login_attempts: dict[str, list[float]] = defaultdict(list)
_action_attempts: dict[str, list[float]] = defaultdict(list)
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


def check_action_rate_limit(
    request: Request,
    action: str,
    *,
    max_calls: int = 30,
    window_seconds: int = 60,
) -> None:
    """Limit expensive authenticated actions per IP (chat, pipeline run)."""
    ip = _client_ip(request)
    key = f"{ip}:{action}"
    now = time.time()
    _action_attempts[key] = [t for t in _action_attempts[key] if now - t < window_seconds]
    if len(_action_attempts[key]) >= max_calls:
        raise HTTPException(status_code=429, detail="Too many requests. Slow down.")
    _action_attempts[key].append(now)


def create_session_token() -> str:
    return serializer.dumps({"authenticated": True, "v": 1})


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
        samesite="strict",
        max_age=SESSION_MAX_AGE,
        secure=bool(secure),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def authenticate(password: str) -> bool:
    if not password:
        return False
    if APP_PASSWORD_HASH:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                APP_PASSWORD_HASH.encode("utf-8"),
            )
        except ValueError:
            return False
    if APP_PASSWORD:
        return safe_compare_password(password, APP_PASSWORD)
    return False


def get_session_from_request(request: Request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


def require_auth(request: Request) -> None:
    token = get_session_from_request(request)
    if not token or not verify_session_token(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
