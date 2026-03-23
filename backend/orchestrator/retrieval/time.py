from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx

from orchestrator.retrieval.fetch import RetrievedDocument
from orchestrator.retrieval.sanitize import wrap_untrusted_source
from orchestrator.security.taint import TaintedString


_TIME_HINTS = (
    "what time",
    "current time",
    "local time",
    "time in ",
    "time is it in",
)

_LOCATION_CLEANUP_SUFFIX = re.compile(
    r"\b(?:right now|now|currently|today|at the moment|include.*|with.*|please.*)\b.*$",
    flags=re.IGNORECASE,
)

_TIMEZONE_OVERRIDES: dict[str, str] = {
    "london": "Europe/London",
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "toronto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid",
    "rome": "Europe/Rome",
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "singapore": "Asia/Singapore",
    "sydney": "Australia/Sydney",
    "auckland": "Pacific/Auckland",
    "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata",
}


def is_time_query(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(hint in lowered for hint in _TIME_HINTS)


def extract_time_location(query: str) -> str | None:
    text = " ".join(str(query or "").split())
    lowered = text.lower()
    patterns = (
        r"\bwhat time is it in\s+(.+)$",
        r"\bcurrent time in\s+(.+)$",
        r"\blocal time in\s+(.+)$",
        r"\btime in\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if not match:
            continue
        start = match.start(1)
        location = text[start:].strip(" ?,.")
        location = re.split(r"[?.!]", location, maxsplit=1)[0].strip(" ?,.")
        location = _LOCATION_CLEANUP_SUFFIX.sub("", location).strip(" ?,.")
        if location:
            return location
    return None


def _format_utc_offset(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


async def fetch_time_document(query: str, *, timeout_seconds: float = 10.0) -> RetrievedDocument | None:
    if not is_time_query(query):
        return None
    location = extract_time_location(query)
    if not location:
        return None

    lowered_location = location.lower().strip()
    timezone_name = _TIMEZONE_OVERRIDES.get(lowered_location)
    label = location
    source_url = "https://www.iana.org/time-zones"

    if timezone_name is None:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            geocode_url = (
                "https://geocoding-api.open-meteo.com/v1/search"
                f"?name={quote_plus(location)}&count=1&language=en&format=json"
            )
            response = await client.get(geocode_url)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items or not isinstance(items[0], dict):
                return None
            first = items[0]
            timezone_name = str(first.get("timezone", "")).strip()
            if not timezone_name:
                return None
            name = str(first.get("name", "")).strip()
            admin1 = str(first.get("admin1", "")).strip()
            country = str(first.get("country", "")).strip()
            label = ", ".join(part for part in (name, admin1, country) if part) or location
            source_url = geocode_url

    try:
        now_local = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        return None

    now_utc = datetime.now(timezone.utc)
    date_text = f"{now_local.strftime('%A')}, {now_local.strftime('%B')} {now_local.day}, {now_local.year}"
    time_text = now_local.strftime("%H:%M:%S")
    timezone_abbr = now_local.tzname() or timezone_name
    utc_offset = _format_utc_offset(now_local)

    summary = (
        f"Location: {label}\n"
        f"Local time: {time_text}\n"
        f"Local date: {date_text}\n"
        f"Timezone: {timezone_name} ({timezone_abbr}, UTC{utc_offset})\n"
        f"Observed at UTC: {now_utc.isoformat()}\n"
        "Source: IANA timezone database with optional Open-Meteo geocoding for place->timezone resolution."
    )
    wrapped = wrap_untrusted_source(summary)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=source_url,
        taint_level="untrusted",
    )
    return RetrievedDocument(
        url=source_url,
        title=f"Current local time for {label}",
        retrieved_at=now_utc.isoformat(),
        text=tainted,
    )

