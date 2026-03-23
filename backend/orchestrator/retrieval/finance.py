from __future__ import annotations

from datetime import datetime, timezone
import io
import re
import csv

import httpx

from orchestrator.retrieval.fetch import RetrievedDocument
from orchestrator.retrieval.sanitize import wrap_untrusted_source
from orchestrator.security.taint import TaintedString


_CRYPTO_HINTS = (
    "bitcoin",
    "ethereum",
    "btc",
    "eth",
    "crypto price",
    "price of bitcoin",
    "price of ethereum",
)

_TREASURY_HINTS = (
    "10-year treasury",
    "10 year treasury",
    "us10y",
    "us 10y",
    "10yr treasury",
    "10-year yield",
    "10 year yield",
    "dgs10",
)


def is_crypto_price_query(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(hint in lowered for hint in _CRYPTO_HINTS)


def is_treasury_yield_query(query: str) -> bool:
    lowered = str(query or "").lower()
    if any(hint in lowered for hint in _TREASURY_HINTS):
        return True
    return bool(re.search(r"\bus\s*10[- ]?year\b", lowered))


def is_finance_query(query: str) -> bool:
    return is_crypto_price_query(query) or is_treasury_yield_query(query)


def _unix_to_iso(value: object) -> str | None:
    try:
        ts = int(str(value))
    except Exception:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


async def _fetch_crypto_document(*, timeout_seconds: float) -> RetrievedDocument | None:
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum&vs_currencies=usd&include_last_updated_at=true"
    )
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        return None
    btc = payload.get("bitcoin") if isinstance(payload.get("bitcoin"), dict) else {}
    eth = payload.get("ethereum") if isinstance(payload.get("ethereum"), dict) else {}
    btc_usd = btc.get("usd")
    eth_usd = eth.get("usd")
    if btc_usd is None and eth_usd is None:
        return None

    now_utc = datetime.now(timezone.utc).isoformat()
    btc_updated_iso = _unix_to_iso(btc.get("last_updated_at"))
    eth_updated_iso = _unix_to_iso(eth.get("last_updated_at"))
    summary = (
        f"Bitcoin (BTC) USD: {btc_usd}\n"
        f"Ethereum (ETH) USD: {eth_usd}\n"
        f"BTC source timestamp (UTC): {btc_updated_iso or 'unknown'}\n"
        f"ETH source timestamp (UTC): {eth_updated_iso or 'unknown'}\n"
        f"Observed at UTC: {now_utc}\n"
        "Source: CoinGecko simple price API."
    )
    wrapped = wrap_untrusted_source(summary)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=url,
        taint_level="untrusted",
    )
    return RetrievedDocument(
        url=url,
        title="CoinGecko BTC/ETH spot prices",
        retrieved_at=now_utc,
        text=tainted,
    )


async def _fetch_us10y_from_yahoo(*, timeout_seconds: float) -> RetrievedDocument | None:
    # Yahoo chart endpoint is public and returns ^TNX where value ~= 10x yield percent.
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=1d&interval=1m"
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    try:
        result = payload["chart"]["result"][0]
        meta = result.get("meta", {})
        regular_market_price = meta.get("regularMarketPrice")
        previous_close = meta.get("chartPreviousClose")
        regular_market_time = meta.get("regularMarketTime")
    except Exception:
        return None
    if regular_market_price is None:
        return None

    try:
        current_yield_pct = float(regular_market_price) / 10.0
    except Exception:
        return None
    prev_yield_pct: float | None = None
    if previous_close is not None:
        try:
            prev_yield_pct = float(previous_close) / 10.0
        except Exception:
            prev_yield_pct = None

    now_utc = datetime.now(timezone.utc).isoformat()
    lines = [f"US 10Y Treasury yield (approx, %): {current_yield_pct:.3f}"]
    if prev_yield_pct is not None:
        lines.append(f"Previous close (approx, %): {prev_yield_pct:.3f}")
    lines.append(
        f"regularMarketTime (unix): {regular_market_time}\n"
        f"regularMarketTime (UTC): {_unix_to_iso(regular_market_time) or 'unknown'}"
    )
    lines.append(f"Observed at UTC: {now_utc}")
    lines.append("Source: Yahoo Finance chart API for ^TNX (value scaled by 10).")
    summary = "\n".join(lines)
    wrapped = wrap_untrusted_source(summary)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=url,
        taint_level="untrusted",
    )
    return RetrievedDocument(
        url=url,
        title="Yahoo Finance ^TNX (US 10Y Treasury yield proxy)",
        retrieved_at=now_utc,
        text=tainted,
    )


async def _fetch_us10y_from_fred(*, timeout_seconds: float) -> RetrievedDocument | None:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        body = response.text

    rows = list(csv.DictReader(io.StringIO(body)))
    series: list[tuple[str, float]] = []
    for row in rows:
        date_raw = str(row.get("DATE", "")).strip()
        value_raw = str(row.get("DGS10", "")).strip()
        if not date_raw or not value_raw or value_raw == ".":
            continue
        try:
            value = float(value_raw)
        except Exception:
            continue
        series.append((date_raw, value))
    if not series:
        # Some upstreams/proxies return transformed content that breaks CSV parsing.
        # Attempt to recover date/value pairs directly from raw text.
        for date_raw, value_raw in re.findall(r"(\d{4}-\d{2}-\d{2})\s*,\s*([0-9]+(?:\.[0-9]+)?)", body):
            try:
                series.append((date_raw, float(value_raw)))
            except Exception:
                continue
    if not series:
        now_utc = datetime.now(timezone.utc).isoformat()
        summary = (
            "US 10Y Treasury yield: unavailable (source reachable, numeric rows not parsed).\n"
            f"Observed at UTC: {now_utc}\n"
            "Source: FRED DGS10 CSV series."
        )
        wrapped = wrap_untrusted_source(summary)
        tainted = TaintedString(
            value=wrapped,
            source="retrieved_text",
            source_id=url,
            taint_level="untrusted",
        )
        return RetrievedDocument(
            url=url,
            title="FRED DGS10 (US 10Y Treasury yield)",
            retrieved_at=now_utc,
            text=tainted,
        )

    latest_date, latest_value = series[-1]
    prev_date, prev_value = series[-2] if len(series) > 1 else (latest_date, latest_value)
    delta = latest_value - prev_value
    trend = "flat"
    if delta > 0.02:
        trend = "up"
    elif delta < -0.02:
        trend = "down"
    now_utc = datetime.now(timezone.utc).isoformat()
    summary = (
        f"US 10Y Treasury yield (%): {latest_value:.3f}\n"
        f"Latest date (series): {latest_date}\n"
        f"Previous date/value: {prev_date} / {prev_value:.3f}\n"
        f"Short trend signal: {trend} ({delta:+.3f} vs prior point)\n"
        f"Observed at UTC: {now_utc}\n"
        "Source: FRED DGS10 CSV series."
    )
    wrapped = wrap_untrusted_source(summary)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=url,
        taint_level="untrusted",
    )
    return RetrievedDocument(
        url=url,
        title="FRED DGS10 (US 10Y Treasury yield)",
        retrieved_at=now_utc,
        text=tainted,
    )


async def _fetch_us10y_document(*, timeout_seconds: float) -> RetrievedDocument | None:
    # Yahoo is preferred for near-real-time values, but can return 429/403.
    try:
        yahoo = await _fetch_us10y_from_yahoo(timeout_seconds=timeout_seconds)
    except Exception:
        yahoo = None
    if yahoo is not None:
        return yahoo
    return await _fetch_us10y_from_fred(timeout_seconds=timeout_seconds)


async def fetch_finance_document(query: str, *, timeout_seconds: float = 10.0) -> RetrievedDocument | None:
    if is_crypto_price_query(query):
        doc = await _fetch_crypto_document(timeout_seconds=timeout_seconds)
        if doc is not None:
            return doc
    if is_treasury_yield_query(query):
        doc = await _fetch_us10y_document(timeout_seconds=timeout_seconds)
        if doc is not None:
            return doc
    return None
