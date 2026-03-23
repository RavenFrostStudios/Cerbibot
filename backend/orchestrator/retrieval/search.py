from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlparse

from orchestrator.retrieval.browser_worker import browser_search_brave, browser_search_duckduckgo


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


_SUPPORTED_PROVIDERS = {
    "auto",
    "duckduckgo_html",
    "browser_brave",
    "browser_duckduckgo",
    "brave_api",
    "tavily_api",
    "exa_api",
    "serpapi",
}


def _extract_duckduckgo_results(html: str) -> list[SearchResult]:
    pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        flags=re.DOTALL,
    )
    results: list[SearchResult] = []
    for match in pattern.finditer(html):
        href = unescape(match.group("href"))
        title = re.sub(r"<[^>]+>", "", unescape(match.group("title"))).strip()
        snippet = re.sub(r"<[^>]+>", "", unescape(match.group("snippet"))).strip()
        url = _normalize_duckduckgo_redirect(href)
        if url and title:
            results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def _extract_duckduckgo_lite_results(html: str) -> list[SearchResult]:
    pattern = re.compile(
        r'<a[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
        flags=re.DOTALL,
    )
    results: list[SearchResult] = []
    seen: set[str] = set()
    for match in pattern.finditer(html):
        href = unescape(match.group("href"))
        title = re.sub(r"<[^>]+>", "", unescape(match.group("title"))).strip()
        if not title:
            continue
        url = _normalize_duckduckgo_redirect(href)
        if not url or url in seen:
            continue
        host = _host(url)
        if host.endswith("duckduckgo.com"):
            continue
        seen.add(url)
        results.append(SearchResult(title=title, url=url, snippet=""))
    return results


def _normalize_duckduckgo_redirect(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return target
    if parsed.scheme in {"http", "https"}:
        return url
    return None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _looks_like_duckduckgo_challenge(status_code: int, body: str) -> bool:
    if status_code in {202, 429, 503}:
        return True
    lowered = body.lower()
    markers = (
        "duckduckgo.com/50x.html",
        "anomaly",
        "automated traffic",
        "please complete the challenge",
        "detected unusual traffic",
    )
    return any(marker in lowered for marker in markers)


def _is_domain_allowed(url: str, allowlist: list[str] | None, denylist: list[str] | None) -> bool:
    host = _host(url)
    if denylist and any(host == d.lower() or host.endswith(f".{d.lower()}") for d in denylist):
        return False
    if allowlist and not any(host == a.lower() or host.endswith(f".{a.lower()}") for a in allowlist):
        return False
    return True


def _truncate_error(exc: Exception, max_len: int = 120) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _adapter_chain(provider: str) -> list[str]:
    if provider == "auto":
        configured = [
            part.strip().lower()
            for part in os.getenv(
                "MMO_RETRIEVAL_SEARCH_CHAIN",
                "brave_api,tavily_api,exa_api,serpapi,browser_brave,browser_duckduckgo,duckduckgo_html",
            ).split(",")
            if part.strip()
        ]
        chain = [item for item in configured if item in _SUPPORTED_PROVIDERS and item != "auto"]
        return chain or ["browser_brave", "browser_duckduckgo", "duckduckgo_html"]
    return [provider]


def _requires_api_key(adapter_name: str) -> str | None:
    return {
        "brave_api": "BRAVE_SEARCH_API_KEY",
        "tavily_api": "TAVILY_API_KEY",
        "exa_api": "EXA_API_KEY",
        "serpapi": "SERPAPI_API_KEY",
    }.get(adapter_name)


async def _search_browser_engine(
    engine: str,
    *,
    query: str,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    if engine == "browser_brave":
        browser_results = await browser_search_brave(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    elif engine == "browser_duckduckgo":
        browser_results = await browser_search_duckduckgo(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    else:
        raise ValueError(f"Unsupported browser search engine: {engine}")
    return [SearchResult(title=item.title, url=item.url, snippet=item.snippet) for item in browser_results]


async def _search_duckduckgo_html(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    import httpx

    query_param = quote_plus(query)
    candidates = [
        (f"https://duckduckgo.com/html/?q={query_param}", _extract_duckduckgo_results),
        (f"https://html.duckduckgo.com/html/?q={query_param}", _extract_duckduckgo_results),
        (f"https://lite.duckduckgo.com/lite/?q={query_param}", _extract_duckduckgo_lite_results),
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    last_error: Exception | None = None
    attempts = 2
    challenge_seen = False
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        for attempt in range(attempts):
            round_only_challenges = True
            round_had_candidate = False
            for url, parser in candidates:
                try:
                    response = await client.get(url, headers=headers)
                except Exception as exc:
                    last_error = exc
                    round_only_challenges = False
                    continue
                round_had_candidate = True
                if response.status_code >= 400:
                    last_error = RuntimeError(f"search HTTP {response.status_code} from {urlparse(url).netloc}")
                    round_only_challenges = False
                    continue
                if _looks_like_duckduckgo_challenge(response.status_code, response.text):
                    challenge_seen = True
                    last_error = RuntimeError("search challenge page returned by duckduckgo")
                    continue
                round_only_challenges = False
                results = parser(response.text)
                filtered = [
                    item for item in results if _is_domain_allowed(item.url, domain_allowlist, domain_denylist)
                ]
                if filtered:
                    return filtered[:max_results]
                last_error = RuntimeError("no parseable search results")
            if challenge_seen and round_had_candidate and round_only_challenges:
                break
            if attempt < attempts - 1:
                await asyncio.sleep(0.2 + random.uniform(0.0, 0.25))

    if last_error is not None:
        raise RuntimeError(f"duckduckgo search failed: {last_error}")
    return []


async def _search_brave_api(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    import httpx

    key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing BRAVE_SEARCH_API_KEY")

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
    }
    params = {"q": query, "count": max_results}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    rows = ((payload or {}).get("web") or {}).get("results") or []
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url or not _is_domain_allowed(url, domain_allowlist, domain_denylist):
            continue
        title = str(row.get("title") or url).strip()
        snippet = str(row.get("description") or "").strip()
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


async def _search_tavily_api(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    import httpx

    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing TAVILY_API_KEY")

    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post("https://api.tavily.com/search", json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json() or {}
    rows = data.get("results") or []
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url or not _is_domain_allowed(url, domain_allowlist, domain_denylist):
            continue
        title = str(row.get("title") or url).strip()
        snippet = str(row.get("content") or "").strip()
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


async def _search_exa_api(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    import httpx

    key = os.getenv("EXA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing EXA_API_KEY")

    headers = {"x-api-key": key, "Content-Type": "application/json"}
    payload = {
        "query": query,
        "numResults": max_results,
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post("https://api.exa.ai/search", headers=headers, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json() or {}
    rows = data.get("results") or []
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url or not _is_domain_allowed(url, domain_allowlist, domain_denylist):
            continue
        title = str(row.get("title") or url).strip()
        snippet = str(row.get("text") or row.get("snippet") or "").strip()
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


async def _search_serpapi(
    query: str,
    *,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    import httpx

    key = os.getenv("SERPAPI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing SERPAPI_API_KEY")

    params = {
        "engine": "google",
        "q": query,
        "api_key": key,
        "num": str(max_results),
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json() or {}
    rows = data.get("organic_results") or []
    results: list[SearchResult] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("link") or "").strip()
        if not url or not _is_domain_allowed(url, domain_allowlist, domain_denylist):
            continue
        title = str(row.get("title") or url).strip()
        snippet = str(row.get("snippet") or "").strip()
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


async def _run_adapter(
    adapter: str,
    *,
    query: str,
    max_results: int,
    timeout_seconds: float,
    domain_allowlist: list[str] | None,
    domain_denylist: list[str] | None,
) -> list[SearchResult]:
    if adapter == "duckduckgo_html":
        return await _search_duckduckgo_html(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    if adapter in {"browser_brave", "browser_duckduckgo"}:
        return await _search_browser_engine(
            adapter,
            query=query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    if adapter == "brave_api":
        return await _search_brave_api(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    if adapter == "tavily_api":
        return await _search_tavily_api(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    if adapter == "exa_api":
        return await _search_exa_api(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    if adapter == "serpapi":
        return await _search_serpapi(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            domain_allowlist=domain_allowlist,
            domain_denylist=domain_denylist,
        )
    raise ValueError(f"Unsupported search adapter: {adapter}")


async def search_web(
    query: str,
    *,
    provider: str = "auto",
    max_results: int = 5,
    timeout_seconds: float = 10.0,
    domain_allowlist: list[str] | None = None,
    domain_denylist: list[str] | None = None,
) -> list[SearchResult]:
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported search provider: {provider}")

    chain = _adapter_chain(provider)
    attempts: list[str] = []

    for adapter in chain:
        key_env = _requires_api_key(adapter)
        if key_env and not os.getenv(key_env, "").strip():
            attempts.append(f"{adapter}=skipped(no_key:{key_env})")
            continue

        try:
            results = await _run_adapter(
                adapter,
                query=query,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
                domain_allowlist=domain_allowlist,
                domain_denylist=domain_denylist,
            )
        except Exception as exc:
            attempts.append(f"{adapter}=failed({_truncate_error(exc)})")
            continue

        if results:
            return results[:max_results]
        attempts.append(f"{adapter}=no_results")

    if attempts:
        raise RuntimeError("search adapters failed: " + "; ".join(attempts))
    return []
