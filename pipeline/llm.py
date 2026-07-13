"""LLM calls for chat — DeepSeek, Anthropic, or OpenAI."""

from __future__ import annotations

import httpx

from config import (
    ANTHROPIC_API_KEY,
    CHAT_ANTHROPIC_MODEL,
    CHAT_DEEPSEEK_MODEL,
    CHAT_MODEL,
    CHAT_OPENAI_MODEL,
    CHAT_PROVIDER,
    DEEPSEEK_DEFAULT_MODEL,
    OPENAI_API_KEY,
)
from logging_config import setup_logging
from pipeline.filter import _call_claude
from pipeline.llm_providers import call_llm, deepseek_configured, resolve_provider
from security import redact_secrets

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
        logger.error("OpenAI API error: %s", redact_secrets(str(exc)))
        return None


def _resolve_chat_model() -> tuple[str, str]:
    """Return (provider, model_id)."""
    provider = resolve_provider(CHAT_PROVIDER)
    if provider == "none":
        return "none", ""

    if provider == "deepseek":
        model = CHAT_MODEL or CHAT_DEEPSEEK_MODEL or DEEPSEEK_DEFAULT_MODEL
        return "deepseek", model

    if provider == "openai":
        model = CHAT_MODEL or CHAT_OPENAI_MODEL
        return "openai", model

    model = CHAT_MODEL or CHAT_ANTHROPIC_MODEL
    return "anthropic", model


def call_chat_llm(system: str, user: str, *, max_tokens: int = 450) -> str | None:
    """
    Chat search + query expansion.

    Provider order when CHAT_PROVIDER=auto:
    1. DeepSeek (if DEEPSEEK_API_KEY set — same as draft pipeline)
    2. Anthropic (Claude Haiku by default)
    3. OpenAI (GPT-4o mini by default)

    Override with CHAT_PROVIDER and CHAT_MODEL.
    """
    provider, model = _resolve_chat_model()
    if provider == "none":
        return None

    if provider == "deepseek":
        return call_llm(
            system,
            user,
            model=model,
            provider=CHAT_PROVIDER,
            max_tokens=max_tokens,
            role="filter",
        )

    if provider == "openai":
        return _call_openai(system, user, model, max_tokens=max_tokens)

    if not ANTHROPIC_API_KEY:
        if deepseek_configured():
            return call_llm(
                system,
                user,
                model=CHAT_DEEPSEEK_MODEL or DEEPSEEK_DEFAULT_MODEL,
                provider="deepseek",
                max_tokens=max_tokens,
                role="filter",
            )
        if OPENAI_API_KEY:
            return _call_openai(system, user, CHAT_MODEL or CHAT_OPENAI_MODEL, max_tokens=max_tokens)
        return None

    return _call_claude(system, user, model, max_tokens=max_tokens)


def chat_llm_status() -> dict[str, str | bool]:
    provider, model = _resolve_chat_model()
    return {
        "provider": provider,
        "model": model,
        "deepseek_configured": deepseek_configured(),
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "openai_configured": bool(OPENAI_API_KEY),
    }
