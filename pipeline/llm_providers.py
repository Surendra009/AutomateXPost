"""Unified LLM providers — DeepSeek (default), Anthropic, OpenAI."""

from __future__ import annotations

import time

import httpx

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_DRAFT_MODEL,
    ANTHROPIC_FILTER_MODEL,
    DEEPSEEK_API_BASE,
    DEEPSEEK_DEFAULT_MODEL,
    OPENAI_DRAFT_MODEL,
    OPENAI_FILTER_MODEL,
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


_DEEPSEEK_MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-chat",
    "deepseek-v4": "deepseek-chat",
    "deepseek-v3": "deepseek-chat",
    "deepseek-chat-v3": "deepseek-chat",
}


def _looks_deepseek_model(model: str) -> bool:
    lower = model.lower()
    return lower.startswith("deepseek") or lower in _DEEPSEEK_MODEL_ALIASES


def _looks_anthropic_model(model: str) -> bool:
    lower = model.lower()
    return lower.startswith("claude")


# Anthropic 404s on retired model ids. Remap ids that users may still have in
# Railway env vars (or that older builds of this app recommended) to active
# replacements per https://platform.claude.com/docs/en/about-claude/model-deprecations
_ANTHROPIC_MODEL_ALIASES = {
    "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-latest": "claude-haiku-4-5-20251001",
    "claude-3-haiku-20240307": "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-5-sonnet-latest": "claude-sonnet-4-6",
    "claude-3-7-sonnet-20250219": "claude-sonnet-4-6",
    "claude-3-7-sonnet-latest": "claude-sonnet-4-6",
    "claude-3-sonnet-20240229": "claude-sonnet-4-6",
    "claude-3-opus-20240229": "claude-sonnet-4-6",
    "claude-3-opus-latest": "claude-sonnet-4-6",
    "claude-sonnet-4-20250514": "claude-sonnet-4-6",
    "claude-opus-4-20250514": "claude-sonnet-4-6",
}


def _anthropic_alias(model: str) -> str:
    return _ANTHROPIC_MODEL_ALIASES.get(model.lower().strip(), model)


def _looks_openai_model(model: str) -> bool:
    lower = model.lower()
    return lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("o3")


def resolve_model_for_provider(provider: str, model: str, *, role: str = "filter") -> str:
    """Map env model names to a valid model id for the chosen provider."""
    raw = (model or "").strip()
    lower = raw.lower()
    default_anthropic = ANTHROPIC_DRAFT_MODEL if role == "draft" else ANTHROPIC_FILTER_MODEL
    default_openai = OPENAI_DRAFT_MODEL if role == "draft" else OPENAI_FILTER_MODEL

    if provider == "deepseek":
        if not raw or _looks_anthropic_model(raw) or _looks_openai_model(raw):
            return DEEPSEEK_DEFAULT_MODEL
        return _DEEPSEEK_MODEL_ALIASES.get(lower, raw)

    if provider == "anthropic":
        if not raw or _looks_deepseek_model(raw) or _looks_openai_model(raw):
            return _anthropic_alias(default_anthropic)
        return _anthropic_alias(raw)

    if provider == "openai":
        if not raw or _looks_deepseek_model(raw) or _looks_anthropic_model(raw):
            return default_openai
        return raw

    return raw or DEEPSEEK_DEFAULT_MODEL


def _anthropic_error_message(exc: Exception, model: str = "") -> str:
    status = getattr(exc, "status_code", None)
    if status == 404:
        return (
            f"Anthropic model '{model or 'unknown'}' not found — likely retired. "
            "Unset ANTHROPIC_FILTER_MODEL / ANTHROPIC_DRAFT_MODEL on Railway to use "
            "the defaults, or set an active id (claude-haiku-4-5-20251001 for filter, "
            "claude-sonnet-4-6 for draft)"
        )
    if status == 401:
        return "Anthropic rejected the API key (HTTP 401)"
    if status == 429:
        return "Anthropic rate limit hit (HTTP 429)"
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", {})
        msg = err.get("message") if isinstance(err, dict) else None
        if msg:
            return f"Anthropic HTTP {status}: {redact_secrets(str(msg))[:160]}"
    return _http_error_message("Anthropic", exc)


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
        msg = _anthropic_error_message(exc, model)
        _set_llm_error(msg)
        logger.error("Anthropic API error: %s", msg)
        # 404 (retired/invalid model) won't succeed on retry with the same id
        if retry and getattr(exc, "status_code", None) != 404:
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
    role: str = "filter",
) -> str | None:
    """Route to DeepSeek, Anthropic, or OpenAI based on provider setting."""
    resolved = resolve_provider(provider)
    model = resolve_model_for_provider(resolved, model, role=role)
    explicit = (provider or "auto").lower().strip()
    if resolved == "deepseek":
        out = _call_deepseek(system, user, model, max_tokens=max_tokens)
        if out:
            return out
        deepseek_error = get_last_llm_error()
        if retry and explicit == "auto" and ANTHROPIC_API_KEY:
            fallback = resolve_model_for_provider("anthropic", "", role=role)
            logger.info("DeepSeek failed — falling back to Anthropic (%s)", fallback)
            out = _call_anthropic(system, user, fallback, max_tokens=max_tokens, retry=False)
            if out:
                return out
            # Keep the primary failure visible — the fallback error alone
            # misdirects debugging toward Anthropic when DeepSeek is the issue
            _set_llm_error(
                f"DeepSeek failed: {deepseek_error or 'unknown error'} | "
                f"Anthropic fallback failed: {get_last_llm_error() or 'unknown error'}"
            )
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
    effective_filter_model = resolve_model_for_provider(filter_p, FILTER_MODEL, role="filter")
    effective_draft_model = resolve_model_for_provider(draft_p, DRAFT_MODEL, role="draft")
    result = {
        "deepseek_configured": deepseek_configured(),
        "deepseek_env_var": None,
        "draft_provider": draft_p,
        "filter_provider": filter_p,
        "draft_model": DRAFT_MODEL,
        "filter_model": FILTER_MODEL,
        "effective_draft_model": effective_draft_model,
        "effective_filter_model": effective_filter_model,
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
        role="filter",
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
        role="draft",
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
        "effective_draft_model": resolve_model_for_provider(draft_p, DRAFT_MODEL, role="draft"),
        "filter_provider": filter_p,
        "filter_model": FILTER_MODEL,
        "effective_filter_model": resolve_model_for_provider(filter_p, FILTER_MODEL, role="filter"),
        "last_error": get_last_llm_error(),
    }
