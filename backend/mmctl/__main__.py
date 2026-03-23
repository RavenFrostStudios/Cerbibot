from __future__ import annotations

import asyncio
from contextlib import contextmanager
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import difflib
import json
import os
from pathlib import Path
import select
import sqlite3
import sys
import time
from typing import Any
from uuid import uuid4

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Group

from orchestrator.session import SessionManager
from orchestrator.observability.redaction import redact_text
import yaml

console = Console()


def _load_orchestrator(config_path: str):
    from orchestrator.config import load_config
    from orchestrator.router import Orchestrator

    config = load_config(config_path)
    return Orchestrator(config)


def _load_route_profiles(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"profiles": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"profiles": {}}
    if not isinstance(payload, dict):
        return {"profiles": {}}
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        payload["profiles"] = {}
    return payload


def _save_route_profiles(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _resolve_skill_path(skill: str) -> Path:
    from orchestrator.skills.registry import discover_skills

    discovered = discover_skills()
    if skill in discovered:
        record = discovered[skill]
        if not record.enabled:
            raise click.ClickException(f"Skill is disabled: {skill}")
        path = Path(record.path).expanduser()
        if path.exists():
            return path

    candidate = Path(skill).expanduser()
    if candidate.exists():
        return candidate

    roots = [
        Path("skills"),
        Path("~/.mmo/skills").expanduser(),
    ]
    for root in roots:
        by_name = root / skill
        if by_name.is_file():
            return by_name
        for filename in ("workflow.yaml", "workflow.yml", f"{skill}.yaml", f"{skill}.yml"):
            path = by_name / filename
            if path.exists():
                return path
    raise click.ClickException(f"Skill file not found: {skill}")


def _load_server_settings(config_path: str) -> dict[str, Any]:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raw = {}
    server = raw.get("server", {}) or {}
    if not isinstance(server, dict):
        server = {}
    return {
        "host": str(server.get("host", "127.0.0.1")),
        "port": int(server.get("port", 8100)),
        "api_key_env": str(server.get("api_key_env", "MMO_SERVER_API_KEY")),
    }


def _load_skill_settings(config_path: str) -> dict[str, Any]:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raw = {}
    skills = raw.get("skills", {}) or {}
    if not isinstance(skills, dict):
        skills = {}
    trusted = [str(item) for item in list(skills.get("trusted_public_keys", []))]
    return {
        "require_signature": bool(skills.get("require_signature", False)),
        "trusted_public_keys": trusted,
    }


async def _daemon_health_ok(config_path: str) -> bool:
    import httpx

    settings = _load_server_settings(config_path)
    base_url = f"http://{settings['host']}:{settings['port']}"
    token = os.getenv(settings["api_key_env"], "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=0.2) as client:
            resp = await client.get(f"{base_url}/v1/health", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def _proxy_ask_to_daemon(
    config_path: str,
    *,
    query: str,
    mode: str | None,
    provider: str | None,
    tools: str | None,
    fact_check: bool,
    no_stream: bool,
    verbose: bool,
) -> bool:
    import httpx

    settings = _load_server_settings(config_path)
    base_url = f"http://{settings['host']}:{settings['port']}"
    token = os.getenv(settings["api_key_env"], "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {
        "query": query,
        "mode": mode,
        "provider": provider,
        "tools": tools,
        "fact_check": fact_check,
        "verbose": verbose,
        "stream": not no_stream,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        if no_stream:
            payload["stream"] = False
            resp = await client.post(f"{base_url}/v1/ask", json=payload, headers=headers)
            if resp.status_code != 200:
                return False
            data = resp.json()
            console.print(data.get("answer", ""))
            return True

        payload["stream"] = True
        async with client.stream("POST", f"{base_url}/v1/ask", json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                return False
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[len("data: ") :].strip()
                if not raw:
                    continue
                event = json.loads(raw)
                event_type = event.get("type")
                if event_type == "chunk" and event.get("text"):
                    console.print(event["text"], end="")
                elif event_type == "status" and event.get("text"):
                    console.print(f"[dim]{event['text']}[/dim]")
                elif event_type == "result":
                    result = event.get("result", {})
                    if result.get("answer"):
                        console.print("")
                        console.print(result["answer"])
                    break
        return True


async def _proxy_chat_turn_to_daemon(
    config_path: str,
    *,
    session_id: str,
    message: str,
    mode: str,
    provider: str | None,
    fact_check: bool,
) -> tuple[bool, str]:
    import httpx

    settings = _load_server_settings(config_path)
    base_url = f"http://{settings['host']}:{settings['port']}"
    token = os.getenv(settings["api_key_env"], "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {
        "session_id": session_id,
        "message": message,
        "mode": mode,
        "provider": provider,
        "fact_check": fact_check,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{base_url}/v1/chat", json=payload, headers=headers)
        if resp.status_code != 200:
            return False, session_id
        data = resp.json()
        result = data.get("result", {})
        console.print(f"[bold green]assistant>[/bold green] {result.get('answer', '')}")
        return True, str(data.get("session_id", session_id))


def _load_memory_components(config_path: str):
    from orchestrator.config import load_config
    from orchestrator.memory.governance import MemoryGovernance
    from orchestrator.memory.store import MemoryStore
    from orchestrator.security.encryption import build_envelope_cipher
    from orchestrator.security.guardian import Guardian

    config = load_config(config_path)
    memory_path = Path(config.budgets.usage_file).expanduser().with_name("memory.db")
    cipher = build_envelope_cipher(config.security.data_protection)
    return MemoryStore(str(memory_path), cipher=cipher), MemoryGovernance(Guardian(config.security))


def _load_artifact_store(config_path: str):
    from orchestrator.config import load_config
    from orchestrator.observability.artifacts import ArtifactStore
    from orchestrator.security.encryption import build_envelope_cipher

    config = load_config(config_path)
    cipher = build_envelope_cipher(config.security.data_protection)
    return ArtifactStore(config.artifacts, cipher=cipher)


def _load_prompt_library(config_path: str):
    from orchestrator.config import load_config
    from orchestrator.prompts.library import PromptLibrary

    config = load_config(config_path)
    return PromptLibrary(config.prompts.directory), config.prompts.selection


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    p = Path(path).expanduser()
    if not p.exists():
        raise click.ClickException(f"File not found: {path}")
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception as exc:
            raise click.ClickException(f"Invalid JSONL at line {idx}: {exc}") from exc
        if not isinstance(item, dict):
            raise click.ClickException(f"JSONL entry at line {idx} must be an object")
        rows.append(item)
    return rows


def _append_jsonl(path: str, row: dict[str, Any]) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(text: str) -> datetime | None:
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except Exception:
        return None


def _redact_config_for_export(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).lower()
            if any(token in key for token in ("key", "token", "secret", "passphrase", "password")):
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact_config_for_export(v)
        return out
    if isinstance(obj, list):
        return [_redact_config_for_export(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def _load_usage_data(config_path: str) -> dict:
    from orchestrator.config import load_config
    from orchestrator.security.encryption import build_envelope_cipher

    config = load_config(config_path)
    usage_path = Path(config.budgets.usage_file).expanduser()
    if not usage_path.exists():
        return {}
    raw = json.loads(usage_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("encrypted") is True:
        cipher = build_envelope_cipher(config.security.data_protection)
        if cipher is None:
            raise click.ClickException("Usage file is encrypted but encryption is not configured")
        payload = raw.get("payload")
        if not isinstance(payload, str):
            raise click.ClickException("Invalid encrypted usage payload")
        return cipher.maybe_decrypt_json(payload)
    return raw


async def _fetch_daemon_dashboard(config_path: str) -> dict[str, Any] | None:
    import httpx

    settings = _load_server_settings(config_path)
    base_url = f"http://{settings['host']}:{settings['port']}"
    token = os.getenv(settings["api_key_env"], "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            health, cost, sessions, memory = await asyncio.gather(
                client.get(f"{base_url}/v1/health", headers=headers),
                client.get(f"{base_url}/v1/cost", headers=headers),
                client.get(f"{base_url}/v1/sessions", headers=headers),
                client.get(f"{base_url}/v1/memory", headers=headers),
            )
        if health.status_code != 200 or cost.status_code != 200:
            return None
        return {
            "source": "daemon",
            "health": health.json(),
            "cost": cost.json(),
            "sessions": sessions.json() if sessions.status_code == 200 else {"sessions": []},
            "memory": memory.json() if memory.status_code == 200 else {"memories": []},
        }
    except Exception:
        return None


async def _doctor_http_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 8.0,
) -> tuple[int, dict[str, Any], int]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.request(method.upper(), url, headers=headers, json=payload)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {"raw": resp.text}
        if not isinstance(data, dict):
            data = {"value": data}
        return int(resp.status_code), data, elapsed_ms
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return 599, {"detail": f"{type(exc).__name__}: {exc}"}, elapsed_ms


def _load_server_token_for_doctor(
    settings: dict[str, Any],
    *,
    token: str | None = None,
    token_env: str | None = None,
) -> tuple[str, str, str]:
    if token:
        return token.strip(), "cli", "<inline>"
    env_name = (token_env or settings["api_key_env"]).strip()
    env_token = os.getenv(env_name, "").strip()
    if env_token:
        return env_token, "env", env_name
    token_file_path = Path(os.getenv("MMO_SERVER_API_KEY_FILE", "~/.mmo/server_api_key.txt")).expanduser()
    if token_file_path.exists():
        file_token = token_file_path.read_text(encoding="utf-8").strip()
        if file_token:
            return file_token, "file", str(token_file_path)
    return "", "missing", env_name


async def _run_doctor_checks(
    config_path: str,
    *,
    request_timeout_seconds: float,
    smoke_providers: bool,
    governance: bool,
    token_override: str | None = None,
    token_env_override: str | None = None,
) -> dict[str, Any]:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = _load_server_settings(config_path)
    base_url = f"http://{settings['host']}:{settings['port']}"
    token, token_source, token_hint = _load_server_token_for_doctor(
        settings,
        token=token_override,
        token_env=token_env_override,
    )
    token_env = str(token_env_override or settings["api_key_env"])
    auth_token: str | None = token or None

    checks: list[dict[str, Any]] = []

    def _record(name: str, status: str, detail: str, latency_ms: int) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "detail": detail,
                "latency_ms": latency_ms,
            }
        )

    status, data, latency_ms = await _doctor_http_json(
        "GET",
        f"{base_url}/v1/health",
        None,
        timeout_seconds=request_timeout_seconds,
    )
    if status in {401, 403}:
        _record("auth:unauthorized_rejected", "PASS", f"HTTP {status}", latency_ms)
    elif status == 200:
        _record("auth:unauthorized_rejected", "PASS", "HTTP 200 (auth disabled)", latency_ms)
        auth_token = None
    else:
        _record("auth:unauthorized_rejected", "FAIL", f"HTTP {status}: {data.get('detail', data)}", latency_ms)

    if token:
        if token_source == "env":
            _record("auth:token_present", "PASS", f"env={token_hint}", 0)
        elif token_source == "file":
            _record("auth:token_present", "PASS", f"file={token_hint}", 0)
        else:
            _record("auth:token_present", "PASS", "provided via --token", 0)
    else:
        if auth_token is None and status == 200:
            _record("auth:token_present", "PASS", f"env={token_env} (not required)", 0)
        else:
            _record("auth:token_present", "FAIL", f"missing env={token_env}", 0)

    endpoint_checks = [
        ("health:authorized", "GET", "/v1/health"),
        ("providers:catalog", "GET", "/v1/providers/catalog"),
        ("providers:list", "GET", "/v1/providers"),
        ("providers:key_status", "GET", "/v1/providers/keys/status"),
        ("routing:roles", "GET", "/v1/routing/roles"),
        ("artifacts:encryption_status", "GET", "/v1/artifacts/encryption/status"),
        ("delegate:health", "GET", "/v1/server/delegate/health"),
        ("ui:settings", "GET", "/v1/server/ui-settings"),
    ]

    providers_payload: dict[str, Any] = {}
    key_status_payload: dict[str, Any] = {}

    for name, method, path in endpoint_checks:
        st, dt, ms = await _doctor_http_json(
            method,
            f"{base_url}{path}",
            auth_token,
            timeout_seconds=request_timeout_seconds,
        )
        if st == 200:
            _record(name, "PASS", "HTTP 200", ms)
            if path == "/v1/providers":
                providers_payload = dt
            elif path == "/v1/providers/keys/status":
                key_status_payload = dt
        else:
            _record(name, "FAIL", f"HTTP {st}: {dt.get('detail', dt)}", ms)

    if smoke_providers:
        providers_rows = providers_payload.get("providers", []) if isinstance(providers_payload, dict) else []
        key_rows = key_status_payload.get("providers", []) if isinstance(key_status_payload, dict) else []
        key_map: dict[str, dict[str, Any]] = {}
        for row in key_rows:
            if isinstance(row, dict):
                key_map[str(row.get("name", ""))] = row
        if not isinstance(providers_rows, list) or not providers_rows:
            _record("providers:smoke", "SKIP", "no providers payload available", 0)
        else:
            for row in providers_rows:
                if not isinstance(row, dict):
                    continue
                provider_name = str(row.get("name", "")).strip()
                if not provider_name:
                    continue
                if not bool(row.get("enabled", False)):
                    _record(f"provider:{provider_name}:smoke", "SKIP", "disabled", 0)
                    continue
                key_set = bool((key_map.get(provider_name) or {}).get("key_set", False))
                if not key_set:
                    _record(f"provider:{provider_name}:smoke", "SKIP", "key not set", 0)
                    continue
                model = str(row.get("model", "")).strip()
                payload = {"model": model} if model else {}
                st, dt, ms = await _doctor_http_json(
                    "POST",
                    f"{base_url}/v1/providers/{provider_name}/test",
                    auth_token,
                    payload=payload,
                    timeout_seconds=max(10.0, request_timeout_seconds),
                )
                if st == 200:
                    test_model = str(dt.get("model", model))
                    latency = int(dt.get("latency_ms", ms))
                    _record(
                        f"provider:{provider_name}:smoke",
                        "PASS",
                        f"model={test_model}",
                        latency,
                    )
                else:
                    _record(
                        f"provider:{provider_name}:smoke",
                        "FAIL",
                        f"HTTP {st}: {dt.get('detail', dt)}",
                        ms,
                    )
    else:
        _record("providers:smoke", "SKIP", "use --smoke-providers to run connection tests", 0)

    if governance:
        st, dt, ms = await _doctor_http_json(
            "POST",
            f"{base_url}/v1/skills/governance/analyze",
            auth_token,
            payload={"include_disabled": False, "limit": 5},
            timeout_seconds=max(10.0, request_timeout_seconds),
        )
        if st == 200:
            summary = dt.get("summary", {}) if isinstance(dt, dict) else {}
            _record(
                "skills:governance",
                "PASS",
                (
                    f"skills={int(summary.get('skills_analyzed', 0))} "
                    f"merge={int(summary.get('merge_candidates', 0))} "
                    f"crossover={int(summary.get('crossover_candidates', 0))}"
                ),
                ms,
            )
        else:
            _record("skills:governance", "FAIL", f"HTTP {st}: {dt.get('detail', dt)}", ms)
    else:
        _record("skills:governance", "SKIP", "use --governance to run skill governance checks", 0)

    passed = sum(1 for item in checks if item["status"] == "PASS")
    failed = sum(1 for item in checks if item["status"] == "FAIL")
    skipped = sum(1 for item in checks if item["status"] == "SKIP")
    return {
        "generated_at": _to_utc_iso(datetime.now(timezone.utc)),
        "base_url": base_url,
        "token_env": token_env,
        "token_present": bool(token),
        "token_source": token_source,
        "token_hint": token_hint,
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": len(checks),
        },
        "checks": checks,
    }


def _read_recent_audit_events(config_path: str, limit: int = 12) -> list[dict[str, Any]]:
    from orchestrator.config import load_config

    cfg = load_config(config_path)
    usage_file = Path(cfg.budgets.usage_file)
    audit_path = usage_file.expanduser().with_name("audit.jsonl")
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _memory_stats(orchestrator) -> dict[str, Any]:
    memory_db = Path(orchestrator.memory_store.db_path)
    count = 0
    oldest = ""
    newest = ""
    if memory_db.exists():
        with sqlite3.connect(memory_db) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM memories"
            ).fetchone()
            if row:
                count = int(row[0] or 0)
                oldest = str(row[1] or "")
                newest = str(row[2] or "")
    return {
        "count": count,
        "size_bytes": memory_db.stat().st_size if memory_db.exists() else 0,
        "oldest": oldest,
        "newest": newest,
    }


def _collect_local_dashboard(config_path: str) -> dict[str, Any]:
    orchestrator = _load_orchestrator(config_path)
    remaining = orchestrator.budgets.remaining()
    state = orchestrator.budgets.state()
    usage = _load_usage_data(config_path)
    daily = usage.get("daily_totals", {"cost": 0.0, "providers": {}}) if isinstance(usage, dict) else {"cost": 0.0}
    monthly = usage.get("monthly_totals", {"cost": 0.0, "providers": {}}) if isinstance(usage, dict) else {"cost": 0.0}
    return {
        "source": "local",
        "providers": sorted(orchestrator.providers.keys()),
        "remaining": remaining,
        "state": {
            "session_spend": state.session_spend,
            "daily_spend": state.daily_spend,
            "monthly_spend": state.monthly_spend,
            "daily_total_cost": float(daily.get("cost", 0.0)),
            "monthly_total_cost": float(monthly.get("cost", 0.0)),
        },
        "rate_limits": orchestrator.rate_limiter.snapshot() if hasattr(orchestrator, "rate_limiter") else {},
        "router_weights": orchestrator.router_weights.snapshot() if hasattr(orchestrator, "router_weights") else {},
        "audit_events": _read_recent_audit_events(config_path),
        "memory_stats": _memory_stats(orchestrator),
    }


def _render_dashboard(payload: dict[str, Any]) -> Group:
    source = payload.get("source", "local")
    title = f"MMO Dashboard ({source})"
    subtitle = time.strftime("%Y-%m-%d %H:%M:%S")
    header = Panel(Text(f"{title}  |  {subtitle}  |  press q to quit", style="bold cyan"))

    if source == "daemon":
        health = payload.get("health", {})
        cost = payload.get("cost", {})
        remaining = cost.get("remaining", health.get("budget_remaining", {}))
        state = cost.get("state", {})
        rate_limits = cost.get("rate_limits", {})
        router_weights = cost.get("router_weights", {})
        memory_list = payload.get("memory", {}).get("memories", [])
        sessions = payload.get("sessions", {}).get("sessions", [])
        request_log = [
            {"event_type": "session", "timestamp": "", "payload": {"session_id": s.get("session_id"), "messages": s.get("messages")}}
            for s in sessions[-12:]
        ]
        memory_stats = {"count": len(memory_list), "size_bytes": 0, "oldest": "", "newest": ""}
        providers = health.get("providers", [])
        daily_total_cost = float(state.get("daily_spend", 0.0))
        monthly_total_cost = float(state.get("monthly_spend", 0.0))
    else:
        remaining = payload.get("remaining", {})
        state = payload.get("state", {})
        rate_limits = payload.get("rate_limits", {})
        router_weights = payload.get("router_weights", {})
        request_log = payload.get("audit_events", [])
        memory_stats = payload.get("memory_stats", {})
        providers = payload.get("providers", [])
        daily_total_cost = float(state.get("daily_total_cost", 0.0))
        monthly_total_cost = float(state.get("monthly_total_cost", 0.0))

    cost_table = Table(title="Cost")
    cost_table.add_column("Metric")
    cost_table.add_column("Value")
    cost_table.add_row("Providers", ", ".join(providers) if providers else "-")
    cost_table.add_row("Today", f"${daily_total_cost:.6f}")
    cost_table.add_row("Month", f"${monthly_total_cost:.6f}")
    cost_table.add_row("Remaining Session", f"${float(remaining.get('session', 0.0)):.6f}")
    cost_table.add_row("Remaining Daily", f"${float(remaining.get('daily', 0.0)):.6f}")
    cost_table.add_row("Remaining Monthly", f"${float(remaining.get('monthly', 0.0)):.6f}")

    provider_table = Table(title="Provider Status")
    provider_table.add_column("Provider")
    provider_table.add_column("RPM")
    provider_table.add_column("TPM")
    provider_table.add_column("Err")
    for provider_name in providers:
        rl = rate_limits.get(provider_name, {})
        provider_weights = router_weights.get(provider_name, {})
        error = provider_weights.get("general", {}).get("error_ema", 0.0) if isinstance(provider_weights, dict) else 0.0
        provider_table.add_row(
            provider_name,
            f"{rl.get('rpm_used', 0)}/{rl.get('rpm_limit', 0)}",
            f"{rl.get('tpm_used', 0)}/{rl.get('tpm_limit', 0)}",
            f"{float(error):.2f}",
        )

    weights_table = Table(title="Router Weights (top domains)")
    weights_table.add_column("Provider")
    weights_table.add_column("Domain")
    weights_table.add_column("Score")
    weights_table.add_column("P50/P95 ms")
    for provider_name, per_domain in list(router_weights.items())[:6]:
        if not isinstance(per_domain, dict):
            continue
        ranked = sorted(per_domain.items(), key=lambda kv: float((kv[1] or {}).get("score", 0.0)), reverse=True)[:2]
        for domain, stats in ranked:
            weights_table.add_row(
                provider_name,
                domain,
                f"{float((stats or {}).get('score', 0.0)):.3f}",
                f"{int((stats or {}).get('p50_latency_ms', 0))}/{int((stats or {}).get('p95_latency_ms', 0))}",
            )

    request_table = Table(title="Request Log")
    request_table.add_column("Time")
    request_table.add_column("Event")
    request_table.add_column("Detail")
    for item in request_log[-8:]:
        payload_data = item.get("payload", {}) if isinstance(item, dict) else {}
        detail = ""
        if isinstance(payload_data, dict):
            detail = str(payload_data.get("tool_name") or payload_data.get("request_id") or payload_data.get("session_id") or "")[:60]
        request_table.add_row(
            str(item.get("timestamp", ""))[:19],
            str(item.get("event_type", "")),
            detail,
        )

    memory_table = Table(title="Memory")
    memory_table.add_column("Metric")
    memory_table.add_column("Value")
    memory_table.add_row("Entries", str(memory_stats.get("count", 0)))
    memory_table.add_row("Size (bytes)", str(memory_stats.get("size_bytes", 0)))
    memory_table.add_row("Oldest", str(memory_stats.get("oldest", ""))[:19] or "-")
    memory_table.add_row("Newest", str(memory_stats.get("newest", ""))[:19] or "-")

    return Group(header, cost_table, provider_table, weights_table, request_table, memory_table)


@contextmanager
def _stdin_cbreak():
    if not sys.stdin.isatty():
        yield
        return
    try:
        import termios
        import tty
    except Exception:
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


@click.group()
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """Multi-Mind Orchestrator CLI."""
    from orchestrator.observability.logging_setup import configure_logging

    configure_logging(debug=debug)
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


@main.command()
@click.argument("query")
@click.option(
    "--mode",
    type=click.Choice(["single", "critique", "retrieval", "debate", "consensus", "council", "auto"]),
    default=None,
)
@click.option("--provider", default=None, help="Force provider for single mode")
@click.option("--tools", default=None, help="Enable tool-use planning for registered built-in/plugin tools")
@click.option("--fact-check/--no-fact-check", default=None, help="Enable claim verification")
@click.option("--no-stream", is_flag=True, default=False, help="Disable streaming output")
@click.option("--verbose", is_flag=True, default=False)
@click.option(
    "--force-full-debate",
    is_flag=True,
    default=False,
    help="Force full multi-stage debate workflow (disable homogeneous single-pass optimization).",
)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def ask(
    query: str,
    mode: str | None,
    provider: str | None,
    tools: str | None,
    fact_check: bool | None,
    no_stream: bool,
    verbose: bool,
    force_full_debate: bool,
    config_path: str,
) -> None:
    """Run a query through the orchestrator."""

    async def _run() -> None:
        daemon_ok = await _daemon_health_ok(config_path)
        bypass_daemon = bool(force_full_debate and (mode == "debate"))
        if daemon_ok:
            if bypass_daemon:
                console.print(
                    "[dim]Bypassing daemon proxy for this run because --force-full-debate is enabled.[/dim]"
                )
            else:
                proxied = await _proxy_ask_to_daemon(
                    config_path,
                    query=query,
                    mode=mode,
                    provider=provider,
                    tools=tools,
                    fact_check=bool(fact_check),
                    no_stream=no_stream,
                    verbose=verbose,
                )
                if proxied:
                    return
        orchestrator = _load_orchestrator(config_path)
        effective_mode = mode or orchestrator.config.default_mode
        effective_fact_check = fact_check if fact_check is not None else (effective_mode == "retrieval")
        request_kwargs = {
            "query": query,
            "mode": mode,
            "provider": provider,
            "verbose": verbose,
            "fact_check": effective_fact_check,
            "force_full_debate": force_full_debate,
        }
        if tools is not None:
            request_kwargs["tools"] = tools
        if no_stream:
            result = await orchestrator.ask(**request_kwargs)
            console.print(result.answer)
        else:
            status_ctx = None
            streamed_any_chunk = False
            result = None
            async for event in orchestrator.ask_stream(**request_kwargs):
                if event.type == "status":
                    if status_ctx is not None:
                        status_ctx.__exit__(None, None, None)
                    status_ctx = console.status(event.text or "Working...", spinner="dots")
                    status_ctx.__enter__()
                elif event.type == "chunk":
                    if status_ctx is not None:
                        status_ctx.__exit__(None, None, None)
                        status_ctx = None
                    if event.text:
                        console.print(event.text, end="")
                        streamed_any_chunk = True
                elif event.type == "result":
                    if status_ctx is not None:
                        status_ctx.__exit__(None, None, None)
                        status_ctx = None
                    result = event.result

            if result is None:
                raise click.ClickException("Streaming failed before producing a final result")
            if streamed_any_chunk:
                console.print("")
            if not streamed_any_chunk:
                console.print(result.answer)

        if result.warnings:
            console.print("\n[yellow]Warnings[/yellow]")
            for warning in result.warnings:
                console.print(f"- {warning}")
        if result.citations:
            console.print("\n[bold]Citations[/bold]")
            for idx, citation in enumerate(result.citations, start=1):
                console.print(f"[{idx}] {citation.url} ({citation.retrieved_at})")
        if result.verification_notes:
            console.print("\n[bold]Verification Notes[/bold]")
            for item in result.verification_notes:
                status = "verified" if item.verified else "unverified"
                console.print(f"- [{status}] {item.claim}")
                if item.sources:
                    console.print(f"  sources: {', '.join(item.sources[:3])}")
                if item.conflicts:
                    console.print(f"  conflicts: {' | '.join(item.conflicts[:2])}")
        if result.tool_outputs:
            console.print("\n[bold]Tool Output[/bold]")
            for item in result.tool_outputs:
                console.print_json(data=item)
        if verbose and result.mode == "critique":
            console.print("\n[bold]Draft[/bold]")
            console.print(result.draft or "")
            console.print("\n[bold]Critique[/bold]")
            console.print(result.critique or "")
            console.print("\n[bold]Refined[/bold]")
            console.print(result.refined or "")
        if verbose and result.mode == "debate":
            console.print("\n[bold]Debater A[/bold]")
            console.print(result.debate_a or "")
            console.print("\n[bold]Debater B[/bold]")
            console.print(result.debate_b or "")
            console.print("\n[bold]Judge[/bold]")
            console.print(result.judge_decision or "")
        if verbose and result.mode == "consensus":
            console.print("\n[bold]Consensus[/bold]")
            if result.consensus_agreement is not None:
                console.print(f"agreement={result.consensus_agreement:.2f}")
            if result.consensus_confidence is not None:
                console.print(f"confidence={result.consensus_confidence:.2f}")
            if result.consensus_adjudicated is not None:
                console.print(f"adjudicated={result.consensus_adjudicated}")
            if result.consensus_answers:
                for provider_name, provider_answer in result.consensus_answers.items():
                    console.print(f"\n[bold]{provider_name}[/bold]")
                    console.print(provider_answer)
        if verbose and result.mode == "council":
            console.print("\n[bold]Council Specialists[/bold]")
            if result.council_outputs:
                for role, specialist_text in result.council_outputs.items():
                    console.print(f"\n[bold]{role}[/bold]")
                    console.print(specialist_text)
            if result.council_notes:
                console.print("\n[bold]Synthesis Notes[/bold]")
                console.print(result.council_notes)
        console.print(
            f"\n[dim]mode={result.mode} provider={result.provider} model={result.model} "
            f"tokens_in={result.tokens_in} tokens_out={result.tokens_out} cost=${result.cost:.6f}[/dim]"
        )

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["single", "critique", "retrieval", "debate", "consensus", "council", "auto"]),
    default="single",
    show_default=True,
)
@click.option("--provider", default=None, help="Force provider for single mode")
@click.option("--tools", default=None, help="Enable tool-use planning for registered built-in/plugin tools")
@click.option("--no-stream", is_flag=True, default=False, help="Disable streaming output")
@click.option("--fact-check/--no-fact-check", default=False, help="Enable claim verification on each turn")
@click.option("--max-context-tokens", default=8000, show_default=True, type=int)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def chat(
    mode: str,
    provider: str | None,
    tools: str | None,
    no_stream: bool,
    fact_check: bool,
    max_context_tokens: int,
    config_path: str,
) -> None:
    """Interactive multi-turn chat mode."""

    async def _run() -> None:
        daemon_ok = await _daemon_health_ok(config_path)
        daemon_session_id = str(uuid4())
        orchestrator = None if daemon_ok else _load_orchestrator(config_path)
        session = SessionManager(max_context_tokens=max_context_tokens) if not daemon_ok else None
        console.print("Entering chat mode. Commands: /clear, /cost, /exit")

        while True:
            try:
                user_input = console.input("[bold cyan]you> [/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nExiting chat.")
                break

            if not user_input:
                continue
            if user_input == "/exit":
                console.print("Exiting chat.")
                break
            if user_input == "/clear":
                if daemon_ok:
                    daemon_session_id = str(uuid4())
                else:
                    assert session is not None
                    session.clear()
                console.print("Context cleared.")
                continue
            if user_input == "/cost":
                if daemon_ok:
                    console.print("Use `mmctl cost` for daemon-backed budget summary.")
                else:
                    assert orchestrator is not None
                    remaining = orchestrator.budgets.remaining()
                    console.print(
                        f"Remaining budgets: session=${remaining['session']:.6f} "
                        f"daily=${remaining['daily']:.6f} monthly=${remaining['monthly']:.6f}"
                    )
                continue

            try:
                if daemon_ok:
                    ok, daemon_session_id = await _proxy_chat_turn_to_daemon(
                        config_path,
                        session_id=daemon_session_id,
                        message=user_input,
                        mode=mode,
                        provider=provider,
                        fact_check=fact_check or mode == "retrieval",
                    )
                    if not ok:
                        console.print("[red]Error:[/red] daemon chat request failed")
                    continue

                assert session is not None
                assert orchestrator is not None
                session.add("user", user_input)
                session.trim()
                context = session.export()[:-1]
                if no_stream:
                    result = await orchestrator.ask(
                        query=user_input,
                        mode=mode,
                        provider=provider,
                        verbose=False,
                        context_messages=context,
                        fact_check=fact_check or mode == "retrieval",
                        tools=tools,
                    )
                    console.print(f"[bold green]assistant>[/bold green] {result.answer}")
                else:
                    console.print("[bold green]assistant>[/bold green] ", end="")
                    status_ctx = None
                    streamed_chunks: list[str] = []
                    result = None
                    async for event in orchestrator.ask_stream(
                        query=user_input,
                        mode=mode,
                        provider=provider,
                        verbose=False,
                        context_messages=context,
                        fact_check=fact_check or mode == "retrieval",
                        tools=tools,
                    ):
                        if event.type == "status":
                            if status_ctx is not None:
                                status_ctx.__exit__(None, None, None)
                            status_ctx = console.status(event.text or "Working...", spinner="dots")
                            status_ctx.__enter__()
                        elif event.type == "chunk":
                            if status_ctx is not None:
                                status_ctx.__exit__(None, None, None)
                                status_ctx = None
                            if event.text:
                                streamed_chunks.append(event.text)
                                console.print(event.text, end="")
                        elif event.type == "result":
                            if status_ctx is not None:
                                status_ctx.__exit__(None, None, None)
                                status_ctx = None
                            result = event.result
                    console.print("")
                    if result is None:
                        raise click.ClickException("No result produced in chat stream")
                    if not streamed_chunks:
                        console.print(result.answer)

                if result.warnings:
                    console.print("[yellow]Warnings:[/yellow]")
                    for warning in result.warnings:
                        console.print(f"- {warning}")

                session.add("assistant", result.answer)
                session.trim()
            except Exception as exc:
                console.print(f"[red]Error:[/red] {exc}")

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def config() -> None:
    """Configuration commands."""


@main.group()
def policy() -> None:
    """Policy-as-code commands."""


@policy.command("check")
@click.option("--path", "policy_dir", default="policies", show_default=True)
def policy_check(policy_dir: str) -> None:
    """Validate policy files in a directory."""
    from orchestrator.security.policy_loader import load_policy_file

    root = Path(policy_dir).expanduser()
    if not root.exists():
        raise click.ClickException(f"Policy directory not found: {policy_dir}")
    files = sorted(root.glob("*.yaml"))
    if not files:
        raise click.ClickException(f"No policy files found in: {policy_dir}")
    for file in files:
        load_policy_file(str(file))
    console.print(f"Policy files valid: {len(files)}")


@policy.command("diff")
@click.argument("baseline")
@click.argument("current")
def policy_diff_cmd(baseline: str, current: str) -> None:
    """Show policy diff and widenings/tightenings."""
    from orchestrator.security.policy_loader import load_policy_file, policy_diff

    base = load_policy_file(baseline)
    cur = load_policy_file(current)
    diff = policy_diff(base, cur)

    table = Table(title="Policy Diff")
    table.add_column("Type")
    table.add_column("Category")
    table.add_column("Items")
    for cat, items in diff["widenings"].items():
        table.add_row("widening", cat, ", ".join(items) if items else "-")
    for cat, items in diff["tightenings"].items():
        table.add_row("tightening", cat, ", ".join(items) if items else "-")
    console.print(table)


@policy.command("audit")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--policy-file", default="policies/base.yaml", show_default=True)
def policy_audit(config_path: str, policy_file: str) -> None:
    """Check drift between config-derived security policy and policy file."""
    from orchestrator.config import load_config
    from orchestrator.security.policy import build_security_policy
    from orchestrator.security.policy_loader import load_policy_file, policy_diff, policy_hash

    cfg = load_config(config_path)
    runtime_policy = build_security_policy(cfg.security)
    file_policy = load_policy_file(policy_file)
    diff = policy_diff(file_policy, runtime_policy)
    console.print(f"policy_file_hash={policy_hash(file_policy)}")
    console.print(f"runtime_policy_hash={policy_hash(runtime_policy)}")
    has_widening = any(bool(v) for v in diff["widenings"].values())
    if has_widening:
        console.print("[yellow]Policy drift detected (runtime is wider than file policy).[/yellow]")
    else:
        console.print("Policy audit: no widening drift detected.")


@main.group()
def router() -> None:
    """Router weight commands."""


@main.group()
def prompts() -> None:
    """Prompt library commands."""


@prompts.command("list")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def prompts_list(config_path: str) -> None:
    """List prompt templates."""
    library, selection = _load_prompt_library(config_path)
    rows = library.list_templates()
    if not rows:
        console.print("No prompt templates found.")
        return
    table = Table(title="Prompt Templates")
    table.add_column("ID")
    table.add_column("Role")
    table.add_column("Vars")
    table.add_column("Selected")
    for tpl in rows:
        selected = "yes" if selection.get(tpl.role) in {f"{tpl.name}_latest", tpl.template_id, tpl.name} else ""
        table.add_row(tpl.template_id, tpl.role, ", ".join(tpl.variables), selected)
    console.print(table)


@prompts.command("show")
@click.argument("selector")
@click.option("--role", default=None, help="Optional role hint")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def prompts_show(selector: str, role: str | None, config_path: str) -> None:
    """Show a prompt template."""
    library, _ = _load_prompt_library(config_path)
    tpl = library.resolve(selector, role=role)
    console.print(f"id={tpl.template_id} role={tpl.role} hash={tpl.content_hash}")
    console.print(tpl.template)


@prompts.command("test")
@click.argument("selector")
@click.option("--role", default=None, help="Optional role hint")
@click.option("--query", default="hello", show_default=True)
@click.option("--draft", default="draft text", show_default=True)
@click.option("--critique", default="critique text", show_default=True)
@click.option("--answer", default="answer text", show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def prompts_test(
    selector: str,
    role: str | None,
    query: str,
    draft: str,
    critique: str,
    answer: str,
    config_path: str,
) -> None:
    """Render a prompt template with test variables."""
    library, _ = _load_prompt_library(config_path)
    rendered = library.render(
        selector,
        role=role,
        variables={"query": query, "draft": draft, "critique": critique, "answer": answer, "context": ""},
    )
    console.print(rendered)


@router.command("show")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def router_show(config_path: str) -> None:
    """Show learned router weights."""
    orchestrator = _load_orchestrator(config_path)
    snapshot = orchestrator.router_weights.snapshot()
    table = Table(title="Router Weights")
    table.add_column("Provider")
    table.add_column("Domain")
    table.add_column("Score")
    table.add_column("Count")
    table.add_column("P50 ms")
    table.add_column("P95 ms")
    for provider_name, per_domain in snapshot.items():
        for domain, stats in per_domain.items():
            table.add_row(
                provider_name,
                domain,
                f"{float(stats['score']):.3f}",
                str(int(stats["count"])),
                str(int(stats["p50_latency_ms"])),
                str(int(stats["p95_latency_ms"])),
            )
    console.print(table)


@router.command("reset")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def router_reset(config_path: str) -> None:
    """Reset learned router weights."""
    orchestrator = _load_orchestrator(config_path)
    orchestrator.router_weights.reset()
    console.print("Router weights reset.")


@main.group()
def history() -> None:
    """Run artifact history commands."""


@history.command("list")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def history_list(limit: int, config_path: str) -> None:
    """List recent run artifacts."""
    store = _load_artifact_store(config_path)
    rows = store.list_summaries(limit=max(1, limit))
    if not rows:
        console.print("No artifacts found.")
        return
    table = Table(title="Run Artifacts")
    table.add_column("Request ID")
    table.add_column("Started")
    table.add_column("Mode")
    table.add_column("Cost")
    table.add_column("Query")
    for row in rows:
        table.add_row(row.request_id, row.started_at, row.mode, f"{row.cost:.6f}", row.query_preview)
    console.print(table)


@history.command("show")
@click.argument("request_id")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def history_show(request_id: str, config_path: str) -> None:
    """Show full artifact detail."""
    store = _load_artifact_store(config_path)
    data = store.load(request_id)
    console.print_json(data=data)


@history.command("replay")
@click.argument("request_id")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def history_replay(request_id: str, config_path: str) -> None:
    """Replay artifact query with current config and diff output."""
    store = _load_artifact_store(config_path)
    payload = store.load(request_id)
    artifact = payload.get("artifact", {})
    query = str(artifact.get("query", "")).strip()
    if not query:
        raise click.ClickException("Artifact query text is empty; cannot replay")
    mode = artifact.get("mode")
    provider = artifact.get("provider_override")
    fact_check = bool((artifact.get("request_options") or {}).get("fact_check", False))

    orchestrator = _load_orchestrator(config_path)
    replay_result = asyncio.run(
        orchestrator.ask(
            query=query,
            mode=str(mode) if mode is not None else None,
            provider=str(provider) if provider is not None else None,
            fact_check=fact_check,
            verbose=False,
        )
    )
    old_answer = str(((artifact.get("result") or {}).get("answer")) or "")
    new_answer = replay_result.answer
    console.print(f"Replay mode={replay_result.mode} provider={replay_result.provider} cost=${replay_result.cost:.6f}")
    if old_answer == new_answer:
        console.print("No output diff.")
        return
    diff = difflib.unified_diff(
        old_answer.splitlines(),
        new_answer.splitlines(),
        fromfile=f"artifact:{request_id}",
        tofile="replay:current",
        lineterm="",
    )
    console.print("\n".join(diff))


@history.command("export")
@click.argument("request_id")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml"]), default="json", show_default=True)
@click.option("--out", "out_path", default=None, help="Output file path")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def history_export(request_id: str, fmt: str, out_path: str | None, config_path: str) -> None:
    """Export artifact to json or yaml."""
    store = _load_artifact_store(config_path)
    target = out_path or f"./{request_id}.{fmt}"
    path = store.export(request_id, output_path=target, fmt=fmt)
    console.print(f"Exported: {path}")


@config.command(name="check")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def config_check(config_path: str) -> None:
    """Validate config file."""
    from orchestrator.config import ConfigError, load_config

    try:
        load_config(config_path)
        console.print(f"Config valid: {config_path}")
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def cost(config_path: str) -> None:
    """Show usage/cost summary."""
    orchestrator = _load_orchestrator(config_path)
    usage_path = Path(orchestrator.budgets.config.usage_file)
    if not usage_path.exists():
        console.print("No usage data yet.")
        return

    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    if isinstance(usage, dict) and usage.get("encrypted") is True:
        cipher = getattr(orchestrator, "cipher", None)
        if cipher is None:
            raise click.ClickException("Usage file is encrypted but encryption is not configured")
        payload = usage.get("payload")
        if not isinstance(payload, str):
            raise click.ClickException("Invalid encrypted usage payload")
        usage = cipher.maybe_decrypt_json(payload)
    daily = usage.get("daily_totals", {"cost": 0.0, "providers": {}})
    monthly = usage.get("monthly_totals", {"cost": 0.0, "providers": {}})

    table = Table(title="Usage Summary")
    table.add_column("Period")
    table.add_column("Cost (USD)")
    table.add_row("Today", f"{daily['cost']:.6f}")
    table.add_row("Month", f"{monthly['cost']:.6f}")

    remaining = orchestrator.budgets.remaining()
    table.add_row("Remaining Session", f"{remaining['session']:.6f}")
    table.add_row("Remaining Daily", f"{remaining['daily']:.6f}")
    table.add_row("Remaining Monthly", f"{remaining['monthly']:.6f}")
    console.print(table)

    limiter = getattr(orchestrator, "rate_limiter", None)
    if limiter is not None:
        snapshot = limiter.snapshot()
        if snapshot:
            rate_table = Table(title="Rate Limit Status")
            rate_table.add_column("Provider")
            rate_table.add_column("RPM")
            rate_table.add_column("TPM")
            rate_table.add_column("RPM Headroom")
            rate_table.add_column("TPM Headroom")
            for provider_name, stats in snapshot.items():
                rate_table.add_row(
                    provider_name,
                    f"{stats['rpm_used']}/{stats['rpm_limit']}",
                    f"{stats['tpm_used']}/{stats['tpm_limit']}",
                    f"{float(stats['rpm_headroom']):.2f}",
                    f"{float(stats['tpm_headroom']):.2f}",
                )
            console.print(rate_table)


@main.group()
def report() -> None:
    """Reporting commands."""


@report.command("generate")
@click.option("--period", type=click.Choice(["day", "week", "month"]), default="week", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json", "csv", "markdown"]), default="terminal", show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def report_generate(period: str, fmt: str, config_path: str) -> None:
    """Generate usage report."""
    orchestrator = _load_orchestrator(config_path)
    now = datetime.now(timezone.utc)
    if period == "day":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        since = now - timedelta(days=7)
    else:
        since = now - timedelta(days=30)

    store = _load_artifact_store(config_path)
    summaries = store.list_summaries(limit=5000)
    selected = [row for row in summaries if (_parse_iso(row.started_at) or datetime.min.replace(tzinfo=timezone.utc)) >= since]

    total_queries = len(selected)
    total_cost = sum(float(row.cost) for row in selected)
    by_mode: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    cost_by_provider: dict[str, float] = {}
    flag_counts: dict[str, int] = {}
    latency_by_provider: dict[str, list[int]] = {}
    fact_conflicts: dict[str, int] = {}

    for row in selected:
        try:
            payload = store.load(row.request_id)
            artifact = payload.get("artifact", {})
        except Exception:
            continue
        result = artifact.get("result", {}) or {}
        mode = str(result.get("mode", row.mode))
        provider = str(result.get("provider", "")) or "unknown"
        by_mode[mode] = by_mode.get(mode, 0) + 1
        by_provider[provider] = by_provider.get(provider, 0) + 1
        cost_by_provider[provider] = cost_by_provider.get(provider, 0.0) + float(result.get("cost", row.cost))
        for flag in list(result.get("warnings", []) or []):
            key = str(flag)
            flag_counts[key] = flag_counts.get(key, 0) + 1
        latency = int(artifact.get("duration_ms", 0))
        latency_by_provider.setdefault(provider, []).append(latency)
        for claim in list(artifact.get("fact_check", []) or []):
            for conflict in list((claim or {}).get("conflicts", []) or []):
                ckey = str(conflict)
                fact_conflicts[ckey] = fact_conflicts.get(ckey, 0) + 1

    budget_remaining = orchestrator.budgets.remaining()
    cfg_budgets = getattr(getattr(orchestrator, "config", None), "budgets", None)

    def _pct_used(remaining_value: float, cap_value: Any) -> float:
        try:
            cap = float(cap_value)
        except Exception:
            return 0.0
        if cap <= 0:
            return 0.0
        return max(0.0, 100.0 * (1.0 - (float(remaining_value) / cap)))

    budget_util = {
        "session_pct_used": _pct_used(
            budget_remaining.get("session", 0.0),
            getattr(cfg_budgets, "session_usd_cap", None),
        ),
        "daily_pct_used": _pct_used(
            budget_remaining.get("daily", 0.0),
            getattr(cfg_budgets, "daily_usd_cap", None),
        ),
        "monthly_pct_used": _pct_used(
            budget_remaining.get("monthly", 0.0),
            getattr(cfg_budgets, "monthly_usd_cap", None),
        ),
    }
    avg_latency = {
        provider: (sum(vals) / max(1, len(vals)))
        for provider, vals in latency_by_provider.items()
        if vals
    }

    report_obj = {
        "period": period,
        "since": _to_utc_iso(since),
        "generated_at": _to_utc_iso(now),
        "total_queries": total_queries,
        "total_cost_usd": round(total_cost, 6),
        "cost_by_provider": {provider: round(cost, 6) for provider, cost in cost_by_provider.items()},
        "count_by_mode": by_mode,
        "count_by_provider": by_provider,
        "guardian_flag_frequency": flag_counts,
        "avg_latency_ms_by_provider": {k: round(v, 2) for k, v in avg_latency.items()},
        "budget_utilization_pct": {k: round(v, 2) for k, v in budget_util.items()},
        "top_fact_check_failures": sorted(fact_conflicts.items(), key=lambda kv: kv[1], reverse=True)[:5],
    }

    if fmt == "json":
        console.print_json(data=report_obj)
        return
    if fmt == "csv":
        rows = [
            ["metric", "value"],
            ["period", period],
            ["since", report_obj["since"]],
            ["total_queries", str(total_queries)],
            ["total_cost_usd", f"{total_cost:.6f}"],
            ["count_by_mode", json.dumps(by_mode, sort_keys=True)],
            ["count_by_provider", json.dumps(by_provider, sort_keys=True)],
            ["guardian_flag_frequency", json.dumps(flag_counts, sort_keys=True)],
            ["avg_latency_ms_by_provider", json.dumps(report_obj["avg_latency_ms_by_provider"], sort_keys=True)],
            ["budget_utilization_pct", json.dumps(report_obj["budget_utilization_pct"], sort_keys=True)],
        ]
        writer = csv.writer(sys.stdout)
        writer.writerows(rows)
        return
    if fmt == "markdown":
        lines = [
            f"# MMO Report ({period})",
            f"- Generated: {report_obj['generated_at']}",
            f"- Since: {report_obj['since']}",
            f"- Total queries: {total_queries}",
            f"- Total cost (USD): {total_cost:.6f}",
            f"- Modes: {by_mode}",
            f"- Providers: {by_provider}",
            f"- Guardian flags: {flag_counts}",
            f"- Avg latency (ms/provider): {report_obj['avg_latency_ms_by_provider']}",
            f"- Budget utilization (%): {report_obj['budget_utilization_pct']}",
            f"- Top fact-check failures: {report_obj['top_fact_check_failures']}",
        ]
        console.print("\n".join(lines))
        return

    table = Table(title=f"Usage Report ({period})")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Total Queries", str(total_queries))
    table.add_row("Total Cost (USD)", f"{total_cost:.6f}")
    table.add_row("Most Used Modes", ", ".join(f"{k}:{v}" for k, v in sorted(by_mode.items(), key=lambda kv: kv[1], reverse=True)[:5]) or "-")
    table.add_row(
        "Most Used Providers",
        ", ".join(f"{k}:{v}" for k, v in sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)[:5]) or "-",
    )
    table.add_row("Guardian Flag Frequency", ", ".join(f"{k}:{v}" for k, v in sorted(flag_counts.items())[:5]) or "-")
    table.add_row("Avg Latency (ms/provider)", ", ".join(f"{k}:{v:.1f}" for k, v in avg_latency.items()) or "-")
    table.add_row("Budget Utilization", ", ".join(f"{k}:{v:.2f}%" for k, v in report_obj["budget_utilization_pct"].items()))
    table.add_row(
        "Top Fact-check Failures",
        ", ".join(f"{k}:{v}" for k, v in report_obj["top_fact_check_failures"]) or "-",
    )
    console.print(table)


@main.group()
def export() -> None:
    """Export commands."""


@export.command("config")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml"]), default="yaml", show_default=True)
@click.option("--out", "out_path", default=None, help="Output path (stdout if omitted)")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def export_config(fmt: str, out_path: str | None, config_path: str) -> None:
    """Export current effective config with redactions."""
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    redacted = _redact_config_for_export(raw)
    if out_path:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            target.write_text(json.dumps(redacted, indent=2, sort_keys=True), encoding="utf-8")
        else:
            target.write_text(yaml.safe_dump(redacted, sort_keys=False), encoding="utf-8")
        console.print(f"Exported: {target}")
        return
    if fmt == "json":
        console.print_json(data=redacted)
    else:
        console.print(yaml.safe_dump(redacted, sort_keys=False))


@export.command("policies")
@click.option("--policy-dir", default="policies", show_default=True)
@click.option("--out", "out_path", default=None, help="Output json path")
def export_policies(policy_dir: str, out_path: str | None) -> None:
    """Export current policy set."""
    from orchestrator.security.policy_loader import load_policy_file, policy_hash

    root = Path(policy_dir).expanduser()
    if not root.exists():
        raise click.ClickException(f"Policy directory not found: {policy_dir}")
    bundle: dict[str, Any] = {}
    for file in sorted(root.glob("*.yaml")):
        policy = load_policy_file(str(file))
        bundle[file.name] = {"hash": policy_hash(policy), "policy": _redact_config_for_export(asdict(policy))}
    if out_path:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Exported: {target}")
        return
    console.print_json(data=bundle)


@export.command("memories")
@click.option("--out", "out_path", default=None, help="Output json path")
@click.option("--limit", default=1000, show_default=True, type=int)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def export_memories(out_path: str | None, limit: int, config_path: str) -> None:
    """Export redacted memories."""
    store, _ = _load_memory_components(config_path)
    rows = store.list_records(limit=max(1, limit), min_confidence=0.0)
    def _export_text(value: Any, redaction_status: Any) -> str:
        text = str(value or "")
        if str(redaction_status).lower() == "redacted":
            return "[REDACTED]"
        return redact_text(text)

    payload = [
        {
            "id": row.id,
            "statement": _export_text(row.statement, row.redaction_status),
            "source_type": row.source_type,
            "source_ref": _export_text(row.source_ref, row.redaction_status),
            "confidence": row.confidence,
            "ttl_days": row.ttl_days,
            "created_at": row.created_at,
            "reviewed_by": row.reviewed_by,
            "redaction_status": row.redaction_status,
        }
        for row in rows
    ]
    if out_path:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Exported: {target}")
        return
    console.print_json(data=payload)


@main.command()
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--request-timeout-seconds", default=8.0, show_default=True, type=float)
@click.option("--smoke-providers", is_flag=True, default=False, help="Run enabled-provider connection tests")
@click.option("--governance", is_flag=True, default=False, help="Run skill governance/bloat analysis checks")
@click.option("--token", "token_override", default=None, help="Bearer token (overrides env/file lookup)")
@click.option("--token-env", "token_env_override", default=None, help="Env var name to read bearer token from")
@click.option("--json-out", "json_out", default=None, help="Write full doctor report JSON to this path")
def doctor(
    config_path: str,
    request_timeout_seconds: float,
    smoke_providers: bool,
    governance: bool,
    token_override: str | None,
    token_env_override: str | None,
    json_out: str | None,
) -> None:
    """Run daemon diagnostics with actionable pass/fail checks."""
    report = asyncio.run(
        _run_doctor_checks(
            config_path,
            request_timeout_seconds=request_timeout_seconds,
            smoke_providers=smoke_providers,
            governance=governance,
            token_override=token_override,
            token_env_override=token_env_override,
        )
    )
    table = Table(title="MMO Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Latency")
    table.add_column("Detail")
    status_style = {"PASS": "green", "FAIL": "red", "SKIP": "yellow"}
    for item in report.get("checks", []):
        status = str(item.get("status", "FAIL"))
        detail = str(item.get("detail", ""))
        table.add_row(
            str(item.get("name", "")),
            f"[{status_style.get(status, 'white')}]{status}[/{status_style.get(status, 'white')}]",
            f"{int(item.get('latency_ms', 0))}ms",
            detail,
        )
    console.print(table)
    summary = report.get("summary", {})
    console.print(
        f"[bold]Summary:[/bold] passed={summary.get('passed', 0)} "
        f"failed={summary.get('failed', 0)} skipped={summary.get('skipped', 0)} total={summary.get('total', 0)}"
    )
    if json_out:
        out_path = Path(json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        console.print(f"Wrote doctor report: {out_path}")
    if int(summary.get("failed", 0)) > 0:
        raise click.ClickException(f"{summary.get('failed', 0)} doctor checks failed")


@export.command("artifacts")
@click.option("--since", default=None, help="Only export artifacts since YYYY-MM-DD")
@click.option("--out", "out_path", default=None, help="Output json path")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def export_artifacts(since: str | None, out_path: str | None, config_path: str) -> None:
    """Export run artifacts."""
    store = _load_artifact_store(config_path)
    cutoff = None
    if since:
        try:
            cutoff = datetime.fromisoformat(f"{since}T00:00:00+00:00")
        except Exception as exc:
            raise click.ClickException(f"Invalid --since date: {since}") from exc
    exported: list[dict[str, Any]] = []
    for row in store.list_summaries(limit=5000):
        started = _parse_iso(row.started_at)
        if cutoff is not None and (started is None or started < cutoff):
            continue
        try:
            exported.append(store.load(row.request_id))
        except Exception:
            continue
    if out_path:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(exported, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"Exported: {target}")
        return
    console.print_json(data=exported)


@main.command()
@click.option("--refresh-seconds", default=5.0, show_default=True, type=float, help="Dashboard refresh interval")
@click.option("--once", is_flag=True, default=False, help="Render one snapshot and exit")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def dashboard(refresh_seconds: float, once: bool, config_path: str) -> None:
    """Show live monitoring dashboard."""

    def _collect_payload() -> dict[str, Any]:
        daemon = asyncio.run(_fetch_daemon_dashboard(config_path))
        if daemon is not None:
            return daemon
        return _collect_local_dashboard(config_path)

    if once:
        console.print(_render_dashboard(_collect_payload()))
        return

    refresh_seconds = max(0.5, refresh_seconds)
    with _stdin_cbreak():
        with Live(_render_dashboard(_collect_payload()), refresh_per_second=4, console=console) as live:
            while True:
                if sys.stdin.isatty():
                    readable, _, _ = select.select([sys.stdin], [], [], refresh_seconds)
                    if readable:
                        ch = sys.stdin.read(1)
                        if ch.lower() == "q":
                            break
                else:
                    time.sleep(refresh_seconds)
                live.update(_render_dashboard(_collect_payload()))


@main.command()
@click.option("--host", default=None, help="Bind host override (default from config)")
@click.option("--port", default=None, type=int, help="Bind port override (default from config)")
@click.option("--expose", is_flag=True, default=False, help="Bind to 0.0.0.0 with warning")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def serve(host: str | None, port: int | None, expose: bool, config_path: str) -> None:
    """Start local HTTP daemon server."""
    from orchestrator.server import run_server

    orchestrator = _load_orchestrator(config_path)
    bind_host = host or orchestrator.config.server.host
    bind_port = port or orchestrator.config.server.port
    if expose:
        bind_host = "0.0.0.0"
        console.print("[yellow]Warning:[/yellow] --expose enabled; server is reachable on all interfaces.")
    run_server(orchestrator, host=bind_host, port=bind_port)


@main.group()
def eval() -> None:
    """Evaluation commands."""


@eval.command(name="run")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def eval_run(config_path: str) -> None:
    """Run the minimal eval harness."""
    from evaluation.harness import run_eval

    async def _run() -> None:
        orchestrator = _load_orchestrator(config_path)
        summary = await run_eval(orchestrator, "evaluation/tasks", "evaluation/baselines")
        console.print(f"Eval complete: {summary['total']} tasks, output in evaluation/baselines/latest_eval.json")

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@eval.command(name="adversarial")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--fixtures-dir", default="evaluation/adversarial", show_default=True)
@click.option("--out-file", default="evaluation/baselines/adversarial_latest.json", show_default=True)
def eval_adversarial(config_path: str, fixtures_dir: str, out_file: str) -> None:
    """Run adversarial evaluation fixtures and report security-layer catches."""
    from evaluation.adversarial.runner import run_adversarial_eval

    async def _run() -> None:
        orchestrator = _load_orchestrator(config_path)
        summary = await run_adversarial_eval(orchestrator, fixtures_dir, out_file)
        console.print(
            f"Adversarial eval complete: total={summary['total']} passed={summary['passed']} failed={summary['failed']} "
            f"output={out_file}"
        )

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@eval.command(name="roles")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--fixtures", "fixtures_path", default="evaluation/roles/critique_roles.yaml", show_default=True)
@click.option("--out-file", default="evaluation/baselines/role_eval_latest.json", show_default=True)
@click.option("--strategy", type=click.Choice(["quality", "balanced", "cost"]), default="balanced", show_default=True)
@click.option("--apply-best", is_flag=True, default=False, help="Apply recommended critique role routes after evaluation.")
def eval_roles(config_path: str, fixtures_path: str, out_file: str, strategy: str, apply_best: bool) -> None:
    """Benchmark drafter/critic/refiner providers and optionally apply best role routes."""
    from evaluation.role_harness import run_role_eval

    async def _run() -> None:
        orchestrator = _load_orchestrator(config_path)
        summary = await run_role_eval(
            orchestrator=orchestrator,
            fixtures_path=fixtures_path,
            out_file=out_file,
            strategy=strategy,
            apply_best=apply_best,
        )

        console.print(
            f"Role eval complete: fixtures={summary['fixtures_total']} providers={len(summary['enabled_providers'])} "
            f"strategy={summary['strategy']}"
        )
        for role in ("drafter", "critic", "refiner"):
            table = Table(title=f"Role Benchmark: {role}")
            table.add_column("Rank")
            table.add_column("Provider")
            table.add_column("Model")
            table.add_column("Score")
            table.add_column("JSON%")
            table.add_column("LowSig%")
            table.add_column("Err%")
            table.add_column("Avg Cost")
            ranked = list((summary.get("ranked") or {}).get(role, []))
            for idx, row in enumerate(ranked[:5], start=1):
                table.add_row(
                    str(idx),
                    str(row.get("provider", "")),
                    str(row.get("model", "")),
                    f"{float(row.get('score', 0.0)):.3f}",
                    f"{float(row.get('json_valid_rate', 0.0)) * 100:.1f}",
                    f"{float(row.get('low_signal_rate', 1.0)) * 100:.1f}",
                    f"{float(row.get('error_rate', 1.0)) * 100:.1f}",
                    f"{float(row.get('avg_cost', 0.0)):.6f}",
                )
            console.print(table)

        winners = summary.get("winners", {})
        if isinstance(winners, dict):
            console.print(
                "Recommended critique routes: "
                f"drafter={winners.get('drafter', '?')} "
                f"critic={winners.get('critic', '?')} "
                f"refiner={winners.get('refiner', '?')}"
            )
        if apply_best:
            console.print("Applied recommended routes to orchestrator role routing.")
        console.print(f"Saved report: {out_file}")

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def routes() -> None:
    """Role routing profile commands."""


@routes.command("profile-save")
@click.option("--name", "profile_name", required=True, help="Profile name (e.g., stable, experimental).")
@click.option("--profiles-file", default="evaluation/routes/profiles.json", show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def routes_profile_save(profile_name: str, profiles_file: str, config_path: str) -> None:
    """Save current role routes into a named profile."""
    orchestrator = _load_orchestrator(config_path)
    routes_payload = orchestrator.get_role_routes()
    profiles_path = Path(profiles_file).expanduser()
    payload = _load_route_profiles(profiles_path)
    profiles = payload.setdefault("profiles", {})
    assert isinstance(profiles, dict)
    profiles[str(profile_name)] = {
        "routes": routes_payload,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_route_profiles(profiles_path, payload)
    console.print(f"Saved route profile '{profile_name}' to {profiles_path}")


@routes.command("profile-apply")
@click.option("--name", "profile_name", required=True, help="Profile name to apply.")
@click.option("--profiles-file", default="evaluation/routes/profiles.json", show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def routes_profile_apply(profile_name: str, profiles_file: str, config_path: str) -> None:
    """Apply a named role-route profile."""
    profiles_path = Path(profiles_file).expanduser()
    payload = _load_route_profiles(profiles_path)
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        raise click.ClickException(f"Invalid profiles file: {profiles_path}")
    record = profiles.get(str(profile_name))
    if not isinstance(record, dict):
        raise click.ClickException(f"Profile '{profile_name}' not found in {profiles_path}")
    routes_payload = record.get("routes")
    if not isinstance(routes_payload, dict):
        raise click.ClickException(f"Profile '{profile_name}' has no valid routes payload")

    orchestrator = _load_orchestrator(config_path)
    applied = orchestrator.apply_role_routes(routes_payload)
    console.print(f"Applied route profile '{profile_name}' from {profiles_path}")
    console.print_json(data=applied)


@routes.command("profile-list")
@click.option("--profiles-file", default="evaluation/routes/profiles.json", show_default=True)
def routes_profile_list(profiles_file: str) -> None:
    """List available route profiles."""
    profiles_path = Path(profiles_file).expanduser()
    payload = _load_route_profiles(profiles_path)
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        console.print(f"No route profiles found in {profiles_path}")
        return
    table = Table(title="Route Profiles")
    table.add_column("Name")
    table.add_column("Saved At")
    table.add_column("Sections")
    for name in sorted(profiles.keys()):
        record = profiles.get(name, {})
        saved_at = str(record.get("saved_at", ""))
        routes_payload = record.get("routes", {})
        sections = str(len(routes_payload)) if isinstance(routes_payload, dict) else "0"
        table.add_row(str(name), saved_at, sections)
    console.print(table)


@routes.command("promote")
@click.option("--profiles-file", default="evaluation/routes/profiles.json", show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--runs", type=int, required=True, help="Number of experimental runs evaluated.")
@click.option("--low-signal", type=int, required=True, help="Count of low-signal warnings across the runs.")
@click.option("--empty-final", type=int, required=True, help="Count of empty/low final-answer fallbacks.")
@click.option("--avg-cost", type=float, required=True, help="Average cost per run in USD.")
@click.option("--avg-latency-ms", type=int, required=True, help="Average latency per run in ms.")
@click.option("--max-low-signal-rate", type=float, default=0.20, show_default=True)
@click.option("--max-empty-final", type=int, default=0, show_default=True)
@click.option("--max-avg-cost", type=float, default=0.01, show_default=True)
@click.option("--max-avg-latency-ms", type=int, default=120000, show_default=True)
@click.option("--stable-profile", default="stable", show_default=True)
@click.option("--experimental-profile", default="experimental", show_default=True)
def routes_promote(
    profiles_file: str,
    config_path: str,
    runs: int,
    low_signal: int,
    empty_final: int,
    avg_cost: float,
    avg_latency_ms: int,
    max_low_signal_rate: float,
    max_empty_final: int,
    max_avg_cost: float,
    max_avg_latency_ms: int,
    stable_profile: str,
    experimental_profile: str,
) -> None:
    """Apply experimental profile only if acceptance gate passes; otherwise apply stable."""
    if runs <= 0:
        raise click.ClickException("--runs must be > 0")
    low_signal_rate = float(low_signal) / float(runs)
    checks = {
        "low_signal_rate": low_signal_rate <= max_low_signal_rate,
        "empty_final": empty_final <= max_empty_final,
        "avg_cost": avg_cost <= max_avg_cost,
        "avg_latency_ms": avg_latency_ms <= max_avg_latency_ms,
    }
    passed = all(checks.values())
    chosen = experimental_profile if passed else stable_profile

    console.print(
        "Promotion gate: "
        f"runs={runs} low_signal_rate={low_signal_rate:.3f} empty_final={empty_final} "
        f"avg_cost={avg_cost:.6f} avg_latency_ms={avg_latency_ms} -> "
        f"{'PASS' if passed else 'FAIL'}"
    )
    for key, ok in checks.items():
        console.print(f"- {key}: {'PASS' if ok else 'FAIL'}")

    profiles_path = Path(profiles_file).expanduser()
    payload = _load_route_profiles(profiles_path)
    profiles = payload.get("profiles", {})
    if not isinstance(profiles, dict):
        raise click.ClickException(f"Invalid profiles file: {profiles_path}")
    record = profiles.get(str(chosen))
    if not isinstance(record, dict):
        raise click.ClickException(f"Profile '{chosen}' not found in {profiles_path}")
    routes_payload = record.get("routes")
    if not isinstance(routes_payload, dict):
        raise click.ClickException(f"Profile '{chosen}' has no valid routes payload")

    orchestrator = _load_orchestrator(config_path)
    orchestrator.apply_role_routes(routes_payload)
    decision = {
        "passed": passed,
        "applied_profile": chosen,
        "metrics": {
            "runs": runs,
            "low_signal": low_signal,
            "empty_final": empty_final,
            "avg_cost": avg_cost,
            "avg_latency_ms": avg_latency_ms,
        },
        "thresholds": {
            "max_low_signal_rate": max_low_signal_rate,
            "max_empty_final": max_empty_final,
            "max_avg_cost": max_avg_cost,
            "max_avg_latency_ms": max_avg_latency_ms,
        },
        "checks": checks,
    }
    payload["last_promotion"] = decision
    _save_route_profiles(profiles_path, payload)
    console.print(f"Applied profile '{chosen}' and saved promotion decision to {profiles_path}")
    console.print_json(data=decision)


@main.group()
def batch() -> None:
    """Batch processing commands."""


async def _run_batch_jobs(
    *,
    orchestrator,
    jobs: list[dict[str, Any]],
    output_file: str,
    parallel: int,
) -> dict[str, int]:
    from orchestrator.budgets import BudgetExceededError

    sem = asyncio.Semaphore(max(1, parallel))
    stop_on_budget = asyncio.Event()
    summary = {"total": len(jobs), "completed": 0, "failed": 0, "skipped": 0}

    async def _one(job: dict[str, Any]) -> None:
        if stop_on_budget.is_set():
            summary["skipped"] += 1
            return
        jid = str(job.get("id", "")).strip() or str(uuid4())
        query = str(job.get("query", "")).strip()
        if not query:
            summary["failed"] += 1
            _append_jsonl(output_file, {"id": jid, "status": "failed", "error": "missing query"})
            return
        async with sem:
            try:
                result = await orchestrator.ask(
                    query=query,
                    mode=str(job.get("mode")) if job.get("mode") is not None else None,
                    provider=str(job.get("provider")) if job.get("provider") is not None else None,
                    fact_check=bool(job.get("fact_check", False)),
                    tools=str(job.get("tools")) if job.get("tools") is not None else None,
                    verbose=False,
                )
                summary["completed"] += 1
                _append_jsonl(
                    output_file,
                    {
                        "id": jid,
                        "status": "ok",
                        "mode": result.mode,
                        "provider": result.provider,
                        "answer": result.answer,
                        "cost": result.cost,
                        "tokens": result.tokens_in + result.tokens_out,
                        "guardian_flags": result.warnings or [],
                    },
                )
            except BudgetExceededError as exc:
                stop_on_budget.set()
                summary["failed"] += 1
                _append_jsonl(output_file, {"id": jid, "status": "failed", "error": str(exc), "budget_exceeded": True})
            except Exception as exc:
                summary["failed"] += 1
                _append_jsonl(output_file, {"id": jid, "status": "failed", "error": str(exc)})

    await asyncio.gather(*[_one(job) for job in jobs])
    return summary


@batch.command("run")
@click.argument("input_file")
@click.option("--output-file", default="evaluation/batch_results.jsonl", show_default=True)
@click.option("--parallel", default=1, type=int, show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def batch_run(input_file: str, output_file: str, parallel: int, config_path: str) -> None:
    """Run batch queries from JSONL input."""
    jobs = _read_jsonl(input_file)
    if not jobs:
        raise click.ClickException("Input JSONL has no jobs")
    output_path = Path(output_file).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    meta_path = output_path.with_suffix(output_path.suffix + ".meta.json")
    meta_path.write_text(json.dumps({"input_file": str(Path(input_file).expanduser())}, indent=2), encoding="utf-8")

    orchestrator = _load_orchestrator(config_path)
    summary = asyncio.run(_run_batch_jobs(orchestrator=orchestrator, jobs=jobs, output_file=str(output_path), parallel=parallel))
    console.print(
        f"Batch complete: total={summary['total']} completed={summary['completed']} failed={summary['failed']} skipped={summary['skipped']} output={output_path}"
    )


@batch.command("resume")
@click.argument("output_file")
@click.option("--input-file", default=None, help="Optional override if sidecar metadata is missing")
@click.option("--parallel", default=1, type=int, show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def batch_resume(output_file: str, input_file: str | None, parallel: int, config_path: str) -> None:
    """Resume a partial batch, skipping completed IDs."""
    output_path = Path(output_file).expanduser()
    if not output_path.exists():
        raise click.ClickException(f"Output file not found: {output_file}")
    meta_path = output_path.with_suffix(output_path.suffix + ".meta.json")
    if input_file is None:
        if not meta_path.exists():
            raise click.ClickException("Missing batch metadata; pass --input-file")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        input_file = str(meta.get("input_file", ""))
    if not input_file:
        raise click.ClickException("Input file not available; pass --input-file")

    done_ids = {str(item.get("id")) for item in _read_jsonl(str(output_path)) if isinstance(item, dict) and item.get("status") == "ok"}
    jobs = [job for job in _read_jsonl(input_file) if str(job.get("id", "")) not in done_ids]
    if not jobs:
        console.print("Nothing to resume.")
        return
    orchestrator = _load_orchestrator(config_path)
    summary = asyncio.run(_run_batch_jobs(orchestrator=orchestrator, jobs=jobs, output_file=str(output_path), parallel=parallel))
    console.print(
        f"Batch resume complete: total={summary['total']} completed={summary['completed']} failed={summary['failed']} skipped={summary['skipped']}"
    )


@batch.command("report")
@click.argument("output_file")
def batch_report(output_file: str) -> None:
    """Generate summary statistics for batch output JSONL."""
    rows = _read_jsonl(output_file)
    if not rows:
        raise click.ClickException("No batch results found")

    total = len(rows)
    ok = [r for r in rows if r.get("status") == "ok"]
    failed = [r for r in rows if r.get("status") != "ok"]
    total_cost = sum(float(r.get("cost", 0.0)) for r in ok)
    total_tokens = sum(int(r.get("tokens", 0)) for r in ok)
    mode_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    for row in ok:
        mode = str(row.get("mode", ""))
        provider = str(row.get("provider", ""))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        for flag in list(row.get("guardian_flags", [])):
            key = str(flag)
            flag_counts[key] = flag_counts.get(key, 0) + 1

    table = Table(title="Batch Report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Total Queries", str(total))
    table.add_row("Completed", str(len(ok)))
    table.add_row("Failed/Skipped", str(len(failed)))
    table.add_row("Total Cost (USD)", f"{total_cost:.6f}")
    table.add_row("Total Tokens", str(total_tokens))
    table.add_row("Avg Cost per Query", f"{(total_cost / max(1, len(ok))):.6f}")
    table.add_row("Mode Distribution", ", ".join(f"{k}:{v}" for k, v in sorted(mode_counts.items())) or "-")
    table.add_row("Provider Distribution", ", ".join(f"{k}:{v}" for k, v in sorted(provider_counts.items())) or "-")
    table.add_row("Guardian Flag Summary", ", ".join(f"{k}:{v}" for k, v in sorted(flag_counts.items())) or "-")
    console.print(table)


@main.group()
def skill() -> None:
    """Workflow skill commands."""


@skill.command("list")
def skill_list() -> None:
    """List installed/discovered skills."""
    from orchestrator.skills.registry import discover_skills

    rows = discover_skills()
    if not rows:
        console.print("No skills installed.")
        return
    table = Table(title="Skills")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Signed")
    table.add_column("Checksum")
    table.add_column("Path")
    for name in sorted(rows):
        record = rows[name]
        table.add_row(
            record.name,
            "yes" if record.enabled else "no",
            "yes" if record.signature_verified else "no",
            record.checksum[:20] if record.checksum else "-",
            record.path,
        )
    console.print(table)


@skill.command("analyze-bloat")
@click.option("--out-dir", default="evaluation/skills_governance", show_default=True)
@click.option("--include-disabled", is_flag=True, default=False, help="Include disabled skills in analysis")
@click.option("--merge-threshold", default=0.72, type=float, show_default=True)
@click.option("--crossover-min", default=0.45, type=float, show_default=True)
@click.option("--crossover-max-io", default=0.34, type=float, show_default=True)
def skill_analyze_bloat(
    out_dir: str,
    include_disabled: bool,
    merge_threshold: float,
    crossover_min: float,
    crossover_max_io: float,
) -> None:
    """Analyze installed skills for merge/crossover bloat candidates."""
    from orchestrator.skills.governance import analyze_skill_bloat

    if not (0.0 <= merge_threshold <= 1.0):
        raise click.ClickException("--merge-threshold must be between 0 and 1")
    if not (0.0 <= crossover_min <= 1.0):
        raise click.ClickException("--crossover-min must be between 0 and 1")
    if not (0.0 <= crossover_max_io <= 1.0):
        raise click.ClickException("--crossover-max-io must be between 0 and 1")

    result = analyze_skill_bloat(
        out_dir=out_dir,
        include_disabled=include_disabled,
        merge_threshold=merge_threshold,
        crossover_min=crossover_min,
        crossover_max_io=crossover_max_io,
    )
    console.print(
        "Skill bloat analysis complete: "
        f"skills={result['skills_analyzed']} merge={result['merge_candidates']} "
        f"crossover={result['crossover_candidates']}"
    )
    console.print(f"Artifacts written to: {result['out_dir']}")


@skill.command("install")
@click.argument("source")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--require-signature/--no-require-signature", default=None, help="Override config signature requirement")
def skill_install(source: str, config_path: str, require_signature: bool | None) -> None:
    """Install a workflow skill from local file/directory."""
    from orchestrator.skills.registry import install_skill

    settings = _load_skill_settings(config_path)
    require_sig = settings["require_signature"] if require_signature is None else require_signature
    trusted_public_keys = list(settings["trusted_public_keys"])
    try:
        record = install_skill(
            source,
            require_signature=require_sig,
            trusted_public_keys=trusted_public_keys,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Installed skill: {record.name}")
    console.print(record.path)
    console.print(f"checksum={record.checksum}")
    console.print(f"signature_verified={record.signature_verified}")


@skill.command("enable")
@click.argument("name")
def skill_enable(name: str) -> None:
    """Enable an installed skill."""
    from orchestrator.skills.registry import set_skill_enabled

    try:
        record = set_skill_enabled(name, True)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Enabled skill: {record.name}")


@skill.command("disable")
@click.argument("name")
def skill_disable(name: str) -> None:
    """Disable an installed skill."""
    from orchestrator.skills.registry import set_skill_enabled

    try:
        record = set_skill_enabled(name, False)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Disabled skill: {record.name}")


@skill.command("test")
@click.argument("name_or_path")
@click.option("--input", "input_json", default="{}", show_default=True, help="JSON object for run-mode tests")
@click.option("--run", is_flag=True, default=False, help="Execute the workflow after validation")
@click.option("--adversarial", is_flag=True, default=False, help="Run adversarial fixtures against this skill")
@click.option("--fixtures", default=None, help="Path to adversarial fixtures YAML list")
@click.option("--mode", default="single", show_default=True)
@click.option("--provider", default=None)
@click.option("--budget-cap", default=None, type=float)
@click.option(
    "--shadow-confirm",
    is_flag=True,
    default=False,
    help="Acknowledge predicted outcome report for risky skill steps.",
)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def skill_test(
    name_or_path: str,
    input_json: str,
    run: bool,
    adversarial: bool,
    fixtures: str | None,
    mode: str,
    provider: str | None,
    budget_cap: float | None,
    shadow_confirm: bool,
    config_path: str,
) -> None:
    """Validate (and optionally execute) a workflow skill."""
    from orchestrator.skills.registry import validate_workflow_file
    from orchestrator.skills.workflow import run_workflow_skill

    skill_path = _resolve_skill_path(name_or_path)
    valid, errors, _data = validate_workflow_file(str(skill_path))
    if not valid:
        raise click.ClickException("; ".join(errors))
    console.print(f"Skill validation passed: {skill_path}")
    if not run and not adversarial:
        return

    try:
        parsed_input = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--input must be valid JSON: {exc}") from exc
    if not isinstance(parsed_input, dict):
        raise click.ClickException("--input must decode to a JSON object")
    if shadow_confirm:
        parsed_input["_shadow_confirm"] = True

    orchestrator = _load_orchestrator(config_path)
    if run:
        result = asyncio.run(
            run_workflow_skill(
                orchestrator,
                skill_path=str(skill_path),
                input_data={str(k): v for k, v in parsed_input.items()},
                mode=mode,
                provider=provider,
                budget_cap_usd=budget_cap,
            )
        )
        console.print(
            f"Skill test run passed: name={result.skill_name} steps={result.steps_executed} cost=${result.total_cost:.6f}"
        )
    if adversarial:
        from orchestrator.skills.testing import run_skill_adversarial_tests

        fixture_path = fixtures
        if fixture_path is None:
            stem = skill_path.stem
            if stem.endswith(".workflow"):
                stem = stem[: -len(".workflow")]
            candidate = Path("evaluation/skills_adversarial") / f"{stem}.yaml"
            if candidate.exists():
                fixture_path = str(candidate)
            else:
                raise click.ClickException(
                    "No default adversarial fixtures found; pass --fixtures <path> "
                    "or create evaluation/skills_adversarial/<skill>.yaml"
                )
        summary = asyncio.run(
            run_skill_adversarial_tests(
                orchestrator,
                skill_path=str(skill_path),
                fixtures_path=fixture_path,
                mode=mode,
                provider=provider,
                budget_cap_usd=budget_cap,
            )
        )
        console.print(
            f"Skill adversarial test summary: total={summary['total']} passed={summary['passed']} failed={summary['failed']}"
        )
        if summary["failed"] > 0:
            failed_ids = [str(item.get("case_id")) for item in summary["results"] if not item.get("passed")]
            raise click.ClickException(f"Adversarial failures: {', '.join(failed_ids)}")


@skill.command("checksum")
@click.argument("name_or_path")
def skill_checksum(name_or_path: str) -> None:
    """Compute deterministic checksum for a skill file/directory."""
    from orchestrator.skills.signing import compute_skill_checksum

    skill_path = _resolve_skill_path(name_or_path)
    console.print(compute_skill_checksum(str(skill_path)))


@skill.command("keygen")
@click.option("--private-key", "private_key_path", default=None, help="Private key output path (PEM)")
@click.option("--public-key", "public_key_path", default=None, help="Public key output path (PEM)")
@click.option("--overwrite", is_flag=True, default=False, help="Overwrite existing files")
def skill_keygen(private_key_path: str | None, public_key_path: str | None, overwrite: bool) -> None:
    """Generate an Ed25519 keypair for skill signing."""
    from orchestrator.skills.signing import generate_skill_keypair

    try:
        private_path, public_path = generate_skill_keypair(
            private_key_path=private_key_path,
            public_key_path=public_key_path,
            overwrite=overwrite,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Generated private key: {private_path}")
    console.print(f"Generated public key: {public_path}")


@skill.command("sign")
@click.argument("name_or_path")
@click.option("--private-key", "private_key_path", required=True, help="Path to Ed25519 private key PEM")
@click.option("--signature", "signature_path", default=None, help="Optional signature output path")
@click.option("--signer", default=None, help="Optional signer label")
def skill_sign(name_or_path: str, private_key_path: str, signature_path: str | None, signer: str | None) -> None:
    """Sign a skill using an Ed25519 private key."""
    from orchestrator.skills.signing import sign_skill

    skill_path = _resolve_skill_path(name_or_path)
    out = sign_skill(
        str(skill_path),
        private_key_path=private_key_path,
        signature_path=signature_path,
        signer=signer,
    )
    console.print(f"Wrote signature: {out}")


@skill.command("verify")
@click.argument("name_or_path")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
@click.option("--public-key", "public_keys", multiple=True, help="Path to trusted Ed25519 public key PEM")
@click.option("--signature", "signature_path", default=None, help="Optional signature file path")
def skill_verify(
    name_or_path: str,
    config_path: str,
    public_keys: tuple[str, ...],
    signature_path: str | None,
) -> None:
    """Verify skill signature and checksum."""
    from orchestrator.skills.signing import verify_skill_signature

    settings = _load_skill_settings(config_path)
    keys = list(public_keys) if public_keys else list(settings["trusted_public_keys"])
    skill_path = _resolve_skill_path(name_or_path)
    ok, reason = verify_skill_signature(str(skill_path), public_key_paths=keys, signature_path=signature_path)
    if not ok:
        raise click.ClickException(reason)
    console.print(f"Signature verified: {reason}")


@skill.command("run")
@click.argument("skill_ref")
@click.option("--input", "input_json", default="{}", show_default=True, help="JSON object of workflow inputs")
@click.option("--mode", default="single", show_default=True, help="Default mode for model_call steps")
@click.option("--provider", default=None, help="Optional provider override for model_call steps")
@click.option("--budget-cap", default=None, type=float, help="Override workflow budget cap in USD")
@click.option(
    "--shadow-confirm",
    is_flag=True,
    default=False,
    help="Acknowledge predicted outcome report for risky skill steps.",
)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def skill_run(
    skill_ref: str,
    input_json: str,
    mode: str,
    provider: str | None,
    budget_cap: float | None,
    shadow_confirm: bool,
    config_path: str,
) -> None:
    """Run a declarative workflow skill file."""
    from orchestrator.skills.workflow import run_workflow_skill

    try:
        parsed_input = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--input must be valid JSON: {exc}") from exc
    if not isinstance(parsed_input, dict):
        raise click.ClickException("--input must decode to a JSON object")
    if shadow_confirm:
        parsed_input["_shadow_confirm"] = True
    if budget_cap is not None and budget_cap <= 0:
        raise click.ClickException("--budget-cap must be > 0")

    skill_path = _resolve_skill_path(skill_ref)
    orchestrator = _load_orchestrator(config_path)
    result = asyncio.run(
        run_workflow_skill(
            orchestrator,
            skill_path=str(skill_path),
            input_data={str(k): v for k, v in parsed_input.items()},
            mode=mode,
            provider=provider,
            budget_cap_usd=budget_cap,
        )
    )
    console.print(
        f"Skill run complete: name={result.skill_name} steps={result.steps_executed} cost=${result.total_cost:.6f}"
    )
    console.print_json(data=result.outputs)


@main.group()
def tool() -> None:
    """Tool simulation commands."""


@tool.command(name="simulate")
@click.option("--tool-name", default="fetch_url", show_default=True)
@click.option("--arg", "arg_pairs", multiple=True, help="Tool arg as key=value (repeatable)")
@click.option("--request-id", default="sim-request", show_default=True)
@click.option("--estimated-cost", default=0.0, type=float, show_default=True)
@click.option("--require-human", is_flag=True, default=False, help="Force human approval requirement")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def tool_simulate(
    tool_name: str,
    arg_pairs: tuple[str, ...],
    request_id: str,
    estimated_cost: float,
    require_human: bool,
    config_path: str,
) -> None:
    """Run propose -> broker -> human gate -> execute pipeline."""
    from orchestrator.config import load_config
    from orchestrator.observability.audit import AuditLogger
    from orchestrator.security.broker import CapabilityBroker, RequestContext
    from orchestrator.security.encryption import build_envelope_cipher
    from orchestrator.security.guardian import Guardian
    from orchestrator.security.human_gate import HumanGate
    from orchestrator.security.policy import ToolPolicy, build_security_policy
    from orchestrator.security.taint import TaintedString
    from orchestrator.tools.simulated import execute_simulated_tool
    from orchestrator.budgets import BudgetTracker

    config = load_config(config_path)
    cipher = build_envelope_cipher(config.security.data_protection)
    audit_path = Path(config.budgets.usage_file).expanduser().with_name("audit.jsonl")
    args: dict[str, TaintedString] = {}
    for pair in arg_pairs:
        if "=" not in pair:
            raise click.ClickException(f"Invalid --arg format: {pair}. Expected key=value")
        key, value = pair.split("=", 1)
        args[key] = TaintedString(value=value, source="user_input", source_id=f"{request_id}:{key}", taint_level="untrusted")

    policy = build_security_policy(config.security)
    if require_human:
        existing = policy.tool_policies.get(tool_name, ToolPolicy(name=tool_name))
        existing.requires_human_approval = True
        policy.tool_policies[tool_name] = existing
        if tool_name not in policy.tool_allowlist:
            policy.tool_allowlist.append(tool_name)

    broker = CapabilityBroker(
        policy=policy,
        guardian=Guardian(config.security),
        budgets=BudgetTracker(config.budgets, cipher=cipher),
        audit_logger=AuditLogger(str(audit_path), cipher=cipher),
        human_gate=HumanGate(),
    )

    context = RequestContext(
        request_id=request_id,
        requester="mmctl.tool.simulate",
        estimated_cost=estimated_cost,
        approved_plan_tools=[tool_name],
    )
    decision = broker.request_capability(tool_name=tool_name, args=args, request_context=context)
    if hasattr(decision, "reason"):
        console.print(f"[red]Denied[/red]: {decision.reason}")
        console.print(f"Details: {decision.details}")
        return

    result = broker.execute_with_capability(
        token=decision,
        executor=lambda scope: execute_simulated_tool(tool_name, scope),
    )
    if hasattr(result, "reason"):
        console.print(f"[red]Execution denied[/red]: {result.reason}")
        console.print(f"Details: {result.details}")
        return

    console.print("[green]Capability granted and executed[/green]")
    console.print_json(data=result)


@main.group()
def memory() -> None:
    """Governed memory commands."""


@memory.command(name="add")
@click.argument("statement")
@click.option("--source-type", default="user_preference", show_default=True)
@click.option("--source-ref", default="cli.manual", show_default=True)
@click.option("--confidence", default=0.7, type=float, show_default=True)
@click.option("--ttl-days", default=30, type=int, show_default=True)
@click.option("--reviewed-by", default=None, help="Reviewer identity")
@click.option("--model-inferred", is_flag=True, default=False, help="Require confirmation for inferred memory")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def memory_add(
    statement: str,
    source_type: str,
    source_ref: str,
    confidence: float,
    ttl_days: int,
    reviewed_by: str | None,
    model_inferred: bool,
    config_path: str,
) -> None:
    """Store a governed memory statement."""
    from orchestrator.memory.summarize import summarize_for_memory

    if ttl_days <= 0:
        raise click.ClickException("ttl-days must be > 0")
    if confidence < 0 or confidence > 1:
        raise click.ClickException("confidence must be between 0 and 1")

    store, governance = _load_memory_components(config_path)
    summary = summarize_for_memory(statement)
    decision = governance.evaluate_write(
        statement=summary,
        source_type=source_type,
        source_ref=source_ref,
        is_model_inferred=model_inferred,
        confirm_fn=lambda prompt: click.confirm(prompt, default=False),
    )
    if not decision.allowed:
        raise click.ClickException(f"Memory write denied: {decision.reason}")

    record_id = store.add(
        statement=decision.redacted_statement,
        source_type=source_type,
        source_ref=source_ref,
        confidence=confidence,
        ttl_days=ttl_days,
        reviewed_by=reviewed_by,
        redaction_status="redacted",
    )
    console.print(f"Stored memory id={record_id}")


@memory.command(name="list")
@click.option("--limit", default=50, type=int, show_default=True)
@click.option("--min-confidence", default=0.0, type=float, show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def memory_list(limit: int, min_confidence: float, config_path: str) -> None:
    """List stored memories."""
    store, _ = _load_memory_components(config_path)
    rows = store.list_records(limit=limit, min_confidence=min_confidence)
    if not rows:
        console.print("No memories found.")
        return
    table = Table(title="Memories")
    table.add_column("ID")
    table.add_column("Confidence")
    table.add_column("Source")
    table.add_column("Created")
    table.add_column("Statement")
    for row in rows:
        table.add_row(
            str(row.id),
            f"{row.confidence:.2f}",
            f"{row.source_type}:{row.source_ref}",
            row.created_at,
            row.statement,
        )
    console.print(table)


@memory.command(name="search")
@click.argument("query")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--min-confidence", default=0.0, type=float, show_default=True)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def memory_search(query: str, limit: int, min_confidence: float, config_path: str) -> None:
    """Search memories by keyword."""
    store, _ = _load_memory_components(config_path)
    rows = store.search(query, limit=limit, min_confidence=min_confidence)
    if not rows:
        console.print("No matching memories.")
        return
    for row in rows:
        console.print(
            f"- id={row.id} confidence={row.confidence:.2f} source={row.source_type}:{row.source_ref} statement={row.statement}"
        )


@memory.command(name="delete")
@click.argument("record_id", type=int)
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def memory_delete(record_id: int, config_path: str) -> None:
    """Delete a memory by ID."""
    store, _ = _load_memory_components(config_path)
    deleted = store.delete(record_id)
    if not deleted:
        raise click.ClickException(f"Memory id={record_id} not found")
    console.print(f"Deleted memory id={record_id}")


@memory.command(name="clear")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def memory_clear(yes: bool, config_path: str) -> None:
    """Clear all stored memories."""
    if not yes and not click.confirm("Delete all memories?", default=False):
        console.print("Cancelled.")
        return
    store, _ = _load_memory_components(config_path)
    deleted = store.clear()
    console.print(f"Cleared memories: {deleted}")


@main.group()
def secret() -> None:
    """Secret/keyring commands."""


@main.group()
def delegate() -> None:
    """Delegation gateway commands."""


def _delegate_default_socket() -> str:
    state_root = Path(os.getenv("MMO_STATE_DIR", "~/.mmo")).expanduser()
    return str(state_root / "delegate" / "run" / "delegate.sock")


async def _delegate_rpc_call(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise click.ClickException("No response from delegation daemon")
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            raise click.ClickException("Invalid daemon response")
        if not bool(resp.get("ok", False)):
            raise click.ClickException(str(resp.get("error", "delegation daemon error")))
        return resp
    finally:
        writer.close()
        await writer.wait_closed()


async def _delegate_rpc_follow_stream(socket_path: str, payload: dict[str, Any]) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                break
            resp = json.loads(line.decode("utf-8"))
            if not isinstance(resp, dict):
                continue
            if not bool(resp.get("ok", False)):
                raise click.ClickException(str(resp.get("error", "delegation daemon stream error")))
            if resp.get("done") is True:
                console.print(f"run complete: status={resp.get('status')}")
                break
            event = resp.get("event", {})
            if not isinstance(event, dict):
                continue
            console.print(
                f"{str(event.get('timestamp', ''))[:19]} "
                f"[{event.get('event_type', 'event')}] {event.get('message', '')}"
            )
    finally:
        writer.close()
        await writer.wait_closed()


@delegate.command("submit")
@click.argument("objective")
@click.option("--repo", "repo_root", default=".", show_default=True, help="Git repository root")
@click.option("--file", "files", multiple=True, help="Allowed file path (repeatable)")
@click.option("--check", "checks", multiple=True, help="Validation command run in worktree (repeatable)")
@click.option("--risk", type=click.Choice(["low", "medium", "high"]), default="low", show_default=True)
@click.option("--budget", "budget_usd", default=0.25, type=float, show_default=True)
@click.option("--max-minutes", default=10, type=int, show_default=True)
@click.option("--return-format", default="patch", show_default=True)
@click.option("--network/--no-network", "network_enabled", default=False, show_default=True)
@click.option(
    "--executor",
    "executor_cmd",
    default=None,
    help="Optional executor command template. Supports {workspace} and {objective}.",
)
@click.option("--async-run/--sync-run", "async_run", default=False, show_default=True, help="Run job in background")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_submit(
    objective: str,
    repo_root: str,
    files: tuple[str, ...],
    checks: tuple[str, ...],
    risk: str,
    budget_usd: float,
    max_minutes: int,
    return_format: str,
    network_enabled: bool,
    executor_cmd: str | None,
    async_run: bool,
    socket_path: str | None,
) -> None:
    """Submit a delegation job and generate patch-first artifacts."""
    from orchestrator.delegation.gateway import DelegationGateway, DelegationJobSpec

    spec = DelegationJobSpec(
        objective=objective,
        repo_root=repo_root,
        files=[item for item in files if item.strip()],
        checks=[item for item in checks if item.strip()],
        risk=risk,
        budget_usd=budget_usd,
        max_minutes=max_minutes,
        return_format=return_format,
        no_network=not network_enabled,
        executor_cmd=executor_cmd,
    )
    if socket_path:
        req = {"op": "submit", "spec": asdict(spec), "async_run": bool(async_run)}
        resp = asyncio.run(_delegate_rpc_call(socket_path, req))
        record = resp.get("data", {})
        if not isinstance(record, dict):
            raise click.ClickException("Invalid daemon submit response")
    else:
        gateway = DelegationGateway()
        try:
            record = gateway.submit_async(spec) if async_run else gateway.submit(spec)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    console.print(f"job_id={record['job_id']}")
    console.print(f"status={record['status']}")
    console.print(f"artifacts={record['artifacts_dir']}")
    if record.get("error"):
        console.print(f"[red]error[/red]={record['error']}")


@delegate.command("health")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_health(socket_path: str | None) -> None:
    """Check delegation daemon readiness."""
    sock = socket_path or _delegate_default_socket()
    try:
        resp = asyncio.run(_delegate_rpc_call(sock, {"op": "health"}))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    data = resp.get("data", {})
    if not isinstance(data, dict):
        raise click.ClickException("Invalid daemon health response")
    console.print(f"status={data.get('status', 'unknown')}")
    console.print(f"socket={data.get('socket', sock)}")


@delegate.command("list")
@click.option("--limit", default=20, type=int, show_default=True)
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_list(limit: int, socket_path: str | None) -> None:
    """List delegation jobs."""
    from orchestrator.delegation.gateway import DelegationGateway

    if socket_path:
        resp = asyncio.run(_delegate_rpc_call(socket_path, {"op": "list", "limit": max(1, limit)}))
        rows = resp.get("data", [])
    else:
        gateway = DelegationGateway()
        rows = gateway.list_jobs(limit=max(1, limit))
    if not rows:
        console.print("No delegation jobs found.")
        return
    table = Table(title="Delegation Jobs")
    table.add_column("Job ID")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Objective")
    for row in rows:
        spec = row.get("spec", {}) if isinstance(row, dict) else {}
        table.add_row(
            str(row.get("job_id", "")),
            str(row.get("status", "")),
            str(row.get("created_at", ""))[:19],
            str(spec.get("objective", ""))[:64],
        )
    console.print(table)


@delegate.command("show")
@click.argument("job_id")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_show(job_id: str, socket_path: str | None) -> None:
    """Show raw job metadata."""
    from orchestrator.delegation.gateway import DelegationGateway

    if socket_path:
        resp = asyncio.run(_delegate_rpc_call(socket_path, {"op": "show", "job_id": job_id}))
        job = resp.get("data", {})
    else:
        gateway = DelegationGateway()
        try:
            job = gateway.get_job(job_id)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
    console.print_json(data=job)


@delegate.command("fetch")
@click.argument("job_id")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_fetch(job_id: str, socket_path: str | None) -> None:
    """Show artifact bundle location for a job."""
    from orchestrator.delegation.gateway import DelegationGateway

    if socket_path:
        resp = asyncio.run(_delegate_rpc_call(socket_path, {"op": "fetch", "job_id": job_id}))
        data = resp.get("data", {})
        if not isinstance(data, dict):
            raise click.ClickException("Invalid daemon fetch response")
        console.print(str(data.get("artifacts_dir", "")))
        for name in list(data.get("files", [])):
            console.print(f"- {name}")
    else:
        gateway = DelegationGateway()
        try:
            artifacts_dir = gateway.artifacts_path(job_id)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        if not artifacts_dir.exists():
            raise click.ClickException(f"Artifacts not found: {artifacts_dir}")
        console.print(str(artifacts_dir))
        for path in sorted(artifacts_dir.iterdir()):
            console.print(f"- {path.name}")


@delegate.command("apply")
@click.argument("job_id")
@click.option("--check-only", is_flag=True, default=False, help="Validate patch apply without writing")
@click.option("--to-branch", default=None, help="Optional branch to checkout before apply")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
def delegate_apply(job_id: str, check_only: bool, to_branch: str | None, socket_path: str | None) -> None:
    """Apply a delegation patch artifact into the target repository."""
    from orchestrator.delegation.gateway import DelegationGateway

    if socket_path:
        resp = asyncio.run(
            _delegate_rpc_call(
                socket_path,
                {"op": "apply", "job_id": job_id, "check_only": bool(check_only), "to_branch": to_branch},
            )
        )
        result = resp.get("data", {})
    else:
        gateway = DelegationGateway()
        try:
            result = gateway.apply_patch(job_id, check_only=check_only, to_branch=to_branch)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
    if not bool(result.get("ok")):
        raise click.ClickException(str(result.get("stderr") or result.get("stdout") or "patch apply failed"))
    if result.get("reason"):
        console.print(str(result["reason"]))
        return
    console.print("Patch check passed." if check_only else "Patch applied.")
    if result.get("stdout"):
        console.print(str(result["stdout"]))


@delegate.command("follow")
@click.argument("job_id")
@click.option("--socket", "socket_path", default=None, help="Use unix-socket delegation daemon")
@click.option("--poll", "poll_s", default=0.5, show_default=True, type=float)
def delegate_follow(job_id: str, socket_path: str | None, poll_s: float) -> None:
    """Stream delegation job events until completion."""
    from orchestrator.delegation.gateway import DelegationGateway

    if socket_path:
        asyncio.run(_delegate_rpc_follow_stream(socket_path, {"op": "follow", "job_id": job_id, "poll_s": poll_s}))
        return

    gateway = DelegationGateway()
    offset = 0
    terminal_seen = False
    while True:
        events, offset = gateway.read_events(job_id, offset=offset)
        for event in events:
            console.print(f"{str(event.get('timestamp', ''))[:19]} [{event.get('event_type', '')}] {event.get('message', '')}")
        record = gateway.get_job(job_id)
        status = str(record.get("status", ""))
        if status in {"completed", "failed"}:
            if terminal_seen:
                console.print(f"run complete: status={status}")
                return
            terminal_seen = True
        time.sleep(max(0.1, poll_s))


@delegate.command("daemon")
@click.option("--socket", "socket_path", default=None, help="Unix socket path")
def delegate_daemon(socket_path: str | None) -> None:
    """Run unix-socket delegation daemon."""
    from orchestrator.delegation.daemon import DelegationBrokerDaemon
    from orchestrator.delegation.gateway import DelegationGateway

    sock = socket_path or _delegate_default_socket()
    gateway = DelegationGateway()
    daemon = DelegationBrokerDaemon(gateway=gateway, socket_path=Path(sock))
    console.print(f"Delegation daemon listening on: {sock}")
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        console.print("Delegation daemon stopped.")


@secret.command(name="set")
@click.argument("name")
@click.argument("value")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def secret_set(name: str, value: str, config_path: str) -> None:
    """Store a secret in OS keyring (or local keyring fallback)."""
    from orchestrator.config import load_config
    from orchestrator.security.keyring import OSKeyringProvider

    _ = load_config(config_path)
    provider = OSKeyringProvider()
    try:
        import keyring  # type: ignore

        keyring.set_password(provider.service_name, name, value)
    except Exception:
        path = provider.fallback_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {}
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        payload[f"secret:{name}"] = value
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"Stored secret handle: secret://keyring/{name}#value")


@secret.command(name="list")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def secret_list(config_path: str) -> None:
    """List stored secret names (not values)."""
    from orchestrator.config import load_config
    from orchestrator.security.keyring import OSKeyringProvider

    _ = load_config(config_path)
    provider = OSKeyringProvider()
    names: list[str] = []
    try:
        import keyring  # type: ignore
        from keyring.errors import PasswordDeleteError  # type: ignore

        # keyring backends generally do not support list APIs; this is best-effort.
        _ = PasswordDeleteError  # keep import for coverage fallback
    except Exception:
        pass
    if provider.fallback_path.exists():
        payload = json.loads(provider.fallback_path.read_text(encoding="utf-8"))
        for key in payload.keys():
            if key.startswith("secret:"):
                names.append(key.split("secret:", 1)[1])
    if not names:
        console.print("No stored secret names available from current backend.")
        return
    for name in sorted(names):
        console.print(f"- {name}")


@main.group()
def discord() -> None:
    """Discord integration commands."""


@discord.command("start")
@click.option("--config", "config_path", default="config/config.example.yaml", show_default=True)
def discord_start(config_path: str) -> None:
    """Start Discord bot integration (daemon-backed)."""
    try:
        from integrations.discord_bot import run_discord_bot
    except ModuleNotFoundError as exc:
        raise click.ClickException("Discord integration module unavailable.") from exc

    try:
        run_discord_bot(config_path)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
