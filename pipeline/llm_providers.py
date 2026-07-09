"""Unified LLM providers — DeepSeek (default), Anthropic, OpenAI."""

from __future__ import annotations

import httpx

from config import (
    ANTHROPIC_API_KEY,
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_BASE,
    OPENAI_API_KEY,
)
from logging_config import setup_logging
from security import redact_secrets

logger = setup_logging()


def deepseek_configured() -> bool:
    return bool(DEEPSEEK_API_KEY)


def _call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    system: str,
    user: str,
    model: str,
    max_tokens: int,
    label: str,
) -> str | None:
    url = f"{base_url.rstrip('/')}/chat/completions"
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
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.error("%s API error: %s", label, redact_secrets(str(exc)))
        return None


def _call_deepseek(system: str, user: str, model: str, *, max_tokens: int = 1024) -> str | None:
    if not DEEPSEEK_API_KEY:
        return None
    return _call_openai_compatible(
        base_url=DEEPSEEK_API_BASE,
        api_key=DEEPSEEK_API_KEY,
        system=system,
        user=user,
        model=model,
        max_tokens=max_tokens,
        label="DeepSeek",
    )


def _call_openai(system: str, user: str, model: str, *, max_tokens: int = 1024) -> str | None:
    if not OPENAI_API_KEY:
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
        return message.content[0].text
    except Exception as exc:
        logger.error("Anthropic API error: %s", redact_secrets(str(exc)))
        if retry:
            return _call_anthropic(system, user, model, max_tokens=max_tokens, retry=False)
        return None


def resolve_provider(explicit: str) -> str:
    provider = (explicit or "auto").lower().strip()
    if provider != "auto":
        return provider
    if DEEPSEEK_API_KEY:
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
            from config import FILTER_MODEL as _fb

            return _call_anthropic(system, user, _fb, max_tokens=max_tokens, retry=False)
        return None
    if resolved == "openai":
        return _call_openai(system, user, model, max_tokens=max_tokens)
    if resolved == "anthropic":
        return _call_anthropic(system, user, model, max_tokens=max_tokens, retry=retry)
    logger.warning("No LLM provider configured")
    return None


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
    }
