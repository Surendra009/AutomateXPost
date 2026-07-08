"""LLM calls for chat — Anthropic (default) or OpenAI fallback."""

from __future__ import annotations

import httpx

from config import (
    ANTHROPIC_API_KEY,
    CHAT_ANTHROPIC_MODEL,
    CHAT_MODEL,
    CHAT_OPENAI_MODEL,
    CHAT_PROVIDER,
    OPENAI_API_KEY,
)
from logging_config import setup_logging
from pipeline.filter import _call_claude

logger = setup_logging()


def _call_openai(system: str, user: str, model: str, *, max_tokens: int = 512) -> str | None:
    if not OPENAI_API_KEY:
        return None
    try:
        with httpx.Client(timeout=45) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.error("OpenAI API error: %s", exc)
        return None


def _resolve_chat_model() -> tuple[str, str]:
    """Return (provider, model_id)."""
    provider = CHAT_PROVIDER.lower().strip()
    if provider == "auto":
        if ANTHROPIC_API_KEY:
            provider = "anthropic"
        elif OPENAI_API_KEY:
            provider = "openai"
        else:
            return "none", ""

    if provider == "openai":
        model = CHAT_MODEL or CHAT_OPENAI_MODEL
        return "openai", model

    model = CHAT_MODEL or CHAT_ANTHROPIC_MODEL
    return "anthropic", model


def call_chat_llm(system: str, user: str, *, max_tokens: int = 300) -> str | None:
    """
    Summarize chat search results.

    Provider order when CHAT_PROVIDER=auto:
    1. Anthropic (Claude Haiku by default — fast/cheap)
    2. OpenAI (GPT-4o mini by default)

    Override with CHAT_PROVIDER=openai|anthropic and CHAT_MODEL.
    """
    provider, model = _resolve_chat_model()
    if provider == "none":
        return None

    if provider == "openai":
        return _call_openai(system, user, model, max_tokens=max_tokens)

    if not ANTHROPIC_API_KEY:
        if OPENAI_API_KEY:
            return _call_openai(system, user, CHAT_MODEL or CHAT_OPENAI_MODEL, max_tokens=max_tokens)
        return None

    return _call_claude(system, user, model, max_tokens=max_tokens)


def chat_llm_status() -> dict[str, str | bool]:
    provider, model = _resolve_chat_model()
    return {
        "provider": provider,
        "model": model,
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "openai_configured": bool(OPENAI_API_KEY),
    }
