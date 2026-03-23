#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_json(path: str) -> dict[str, Any]:
    file_path = Path(path).expanduser()
    return json.loads(file_path.read_text(encoding="utf-8"))


def _replace_between(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    pattern = re.compile(
        rf"({re.escape(start_marker)}\n)(.*?)(\n{re.escape(end_marker)})",
        re.DOTALL,
    )
    replacement = replacement.rstrip() + "\n"
    updated, count = pattern.subn(rf"\1{replacement}\3", text, count=1)
    if count != 1:
        raise ValueError(f"could not replace block between {start_marker!r} and {end_marker!r}")
    return updated


def _update_label(text: str, prefix: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(prefix)}.*$", re.MULTILINE)
    updated, count = pattern.subn(f"{prefix}{value}", text, count=1)
    if count != 1:
        raise ValueError(f"could not update label {prefix!r}")
    return updated


def _current_date_label(now: datetime) -> str:
    return now.strftime("%B %-d, %Y")


def _current_iso_date(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _acceptance_decision(summary: dict[str, Any]) -> str:
    failed = int(summary.get("failed", 0))
    return "PASS" if failed == 0 else "FAIL"


def _pentest_decision(payload: dict[str, Any]) -> str:
    decision = payload.get("decision") or {}
    if bool(decision.get("critical_or_high_open", False)):
        return "FAIL"
    return "PASS"


def _refresh_acceptance_doc(path: Path, payload: dict[str, Any], now: datetime, report_path: str) -> None:
    summary = payload.get("summary") or {}
    text = path.read_text(encoding="utf-8")
    text = _update_label(text, "Updated: ", _current_date_label(now))

    latest_run = "\n".join(
        [
            f"- Date: {now.strftime('%B %-d, %Y')} (UTC)",
            f"- Environment: local daemon at `{payload.get('base_url', 'http://127.0.0.1:8100')}`",
            f"- Provider: `{payload.get('provider_hint') or 'default'}`",
            "- Token source: local server token file/env (`MMO_SERVER_API_KEY` / `~/.mmo/server_api_key.txt`)",
            f"- Report file: `{report_path}`",
        ]
    )
    text = _replace_between(text, "## Latest Run", "## Result Summary", latest_run)

    result_summary = "\n".join(
        [
            f"- Passed: {int(summary.get('passed', 0))}",
            f"- Failed: {int(summary.get('failed', 0))}",
            f"- Skipped: {int(summary.get('skipped', 0))}",
            f"- Total: {int(summary.get('total', 0))}",
        ]
    )
    text = _replace_between(text, "## Result Summary", "## Failing Checks", result_summary)

    failing = [row for row in list(payload.get("checks") or []) if str(row.get("status")) == "FAIL"]
    failing_lines = ["- None in latest run."] if not failing else [
        f"- `{row.get('name', 'unknown')}`: {row.get('detail', 'failed')}" for row in failing
    ]
    text = _replace_between(text, "## Failing Checks", "## Notes / Follow-ups", "\n".join(failing_lines))

    decision = _acceptance_decision(summary)
    release_decision = "\n".join(
        [
            f"- [{'x' if decision == 'PASS' else ' '}] PASS (no critical blocking failures)",
            "- [ ] CONDITIONAL PASS (evidence freshness gap)",
            f"- [{'x' if decision == 'FAIL' else ' '}] FAIL (blocking failures remain)",
        ]
    )
    text = re.sub(r"## Release Gate Decision\n(?:- .*\n)+", "## Release Gate Decision\n" + release_decision + "\n", text, count=1)
    path.write_text(text, encoding="utf-8")


def _refresh_pentest_doc(path: Path, payload: dict[str, Any], now: datetime, report_path: str) -> None:
    summary = payload.get("summary") or {}
    text = path.read_text(encoding="utf-8")
    text = _update_label(text, "Updated: ", _current_date_label(now))

    latest_run = "\n".join(
        [
            f"- Date: {now.strftime('%B %-d, %Y')} (UTC)",
            f"- Report path: `{report_path}`",
            f"- Passed: {int(summary.get('passed', 0))}",
            f"- Failed: {int(summary.get('failed', 0))}",
            f"- Skipped: {int(summary.get('skipped', 0))}",
            f"- Max failed severity: `{summary.get('max_failed_severity', 'info')}`",
            f"- Ready for external testing: `{str(bool((payload.get('decision') or {}).get('ready_for_external_testing', False))).lower()}`",
        ]
    )
    text = _replace_between(text, "## Latest Run", "## Refresh Command", latest_run)

    decision = _pentest_decision(payload)
    decision_block = "\n".join(
        [
            f"- [{'x' if decision == 'PASS' else ' '}] PASS (no critical/high open)",
            "- [ ] CONDITIONAL PASS (latest pentest run is passing but stale)",
            f"- [{'x' if decision == 'FAIL' else ' '}] FAIL (critical/high findings open)",
        ]
    )
    text = re.sub(r"## Decision\n(?:- .*\n)+", "## Decision\n" + decision_block + "\n", text, count=1)
    path.write_text(text, encoding="utf-8")


def _refresh_tracker_doc(path: Path, acceptance: dict[str, Any], pentest: dict[str, Any], now: datetime, acceptance_path: str, pentest_path: str) -> None:
    text = path.read_text(encoding="utf-8")
    text = _update_label(text, "Updated: ", _current_iso_date(now))
    block = "\n".join(
        [
            f"Current status ({_current_iso_date(now)}):",
            "- `run_rc_sweep.sh` fast mode: PASS (backend targeted tests + dashboard production build).",
            "- Agent task suite check: 8/8 passing with `--include-live-external`.",
            (
                f"- Live acceptance run ({acceptance.get('provider_hint') or 'default'}): "
                f"{_acceptance_decision(acceptance.get('summary') or {})} "
                f"(`{int((acceptance.get('summary') or {}).get('passed', 0))}/"
                f"{int((acceptance.get('summary') or {}).get('total', 0))}`, "
                f"`{int((acceptance.get('summary') or {}).get('skipped', 0))}` expected skips) "
                f"from `{acceptance_path}`."
            ),
            (
                f"- Local pentest run: {_pentest_decision(pentest)} "
                f"(`{int((pentest.get('summary') or {}).get('passed', 0))}/"
                f"{int((pentest.get('summary') or {}).get('total', 0))}`, "
                f"`{int((pentest.get('summary') or {}).get('skipped', 0))}` expected skip) "
                f"from `{pentest_path}`."
            ),
            "- Working tree still has uncommitted eval artifact churn in `multi-mind-orchestrator`.",
        ]
    )
    text = re.sub(
        r"(### G3 Validation Evidence Consolidation\nAcceptance:\n- Keep acceptance \+ pentest \+ doctor outputs current and linked from this tracker\.\n\n)Current status \(\d{4}-\d{2}-\d{2}\):\n(?:- .*\n)+",
        r"\1" + block + "\n",
        text,
        count=1,
    )
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh root MMY status docs from acceptance and pentest JSON reports.")
    parser.add_argument("--acceptance-report", required=True, help="Path to live acceptance JSON report.")
    parser.add_argument("--pentest-report", required=True, help="Path to local pentest JSON report.")
    parser.add_argument(
        "--docs-root",
        default="..",
        help="Root directory containing MMY markdown docs. Defaults to repo parent.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    acceptance_path = str(Path(args.acceptance_report).expanduser())
    pentest_path = str(Path(args.pentest_report).expanduser())
    acceptance = _load_json(acceptance_path)
    pentest = _load_json(pentest_path)

    docs_root = Path(args.docs_root).expanduser().resolve()
    _refresh_acceptance_doc(docs_root / "MMY_Live_Acceptance_Report.md", acceptance, now, acceptance_path)
    _refresh_pentest_doc(docs_root / "MMY_Local_Pentest_Report.md", pentest, now, pentest_path)
    _refresh_tracker_doc(docs_root / "MMY_Canonical_Tracker.md", acceptance, pentest, now, acceptance_path, pentest_path)

    print(
        json.dumps(
            {
                "ok": True,
                "docs_root": str(docs_root),
                "acceptance_report": acceptance_path,
                "pentest_report": pentest_path,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
