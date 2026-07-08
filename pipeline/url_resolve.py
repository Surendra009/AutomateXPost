"""Resolve redirect URLs (e.g. Google News) to canonical article links."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from config import SEC_USER_AGENT
from logging_config import setup_logging

logger = setup_logging()

_REDIRECT_HOSTS = ("news.google.com", "google.com", "www.google.com")
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_OG_IMAGE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.I,
)


def resolve_article_url(url: str, *, timeout: float = 12.0) -> str:
    """Follow redirects and unwrap Google News links to the publisher URL."""
    if not url or not url.startswith("http"):
        return url

    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return url

    if not any(host == h or host.endswith(f".{h}") for h in _REDIRECT_HOSTS):
        return url

    headers = {"User-Agent": SEC_USER_AGENT}
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = client.get(url)
            final = str(resp.url)
            if final and final != url and not _is_google_host(final):
                return final
    except Exception as exc:
        logger.debug("URL resolve failed for %s: %s", url[:80], exc)

    return url


def _is_google_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        return any(host == h or host.endswith(f".{h}") for h in _REDIRECT_HOSTS)
    except Exception:
        return False


def fetch_og_image(url: str, *, timeout: float = 10.0) -> str | None:
    """Return og:image URL if present on the page."""
    resolved = resolve_article_url(url, timeout=timeout)
    if not resolved.startswith("http"):
        return None
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(resolved)
            resp.raise_for_status()
            html = resp.text[:100_000]
    except Exception:
        return None

    for pattern in (_OG_IMAGE, _OG_IMAGE_ALT):
        match = pattern.search(html)
        if match:
            return match.group(1).strip()
    return None
