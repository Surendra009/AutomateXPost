"""Unified LLM providers — DeepSeek (default), Anthropic, OpenAI."""

from __future__ import annotations

import time

import httpx

from config import (
    ANTHROPIC_API_KEY,
    DEEPSEEK_API_BASE,
    FILTER_MODEL,
    OPENAI_API_KEY,
    get_deepseek_key,
)
from logging_config import setup_logging
from security import redact_secrets

logger = setup_logging()

_last_llm_error: str | None = None


def get_last_llm_error() -> str | None:
    return _last_llm_error


def _set_llm_error(message: str | None) -> None:
    global _last_llm_error
    _last_llm_error = message


def deepseek_configured() -> bool:
    return bool(get_deepseek_key())


def _http_error_message(label: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return f"{label} rejected the API key (HTTP 401) — verify the key value on Railway"
        if code == 402:
            return f"{label} account has insufficient balance (HTTP 402)"
        if code == 429:
            return f"{label} rate limit hit (HTTP 429) — try again in a minute"
        if code >= 500:
            return f"{label} server error (HTTP {code}) — try again shortly"
        try:
            body = exc.response.json()
            msg = body.get("error", {}).get("message") or body.get("message")
            if msg:
                return f"{label} HTTP {code}: {redact_secrets(str(msg))[:160]}"
        except Exception:
            pass
        return f"{label} HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{label} request timed out — try again or reduce batch size"
    if isinstance(exc, httpx.HTTPError):
        return f"{label} network error — {redact_secrets(str(exc))[:120]}"
    return f"{label} error — {redact_secrets(str(exc))[:120]}"


def _call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    system: str,
    user: str,
    model: str,
    max_tokens: int,
    label: str,
    retries: int = 2,
) -> str | None:
    url = f"{base_url.rstrip('/')}/chat/completions"
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=90) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.4,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                _set_llm_error(None)
                return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            last_exc = exc
            msg = _http_error_message(label, exc)
            _set_llm_error(msg)
            logger.error("%s API error: %s", label, msg)
            retryable = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
                429,
                500,
                502,
                503,
                504,
            )
            if retryable and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    if last_exc is not None:
        _set_llm_error(_http_error_message(label, last_exc))
    return None


def _call_deepseek(system: str, user: str, model: str, *, max_tokens: int = 1024) -> str | None:
    key = get_deepseek_key()
    if not key:
        _set_llm_error("DEEPSEEK_API_KEY not set — add it in Railway Variables and redeploy")
        return None
    return _call_openai_compatible(
        base_url=DEEPSEEK_API_BASE,
        api_key=key,
        system=system,
        user=user,
        model=model,
        max_tokens=max_tokens,
        label="DeepSeek",
    )


def _call_openai(system: str, user: str, model: str, *, max_tokens: int = 1024) -> str | None:
    if not OPENAI_API_KEY:
        _set_llm_error("OPENAI_API_KEY not set")
        return None
    return _call_openai_compatible(
        base_url="https://api.openai.com/v1",
        api_key=OPENAI_API_KEY,
        system=system,
        user=user,
        model=model,
        max_tokens=max_tokens,
        label="OpenAI",
    )


def _call_anthropic(system: str, user: str, model: str, *, max_tokens: int = 1024, retry: bool = True) -> str | None:
    if not ANTHROPIC_API_KEY:
        _set_llm_error("ANTHROPIC_API_KEY not set")
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        _set_llm_error(None)
        return message.content[0].text
    except Exception as exc:
        msg = _http_error_message("Anthropic", exc)
        _set_llm_error(msg)
        logger.error("Anthropic API error: %s", msg)
        if retry:
            return _call_anthropic(system, user, model, max_tokens=max_tokens, retry=False)
        return None


def resolve_provider(explicit: str) -> str:
    provider = (explicit or "auto").lower().strip()
    if provider != "auto":
        return provider
    if get_deepseek_key():
        return "deepseek"
    if ANTHROPIC_API_KEY:
        return "anthropic"
    if OPENAI_API_KEY:
        return "openai"
    return "none"


def call_llm(
    system: str,
    user: str,
    *,
    model: str,
    provider: str = "auto",
    max_tokens: int = 1024,
    retry: bool = True,
) -> str | None:
    """Route to DeepSeek, Anthropic, or OpenAI based on provider setting."""
    resolved = resolve_provider(provider)
    if resolved == "deepseek":
        out = _call_deepseek(system, user, model, max_tokens=max_tokens)
        if out:
            return out
        if retry and ANTHROPIC_API_KEY:
            logger.info("DeepSeek failed — falling back to Anthropic")
            return _call_anthropic(system, user, FILTER_MODEL, max_tokens=max_tokens, retry=False)
        return None
    if resolved == "openai":
        return _call_openai(system, user, model, max_tokens=max_tokens)
    if resolved == "anthropic":
        return _call_anthropic(system, user, model, max_tokens=max_tokens, retry=retry)
    _set_llm_error("No LLM API key configured — set DEEPSEEK_API_KEY on Railway and redeploy")
    logger.warning("No LLM provider configured")
    return None


def test_llm_connection() -> dict:
    """Live DeepSeek / fallback provider check for settings diagnostics."""
    from config import DRAFT_MODEL, FILTER_MODEL, DRAFT_PROVIDER, FILTER_PROVIDER

    draft_p = resolve_provider(DRAFT_PROVIDER)
    filter_p = resolve_provider(FILTER_PROVIDER)
    result = {
        "deepseek_configured": deepseek_configured(),
        "deepseek_env_var": None,
        "draft_provider": draft_p,
        "filter_provider": filter_p,
        "draft_model": DRAFT_MODEL,
        "filter_model": FILTER_MODEL,
        "filter_ok": False,
        "draft_ok": False,
        "error": None,
    }

    import os
    from config import DEEPSEEK_ENV_NAMES

    for name in DEEPSEEK_ENV_NAMES:
        if os.getenv(name, "").strip():
            result["deepseek_env_var"] = name
            break

    if draft_p == "none":
        result["error"] = (
            "No LLM API key found. Set DEEPSEEK_API_KEY in Railway → Variables → redeploy."
        )
        return result

    filter_reply = call_llm(
        "Reply with JSON only.",
        'Return exactly: {"ok": true}',
        model=FILTER_MODEL,
        provider=FILTER_PROVIDER,
        max_tokens=32,
        retry=False,
    )
    if filter_reply:
        result["filter_ok"] = True
    else:
        result["error"] = get_last_llm_error() or "Filter LLM test failed"
        return result

    draft_reply = call_llm(
        "Reply with one word only.",
        "Say OK",
        model=DRAFT_MODEL,
        provider=DRAFT_PROVIDER,
        max_tokens=8,
        retry=False,
    )
    if draft_reply:
        result["draft_ok"] = True
    else:
        result["error"] = get_last_llm_error() or "Draft LLM test failed"

    return result


def llm_status() -> dict:
    from config import DRAFT_MODEL, FILTER_MODEL, DRAFT_PROVIDER, FILTER_PROVIDER

    draft_p = resolve_provider(DRAFT_PROVIDER)
    filter_p = resolve_provider(FILTER_PROVIDER)
    return {
        "deepseek_configured": deepseek_configured(),
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "openai_configured": bool(OPENAI_API_KEY),
        "draft_provider": draft_p,
        "draft_model": DRAFT_MODEL,
        "filter_provider": filter_p,
        "filter_model": FILTER_MODEL,
        "last_error": get_last_llm_error(),
    }
