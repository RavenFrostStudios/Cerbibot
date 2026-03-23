#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import socket
from urllib import error, request


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    latency_ms: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _result(name: str, ok: bool, detail: str, latency_ms: int) -> CheckResult:
    return CheckResult(name=name, status="PASS" if ok else "FAIL", detail=detail, latency_ms=latency_ms)


def _skip(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, status="SKIP", detail=detail, latency_ms=0)


def _warnings_acknowledge_single_provider_fallback(warnings: list[Any]) -> bool:
    warning_text = " ".join(str(w).lower() for w in warnings)
    return any(
        marker in warning_text
        for marker in (
            "fallback to single provider path",
            "optimized to single-pass",
            "optimized to single pass",
            "resolved to the same provider/model",
            "resolved to the same provider model",
        )
    )


def _mode_check(base_url: str, token: str, mode: str, provider: str | None, *, timeout_seconds: float) -> CheckResult:
    payload: dict[str, Any] = {
        "session_id": f"acceptance-{mode}",
        "message": f"Acceptance check for mode={mode}. Reply with OK.",
        "mode": mode,
    }
    if mode in {"critique", "debate", "consensus", "council"}:
        payload["verbose"] = True
    if provider:
        payload["provider"] = provider
    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/chat",
        token,
        payload,
        timeout_seconds=timeout_seconds,
    )
    if status != 200:
        return _result(
            f"mode:{mode}",
            False,
            f"HTTP {status}: {data.get('detail', data)}",
            latency_ms,
        )
    result = ((data or {}).get("result") or {})
    answer = (result.get("answer") or "").strip()
    if not answer:
        return _result(f"mode:{mode}", False, "empty response", latency_ms)

    warnings = list(result.get("warnings") or [])
    fallback_warn = _warnings_acknowledge_single_provider_fallback(warnings)

    if mode == "critique":
        has_structure = bool(result.get("draft")) and bool(result.get("critique")) and bool(result.get("refined"))
        ok = has_structure or fallback_warn
        detail = "structured response" if has_structure else ("single-provider optimization acknowledged" if fallback_warn else "missing critique structure")
        return _result(f"mode:{mode}", ok, detail, latency_ms)

    if mode == "debate":
        has_structure = bool(result.get("debate_a")) and bool(result.get("debate_b")) and bool(result.get("judge_decision"))
        ok = has_structure or fallback_warn
        detail = "structured response" if has_structure else ("single-provider optimization acknowledged" if fallback_warn else "missing debate structure")
        return _result(f"mode:{mode}", ok, detail, latency_ms)

    if mode == "consensus":
        has_structure = bool(result.get("consensus_answers")) or bool(result.get("consensus_adjudicated"))
        ok = has_structure or fallback_warn
        detail = "structured response" if has_structure else ("single-provider optimization acknowledged" if fallback_warn else "missing consensus structure")
        return _result(f"mode:{mode}", ok, detail, latency_ms)

    if mode == "council":
        has_structure = bool(result.get("council_outputs")) or bool(result.get("council_notes"))
        ok = has_structure or fallback_warn
        detail = "structured response" if has_structure else ("single-provider optimization acknowledged" if fallback_warn else "missing council structure")
        return _result(f"mode:{mode}", ok, detail, latency_ms)

    return _result(f"mode:{mode}", True, "response received", latency_ms)


def _provider_cycle(
    *,
    base_url: str,
    token: str,
    provider: str,
    api_key: str,
    model: str | None,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    status, data, latency_ms = _http_json(
        "POST",
        f"{base_url}/v1/providers/keys",
        token,
        {"provider": provider, "api_key": api_key},
    )
    checks.append(
        _result(
            "provider:set_key",
            status == 200,
            f"HTTP {status}: {data.get('detail', 'ok')}",
            latency_ms,
        )
    )

    test_payload: dict[str, Any] = {}
    if model:
        test_payload["model"] = model
    status, data, latency_ms = _http_json("POST", f"{base_url}/v1/providers/{provider}/test", token, test_payload)
    checks.append(
        _result(
            "provider:test_connection",
            status == 200,
            f"HTTP {status}: {data.get('detail', 'ok')}",
            latency_ms,
        )
    )

    status, data, latency_ms = _http_json("GET", f"{base_url}/v1/providers", token, None)
    if status != 200 or not isinstance(data, dict):
        checks.append(_result("provider:apply_config", False, f"HTTP {status}: {data}", latency_ms))
        return checks
    rows = list((data.get("providers") or []))
    target = None
    for row in rows:
        if str((row or {}).get("name")) == provider:
            target = row
            break
    if target is None:
        checks.append(_result("provider:apply_config", False, f"provider not found: {provider}", 0))
        return checks
    target["enabled"] = True
    if model:
        target["model"] = model
    status, data, latency_ms = _http_json("PUT", f"{base_url}/v1/providers", token, {"providers": [target]})
    checks.append(
        _result(
            "provider:apply_config",
            status == 200,
            f"HTTP {status}: {data.get('detail', 'ok')}",
            latency_ms,
        )
    )
    return checks


def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    checks: list[CheckResult] = []
    started_at = _now()

    status, data, latency_ms = _http_json("GET", f"{base_url}/v1/health", None, None, timeout_seconds=args.request_timeout_seconds)
    checks.append(_result("auth:unauthorized_rejected", status == 401, f"HTTP {status}", latency_ms))

    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/health",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    checks.append(_result("health:authorized", status == 200, f"HTTP {status}", latency_ms))

    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/providers/catalog",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    checks.append(_result("providers:catalog", status == 200, f"HTTP {status}", latency_ms))

    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/providers",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    checks.append(_result("providers:get_config", status == 200, f"HTTP {status}", latency_ms))

    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/providers/keys/status",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    checks.append(_result("providers:key_status", status == 200, f"HTTP {status}", latency_ms))

    status, data, latency_ms = _http_json(
        "GET",
        f"{base_url}/v1/artifacts/encryption/status",
        args.token,
        None,
        timeout_seconds=args.request_timeout_seconds,
    )
    checks.append(_result("artifacts:encryption_status", status == 200, f"HTTP {status}", latency_ms))

    for mode in args.modes:
        checks.append(_mode_check(base_url, args.token, mode, args.provider, timeout_seconds=args.request_timeout_seconds))

    if args.provider and args.provider_api_key:
        checks.extend(
            _provider_cycle(
                base_url=base_url,
                token=args.token,
                provider=args.provider,
                api_key=args.provider_api_key,
                model=args.provider_model,
            )
        )
    else:
        checks.append(
            _skip(
                "provider:key_set_test_apply_cycle",
                "Skipped (provide --provider and --provider-api-key to run mutating provider checks).",
            )
        )

    if args.admin_password:
        status, data, latency_ms = _http_json(
            "POST",
            f"{base_url}/v1/server/audit/security-events",
            args.token,
            {"admin_password": args.admin_password, "limit": 5},
            timeout_seconds=args.request_timeout_seconds,
        )
        checks.append(_result("security:audit_log_access", status == 200, f"HTTP {status}", latency_ms))
    else:
        checks.append(_skip("security:audit_log_access", "Skipped (no --admin-password provided)."))

    passed = sum(1 for c in checks if c.status == "PASS")
    failed = sum(1 for c in checks if c.status == "FAIL")
    skipped = sum(1 for c in checks if c.status == "SKIP")
    report = {
        "started_at": started_at,
        "finished_at": _now(),
        "base_url": base_url,
        "provider_hint": args.provider or "",
        "modes": args.modes,
        "summary": {"passed": passed, "failed": failed, "skipped": skipped, "total": len(checks)},
        "checks": [asdict(c) for c in checks],
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live acceptance checks against MMO daemon API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8100", help="Daemon base URL.")
    parser.add_argument("--token", required=True, help="Bearer token for daemon API.")
    parser.add_argument(
        "--modes",
        default="single,critique,retrieval,debate,consensus,council",
        help="Comma-separated modes to test via /v1/chat.",
    )
    parser.add_argument("--provider", default="", help="Optional provider to force in mode checks.")
    parser.add_argument("--provider-model", default="", help="Optional provider model for provider cycle checks.")
    parser.add_argument("--provider-api-key", default="", help="Optional API key to run provider set/test/apply cycle.")
    parser.add_argument("--admin-password", default="", help="Optional admin password to validate security-events access.")
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--out", default="", help="Optional output report path (.json).")
    args = parser.parse_args()
    args.modes = [m.strip() for m in str(args.modes).split(",") if m.strip()]
    args.provider = str(args.provider).strip() or None
    args.provider_model = str(args.provider_model).strip() or None
    args.provider_api_key = str(args.provider_api_key).strip() or None
    args.admin_password = str(args.admin_password).strip() or None
    return args


def main() -> int:
    args = parse_args()
    report = run(args)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Wrote acceptance report: {args.out}")
    else:
        print(text)
    return 0 if report["summary"]["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
