from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import quote_plus

import httpx

from orchestrator.retrieval.fetch import RetrievedDocument
from orchestrator.retrieval.sanitize import wrap_untrusted_source
from orchestrator.security.taint import TaintedString


@dataclass(slots=True)
class WeatherLocation:
    label: str
    latitude: float
    longitude: float


def is_weather_query(query: str) -> bool:
    lowered = str(query or "").lower()
    hints = ("weather", "temperature", "forecast", "rain", "humidity", "wind")
    return any(hint in lowered for hint in hints)


def extract_weather_location(query: str) -> str | None:
    text = " ".join(str(query or "").split())
    lowered = text.lower()
    for marker in ("weather for ", "weather in ", "temperature in ", "forecast for ", "forecast in "):
        idx = lowered.find(marker)
        if idx >= 0:
            location = text[idx + len(marker) :].strip(" ?,.")
            location = re.split(
                r"[?.!]|(?:\binclude\b)|(?:\bwith\b)|(?:\bplease\b)|(?:\bshow\b)",
                location,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" ?,.")
            location = re.sub(
                r"\b(?:right now|now|currently|today|at the moment)\b.*$",
                "",
                location,
                flags=re.IGNORECASE,
            ).strip(" ?,.")
            location = re.sub(
                r"\b(?:is|please|can you check|can you look up)\b\s*$",
                "",
                location,
                flags=re.IGNORECASE,
            ).strip(" ?,.")
            if location:
                return location
    return None


async def fetch_weather_document(query: str, *, timeout_seconds: float = 10.0) -> RetrievedDocument | None:
    if not is_weather_query(query):
        return None
    location = extract_weather_location(query)
    if not location:
        return None

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        candidates = [location]
        if "," in location:
            parts = [part.strip() for part in location.split(",") if part.strip()]
            if parts:
                city_only = parts[0]
                city_region = ", ".join(parts[:2]) if len(parts) > 1 else parts[0]
                for candidate in (city_region, city_only):
                    if candidate and candidate not in candidates:
                        candidates.append(candidate)

        first: dict[str, object] | None = None
        for candidate in candidates:
            geocode_url = (
                "https://geocoding-api.open-meteo.com/v1/search"
                f"?name={quote_plus(candidate)}&count=1&language=en&format=json"
            )
            geo_response = await client.get(geocode_url)
            geo_response.raise_for_status()
            geo_payload = geo_response.json()
            items = geo_payload.get("results") if isinstance(geo_payload, dict) else None
            if isinstance(items, list) and items and isinstance(items[0], dict):
                first = items[0]
                break
        if first is None:
            return None

        try:
            lat = float(first.get("latitude"))
            lon = float(first.get("longitude"))
        except Exception:
            return None
        name = str(first.get("name", "")).strip()
        admin1 = str(first.get("admin1", "")).strip()
        country = str(first.get("country", "")).strip()
        label = ", ".join(part for part in (name, admin1, country) if part) or location

        weather_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
            "&timezone=auto"
        )
        weather_response = await client.get(weather_url)
        weather_response.raise_for_status()
        payload = weather_response.json()
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict):
            return None

    observed_at = str(current.get("time", "")).strip() or datetime.now(timezone.utc).isoformat()
    summary = (
        f"Location: {label}\n"
        f"Observed at: {observed_at}\n"
        f"Temperature (C): {current.get('temperature_2m')}\n"
        f"Feels like (C): {current.get('apparent_temperature')}\n"
        f"Humidity (%): {current.get('relative_humidity_2m')}\n"
        f"Wind speed (km/h): {current.get('wind_speed_10m')}\n"
        f"Precipitation (mm): {current.get('precipitation')}\n"
        f"Weather code: {current.get('weather_code')}\n"
        "Source: Open-Meteo public weather API."
    )
    wrapped = wrap_untrusted_source(summary)
    tainted = TaintedString(
        value=wrapped,
        source="retrieved_text",
        source_id=weather_url,
        taint_level="untrusted",
    )
    return RetrievedDocument(
        url=weather_url,
        title=f"Open-Meteo current weather for {label}",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        text=tainted,
    )
