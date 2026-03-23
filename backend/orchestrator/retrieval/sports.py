from __future__ import annotations

from datetime import datetime, timezone
import re

import httpx

from orchestrator.retrieval.fetch import RetrievedDocument
from orchestrator.retrieval.sanitize import wrap_untrusted_source
from orchestrator.security.taint import TaintedString


_NBA_STANDINGS_HINTS = (
    "nba standings",
    "latest nba standings",
    "current nba standings",
    "nba table",
    "nba ranking",
    "nba rankings",
)


def is_nba_standings_query(query: str) -> bool:
    lowered = str(query or "").lower()
    if "nba" not in lowered:
        return False
    if any(hint in lowered for hint in _NBA_STANDINGS_HINTS):
        return True
    return bool(re.search(r"\bnba\b.*\bstandings\b", lowered))


def _collect_entry_nodes(node: object, out: list[dict]) -> None:
    if isinstance(node, dict):
        entries = node.get("entries")
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    out.append(item)
        for value in node.values():
            _collect_entry_nodes(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_entry_nodes(item, out)


def _pick_stat(stats: list[dict], names: tuple[str, ...]) -> str:
    lookup = {str(item.get("name", "")).lower(): item for item in stats if isinstance(item, dict)}
    for name in names:
        item = lookup.get(name.lower())
        if not item:
            continue
        display = item.get("displayValue")
        value = item.get("value")
        if display is not None and str(display).strip():
            return str(display).strip()
        if value is not None:
            return str(value).strip()
    return "?"


async def fetch_nba_standings_document(query: str, *, timeout_seconds: float = 10.0) -> RetrievedDocument | None:
    if not is_nba_standings_query(query):
        return None

    url = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()

    entries: list[dict] = []
    _collect_entry_nodes(payload, entries)
    if not entries:
        return None

    rows: list[dict[str, str]] = []
    for item in entries:
        team = item.get("team") if isinstance(item.get("team"), dict) else {}
        team_name = str(team.get("displayName", "")).strip() or str(team.get("shortDisplayName", "")).strip()
        stats = item.get("stats") if isinstance(item.get("stats"), list) else []
        if not team_name or not isinstance(stats, list):
            continue
        wins = _pick_stat(stats, ("wins",))
        losses = _pick_stat(stats, ("losses",))
        pct = _pick_stat(stats, ("winPercent", "winpercentage"))
        gb = _pick_stat(stats, ("gamesBehind",))
        rows.append(
            {
                "team": team_name,
                "wins": wins,
                "losses": losses,
                "pct": pct,
                "gb": gb,
            }
        )

    if not rows:
        return None

    def _pct_value(value: str) -> float:
        try:
            return float(value)
        except Exception:
            return -1.0

    rows.sort(key=lambda row: _pct_value(row["pct"]), reverse=True)
    top_rows = rows[:15]
    lines = ["NBA standings snapshot (from ESPN API):"]
    for idx, row in enumerate(top_rows, start=1):
        lines.append(
            f"{idx}. {row['team']} — {row['wins']}-{row['losses']} (PCT {row['pct']}, GB {row['gb']})"
        )
    now_utc = datetime.now(timezone.utc).isoformat()
    lines.append(f"Observed at UTC: {now_utc}")
    lines.append("Source: ESPN standings API.")
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
        title="NBA standings (ESPN API)",
        retrieved_at=now_utc,
        text=tainted,
    )

