from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os

from orchestrator.retrieval.browser_worker import browser_fetch_page
from orchestrator.retrieval.sanitize import html_to_text, sanitize_retrieved_text, wrap_untrusted_source
from orchestrator.security.scanners import is_ssrf_risky_url
from orchestrator.security.taint import TaintedString


@dataclass(slots=True)
class RetrievedDocument:
    url: str
    title: str
    retrieved_at: str
    text: TaintedString


async def fetch_url_content(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    max_bytes: int = 200_000,
) -> RetrievedDocument:
    if is_ssrf_risky_url(url):
        raise ValueError(f"Blocked risky URL: {url}")

    import httpx

    body = ""
    page_title = ""
    final_url = url
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.text[:max_bytes]
            page_title = _extract_title(body)
            final_url = str(response.url)
            if _looks_like_access_challenge(response.status_code, body):
                raise RuntimeError("challenge page returned by origin")
    except Exception as exc:
        if not _browser_fallback_enabled():
            raise

        browser_page = await browser_fetch_page(url, timeout_seconds=timeout_seconds)
        if not browser_page.text.strip():
            raise RuntimeError("browser fallback returned empty page text") from exc
        page_title = browser_page.title
        final_url = browser_page.final_url or url
        clean_text = sanitize_retrieved_text(browser_page.text[:max_bytes])
    else:
        clean_text = sanitize_retrieved_text(html_to_text(body))
        if _browser_fallback_enabled() and _looks_like_low_signal_content(clean_text):
            browser_page = await browser_fetch_page(url, timeout_seconds=timeout_seconds)
            if browser_page.text.strip():
                page_title = browser_page.title or page_title
                final_url = browser_page.final_url or final_url
                clean_text = sanitize_retrieved_text(browser_page.text[:max_bytes])

    wrapped = wrap_untrusted_source(clean_text)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=final_url,
        taint_level="untrusted",
    )

    return RetrievedDocument(
        url=final_url,
        title=page_title,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        text=tainted,
    )


def _browser_fallback_enabled() -> bool:
    return os.getenv("MMO_RETRIEVAL_BROWSER_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}


def _looks_like_access_challenge(status_code: int, body: str) -> bool:
    if status_code in {202, 403, 429, 503}:
        return True
    lowered = body.lower()
    markers = (
        "cf-challenge",
        "just a moment",
        "enable javascript",
        "access denied",
        "automated traffic",
        "please complete the challenge",
    )
    return any(marker in lowered for marker in markers)


def _extract_title(html: str) -> str:
    start = html.lower().find("<title")
    if start == -1:
        return ""
    start = html.find(">", start)
    if start == -1:
        return ""
    end = html.lower().find("</title>", start)
    if end == -1:
        return ""
    return html[start + 1 : end].strip()


def _looks_like_low_signal_content(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return True
    lowered = normalized.lower()
    weak_markers = (
        "enable javascript",
        "loading",
        "please wait",
        "cookies",
        "privacy choices",
        "subscribe",
        "slickstream",
        "window.",
        "function(",
    )
    if any(marker in lowered for marker in weak_markers):
        return True
    alpha_chars = sum(1 for char in normalized if char.isalpha())
    return alpha_chars < 500
