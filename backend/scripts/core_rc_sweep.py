#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    latency_ms: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(name: str, ok: bool, detail: str, latency_ms: int) -> CheckResult:
    return CheckResult(name=name, status="PASS" if ok else "FAIL", detail=detail, latency_ms=latency_ms)


def _skip(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, status="SKIP", detail=detail, latency_ms=0)


def _http_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 30.0,
) -> tuple[int, Any, int]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, method=method.upper(), headers=headers, data=body)
    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw.strip() else {}
            return int(resp.status), data, int((time.monotonic() - started) * 1000)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {"raw": raw}
        return int(exc.code), data, int((time.monotonic() - started) * 1000)
    except (error.URLError, TimeoutError, socket.timeout) as exc:
        return 599, {"detail": f"Network timeout/error: {type(exc).__name__}: {exc}"}, int((time.monotonic() - started) * 1000)


def _append_check(
    checks: list[CheckResult],
    name: str,
    status: int,
    data: Any,
    latency_ms: int,
    *,
    expected_status: int = 200,
    detail: str | None = None,
) -> None:
    ok = status == expected_status
    if detail is None:
        if isinstance(data, dict):
            detail = str(data.get("detail", f"HTTP {status}"))
        else:
            detail = f"HTTP {status}"
    checks.append(_result(name, ok, detail, latency_ms))


def _draft_workflow_text(skill_name: str) -> str:
    return f"""name: {skill_name}
description: RC sweep ephemeral skill
risk_level: low
manifest:
  purpose: Verify skill lifecycle endpoints in RC sweep
  tools: [system_info]
  data_scope: [repo_metadata]
  permissions: [read_repo]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1
  kill_switch:
    enabled: true
  audit_sink: server.audit
  failure_mode: fail_closed
steps:
  - id: smoke
    tool: system_info
    args: {{}}
"""


def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    checks: list[CheckResult] = []
    started_at = _now()
    session_id = f"rc-sweep-session-{int(time.time())}"
    memory_id: int | None = None

    # Core auth/health and config endpoints
    status, data, latency_ms = _http_json("GET", f"{base_url}/v1/health", None, None, timeout_seconds=args.request_timeout_seconds)
    _append_check(checks, "auth:unauthorized_rejected", status, data, latency_ms, expected_status=401, detail=f"HTTP {status}")

    status, data, latency_ms = _http_json(
        "GET", f"{base_url}/v1/health", args.token, None, timeout_seconds=args.request_timeout_seconds
    )
    _append_check(checks, "health:authorized", status, data, latency_ms, detail=f"HTTP {status}")

    for name, path in [
        ("providers:catalog", "/v1/providers/catalog"),
        ("providers:get_config", "/v1/providers"),
        ("providers:key_status", "/v1/providers/keys/status"),
        ("sessions:list", "/v1/sessions"),
        ("runs:list", "/v1/runs?limit=10"),
        ("memory:list", "/v1/memory"),
        ("skills:list", "/v1/skills"),
        ("artifacts:encryption_status", "/v1/artifacts/encryption/status"),
    ]:
        status, data, latency_ms = _http_json(
            "GET", f"{base_url}{path}", args.token, None, timeout_seconds=args.request_timeout_seconds
        )
        _append_check(checks, name, status, data, latency_ms, detail=f"HTTP {status}")

    # Deterministic chat/session path
    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/chat",
        args.token,
        {
            "session_id": session_id,
            "message": "RC sweep chat check. Reply with OK.",
            "mode": "single",
        },
        timeout_seconds=args.request_timeout_seconds,
    )
    answer = (((data or {}).get("result") or {}).get("answer") or "").strip() if isinstance(data, dict) else ""
    _append_check(
        checks,
        "chat:single_roundtrip",
        status,
        data,
        latency_ms,
        detail=f"HTTP {status}, answer_nonempty={bool(answer)}",
    )

    status, data, latency_ms = _http_json(
        "GET", f"{base_url}/v1/sessions/{session_id}", args.token, None, timeout_seconds=args.request_timeout_seconds
    )
    _append_check(checks, "sessions:detail_roundtrip", status, data, latency_ms, detail=f"HTTP {status}")

    # Deterministic memory add/delete path
    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/memory",
        args.token,
        {
            "statement": f"RC_SWEEP_MEMORY_{int(time.time())}",
            "source_type": "api",
            "source_ref": "rc_sweep",
        },
        timeout_seconds=args.request_timeout_seconds,
    )
    if status == 200 and isinstance(data, dict):
        try:
            memory_id = int(data.get("id"))
        except Exception:
            memory_id = None
    _append_check(checks, "memory:add", status, data, latency_ms, detail=f"HTTP {status}, id={memory_id}")

    if memory_id is not None:
        status, data, latency_ms = _http_json(
            "DELETE",
            f"{base_url}/v1/memory/{memory_id}",
            args.token,
            None,
            timeout_seconds=args.request_timeout_seconds,
        )
        deleted = bool(data.get("deleted", False)) if isinstance(data, dict) else False
        checks.append(_result("memory:delete", status == 200 and deleted, f"HTTP {status}, deleted={deleted}", latency_ms))
    else:
        checks.append(_skip("memory:delete", "Skipped (memory add did not return numeric id)."))

    # Ephemeral skill lifecycle cycle
    skill_name = f"rc_sweep_skill_{int(time.time())}"
    workflow_text = _draft_workflow_text(skill_name)

    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/skills/draft/validate",
        args.token,
        {"workflow_text": workflow_text},
        timeout_seconds=args.request_timeout_seconds,
    )
    valid = bool(data.get("valid", False)) if isinstance(data, dict) else False
    checks.append(_result("skills:draft_validate", status == 200 and valid, f"HTTP {status}, valid={valid}", latency_ms))

    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/skills/draft/save",
        args.token,
        {"workflow_text": workflow_text, "overwrite": False},
        timeout_seconds=args.request_timeout_seconds,
    )
    saved = bool(data.get("saved", False)) if isinstance(data, dict) else False
    checks.append(_result("skills:draft_save", status == 200 and saved, f"HTTP {status}, saved={saved}", latency_ms))

    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/skills/{skill_name}/enable",
        args.token,
        {},
        timeout_seconds=args.request_timeout_seconds,
    )
    enabled = bool(data.get("enabled", False)) if isinstance(data, dict) else False
    checks.append(_result("skills:enable_installed", status == 200 and enabled, f"HTTP {status}, enabled={enabled}", latency_ms))

    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/skills/{skill_name}/test",
        args.token,
        {"run": True, "mode": "single"},
        timeout_seconds=args.request_timeout_seconds,
    )
    has_run = isinstance(data, dict) and isinstance(data.get("run"), dict)
    checks.append(_result("skills:run_installed", status == 200 and has_run, f"HTTP {status}, run={has_run}", latency_ms))

    exported_payload: dict[str, Any] | None = None
    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/skills/{skill_name}/export",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    has_workflow_text = (
        isinstance(data, dict)
        and isinstance(data.get("skill"), dict)
        and bool((data.get("skill") or {}).get("workflow_text"))
    )
    if has_workflow_text and isinstance(data, dict):
        exported_payload = data
    checks.append(_result("skills:export", status == 200 and has_workflow_text, f"HTTP {status}", latency_ms))

    # Delete -> import -> delete cleanup
    status, data, latency_ms = _http_json(
        "DELETE",
        f"{base_url}/v1/skills/{skill_name}",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    deleted = bool(data.get("deleted", False)) if isinstance(data, dict) else False
    checks.append(_result("skills:delete_before_import", status == 200 and deleted, f"HTTP {status}, deleted={deleted}", latency_ms))

    if exported_payload is None:
        checks.append(_skip("skills:import_bundle", "Skipped (export payload unavailable)."))
    else:
        status, data, latency_ms = _http_json(
            "POST",
            f"{base_url}/v1/skills/import",
            args.token,
            exported_payload,
            timeout_seconds=args.request_timeout_seconds,
        )
        imported = bool(data.get("imported", False)) if isinstance(data, dict) else False
        checks.append(_result("skills:import_bundle", status == 200 and imported, f"HTTP {status}, imported={imported}", latency_ms))

        status, data, latency_ms = _http_json(
            "DELETE",
            f"{base_url}/v1/skills/{skill_name}",
            args.token,
            None,
            timeout_seconds=args.request_timeout_seconds,
        )
        deleted = bool(data.get("deleted", False)) if isinstance(data, dict) else False
        checks.append(_result("skills:cleanup_delete", status == 200 and deleted, f"HTTP {status}, deleted={deleted}", latency_ms))

    passed = sum(1 for c in checks if c.status == "PASS")
    failed = sum(1 for c in checks if c.status == "FAIL")
    skipped = sum(1 for c in checks if c.status == "SKIP")
    report = {
        "started_at": started_at,
        "finished_at": _now(),
        "base_url": base_url,
        "summary": {"passed": passed, "failed": failed, "skipped": skipped, "total": len(checks)},
        "checks": [asdict(c) for c in checks],
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RC core sweep checks against MMO daemon.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8100", help="Daemon base URL.")
    parser.add_argument("--token", required=True, help="Bearer token for daemon API.")
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--out", default="", help="Optional output report path (.json).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run(args)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Wrote RC core sweep report: {args.out}")
    else:
        print(text)
    return 0 if report["summary"]["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
