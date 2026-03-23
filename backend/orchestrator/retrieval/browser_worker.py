from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlparse


@dataclass(slots=True)
class BrowserSearchResult:
    title: str
    url: str
    snippet: str


@dataclass(slots=True)
class BrowserFetchedPage:
    final_url: str
    title: str
    text: str


def _normalize_duckduckgo_redirect(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com"):
        params = parse_qs(parsed.query)
        # DDG uses multiple redirect forms over time; prefer explicit target params.
        for key in ("uddg", "rut", "u"):
            target = params.get(key, [None])[0]
            if target:
                return target
        if parsed.path == "/l/":
            target = params.get("uddg", [None])[0]
            if target:
                return target
    if parsed.scheme in {"http", "https"}:
        return url
    return None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_domain_allowed(url: str, allowlist: list[str] | None, denylist: list[str] | None) -> bool:
    host = _host(url)
    if denylist and any(host == d.lower() or host.endswith(f".{d.lower()}") for d in denylist):
        return False
    if allowlist and not any(host == a.lower() or host.endswith(f".{a.lower()}") for a in allowlist):
        return False
    return True


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_identity(url: str) -> str | None:
    return url if _is_http_url(url) else None


async def _browser_collect_links(
    search_url: str,
    *,
    timeout_seconds: float,
) -> list[dict[str, str]]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("playwright is not installed for browser worker") from exc

    timeout_ms = int(max(1.0, timeout_seconds) * 1000)
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()
            await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(2500, timeout_ms))
            except Exception:
                pass
            raw_links = await page.eval_on_selector_all(
                "a[href]",
                (
                    "els => els.map(e => ({"
                    "href: e.href || '', "
                    "title: (e.textContent || '').trim()"
                    "}))"
                ),
            )
        finally:
            await browser.close()
    cleaned: list[dict[str, str]] = []
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        href = unescape(str(item.get("href", "")).strip())
        title = str(item.get("title", "")).strip()
        if href and title:
            cleaned.append({"href": href, "title": title})
    return cleaned


def _filter_browser_results(
    links: list[dict[str, str]],
    *,
    max_results: int,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
    blocked_hosts: set[str],
    normalizer,
) -> list[BrowserSearchResult]:
    results: list[BrowserSearchResult] = []
    seen: set[str] = set()
    for item in links:
        href = item["href"]
        title = item["title"]
        normalized = normalizer(href)
        if not normalized:
            continue
        host = _host(normalized)
        if not host:
            continue
        if any(host == blocked or host.endswith(f".{blocked}") for blocked in blocked_hosts):
            continue
        if normalized in seen:
            continue
        if not _is_domain_allowed(normalized, domain_allowlist, domain_denylist):
            continue
        seen.add(normalized)
        results.append(BrowserSearchResult(title=title, url=normalized, snippet=""))
        if len(results) >= max_results:
            break
    return results


async def browser_fetch_page(
    url: str,
    *,
    timeout_seconds: float,
) -> BrowserFetchedPage:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError("playwright is not installed for browser worker") from exc

    timeout_ms = int(max(1.0, timeout_seconds) * 1000)
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(2500, timeout_ms))
            except Exception:
                pass
            title = (await page.title()).strip()
            text = await page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
            )
            return BrowserFetchedPage(
                final_url=page.url,
                title=title,
                text=str(text or ""),
            )
        finally:
            await browser.close()


async def browser_search_duckduckgo(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[BrowserSearchResult]:
    query_param = quote_plus(query)
    search_url = f"https://duckduckgo.com/?q={query_param}&ia=web"
    links = await _browser_collect_links(search_url, timeout_seconds=timeout_seconds)
    return _filter_browser_results(
        links,
        max_results=max_results,
        domain_allowlist=domain_allowlist,
        domain_denylist=domain_denylist,
        blocked_hosts={"duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"},
        normalizer=_normalize_duckduckgo_redirect,
    )


async def browser_search_brave(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[BrowserSearchResult]:
    query_param = quote_plus(query)
    search_url = f"https://search.brave.com/search?q={query_param}&source=web"
    links = await _browser_collect_links(search_url, timeout_seconds=timeout_seconds)
    return _filter_browser_results(
        links,
        max_results=max_results,
        domain_allowlist=domain_allowlist,
        domain_denylist=domain_denylist,
        blocked_hosts={"search.brave.com", "brave.com", "www.brave.com"},
        normalizer=_normalize_identity,
    )
