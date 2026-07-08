"""Secret handling, log redaction, and production security checks."""

from __future__ import annotations

import os
import re
import secrets
from typing import Iterable

WEAK_SECRET_KEYS = frozenset({
    "",
    "dev-secret-change-in-production",
    "dev-secret-key-change-me",
    "change-me-to-random-string",
    "changeme",
})

MIN_SECRET_KEY_LENGTH = 32
MIN_APP_PASSWORD_LENGTH = 16

_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{10,}", re.I),
    re.compile(r"sk-[a-zA-Z0-9]{10,}", re.I),
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]+", re.I),
    re.compile(r"token=[^&\s\"']+", re.I),
    re.compile(r"(?i)(api[_-]?key|secret|password|authorization)\s*[:=]\s*['\"]?[^'\"\s&]+"),
]


def is_production() -> bool:
    """True when deployed or explicitly hardened."""
    if os.getenv("RAILWAY_ENVIRONMENT"):
        return True
    return os.getenv("ENFORCE_SECURITY", "").lower() in ("1", "true", "yes")


def getenv_secret(name: str, default: str = "") -> str:
    """Read a secret env var (stripped). Never log the return value."""
    return os.getenv(name, default).strip()


def redact_secrets(text: str, *, extra_values: Iterable[str] | None = None) -> str:
    """Remove API keys and tokens from log/error strings."""
    if not text:
        return text
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    if extra_values:
        for value in extra_values:
            if value and len(value) >= 8:
                out = out.replace(value, "[REDACTED]")
    return out


def validate_security_config(
    *,
    secret_key: str,
    app_password: str,
    app_password_hash: str,
) -> None:
    """
    Refuse to start in production with weak defaults.
    Raises RuntimeError when misconfigured.
    """
    if not is_production():
        return

    problems: list[str] = []

    if secret_key in WEAK_SECRET_KEYS or len(secret_key) < MIN_SECRET_KEY_LENGTH:
        problems.append(
            f"SECRET_KEY must be a random string of at least {MIN_SECRET_KEY_LENGTH} "
            "characters in production (e.g. openssl rand -hex 32)"
        )

    if app_password_hash:
        if not app_password_hash.startswith("$2"):
            problems.append("APP_PASSWORD_HASH must be a bcrypt hash ($2b$...)")
    elif not app_password or app_password in ("changeme", "postpilot"):
        problems.append(
            "Set APP_PASSWORD_HASH (recommended) or a strong APP_PASSWORD "
            f"(min {MIN_APP_PASSWORD_LENGTH} chars). "
            "Generate hash: python scripts/hash_password.py 'your-password'"
        )
    elif len(app_password) < MIN_APP_PASSWORD_LENGTH:
        problems.append(
            f"APP_PASSWORD must be at least {MIN_APP_PASSWORD_LENGTH} characters in production, "
            "or use APP_PASSWORD_HASH"
        )

    if problems:
        msg = "PostPilot security check failed:\n- " + "\n- ".join(problems)
        import logging
        logging.getLogger("postpilot").error(msg)
        raise RuntimeError(msg)


def safe_compare_password(plain: str, expected: str) -> bool:
    """Constant-time comparison for legacy plain-text passwords."""
    if not plain or not expected:
        return False
    return secrets.compare_digest(plain.encode("utf-8"), expected.encode("utf-8"))
