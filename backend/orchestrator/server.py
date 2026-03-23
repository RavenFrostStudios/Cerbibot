from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.requests import Request
from orchestrator.config import DataProtectionConfig
from orchestrator.observability.audit import AuditLogger
from orchestrator.security.encryption import build_envelope_cipher
from orchestrator.session import SessionManager
from orchestrator.security.admin_auth import admin_password_status, set_admin_password, verify_admin_password
from orchestrator.security.keyring import delete_secret, get_secret, has_secret, set_secret


class ToolApprovalStore:
    def __init__(self):
        self._records: dict[str, dict[str, Any]] = {}

    def create(
        self,
        *,
        tool_name: str,
        args: dict[str, str],
        reason: str,
        provider: str,
        model: str,
        query: str,
        risk_level: str = "high",
    ) -> dict[str, Any]:
        approval_id = f"approval-{uuid4()}"
        record = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "arguments": dict(args),
            "reason": reason,
            "provider": provider,
            "model": model,
            "query": query,
            "risk_level": risk_level if risk_level in {"low", "medium", "high"} else "high",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._records[approval_id] = record
        return dict(record)

    def get(self, approval_id: str) -> dict[str, Any] | None:
        record = self._records.get(approval_id)
        return dict(record) if record is not None else None

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(self._records.values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
        return [dict(row) for row in rows[: max(1, min(limit, 500))]]

    def approve(self, approval_id: str) -> dict[str, Any] | None:
        record = self._records.get(approval_id)
        if record is None:
            return None
        record["status"] = "approved"
        record["approved_at"] = datetime.now(timezone.utc).isoformat()
        return dict(record)

    def deny(self, approval_id: str) -> dict[str, Any] | None:
        record = self._records.get(approval_id)
        if record is None:
            return None
        record["status"] = "denied"
        record["denied_at"] = datetime.now(timezone.utc).isoformat()
        return dict(record)

    def consume(self, *, tool_approval_id: str, tool_name: str, args: dict[str, str]) -> bool:
        record = self._records.get(tool_approval_id)
        if record is None or record.get("status") != "approved":
            return False
        if record.get("tool_name") != tool_name:
            return False
        if dict(record.get("arguments", {})) != dict(args):
            return False
        record["status"] = "consumed"
        record["consumed_at"] = datetime.now(timezone.utc).isoformat()
        return True


_FAILED_TOOL_STATUSES = {"failed", "error", "denied", "rejected", "timed_out", "timeout"}
_PENDING_TOOL_STATUSES = {"pending", "queued", "waiting"}
_RUNNING_TOOL_STATUSES = {"running"}


def _tool_output_status(item: dict[str, Any]) -> str:
    explicit = str(item.get("status", "")).strip().lower()
    if bool(item.get("timed_out", False)):
        return "failed"
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if explicit in _FAILED_TOOL_STATUSES:
        return "failed"
    if explicit in _PENDING_TOOL_STATUSES:
        return "pending"
    if explicit in _RUNNING_TOOL_STATUSES:
        return "running"
    return "ok"


def _result_status(result: Any) -> str:
    progress_status: str | None = None
    for item in getattr(result, "tool_outputs", None) or []:
        if not isinstance(item, dict):
            continue
        normalized = _tool_output_status(item)
        if normalized == "failed":
            return "failed"
        if normalized in {"running", "pending"}:
            progress_status = normalized
    pending_tool = getattr(result, "pending_tool", None)
    if isinstance(pending_tool, dict) and pending_tool:
        return "pending"
    if progress_status:
        return progress_status
    if not str(getattr(result, "answer", "") or "").strip():
        return "failed"
    if str(getattr(result, "provider", "") or "").strip().lower() == "none":
        return "failed"
    return "ok"


def _result_payload(result: Any) -> dict[str, Any]:
    payload = asdict(result)
    payload["status"] = _result_status(result)
    return payload


def create_app(orchestrator):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse, StreamingResponse
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is required for daemon mode (install optional server dependencies).") from exc

    app = FastAPI(title="Multi-Mind Orchestrator API", version="0.1.0")
    server_cfg = orchestrator.config.server
    token_file = _server_token_file()
    api_key = _load_server_api_key(server_cfg.api_key_env, token_file)
    token_state: dict[str, str] = {"value": api_key}
    sessions_file = _sessions_store_file()
    sessions_cipher = _sessions_cipher()
    ui_settings_file = _ui_settings_store_file()
    ui_settings: dict[str, Any] = _load_ui_settings_from_disk(ui_settings_file, sessions_cipher)
    debug_retrieval_value = ui_settings.get("debugRetrievalWarnings")
    if isinstance(debug_retrieval_value, bool):
        os.environ["MMO_DEBUG_RETRIEVAL_WARNINGS"] = "1" if debug_retrieval_value else "0"
    web_max_sources_value = ui_settings.get("webMaxSources")
    if isinstance(web_max_sources_value, int):
        os.environ["MMO_WEB_MAX_SOURCES"] = str(max(1, min(10, web_max_sources_value)))
    web_assist_mode_value = ui_settings.get("webAssistMode")
    if isinstance(web_assist_mode_value, str) and web_assist_mode_value in {"off", "auto", "confirm"}:
        os.environ["MMO_WEB_ASSIST_MODE"] = web_assist_mode_value
    retrieval_answer_style_value = ui_settings.get("retrievalAnswerStyle")
    if isinstance(retrieval_answer_style_value, str) and retrieval_answer_style_value in {
        "concise_ranked",
        "full_details",
        "source_first",
    }:
        os.environ["MMO_RETRIEVAL_ANSWER_STYLE"] = retrieval_answer_style_value
    sessions, session_projects = _load_sessions_state_from_disk(sessions_file, sessions_cipher)
    runs_file = _runs_store_file()
    runs: dict[str, dict[str, Any]] = _load_runs_from_disk(runs_file, sessions_cipher)
    run_event_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
    approvals = ToolApprovalStore()
    setattr(orchestrator, "tool_approval_store", approvals)
    audit_logger = AuditLogger(str(_audit_store_file(orchestrator)), cipher=_sessions_cipher())
    admin_auth_state: dict[str, int | float] = {"failed_attempts": 0, "locked_until_epoch": 0.0}
    max_failed_attempts = max(1, int(os.getenv("MMO_ADMIN_VERIFY_MAX_FAILED", "5")))
    lockout_seconds = max(1, int(os.getenv("MMO_ADMIN_VERIFY_LOCKOUT_SECONDS", "300")))

    if server_cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=server_cfg.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    scheduler_task: asyncio.Task[None] | None = None

    async def require_auth(authorization: str | None = Header(default=None)) -> None:
        current_api_key = token_state["value"]
        if not current_api_key:
            return
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        supplied = authorization.split(" ", 1)[1].strip()
        if supplied != current_api_key:
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    @app.on_event("startup")
    async def _startup_scheduler() -> None:
        nonlocal scheduler_task

        async def _scheduler_loop() -> None:
            while True:
                try:
                    await _sweep_due_run_triggers(source="interval_scheduler")
                except Exception:
                    pass
                await asyncio.sleep(30)

        scheduler_task = asyncio.create_task(_scheduler_loop())

    @app.on_event("shutdown")
    async def _shutdown_scheduler() -> None:
        nonlocal scheduler_task
        if scheduler_task is None:
            return
        scheduler_task.cancel()
        try:
            await scheduler_task
        except Exception:
            pass
        scheduler_task = None

    def _normalize_project_id(value: Any) -> str:
        text = str(value or "").strip()
        return text or "default"

    def _session_project(session_id: str) -> str:
        return _normalize_project_id(session_projects.get(session_id))

    def _ensure_session_project(session_id: str, project_id: str) -> None:
        existing = _session_project(session_id)
        if session_id in session_projects and existing != project_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"session '{session_id}' belongs to project '{existing}', "
                    f"not '{project_id}'"
                ),
            )
        session_projects[session_id] = project_id

    def _memory_list_records(*, project_id: str, limit: int = 100):
        try:
            return orchestrator.memory_store.list_records(limit=limit, project_id=project_id)
        except TypeError:
            return orchestrator.memory_store.list_records(limit=limit)

    def _memory_find_duplicate(statement: str, *, project_id: str):
        try:
            return orchestrator.memory_store.find_duplicate_statement(statement, project_id=project_id)
        except TypeError:
            return orchestrator.memory_store.find_duplicate_statement(statement)

    def _memory_add(*, project_id: str, **kwargs):
        try:
            return orchestrator.memory_store.add(project_id=project_id, **kwargs)
        except TypeError:
            return orchestrator.memory_store.add(**kwargs)

    def _memory_delete(record_id: int, *, project_id: str):
        try:
            return orchestrator.memory_store.delete(record_id, project_id=project_id)
        except TypeError:
            return orchestrator.memory_store.delete(record_id)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _parse_iso_epoch(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    async def _execute_run_trigger(trigger: dict[str, Any], *, payload_data: dict[str, Any], source: str) -> dict[str, Any]:
        base_message = str(trigger.get("message", "")).strip()
        if not base_message:
            raise HTTPException(status_code=400, detail="trigger message is required")
        webhook_context = ""
        if payload_data:
            webhook_context = "\n\nWebhook payload:\n" + json.dumps(payload_data, sort_keys=True)
        replay = {
            "session_id": str(trigger.get("session_id", "")).strip() or f"trigger-session-{uuid4()}",
            "project_id": _normalize_project_id(trigger.get("project_id")),
            "run_id": f"run-trigger-{uuid4()}",
            "message": f"{base_message}{webhook_context}",
            "mode": str(trigger.get("mode", "single")),
            "provider": str(trigger.get("provider", "")).strip() or None,
            "tools": bool(trigger.get("tools", False)),
            "fact_check": bool(trigger.get("fact_check", False)),
            "assistant_name": str(trigger.get("assistant_name", "")).strip(),
            "assistant_instructions": str(trigger.get("assistant_instructions", "")).strip(),
            "strict_profile": bool(trigger.get("strict_profile", False)),
            "web_assist_mode": str(trigger.get("web_assist_mode", "off")),
            "verbose": True,
        }
        response = await chat(replay, None)
        if isinstance(response, dict):
            result_payload = response
        else:
            try:
                result_payload = json.loads(response.body.decode("utf-8"))
            except Exception:
                result_payload = {}
        trigger["last_triggered_at"] = _now_iso()
        trigger["last_run_id"] = str(result_payload.get("run_id", ""))
        interval_minutes = int(trigger.get("interval_minutes", 0) or 0)
        trigger["next_run_at"] = _schedule_next_run_at(interval_minutes) if interval_minutes > 0 else ""
        trigger["updated_at"] = _now_iso()
        audit_logger.write(
            "run_trigger_fire",
            {
                "trigger_id": str(trigger.get("trigger_id", "")),
                "run_id": trigger["last_run_id"],
                "source": source,
            },
        )
        return {
            "ok": True,
            "trigger_id": str(trigger.get("trigger_id", "")),
            "run_id": trigger["last_run_id"],
            "session_id": result_payload.get("session_id"),
            "result": result_payload.get("result", {}),
        }

    async def _sweep_due_run_triggers(*, source: str) -> dict[str, Any]:
        rows = _normalize_run_triggers(ui_settings.get("runTriggers"))
        now = datetime.now(timezone.utc)
        due = [row for row in rows if _trigger_due(row, now=now)]
        fired: list[dict[str, Any]] = []
        for row in due:
            try:
                fired.append(await _execute_run_trigger(row, payload_data={}, source=source))
            except Exception as exc:
                audit_logger.write(
                    "run_trigger_fire_failed",
                    {"trigger_id": str(row.get("trigger_id", "")), "reason": str(exc), "source": source},
                )
        if due:
            ui_settings["runTriggers"] = rows
            _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        return {"checked": len(rows), "due": len(due), "fired": len(fired), "runs": fired}

    def _run_stale_threshold_seconds() -> int:
        raw = os.getenv("MMO_RUN_STALE_SECONDS", "120")
        try:
            value = int(raw)
        except Exception:
            value = 120
        return max(1, value)

    def _run_stale_info(row: dict[str, Any], *, now_epoch: float | None = None) -> tuple[bool, int]:
        status = str(row.get("status", "")).strip().lower()
        if status not in {"running", "resuming", "waiting", "paused"}:
            return False, 0
        last = _parse_iso_epoch(row.get("last_heartbeat_at"))
        if last is None:
            last = _parse_iso_epoch(row.get("updated_at"))
        if last is None:
            return False, 0
        now = now_epoch if now_epoch is not None else time.time()
        age_seconds = max(0, int(now - last))
        return age_seconds >= _run_stale_threshold_seconds(), age_seconds

    def _decorate_run(row: dict[str, Any], *, now_epoch: float | None = None) -> dict[str, Any]:
        out = dict(row)
        stale, age_seconds = _run_stale_info(out, now_epoch=now_epoch)
        out["stalled"] = stale
        out["stalled_seconds"] = age_seconds
        return out

    def _sanitize_checkpoint_request(raw: dict[str, Any]) -> dict[str, Any]:
        redacted_tokens = {"password", "api_key", "token", "secret"}
        out: dict[str, Any] = {}
        for key, value in raw.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if any(token in key_lower for token in redacted_tokens):
                out[key_text] = "[redacted]"
                continue
            if isinstance(value, dict):
                out[key_text] = _sanitize_checkpoint_request(value)
            elif isinstance(value, list):
                items: list[Any] = []
                for item in value:
                    if isinstance(item, dict):
                        items.append(_sanitize_checkpoint_request(item))
                    else:
                        items.append(item)
                out[key_text] = items
            else:
                out[key_text] = value
        return out

    def _normalize_run_dependencies(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            value = str(item).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
            if len(out) >= 100:
                break
        return out

    def _normalize_run_blockers(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            blocker_id = str(item.get("blocker_id", f"blocker-{idx + 1}")).strip() or f"blocker-{idx + 1}"
            code = str(item.get("code", "")).strip()[:80]
            message = str(item.get("message", "")).strip()[:240]
            severity_raw = str(item.get("severity", "medium")).strip().lower()
            severity = severity_raw if severity_raw in {"low", "medium", "high"} else "medium"
            status_raw = str(item.get("status", "open")).strip().lower()
            status = status_raw if status_raw in {"open", "resolved"} else "open"
            out.append(
                {
                    "blocker_id": blocker_id,
                    "code": code,
                    "message": message,
                    "severity": severity,
                    "status": status,
                }
            )
            if len(out) >= 100:
                break
        return out

    def _run_is_blocked(row: dict[str, Any]) -> bool:
        return len(_collect_open_blockers(row)) > 0

    def _dependency_blockers(row: dict[str, Any]) -> list[dict[str, Any]]:
        dependencies = _normalize_run_dependencies(row.get("dependencies", []))
        out: list[dict[str, Any]] = []
        for dep_id in dependencies:
            dep = runs.get(dep_id)
            if dep is None:
                out.append(
                    {
                        "blocker_id": f"dependency:{dep_id}",
                        "code": "dependency_missing",
                        "message": f"Dependency run '{dep_id}' does not exist.",
                        "severity": "high",
                        "status": "open",
                    }
                )
                continue
            dep_status = str(dep.get("status", "")).strip().lower()
            if dep_status == "completed":
                continue
            code = "dependency_pending"
            severity = "medium"
            if dep_status == "failed":
                code = "dependency_failed"
                severity = "high"
            elif dep_status == "blocked":
                code = "dependency_blocked"
            elif dep_status in {"resuming", "running"}:
                code = "dependency_running"
            out.append(
                {
                    "blocker_id": f"dependency:{dep_id}",
                    "code": code,
                    "message": f"Dependency run '{dep_id}' is not completed (status={dep_status or 'unknown'}).",
                    "severity": severity,
                    "status": "open",
                }
            )
        return out

    def _collect_open_blockers(row: dict[str, Any]) -> list[dict[str, Any]]:
        explicit = [
            blocker
            for blocker in _normalize_run_blockers(row.get("blockers", []))
            if str(blocker.get("status", "open")).lower() == "open"
        ]
        merged: dict[str, dict[str, Any]] = {
            str(blocker.get("blocker_id", f"blocker-{idx + 1}")): blocker
            for idx, blocker in enumerate(explicit)
        }
        for blocker in _dependency_blockers(row):
            blocker_id = str(blocker.get("blocker_id", ""))
            if blocker_id in merged:
                continue
            merged[blocker_id] = blocker
        return list(merged.values())

    def _require_run_ready(run_id: str) -> None:
        row = runs.get(run_id) or {}
        blockers = _collect_open_blockers(row)
        if not blockers:
            return
        _update_run(
            run_id,
            status="blocked",
            checkpoint={"stage": "blocked_on_dependency", "blockers": blockers},
        )
        details = "; ".join(str(item.get("message", "")).strip() for item in blockers[:3] if item.get("message"))
        detail = details or "Run is blocked by unresolved dependencies."
        raise HTTPException(status_code=409, detail=detail)

    def _start_run(
        *,
        run_id: str,
        endpoint: str,
        request: dict[str, Any],
        session_id: str | None = None,
        status: str = "running",
    ) -> dict[str, Any]:
        now = _now_iso()
        record = runs.get(run_id, {})
        if not record:
            record["run_id"] = run_id
            record["created_at"] = now
        record["endpoint"] = endpoint
        record["status"] = status
        record["updated_at"] = now
        record["request"] = _sanitize_checkpoint_request(request)
        record["session_id"] = session_id
        deps_raw = request.get("depends_on", request.get("dependencies")) if isinstance(request, dict) else []
        record["dependencies"] = _normalize_run_dependencies(deps_raw)
        blockers_raw = request.get("blockers", []) if isinstance(request, dict) else []
        record["blockers"] = _normalize_run_blockers(blockers_raw)
        record["heartbeat_count"] = int(record.get("heartbeat_count", 0))
        record["resume_count"] = int(record.get("resume_count", 0))
        open_blockers = _collect_open_blockers(record)
        if open_blockers:
            record["status"] = "blocked"
            record["checkpoint"] = {"stage": "blocked_on_dependency", "blockers": open_blockers}
        else:
            record["checkpoint"] = {"stage": "started"}
        runs[run_id] = record
        _save_runs_to_disk(runs, runs_file, sessions_cipher)
        _emit_run_event("upsert", run_id, record)
        return dict(record)

    def _update_run(run_id: str, *, status: str | None = None, **fields: Any) -> dict[str, Any]:
        now = _now_iso()
        record = runs.get(run_id, {})
        if not record:
            record = {"run_id": run_id, "created_at": now}
        if status is not None:
            record["status"] = status
        for key, value in fields.items():
            record[key] = value
        record["updated_at"] = now
        runs[run_id] = record
        _save_runs_to_disk(runs, runs_file, sessions_cipher)
        _emit_run_event("upsert", run_id, record)
        return dict(record)

    def _emit_run_event(event_type: str, run_id: str, row: dict[str, Any] | None = None) -> None:
        if event_type not in {"upsert", "delete"}:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "run_id": run_id,
            "sent_at": _now_iso(),
        }
        if event_type == "upsert":
            run_row = row if isinstance(row, dict) else runs.get(run_id)
            if isinstance(run_row, dict):
                payload["run"] = _decorate_run(run_row)
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in list(run_event_subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    stale.append(queue)
            except Exception:
                stale.append(queue)
        for queue in stale:
            run_event_subscribers.discard(queue)

    def _map_orchestrator_error(exc: Exception) -> HTTPException:
        text = str(exc).strip()
        lower = text.lower()
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=text or "Invalid request")
        if any(token in lower for token in ("resource_exhausted", "quota", "429", "too many requests", "rate limit", "rate-limit")):
            return HTTPException(status_code=429, detail=f"Provider quota/rate-limit error: {text}")
        if any(token in lower for token in ("timed out", "timeout")):
            return HTTPException(status_code=504, detail=f"Provider timeout error: {text}")
        if any(
            token in lower
            for token in (
                "connection failed",
                "service unavailable",
                "temporarily unavailable",
                "no provider available after rate-limit fallback",
            )
        ):
            return HTTPException(status_code=503, detail=f"Provider unavailable: {text}")
        return HTTPException(status_code=502, detail=f"Upstream provider error: {type(exc).__name__}: {text}")

    async def _run_orchestrator_ask(**kwargs):
        try:
            return await orchestrator.ask(**kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            raise _map_orchestrator_error(exc)

    def _check_admin_lockout() -> None:
        locked_until = float(admin_auth_state["locked_until_epoch"])
        now = time.time()
        if now < locked_until:
            remaining = int(max(1, locked_until - now))
            audit_logger.write("admin_password_locked", {"retry_after_seconds": remaining})
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed admin password attempts. Try again in {remaining}s.",
            )

    def _normalize_web_assist_mode(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if normalized in {"off", "auto", "confirm"}:
            return normalized
        return None

    def _record_admin_verify_result(ok: bool) -> None:
        if ok:
            admin_auth_state["failed_attempts"] = 0
            admin_auth_state["locked_until_epoch"] = 0.0
            return
        attempts = int(admin_auth_state["failed_attempts"]) + 1
        admin_auth_state["failed_attempts"] = attempts
        if attempts >= max_failed_attempts:
            admin_auth_state["failed_attempts"] = 0
            admin_auth_state["locked_until_epoch"] = time.time() + lockout_seconds

    def _require_admin_password_if_configured(payload: dict[str, Any], *, audit_reason: str) -> None:
        status = admin_password_status()
        if not status.get("configured"):
            return
        _check_admin_lockout()
        password = str(payload.get("admin_password", ""))
        if not verify_admin_password(password):
            _record_admin_verify_result(False)
            audit_logger.write("admin_password_verify_failed", {"reason": audit_reason})
            raise HTTPException(status_code=401, detail="Invalid admin password")
        _record_admin_verify_result(True)

    @app.get("/v1/health")
    async def health(_: None = Depends(require_auth)):
        remaining = orchestrator.budgets.remaining()
        return {
            "status": "ok",
            "providers": sorted(orchestrator.providers.keys()),
            "budget_remaining": remaining,
        }

    @app.get("/v1/server/delegate/health")
    async def delegate_health(_: None = Depends(require_auth)):
        socket_path = _delegate_socket_path()
        payload = await _probe_delegate_socket(socket_path)
        return payload

    @app.get("/v1/server/delegate/jobs")
    async def delegate_jobs_list(limit: int = 50, _: None = Depends(require_auth)):
        from orchestrator.delegation.gateway import DelegationGateway

        gateway = DelegationGateway()
        rows = gateway.list_jobs(limit=max(1, min(int(limit), 500)))
        return {"jobs": rows}

    @app.post("/v1/server/delegate/jobs/{job_id}/delete")
    async def delegate_jobs_delete_one(job_id: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.delegation.gateway import DelegationGateway

        audit_logger.write("delegate_job_delete_attempt", {"scope": "single", "job_id": str(job_id)})
        _require_admin_password_if_configured(payload, audit_reason="delegate_job_delete_invalid_password")
        allow_running = bool(payload.get("allow_running", False))
        gateway = DelegationGateway()
        try:
            deleted = gateway.delete_job(str(job_id), allow_running=allow_running)
        except ValueError as exc:
            audit_logger.write("delegate_job_delete_failed", {"scope": "single", "job_id": str(job_id), "error": str(exc)})
            raise HTTPException(status_code=400, detail=str(exc))
        if not deleted:
            audit_logger.write("delegate_job_delete_failed", {"scope": "single", "job_id": str(job_id), "error": "job not found"})
            raise HTTPException(status_code=404, detail="job not found")
        audit_logger.write("delegate_job_delete_success", {"scope": "single", "job_id": str(job_id), "count": 1})
        return {"deleted": 1, "job_ids": [str(job_id)]}

    @app.post("/v1/server/delegate/jobs/delete-all")
    async def delegate_jobs_delete_all(payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.delegation.gateway import DelegationGateway

        audit_logger.write("delegate_job_delete_attempt", {"scope": "all"})
        _require_admin_password_if_configured(payload, audit_reason="delegate_job_delete_invalid_password")
        older_than_raw = payload.get("older_than_days")
        older_than_days: int | None
        if older_than_raw in (None, "", 0, "0"):
            older_than_days = None
        else:
            try:
                older_than_days = max(1, int(older_than_raw))
            except Exception:
                raise HTTPException(status_code=400, detail="older_than_days must be an integer > 0")
        limit_raw = payload.get("limit", 1000)
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 1000
        limit = max(1, min(limit, 5000))
        allow_running = bool(payload.get("allow_running", False))
        gateway = DelegationGateway()
        result = gateway.delete_jobs(older_than_days=older_than_days, limit=limit, allow_running=allow_running)
        deleted_ids = [str(item) for item in list(result.get("deleted", []))]
        skipped = result.get("skipped", [])
        if isinstance(skipped, list) and skipped:
            audit_logger.write("delegate_job_delete_failed", {"scope": "all", "count": len(skipped), "skipped": skipped[:20]})
        audit_logger.write(
            "delegate_job_delete_success",
            {"scope": "all", "count": len(deleted_ids), "older_than_days": older_than_days},
        )
        return {
            "deleted": len(deleted_ids),
            "job_ids": deleted_ids,
            "older_than_days": older_than_days,
            "skipped": skipped if isinstance(skipped, list) else [],
        }

    @app.post("/v1/server/token/rotate")
    async def rotate_server_token(_: None = Depends(require_auth)):
        token = secrets.token_urlsafe(32)
        _write_server_api_key(token_file, token)
        os.environ[server_cfg.api_key_env] = token
        token_state["value"] = token
        return {"token": token, "token_file": str(token_file)}

    @app.get("/v1/server/admin-password/status")
    async def get_admin_password_status(_: None = Depends(require_auth)):
        return admin_password_status()

    @app.post("/v1/server/admin-password/set")
    async def post_admin_password_set(payload: dict[str, Any], _: None = Depends(require_auth)):
        password = str(payload.get("password", ""))
        try:
            set_admin_password(password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"configured": True}

    @app.post("/v1/server/token/recover")
    async def recover_server_token(payload: dict[str, Any]):
        _check_admin_lockout()
        password = str(payload.get("admin_password", ""))
        if not verify_admin_password(password):
            _record_admin_verify_result(False)
            audit_logger.write("admin_password_recover_failed", {"reason": "invalid_password"})
            raise HTTPException(status_code=401, detail="Invalid admin password")
        _record_admin_verify_result(True)
        token = secrets.token_urlsafe(32)
        _write_server_api_key(token_file, token)
        os.environ[server_cfg.api_key_env] = token
        token_state["value"] = token
        audit_logger.write("admin_password_recover_success", {})
        return {"token": token, "token_file": str(token_file)}

    @app.post("/v1/server/admin-password/verify")
    async def post_admin_password_verify(payload: dict[str, Any], _: None = Depends(require_auth)):
        _check_admin_lockout()
        password = str(payload.get("password", ""))
        if not verify_admin_password(password):
            _record_admin_verify_result(False)
            audit_logger.write("admin_password_verify_failed", {"reason": "invalid_password"})
            raise HTTPException(status_code=401, detail="Invalid admin password")
        _record_admin_verify_result(True)
        audit_logger.write("admin_password_verify_success", {})
        return {"ok": True}

    @app.post("/v1/server/audit/event")
    async def post_server_audit_event(payload: dict[str, Any], _: None = Depends(require_auth)):
        event_type = str(payload.get("event_type", "")).strip()
        event_payload = payload.get("payload", {})
        if not event_type:
            raise HTTPException(status_code=400, detail="event_type is required")
        if len(event_type) > 80 or not re.fullmatch(r"[a-zA-Z0-9_.:\-/]+", event_type):
            raise HTTPException(status_code=400, detail="invalid event_type")
        if not isinstance(event_payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")
        audit_logger.write(event_type, event_payload)
        return {"ok": True}

    @app.post("/v1/server/audit/security-events")
    async def post_server_security_events(payload: dict[str, Any], _: None = Depends(require_auth)):
        _require_admin_password_if_configured(payload, audit_reason="invalid_password")

        limit_raw = payload.get("limit", 50)
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))
        rows = _read_recent_audit_events(
            _audit_store_file(orchestrator),
            _sessions_cipher(),
            limit=limit,
            event_types={
                "admin_password_locked",
                "admin_password_recover_failed",
                "admin_password_recover_success",
                "admin_password_verify_failed",
                "admin_password_verify_success",
                "ui.export_attempt",
                "ui.export_cancelled",
                "ui.export_auth_ok",
                "ui.export_auth_failed",
                "ui.export_success",
                "ui.export_failed",
                "remote_access_enable",
                "remote_access_rebind",
                "remote_access_revoke",
                "run_trigger_save",
                "run_trigger_rotate_secret",
                "run_trigger_fire",
                "run_trigger_fire_failed",
                "artifact_export_attempt",
                "artifact_export_success",
                "artifact_delete_attempt",
                "artifact_delete_success",
                "delegate_job_delete_attempt",
                "delegate_job_delete_success",
                "delegate_job_delete_failed",
            },
        )
        return {"events": rows}

    @app.get("/v1/server/ui-settings")
    async def get_ui_settings(_: None = Depends(require_auth)):
        return {"settings": dict(ui_settings)}

    @app.put("/v1/server/ui-settings")
    async def put_ui_settings(payload: dict[str, Any], _: None = Depends(require_auth)):
        raw_settings = payload.get("settings", payload)
        if not isinstance(raw_settings, dict):
            raise HTTPException(status_code=400, detail="settings object is required")
        existing_remote_access = ui_settings.get("remoteAccess")
        existing_run_triggers = ui_settings.get("runTriggers")
        normalized = _normalize_ui_settings(raw_settings)
        if "remoteAccess" not in normalized and existing_remote_access is not None:
            normalized["remoteAccess"] = _normalize_remote_access_profile(existing_remote_access)
        if "runTriggers" not in normalized and existing_run_triggers is not None:
            normalized["runTriggers"] = _normalize_run_triggers(existing_run_triggers)
        debug_retrieval_value = normalized.get("debugRetrievalWarnings")
        if isinstance(debug_retrieval_value, bool):
            os.environ["MMO_DEBUG_RETRIEVAL_WARNINGS"] = "1" if debug_retrieval_value else "0"
        web_max_sources_value = normalized.get("webMaxSources")
        if isinstance(web_max_sources_value, int):
            os.environ["MMO_WEB_MAX_SOURCES"] = str(max(1, min(10, web_max_sources_value)))
        web_assist_mode_value = normalized.get("webAssistMode")
        if isinstance(web_assist_mode_value, str) and web_assist_mode_value in {"off", "auto", "confirm"}:
            os.environ["MMO_WEB_ASSIST_MODE"] = web_assist_mode_value
        retrieval_answer_style_value = normalized.get("retrievalAnswerStyle")
        if isinstance(retrieval_answer_style_value, str) and retrieval_answer_style_value in {
            "concise_ranked",
            "full_details",
            "source_first",
        }:
            os.environ["MMO_RETRIEVAL_ANSWER_STYLE"] = retrieval_answer_style_value
        ui_settings.clear()
        ui_settings.update(normalized)
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        return {"settings": dict(ui_settings)}

    @app.get("/v1/server/mcp/servers")
    async def get_mcp_servers(_: None = Depends(require_auth)):
        return {"servers": _normalize_mcp_servers(ui_settings.get("mcpServers", []))}

    @app.put("/v1/server/mcp/servers")
    async def put_mcp_servers(payload: dict[str, Any], _: None = Depends(require_auth)):
        raw_servers = payload.get("servers", payload)
        if not isinstance(raw_servers, list):
            raise HTTPException(status_code=400, detail="servers array is required")
        normalized = _normalize_mcp_servers(raw_servers)
        ui_settings["mcpServers"] = normalized
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        return {"servers": normalized}

    def _resolve_mcp_headers(server: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        explicit = server.get("headers", {})
        if isinstance(explicit, dict):
            for key, value in explicit.items():
                k = str(key).strip()
                v = str(value).strip()
                if k and v:
                    headers[k] = v
        env_refs = server.get("header_env_refs", {})
        if isinstance(env_refs, dict):
            for key, env_name in env_refs.items():
                k = str(key).strip()
                env_key = str(env_name).strip()
                if not k or not env_key:
                    continue
                env_value = os.getenv(env_key, "").strip()
                if env_value:
                    headers[k] = env_value
        return headers

    async def _probe_mcp_tools_http(base_url: str, timeout_seconds: float, request_headers: dict[str, str]) -> list[str]:
        import httpx

        normalized_base = base_url.rstrip("/")
        attempts: list[tuple[str, str, dict[str, Any] | None]] = [
            ("POST", f"{normalized_base}/tools/list", {}),
            ("POST", f"{normalized_base}/v1/tools/list", {}),
            ("GET", f"{normalized_base}/tools", None),
        ]
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            for method, url, body in attempts:
                try:
                    if method == "POST":
                        response = await client.post(url, json=body, headers=request_headers or None)
                    else:
                        response = await client.get(url, headers=request_headers or None)
                except Exception:
                    continue
                if response.status_code >= 400:
                    continue
                try:
                    payload = response.json()
                except Exception:
                    continue
                rows: Any = None
                if isinstance(payload, dict):
                    rows = payload.get("tools", payload.get("items"))
                elif isinstance(payload, list):
                    rows = payload
                if not isinstance(rows, list):
                    continue
                names: list[str] = []
                for item in rows:
                    if isinstance(item, str):
                        name = item.strip()
                    elif isinstance(item, dict):
                        name = str(item.get("name") or item.get("id") or item.get("title") or "").strip()
                    else:
                        name = ""
                    if name and name not in names:
                        names.append(name)
                if names:
                    return names[:64]
        return []

    async def _probe_mcp_server(
        server: dict[str, Any],
        *,
        probe_tools: bool,
    ) -> dict[str, Any]:
        import httpx

        started = time.perf_counter()
        name = str(server.get("name", "")).strip() or "unnamed"
        transport = str(server.get("transport", "stdio")).strip().lower()
        enabled = bool(server.get("enabled", True))
        declared_tools = [str(x).strip() for x in server.get("declared_tools", []) if str(x).strip()]
        request_headers = _resolve_mcp_headers(server)
        status = "FAIL"
        detail = "not checked"
        error_code = "none"
        reachable = False
        tools: list[str] = []
        remediation = ""

        try:
            if not enabled:
                return {
                    "name": name,
                    "transport": transport,
                    "enabled": False,
                    "status": "SKIP",
                    "reachable": False,
                    "latency_ms": 0,
                    "detail": "disabled",
                    "error_code": "disabled",
                    "tools": declared_tools,
                    "remediation": "Enable this MCP server to run health checks.",
                }

            if transport == "stdio":
                command = str(server.get("command", "")).strip()
                if not command:
                    error_code = "config"
                    detail = "missing command"
                    remediation = "Set command for stdio transport (for example: npx @modelcontextprotocol/server-... )."
                else:
                    command_bin = command.split()[0]
                    resolved = shutil.which(command_bin) if command_bin else None
                    if resolved:
                        reachable = True
                        status = "PASS"
                        detail = f"command found: {resolved}"
                        tools = declared_tools
                        if not tools:
                            remediation = "Add declared tools or enable HTTP inventory endpoint for live tool listing."
                    else:
                        error_code = "not_found"
                        detail = f"command not found in PATH: {command_bin}"
                        remediation = "Install the MCP server command or use full path."
            elif transport in {"http", "sse", "ws"}:
                url = str(server.get("url", "")).strip()
                if not url:
                    error_code = "config"
                    detail = "missing url"
                    remediation = "Set URL for HTTP/SSE/WS transport."
                else:
                    try:
                        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
                            response = await client.get(url, headers=request_headers or None)
                        reachable = response.status_code < 500
                        if reachable:
                            status = "PASS"
                            detail = f"HTTP {response.status_code}"
                        else:
                            error_code = "unreachable"
                            detail = f"HTTP {response.status_code}"
                            remediation = "Server responded with 5xx; check MCP process logs."
                    except TimeoutError:
                        error_code = "timeout"
                        detail = "connection timed out"
                        remediation = "Increase timeout or check network/firewall."
                    except Exception as exc:
                        lowered = str(exc).lower()
                        if "timed out" in lowered or "timeout" in lowered:
                            error_code = "timeout"
                            remediation = "Increase timeout or check network/firewall."
                        else:
                            error_code = "network"
                            remediation = "Verify URL, host, and MCP server process."
                        detail = str(exc).strip() or "connection failed"

                    if reachable:
                        if probe_tools:
                            tools = await _probe_mcp_tools_http(url, timeout_seconds=4.0, request_headers=request_headers)
                        if not tools:
                            tools = declared_tools
                        if not tools:
                            remediation = "No tools discovered. Confirm MCP endpoint supports /tools listing or add declared tools."
            else:
                error_code = "config"
                detail = f"unsupported transport: {transport}"
                remediation = "Use one of: stdio, http, sse, ws."
        except Exception as exc:  # pragma: no cover - defensive
            error_code = "error"
            detail = str(exc).strip() or "probe failed"
            remediation = remediation or "Review MCP server configuration and retry."

        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "transport": transport,
            "enabled": enabled,
            "status": status,
            "reachable": reachable,
            "latency_ms": latency_ms,
            "detail": detail,
            "error_code": error_code,
            "tools": tools[:64],
            "remediation": remediation,
        }

    @app.post("/v1/server/mcp/health")
    async def run_mcp_health(payload: dict[str, Any] | None = None, _: None = Depends(require_auth)):
        body = payload or {}
        include_disabled = bool(body.get("include_disabled", False))
        probe_tools = bool(body.get("probe_tools", True))
        server_names = {
            str(name).strip() for name in (body.get("server_names", []) or []) if str(name).strip()
        }
        servers = _normalize_mcp_servers(ui_settings.get("mcpServers", []))
        if server_names:
            servers = [server for server in servers if str(server.get("name", "")) in server_names]
        if not include_disabled:
            servers = [server for server in servers if bool(server.get("enabled", True))]
        checks: list[dict[str, Any]] = []
        for server in servers:
            checks.append(await _probe_mcp_server(server, probe_tools=probe_tools))
        summary = {
            "passed": sum(1 for row in checks if str(row.get("status", "")).upper() == "PASS"),
            "failed": sum(1 for row in checks if str(row.get("status", "")).upper() == "FAIL"),
            "skipped": sum(1 for row in checks if str(row.get("status", "")).upper() == "SKIP"),
            "total": len(checks),
        }
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "checks": checks,
        }

    @app.get("/v1/server/setup-status")
    async def server_setup_status(_: None = Depends(require_auth)):
        provider_rows = []
        key_rows = []
        for name, cfg in orchestrator.config.providers.items():
            provider_rows.append(
                {
                    "name": name,
                    "enabled": bool(cfg.enabled),
                    "model": str(cfg.models.deep),
                }
            )
            env_name = str(cfg.api_key_env)
            env_present = bool(os.getenv(env_name, "").strip())
            keyring_present = has_secret(env_name)
            key_rows.append(
                {
                    "name": name,
                    "key_set": env_present or keyring_present,
                }
            )
        enabled_providers = [row["name"] for row in provider_rows if bool(row.get("enabled"))]
        key_map = {str(row["name"]): bool(row.get("key_set")) for row in key_rows}
        enabled_with_keys = [name for name in enabled_providers if key_map.get(name, False)]

        invalid_routes: list[str] = []
        routing_valid = True
        getter = getattr(orchestrator, "get_role_routes", None)
        routes = getter() if callable(getter) else {}
        if isinstance(routes, dict):
            critique = routes.get("critique", {})
            if isinstance(critique, dict):
                for field in ("drafter_provider", "critic_provider", "refiner_provider"):
                    value = str(critique.get(field, "")).strip()
                    if value and value not in enabled_providers:
                        invalid_routes.append(f"critique.{field}")
            debate = routes.get("debate", {})
            if isinstance(debate, dict):
                for field in ("debater_a_provider", "debater_b_provider", "judge_provider", "synthesizer_provider"):
                    value = str(debate.get(field, "")).strip()
                    if value and value not in enabled_providers:
                        invalid_routes.append(f"debate.{field}")
            consensus = routes.get("consensus", {})
            if isinstance(consensus, dict):
                value = str(consensus.get("adjudicator_provider", "")).strip()
                if value and value not in enabled_providers:
                    invalid_routes.append("consensus.adjudicator_provider")
            council = routes.get("council", {})
            if isinstance(council, dict):
                value = str(council.get("synthesizer_provider", "")).strip()
                if value and value not in enabled_providers:
                    invalid_routes.append("council.synthesizer_provider")
                specialist_roles = council.get("specialist_roles", {})
                if isinstance(specialist_roles, dict):
                    for role_name, provider_name in specialist_roles.items():
                        value = str(provider_name).strip()
                        if value and value not in enabled_providers:
                            invalid_routes.append(f"council.specialist_roles.{role_name}")
        routing_valid = len(invalid_routes) == 0

        delegate = await _probe_delegate_socket(_delegate_socket_path())
        delegation_reachable = bool(delegate.get("reachable", False))

        token_configured = bool(token_state.get("value"))
        enabled_provider_present = len(enabled_providers) > 0
        enabled_provider_has_key = len(enabled_with_keys) > 0
        ready = all(
            [
                token_configured,
                enabled_provider_present,
                enabled_provider_has_key,
                routing_valid,
            ]
        )
        return {
            "ready": ready,
            "checks": {
                "token_configured": token_configured,
                "enabled_provider_present": enabled_provider_present,
                "enabled_provider_has_key": enabled_provider_has_key,
                "role_routing_valid": routing_valid,
                "delegation_reachable": delegation_reachable,
            },
            "details": {
                "enabled_providers": enabled_providers,
                "enabled_providers_with_keys": enabled_with_keys,
                "invalid_routes": invalid_routes,
                "delegation_status": str(delegate.get("status", "")),
                "delegation_detail": str(delegate.get("detail", "")),
            },
        }

    @app.get("/v1/server/remote-access/status")
    async def get_remote_access_status(_: None = Depends(require_auth)):
        return _build_remote_access_response(ui_settings.get("remoteAccess"), setup_status=admin_password_status())

    @app.post("/v1/server/remote-access/configure")
    async def configure_remote_access(payload: dict[str, Any], _: None = Depends(require_auth)):
        setup = admin_password_status()
        if not bool(setup.get("configured", False)):
            raise HTTPException(status_code=400, detail="Admin password must be configured before enabling remote access")
        _require_admin_password_if_configured(payload, audit_reason="remote_access_invalid_password")
        current = _normalize_remote_access_profile(ui_settings.get("remoteAccess"))
        profile = _normalize_remote_access_profile(payload)
        preflight_error = _validate_remote_access_profile(profile)
        if preflight_error:
            raise HTTPException(status_code=400, detail=preflight_error)
        action = "remote_access_rebind" if current.get("enabled") else "remote_access_enable"
        ui_settings["remoteAccess"] = profile
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        audit_logger.write(
            action,
            {
                "mode": profile.get("mode"),
                "bind_host": profile.get("bind_host"),
                "bind_port": profile.get("bind_port"),
                "public_base_url": profile.get("public_base_url"),
            },
        )
        return _build_remote_access_response(profile, setup_status=setup)

    @app.post("/v1/server/remote-access/revoke")
    async def revoke_remote_access(payload: dict[str, Any], _: None = Depends(require_auth)):
        _require_admin_password_if_configured(payload, audit_reason="remote_access_invalid_password")
        current = _normalize_remote_access_profile(ui_settings.get("remoteAccess"))
        profile = dict(current)
        profile["enabled"] = False
        profile["updated_at"] = datetime.now(timezone.utc).isoformat()
        ui_settings["remoteAccess"] = profile
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        audit_logger.write(
            "remote_access_revoke",
            {
                "mode": profile.get("mode"),
                "bind_host": profile.get("bind_host"),
                "bind_port": profile.get("bind_port"),
                "public_base_url": profile.get("public_base_url"),
            },
        )
        return _build_remote_access_response(profile, setup_status=admin_password_status())

    @app.post("/v1/server/remote-access/health")
    async def remote_access_health(_: None = Depends(require_auth)):
        profile = _normalize_remote_access_profile(ui_settings.get("remoteAccess"))
        report = await _probe_remote_access(profile=profile, bearer_token=token_state.get("value", ""))
        return report

    @app.post("/v1/server/doctor")
    async def server_doctor(payload: dict[str, Any] | None = None, _: None = Depends(require_auth)):
        checks: list[dict[str, Any]] = []
        smoke_providers = bool((payload or {}).get("smoke_providers", False))
        run_governance = bool((payload or {}).get("governance", False))

        def _add(name: str, status: str, detail: str, latency_ms: int = 0) -> None:
            checks.append(
                {
                    "name": name,
                    "status": status,
                    "detail": detail,
                    "latency_ms": int(latency_ms),
                }
            )

        if token_state.get("value"):
            _add("auth:token_present", "PASS", "daemon token is configured", 0)
        else:
            _add("auth:token_present", "FAIL", "daemon token is missing", 0)

        remaining = orchestrator.budgets.remaining()
        _add("health:context", "PASS", f"providers={len(orchestrator.providers)} budget_daily=${float(remaining.get('daily', 0.0)):.6f}", 0)

        catalog = _default_provider_catalog()
        _add("providers:catalog", "PASS", f"providers={len(catalog)}", 0)

        provider_rows = []
        key_rows = []
        for name, cfg in orchestrator.config.providers.items():
            provider_rows.append(
                {
                    "name": name,
                    "enabled": bool(cfg.enabled),
                    "model": str(cfg.models.deep),
                    "fast_model": str(cfg.models.fast),
                    "api_key_env": str(cfg.api_key_env),
                }
            )
            env_name = str(cfg.api_key_env)
            env_present = bool(os.getenv(env_name, "").strip())
            keyring_present = has_secret(env_name)
            key_rows.append(
                {
                    "name": name,
                    "api_key_env": env_name,
                    "key_set": env_present or keyring_present,
                    "source": "env" if env_present else ("keyring" if keyring_present else "none"),
                }
            )
        _add("providers:list", "PASS", f"providers={len(provider_rows)}", 0)
        _add("providers:key_status", "PASS", f"key_set={sum(1 for row in key_rows if row['key_set'])}", 0)

        getter = getattr(orchestrator, "get_role_routes", None)
        if callable(getter):
            routes = getter()
            _add("routing:roles", "PASS", f"sections={len(routes) if isinstance(routes, dict) else 0}", 0)
        else:
            _add("routing:roles", "FAIL", "role routing not supported", 0)

        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            _add("artifacts:encryption_status", "PASS", "artifact store unavailable", 0)
        else:
            root = getattr(store, "root", None)
            cipher = getattr(store, "cipher", None)
            sampled = 0
            encrypted = 0
            if isinstance(root, Path):
                for path in sorted(root.glob("*.json"))[:50]:
                    try:
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        sampled += 1
                        if isinstance(raw, dict) and raw.get("encrypted") is True:
                            encrypted += 1
                    except Exception:
                        continue
            _add(
                "artifacts:encryption_status",
                "PASS",
                f"enabled={bool(cipher is not None)} sampled={sampled} encrypted={encrypted}",
                0,
            )

        socket_path = _delegate_socket_path()
        delegate = await _probe_delegate_socket(socket_path)
        if bool(delegate.get("reachable", False)):
            _add("delegate:health", "PASS", str(delegate.get("status", "ok")), 0)
        else:
            _add("delegate:health", "FAIL", str(delegate.get("detail") or delegate.get("status") or "unreachable"), 0)

        _add("ui:settings", "PASS", f"keys={len(ui_settings)}", 0)

        if smoke_providers:
            key_map = {str(row.get("name", "")): row for row in key_rows}
            for row in sorted(provider_rows, key=lambda item: str(item.get("name", ""))):
                provider_name = str(row.get("name", "")).strip()
                if not provider_name:
                    continue
                if not bool(row.get("enabled", False)):
                    _add(f"provider:{provider_name}:smoke", "SKIP", "disabled", 0)
                    continue
                key_set = bool((key_map.get(provider_name) or {}).get("key_set", False))
                if not key_set:
                    _add(f"provider:{provider_name}:smoke", "SKIP", "key not set", 0)
                    continue
                adapter = orchestrator.providers.get(provider_name)
                if adapter is None:
                    _add(f"provider:{provider_name}:smoke", "FAIL", "provider not active", 0)
                    continue
                model = str(row.get("model", "")).strip()
                started = time.monotonic()
                try:
                    _ = await adapter.complete(
                        prompt="Connection test: respond with OK.",
                        model=model or str(orchestrator.config.providers[provider_name].models.fast),
                        max_tokens=8,
                        temperature=0.0,
                    )
                    latency_ms = int((time.monotonic() - started) * 1000)
                    _add(f"provider:{provider_name}:smoke", "PASS", f"model={model}", latency_ms)
                except Exception as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    _add(f"provider:{provider_name}:smoke", "FAIL", f"{type(exc).__name__}: {exc}", latency_ms)
        else:
            _add("providers:smoke", "SKIP", "use smoke_providers=true to run provider connection tests", 0)

        if run_governance:
            started = time.monotonic()
            try:
                from orchestrator.skills.governance import analyze_skill_bloat

                out_dir = str((payload or {}).get("governance_out_dir", os.getenv("MMO_SKILL_GOVERNANCE_OUT", "/tmp/mmy-skills-governance")))
                result = analyze_skill_bloat(out_dir=out_dir, include_disabled=False)
                latency_ms = int((time.monotonic() - started) * 1000)
                _add(
                    "skills:governance",
                    "PASS",
                    (
                        f"skills={int(result.get('skills_analyzed', 0))} "
                        f"merge={int(result.get('merge_candidates', 0))} "
                        f"crossover={int(result.get('crossover_candidates', 0))}"
                    ),
                    latency_ms,
                )
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                _add("skills:governance", "FAIL", f"{type(exc).__name__}: {exc}", latency_ms)
        else:
            _add("skills:governance", "SKIP", "use governance=true to run skill governance checks", 0)

        passed = sum(1 for item in checks if item["status"] == "PASS")
        failed = sum(1 for item in checks if item["status"] == "FAIL")
        skipped = sum(1 for item in checks if item["status"] == "SKIP")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "total": len(checks),
            },
            "checks": checks,
        }

    @app.post("/v1/skills/governance/analyze")
    async def skills_governance_analyze(payload: dict[str, Any] | None = None, _: None = Depends(require_auth)):
        from orchestrator.skills.governance import analyze_skill_bloat

        request = payload or {}
        include_disabled = bool(request.get("include_disabled", False))
        out_dir = str(request.get("out_dir", os.getenv("MMO_SKILL_GOVERNANCE_OUT", "/tmp/mmy-skills-governance")))
        limit_raw = request.get("limit", 20)
        try:
            limit = max(1, min(200, int(limit_raw)))
        except Exception:
            raise HTTPException(status_code=400, detail="limit must be an integer")

        try:
            result = analyze_skill_bloat(
                out_dir=out_dir,
                include_disabled=include_disabled,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"governance analysis failed: {exc}")

        out_path = Path(str(result.get("out_dir", out_dir))).expanduser()
        merge_rows: list[dict[str, Any]] = []
        crossover_rows: list[dict[str, Any]] = []
        try:
            merge_payload = json.loads((out_path / "merge_candidates.json").read_text(encoding="utf-8"))
            if isinstance(merge_payload, list):
                merge_rows = [item for item in merge_payload if isinstance(item, dict)][:limit]
        except Exception:
            merge_rows = []
        try:
            crossover_payload = json.loads((out_path / "crossover_candidates.json").read_text(encoding="utf-8"))
            if isinstance(crossover_payload, list):
                crossover_rows = [item for item in crossover_payload if isinstance(item, dict)][:limit]
        except Exception:
            crossover_rows = []

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "skills_analyzed": int(result.get("skills_analyzed", 0)),
                "merge_candidates": int(result.get("merge_candidates", 0)),
                "crossover_candidates": int(result.get("crossover_candidates", 0)),
            },
            "artifacts": {
                "out_dir": str(out_path),
                "merge_candidates_path": str(out_path / "merge_candidates.json"),
                "crossover_candidates_path": str(out_path / "crossover_candidates.json"),
                "skills_bloat_report_path": str(out_path / "skills_bloat_report.md"),
                "deprecation_plan_path": str(out_path / "deprecation_plan.md"),
            },
            "merge_candidates": merge_rows,
            "crossover_candidates": crossover_rows,
        }

    @app.get("/v1/cost")
    async def cost(_: None = Depends(require_auth)):
        remaining = orchestrator.budgets.remaining()
        state = orchestrator.budgets.state()
        totals = {"daily_totals": {"cost": 0.0, "requests": 0, "providers": {}}, "monthly_totals": {"cost": 0.0, "requests": 0, "providers": {}}}
        usage_totals = getattr(orchestrator.budgets, "usage_totals", None)
        if callable(usage_totals):
            try:
                raw_totals = usage_totals()
                if isinstance(raw_totals, dict):
                    totals["daily_totals"] = raw_totals.get("daily_totals", totals["daily_totals"])
                    totals["monthly_totals"] = raw_totals.get("monthly_totals", totals["monthly_totals"])
            except Exception:
                pass
        limiter = getattr(orchestrator, "rate_limiter", None)
        rate_limits = limiter.snapshot() if limiter is not None else {}
        router_weights = {}
        weights = getattr(orchestrator, "router_weights", None)
        if weights is not None:
            router_weights = weights.snapshot()
        return {
            "remaining": remaining,
            "state": {
                "session_spend": state.session_spend,
                "daily_spend": state.daily_spend,
                "monthly_spend": state.monthly_spend,
            },
            "totals": totals,
            "rate_limits": rate_limits,
            "router_weights": router_weights,
        }

    @app.get("/v1/providers")
    async def providers_list(_: None = Depends(require_auth)):
        rows = []
        for name, cfg in orchestrator.config.providers.items():
            rows.append(
                {
                    "name": name,
                    "enabled": bool(cfg.enabled),
                    "model": str(cfg.models.deep),
                    "fast_model": str(cfg.models.fast),
                    "api_key_env": str(cfg.api_key_env),
                }
            )
        rows.sort(key=lambda item: item["name"])
        return {"providers": rows}

    @app.get("/v1/routing/roles")
    async def routing_roles_get(_: None = Depends(require_auth)):
        getter = getattr(orchestrator, "get_role_routes", None)
        if not callable(getter):
            raise HTTPException(status_code=501, detail="role routing is not supported by this orchestrator")
        return {"routing": getter()}

    @app.put("/v1/routing/roles")
    async def routing_roles_put(payload: dict[str, Any], _: None = Depends(require_auth)):
        updater = getattr(orchestrator, "apply_role_routes", None)
        if not callable(updater):
            raise HTTPException(status_code=501, detail="role routing is not supported by this orchestrator")
        raw = payload.get("routing", payload)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="routing object is required")
        try:
            applied = updater(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"failed to apply routing config: {exc}")
        return {"routing": applied}

    @app.get("/v1/providers/keys/status")
    async def provider_key_status(_: None = Depends(require_auth)):
        rows = []
        for name, cfg in orchestrator.config.providers.items():
            env_name = str(cfg.api_key_env)
            env_present = bool(os.getenv(env_name, "").strip())
            keyring_present = has_secret(env_name)
            rows.append(
                {
                    "name": name,
                    "api_key_env": env_name,
                    "key_set": env_present or keyring_present,
                    "source": "env" if env_present else ("keyring" if keyring_present else "none"),
                }
            )
        rows.sort(key=lambda item: item["name"])
        return {"providers": rows}

    @app.post("/v1/providers/keys")
    async def provider_key_set(payload: dict[str, Any], _: None = Depends(require_auth)):
        provider_name = str(payload.get("provider", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        if not provider_name:
            raise HTTPException(status_code=400, detail="provider is required")
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key is required")
        if provider_name not in orchestrator.config.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        cfg = orchestrator.config.providers[provider_name]
        env_name = str(cfg.api_key_env)
        try:
            set_secret(env_name, api_key)
            os.environ[env_name] = api_key
            rebuild = getattr(orchestrator, "_rebuild_provider_runtime", None)
            if callable(rebuild):
                rebuild()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"failed to store provider key: {exc}")
        return {"provider": provider_name, "api_key_env": env_name, "key_set": True}

    @app.delete("/v1/providers/keys/{provider_name}")
    async def provider_key_delete(provider_name: str, _: None = Depends(require_auth)):
        name = str(provider_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail="provider is required")
        if name not in orchestrator.config.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        cfg = orchestrator.config.providers[name]
        env_name = str(cfg.api_key_env)
        deleted = delete_secret(env_name)
        os.environ.pop(env_name, None)
        rebuild = getattr(orchestrator, "_rebuild_provider_runtime", None)
        if callable(rebuild):
            try:
                rebuild()
            except Exception:
                # key removal can make provider runtime invalid if no enabled provider has credentials
                pass
        return {"provider": name, "api_key_env": env_name, "deleted": deleted}

    @app.post("/v1/providers/{provider_name}/test")
    async def provider_test_connection(
        provider_name: str,
        payload: dict[str, Any] | None = None,
        _: None = Depends(require_auth),
    ):
        name = str(provider_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail="provider is required")
        if name not in orchestrator.config.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        cfg = orchestrator.config.providers[name]
        env_name = str(cfg.api_key_env)
        current = os.getenv(env_name, "").strip()
        if not current:
            stored = get_secret(env_name)
            if stored:
                os.environ[env_name] = stored
                current = stored
        if not current:
            raise HTTPException(status_code=400, detail=f"provider key is missing ({env_name})")

        adapter = orchestrator.providers.get(name)
        if adapter is None:
            raise HTTPException(status_code=400, detail="provider is not active; enable it and apply provider config")

        selected_model = str((payload or {}).get("model", "")).strip() or str(cfg.models.fast)
        started = time.monotonic()
        try:
            _ = await adapter.complete(
                prompt="Connection test: respond with OK.",
                model=selected_model,
                max_tokens=8,
                temperature=0.0,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"provider test failed: {type(exc).__name__}: {exc}")

        return {
            "provider": name,
            "model": selected_model,
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    @app.get("/v1/providers/catalog")
    async def providers_catalog(_: None = Depends(require_auth)):
        return {"catalog": _default_provider_catalog()}

    @app.get("/v1/providers/{provider_name}/models")
    async def provider_models(provider_name: str, _: None = Depends(require_auth)):
        name = str(provider_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail="provider is required")
        if name not in orchestrator.config.providers:
            raise HTTPException(status_code=404, detail="provider not found")
        cfg = orchestrator.config.providers[name]
        configured = [_normalize_model_id(cfg.models.fast), _normalize_model_id(cfg.models.deep)]
        catalog = _default_provider_catalog().get(name, [])
        discovered: list[str] = []
        warnings: list[str] = []
        adapter = orchestrator.providers.get(name)
        if adapter is None:
            warnings.append("provider is not active; using catalog models")
        else:
            try:
                if hasattr(adapter, "detect_models") and callable(getattr(adapter, "detect_models")):
                    detected = getattr(adapter, "detect_models")()
                    if inspect.isawaitable(detected):
                        await detected
                    available = getattr(adapter, "available_models", [])
                    discovered = [_normalize_model_id(item) for item in (available or []) if _normalize_model_id(item)]
                else:
                    client = getattr(adapter, "client", None)
                    models_api = getattr(client, "models", None) if client is not None else None
                    list_fn = getattr(models_api, "list", None) if models_api is not None else None
                    if callable(list_fn):
                        listed = list_fn()
                        if inspect.isawaitable(listed):
                            listed = await listed
                        rows = getattr(listed, "data", []) or []
                        for row in rows:
                            value = _normalize_model_id(getattr(row, "id", "") or getattr(row, "name", ""))
                            if value:
                                discovered.append(value)
                    else:
                        warnings.append("provider adapter does not support model listing")
            except Exception as exc:
                warnings.append(f"live model discovery failed: {type(exc).__name__}: {exc}")
        combined = [item for item in (discovered + configured + catalog) if item]
        deduped = sorted({item for item in combined})
        source = "live" if discovered else ("catalog" if catalog else "configured")
        return {
            "provider": name,
            "models": deduped,
            "configured_model": _normalize_model_id(cfg.models.deep),
            "fast_model": _normalize_model_id(cfg.models.fast),
            "source": source,
            "warnings": warnings,
        }

    @app.put("/v1/providers")
    async def providers_update(payload: dict[str, Any], _: None = Depends(require_auth)):
        rows = payload.get("providers")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail="providers list is required")
        normalized: list[dict[str, str | bool]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": str(item.get("name", "")).strip(),
                    "enabled": bool(item.get("enabled", False)),
                    "model": str(item.get("model", "")).strip(),
                }
            )
        if not normalized:
            raise HTTPException(status_code=400, detail="at least one provider entry is required")
        try:
            result = orchestrator.apply_provider_overrides(normalized)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"failed to apply provider config: {exc}")
        return result

    @app.post("/v1/ask")
    async def ask(payload: dict[str, Any], _: None = Depends(require_auth)):
        query = str(payload.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        project_id = _normalize_project_id(payload.get("project_id"))
        run_id = str(payload.get("run_id", "")).strip() or f"run-{uuid4()}"
        mode = payload.get("mode")
        provider = payload.get("provider")
        stream = bool(payload.get("stream", False))
        tools = payload.get("tools")
        fact_check = bool(payload.get("fact_check", False))
        verbose = bool(payload.get("verbose", False))
        tool_approval_id = payload.get("tool_approval_id")
        assistant_name = str(payload.get("assistant_name", "")).strip()
        assistant_instructions = str(payload.get("assistant_instructions", "")).strip()
        strict_profile = bool(payload.get("strict_profile", False))
        web_assist_mode = _normalize_web_assist_mode(payload.get("web_assist_mode"))
        dependencies = _normalize_run_dependencies(payload.get("depends_on", payload.get("dependencies")))
        blockers = _normalize_run_blockers(payload.get("blockers"))
        checkpoint_request = {
            "query": query,
            "mode": str(mode) if mode is not None else None,
            "provider": str(provider) if provider is not None else None,
            "stream": stream,
            "tools": str(tools) if tools is not None else None,
            "fact_check": fact_check,
            "verbose": verbose,
            "tool_approval_id": str(tool_approval_id) if tool_approval_id is not None else None,
            "assistant_name": assistant_name,
            "assistant_instructions": assistant_instructions,
            "strict_profile": strict_profile,
            "web_assist_mode": web_assist_mode,
            "project_id": project_id,
            "run_id": run_id,
            "depends_on": dependencies,
            "blockers": blockers,
        }
        _start_run(run_id=run_id, endpoint="ask", request=checkpoint_request, status="running")
        _require_run_ready(run_id)
        _update_run(run_id, checkpoint={"stage": "calling_orchestrator"})
        effective_query = _apply_assistant_profile(
            query,
            assistant_name,
            assistant_instructions,
            strict_profile=strict_profile,
        )

        if not stream:
            try:
                result = await _run_orchestrator_ask(
                    query=effective_query,
                    mode=str(mode) if mode is not None else None,
                    provider=str(provider) if provider is not None else None,
                    fact_check=fact_check,
                    tools=str(tools) if tools is not None else None,
                    verbose=verbose,
                    tool_approval_id=str(tool_approval_id) if tool_approval_id is not None else None,
                    project_id=project_id,
                    web_assist_mode=web_assist_mode,
                )
            except HTTPException as exc:
                _update_run(
                    run_id,
                    status="failed",
                    checkpoint={"stage": "failed_timeout" if int(exc.status_code) == 504 else "failed"},
                    error_detail=str(exc.detail),
                    error_code=int(exc.status_code),
                )
                raise
            compliance_error = _profile_compliance_error(result.answer, assistant_instructions)
            if strict_profile and compliance_error:
                retry_query = _strict_retry_query(effective_query, compliance_error)
                try:
                    result = await _run_orchestrator_ask(
                        query=retry_query,
                        mode=str(mode) if mode is not None else None,
                        provider=str(provider) if provider is not None else None,
                        fact_check=fact_check,
                        tools=str(tools) if tools is not None else None,
                        verbose=verbose,
                        tool_approval_id=str(tool_approval_id) if tool_approval_id is not None else None,
                        project_id=project_id,
                        web_assist_mode=web_assist_mode,
                    )
                except HTTPException as exc:
                    _update_run(
                        run_id,
                        status="failed",
                        checkpoint={"stage": "failed_timeout" if int(exc.status_code) == 504 else "failed"},
                        error_detail=str(exc.detail),
                        error_code=int(exc.status_code),
                    )
                    raise
                second_error = _profile_compliance_error(result.answer, assistant_instructions)
                if second_error:
                    result.answer = _coerce_profile_output(result.answer, assistant_instructions)
            if strict_profile and _has_profile_constraints(assistant_instructions):
                result.answer = _coerce_profile_output(result.answer, assistant_instructions)
            result.answer = _strip_profile_echo_preamble(
                result.answer,
                assistant_name=assistant_name,
                assistant_instructions=assistant_instructions,
            )
            response_payload = _result_payload(result)
            _update_run(
                run_id,
                status="completed",
                checkpoint={"stage": "completed"},
                result_summary={
                    "mode": result.mode,
                    "provider": result.provider,
                    "model": result.model,
                    "cost": result.cost,
                },
            )
            response_payload["run_id"] = run_id
            return JSONResponse(response_payload)

        async def event_stream():
            try:
                async for event in orchestrator.ask_stream(
                    query=effective_query,
                    mode=str(mode) if mode is not None else None,
                    provider=str(provider) if provider is not None else None,
                    fact_check=fact_check,
                    tools=str(tools) if tools is not None else None,
                    verbose=verbose,
                    tool_approval_id=str(tool_approval_id) if tool_approval_id is not None else None,
                    project_id=project_id,
                    web_assist_mode=web_assist_mode,
                ):
                    if event.type == "result" and event.result is not None:
                        _update_run(
                            run_id,
                            status="completed",
                            result_summary={
                                "mode": event.result.mode,
                                "provider": event.result.provider,
                                "model": event.result.model,
                                "cost": event.result.cost,
                            },
                        )
                        data = {"type": "result", "result": _result_payload(event.result), "run_id": run_id}
                    else:
                        data = {"type": event.type, "text": event.text, "run_id": run_id}
                    yield f"data: {json.dumps(data)}\n\n"
            except Exception as exc:
                _update_run(run_id, status="failed", error_detail=str(exc), error_code=500)
                raise

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/v1/chat")
    async def chat(payload: dict[str, Any], _: None = Depends(require_auth)):
        message = str(payload.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        project_id = _normalize_project_id(payload.get("project_id"))
        session_id = str(payload.get("session_id") or uuid4())
        run_id = str(payload.get("run_id", "")).strip() or f"run-{uuid4()}"
        mode = str(payload.get("mode", "single"))
        provider = payload.get("provider")
        fact_check = bool(payload.get("fact_check", False))
        tools = payload.get("tools")
        verbose = bool(payload.get("verbose", False))
        tool_approval_id = payload.get("tool_approval_id")
        assistant_name = str(payload.get("assistant_name", "")).strip()
        assistant_instructions = str(payload.get("assistant_instructions", "")).strip()
        strict_profile = bool(payload.get("strict_profile", False))
        web_assist_mode = _normalize_web_assist_mode(payload.get("web_assist_mode"))
        dependencies = _normalize_run_dependencies(payload.get("depends_on", payload.get("dependencies")))
        blockers = _normalize_run_blockers(payload.get("blockers"))
        checkpoint_request = {
            "session_id": session_id,
            "message": message,
            "mode": mode,
            "provider": str(provider) if provider is not None else None,
            "fact_check": fact_check,
            "tools": str(tools) if tools is not None else None,
            "verbose": verbose,
            "tool_approval_id": str(tool_approval_id) if tool_approval_id is not None else None,
            "assistant_name": assistant_name,
            "assistant_instructions": assistant_instructions,
            "strict_profile": strict_profile,
            "web_assist_mode": web_assist_mode,
            "project_id": project_id,
            "run_id": run_id,
            "depends_on": dependencies,
            "blockers": blockers,
        }
        _start_run(run_id=run_id, endpoint="chat", request=checkpoint_request, session_id=session_id, status="running")
        _require_run_ready(run_id)
        _update_run(run_id, checkpoint={"stage": "loading_session_context"})

        _ensure_session_project(session_id, project_id)
        session = sessions.setdefault(session_id, SessionManager(max_context_tokens=8000))
        session.add("user", message)
        session.trim()
        context = session.export()[:-1]
        effective_message = _apply_assistant_profile(
            message,
            assistant_name,
            assistant_instructions,
            strict_profile=strict_profile,
        )
        _update_run(run_id, checkpoint={"stage": "calling_orchestrator"})
        try:
                result = await _run_orchestrator_ask(
                    query=effective_message,
                    mode=mode,
                    provider=str(provider) if provider is not None else None,
                    context_messages=context,
                    fact_check=fact_check,
                    tools=str(tools) if tools is not None else None,
                    verbose=verbose,
                    tool_approval_id=str(tool_approval_id) if tool_approval_id is not None else None,
                    project_id=project_id,
                    web_assist_mode=web_assist_mode,
                )
        except HTTPException as exc:
            _update_run(
                run_id,
                status="failed",
                session_id=session_id,
                checkpoint={"stage": "failed_timeout" if int(exc.status_code) == 504 else "failed"},
                error_detail=str(exc.detail),
                error_code=int(exc.status_code),
            )
            raise
        compliance_error = _profile_compliance_error(result.answer, assistant_instructions)
        if strict_profile and compliance_error:
            retry_query = _strict_retry_query(effective_message, compliance_error)
            try:
                    result = await _run_orchestrator_ask(
                        query=retry_query,
                        mode=mode,
                        provider=str(provider) if provider is not None else None,
                        context_messages=context,
                        fact_check=fact_check,
                        tools=str(tools) if tools is not None else None,
                        verbose=verbose,
                        tool_approval_id=str(tool_approval_id) if tool_approval_id is not None else None,
                        project_id=project_id,
                        web_assist_mode=web_assist_mode,
                    )
            except HTTPException as exc:
                _update_run(
                    run_id,
                    status="failed",
                    session_id=session_id,
                    checkpoint={"stage": "failed_timeout" if int(exc.status_code) == 504 else "failed"},
                    error_detail=str(exc.detail),
                    error_code=int(exc.status_code),
                )
                raise
            second_error = _profile_compliance_error(result.answer, assistant_instructions)
            if second_error:
                result.answer = _coerce_profile_output(result.answer, assistant_instructions)
        if strict_profile and _has_profile_constraints(assistant_instructions):
            result.answer = _coerce_profile_output(result.answer, assistant_instructions)
        result.answer = _strip_profile_echo_preamble(
            result.answer,
            assistant_name=assistant_name,
            assistant_instructions=assistant_instructions,
        )
        assistant_status = _result_status(result)
        assistant_metadata: dict[str, Any] = {
            "mode": result.mode,
            "provider": result.provider,
            "tokens": result.tokens_in + result.tokens_out,
            "cost": result.cost,
            "status": assistant_status,
            "warnings": result.warnings or [],
            "tool_outputs": result.tool_outputs or [],
            "pending_tool": result.pending_tool,
            "shared_state": getattr(result, "shared_state", None),
        }
        session.add("assistant", result.answer, metadata=assistant_metadata)
        session.trim()
        _save_sessions_to_disk(sessions, sessions_file, sessions_cipher, session_projects=session_projects)
        _update_run(
            run_id,
            status="completed",
            session_id=session_id,
            checkpoint={"stage": "completed"},
            result_summary={
                "mode": result.mode,
                "provider": result.provider,
                "model": result.model,
                "cost": result.cost,
            },
        )
        return {"session_id": session_id, "project_id": project_id, "run_id": run_id, "result": _result_payload(result)}

    def _session_title(manager: SessionManager, session_id: str) -> str:
        for message in manager.state.messages:
            if message.role == "user":
                text = message.content.strip()
                if not text:
                    break
                return text[:60] + ("..." if len(text) > 60 else "")
        return session_id

    @app.get("/v1/sessions")
    async def list_sessions(project_id: str | None = None, _: None = Depends(require_auth)):
        filter_project = _normalize_project_id(project_id) if project_id is not None else None
        return {
            "sessions": [
                {
                    "session_id": sid,
                    "project_id": _session_project(sid),
                    "title": _session_title(manager, sid),
                    "messages": len(manager.state.messages),
                }
                for sid, manager in sessions.items()
                if filter_project is None or _session_project(sid) == filter_project
            ]
        }

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, project_id: str | None = None, _: None = Depends(require_auth)):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        stored_project = _session_project(session_id)
        if project_id is not None and stored_project != _normalize_project_id(project_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "session_id": session_id,
            "project_id": stored_project,
            "title": _session_title(session, session_id),
            "messages": session.export(),
        }

    @app.get("/v1/sessions/{session_id}/dag")
    async def get_session_dag(session_id: str, project_id: str | None = None, _: None = Depends(require_auth)):
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        stored_project = _session_project(session_id)
        if project_id is not None and stored_project != _normalize_project_id(project_id):
            raise HTTPException(status_code=404, detail="session not found")

        latest_assistant: dict[str, Any] | None = None
        for msg in reversed(session.export()):
            if str(msg.get("role", "")) == "assistant":
                meta = msg.get("metadata")
                if isinstance(meta, dict):
                    latest_assistant = meta
                    break

        if latest_assistant is None:
            return {
                "session_id": session_id,
                "dag": {"source": "none", "nodes": [], "edges": []},
            }

        shared = latest_assistant.get("shared_state")
        if isinstance(shared, dict):
            stages = shared.get("stages")
            if isinstance(stages, list) and stages:
                nodes: list[dict[str, Any]] = []
                edges: list[dict[str, Any]] = []
                for idx, stage in enumerate(stages):
                    stage_name = f"stage-{idx + 1}"
                    details: dict[str, Any] = {}
                    if isinstance(stage, dict):
                        stage_name = str(stage.get("name", stage_name))
                        details = {k: v for k, v in stage.items() if k != "name"}
                    node_id = f"s{idx + 1}:{stage_name}"
                    nodes.append(
                        {
                            "id": node_id,
                            "label": stage_name,
                            "status": "completed",
                            "details": details,
                        }
                    )
                    if idx > 0:
                        edges.append(
                            {
                                "from": nodes[idx - 1]["id"],
                                "to": node_id,
                                "type": "depends_on",
                            }
                        )
                return {
                    "session_id": session_id,
                    "project_id": stored_project,
                    "dag": {
                        "source": "shared_state",
                        "mode": str(shared.get("mode", latest_assistant.get("mode", ""))),
                        "nodes": nodes,
                        "edges": edges,
                    },
                }

        mode = str(latest_assistant.get("mode", "single"))
        if mode == "critique":
            labels = ["draft", "critique", "refine", "final"]
        elif mode == "debate":
            labels = ["debater_a", "debater_b", "judge", "synthesizer", "final"]
        elif mode == "consensus":
            labels = ["participants", "adjudicator", "final"]
        elif mode == "council":
            labels = ["specialists", "synthesizer", "final"]
        elif mode == "retrieval":
            labels = ["retrieve_sources", "synthesize", "final"]
        else:
            labels = ["single_response"]

        nodes = [
            {
                "id": f"h{idx + 1}:{label}",
                "label": label,
                "status": "completed",
                "details": {},
            }
            for idx, label in enumerate(labels)
        ]
        edges = [
            {"from": nodes[idx - 1]["id"], "to": nodes[idx]["id"], "type": "depends_on"}
            for idx in range(1, len(nodes))
        ]
        return {
            "session_id": session_id,
            "project_id": stored_project,
            "dag": {
                "source": "heuristic",
                "mode": mode,
                "nodes": nodes,
                "edges": edges,
            },
        }

    @app.get("/v1/runs")
    async def list_runs(
        status: str | None = None,
        limit: int = 100,
        blocked: bool | None = None,
        dependency: str | None = None,
        stalled: bool | None = None,
        _: None = Depends(require_auth),
    ):
        rows = list(runs.values())
        if status:
            if status.strip().lower() == "blocked":
                rows = [row for row in rows if _run_is_blocked(row)]
            else:
                rows = [row for row in rows if str(row.get("status", "")).strip().lower() == status.strip().lower()]
        if blocked is True:
            rows = [row for row in rows if _run_is_blocked(row)]
        elif blocked is False:
            rows = [row for row in rows if not _run_is_blocked(row)]
        dependency_id = str(dependency or "").strip()
        if dependency_id:
            rows = [
                row
                for row in rows
                if dependency_id in _normalize_run_dependencies(row.get("dependencies", []))
            ]
        now_epoch = time.time()
        if stalled is True:
            rows = [row for row in rows if _run_stale_info(row, now_epoch=now_epoch)[0]]
        elif stalled is False:
            rows = [row for row in rows if not _run_stale_info(row, now_epoch=now_epoch)[0]]
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        bounded = rows[: max(1, min(limit, 500))]
        return {"runs": [_decorate_run(row, now_epoch=now_epoch) for row in bounded]}

    @app.get("/v1/runs/dag")
    async def runs_dag(limit: int = 100, _: None = Depends(require_auth)):
        rows = list(runs.values())
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        bounded = rows[: max(1, min(limit, 500))]
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()

        def _ensure_stub(node_id: str) -> None:
            if node_id in node_ids:
                return
            node_ids.add(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "label": node_id[:22],
                    "status": "external",
                    "details": {"external": True},
                }
            )

        for row in bounded:
            run_id = str(row.get("run_id", "")).strip()
            if not run_id:
                continue
            if run_id not in node_ids:
                node_ids.add(run_id)
                blockers = _normalize_run_blockers(row.get("blockers", []))
                open_blockers = _collect_open_blockers(row)
                stale, stale_age = _run_stale_info(row)
                node_status = "blocked" if open_blockers else str(row.get("status", "unknown"))
                if stale and node_status in {"running", "resuming", "waiting", "paused"}:
                    node_status = "stalled"
                nodes.append(
                    {
                        "id": run_id,
                        "label": run_id[:22],
                        "status": node_status,
                        "details": {
                            "endpoint": row.get("endpoint"),
                            "checkpoint": row.get("checkpoint"),
                            "session_id": row.get("session_id"),
                            "dependencies": _normalize_run_dependencies(row.get("dependencies", [])),
                            "blockers_open": len(open_blockers),
                            "blockers_total": len(blockers),
                            "stalled": stale,
                            "stalled_seconds": stale_age,
                        },
                    }
                )
            for dep in _normalize_run_dependencies(row.get("dependencies", [])):
                _ensure_stub(dep)
                edges.append({"from": dep, "to": run_id, "type": "depends_on"})
            blockers = _normalize_run_blockers(row.get("blockers", []))
            for blocker in blockers:
                if str(blocker.get("status", "open")).lower() != "open":
                    continue
                blocker_node_id = f"{run_id}:blocker:{str(blocker.get('blocker_id', 'unknown'))}"
                if blocker_node_id not in node_ids:
                    node_ids.add(blocker_node_id)
                    nodes.append(
                        {
                            "id": blocker_node_id,
                            "label": str(blocker.get("message") or blocker.get("code") or "blocker")[:48],
                            "status": "blocked",
                            "details": {"run_id": run_id, **blocker},
                        }
                    )
                edges.append({"from": blocker_node_id, "to": run_id, "type": "blocked_by"})

        return {"dag": {"nodes": nodes, "edges": edges, "count": len(nodes)}}

    @app.get("/v1/runs/events")
    async def stream_runs_events(_: None = Depends(require_auth)):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        run_event_subscribers.add(queue)

        async def event_stream():
            try:
                yield f"event: ready\ndata: {json.dumps({'type': 'ready', 'sent_at': _now_iso()})}\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield f"event: heartbeat\ndata: {json.dumps({'type': 'heartbeat', 'sent_at': _now_iso()})}\n\n"
                        continue
                    yield f"event: run\ndata: {json.dumps(payload)}\n\n"
            finally:
                run_event_subscribers.discard(queue)

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, _: None = Depends(require_auth)):
        row = runs.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run": _decorate_run(row)}

    @app.delete("/v1/runs/{run_id}")
    async def delete_run(run_id: str, _: None = Depends(require_auth)):
        removed = runs.pop(run_id, None)
        _save_runs_to_disk(runs, runs_file, sessions_cipher)
        if removed is not None:
            _emit_run_event("delete", run_id, None)
        return {"deleted": removed is not None, "run_id": run_id}

    @app.post("/v1/runs/clear")
    async def clear_runs(payload: dict[str, Any], _: None = Depends(require_auth)):
        status = str(payload.get("status", "")).strip().lower()
        if status:
            targets = [run_id for run_id, row in runs.items() if str(row.get("status", "")).strip().lower() == status]
        else:
            targets = list(runs.keys())
        for run_id in targets:
            runs.pop(run_id, None)
        _save_runs_to_disk(runs, runs_file, sessions_cipher)
        for run_id in targets:
            _emit_run_event("delete", run_id, None)
        return {"deleted": len(targets), "status": status or "all"}

    @app.post("/v1/runs/{run_id}/heartbeat")
    async def heartbeat_run(run_id: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        row = runs.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        checkpoint: dict[str, Any] = {}
        stage = payload.get("stage")
        note = payload.get("note")
        progress = payload.get("progress")
        if isinstance(stage, str) and stage.strip():
            checkpoint["stage"] = stage.strip()
        if isinstance(note, str) and note.strip():
            checkpoint["note"] = note.strip()
        if isinstance(progress, (int, float)):
            checkpoint["progress"] = max(0.0, min(1.0, float(progress)))
        deps_raw = payload.get("depends_on", payload.get("dependencies"))
        blockers_raw = payload.get("blockers")
        updates: dict[str, Any] = {}
        if deps_raw is not None:
            updates["dependencies"] = _normalize_run_dependencies(deps_raw)
        if blockers_raw is not None:
            updates["blockers"] = _normalize_run_blockers(blockers_raw)
        heartbeat_count = int(row.get("heartbeat_count", 0)) + 1
        status_raw = str(payload.get("status", "")).strip().lower()
        allowed = {"running", "paused", "waiting", "resuming", "blocked"}
        status = status_raw if status_raw in allowed else "running"
        if _run_is_blocked({**row, **updates}):
            status = "blocked"
            checkpoint.setdefault("stage", "blocked_on_dependency")
            checkpoint["blockers"] = _collect_open_blockers({**row, **updates})
        updated = _update_run(
            run_id,
            status=status,
            checkpoint=checkpoint,
            last_heartbeat_at=_now_iso(),
            heartbeat_count=heartbeat_count,
            **updates,
        )
        return {"run": updated}

    @app.post("/v1/runs/{run_id}/dependencies")
    async def update_run_dependencies(run_id: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        row = runs.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        deps_raw = payload.get("depends_on", payload.get("dependencies", row.get("dependencies", [])))
        blockers_raw = payload.get("blockers", row.get("blockers", []))
        dependencies = _normalize_run_dependencies(deps_raw)
        blockers = _normalize_run_blockers(blockers_raw)
        status_raw = str(payload.get("status", "")).strip().lower()
        status: str | None = status_raw if status_raw in {"running", "paused", "waiting", "resuming", "blocked", "completed", "failed"} else None
        checkpoint = row.get("checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        checkpoint = dict(checkpoint)
        candidate = {"dependencies": dependencies, "blockers": blockers}
        if _run_is_blocked(candidate):
            if status in {None, "running", "waiting"}:
                status = "blocked"
            checkpoint["stage"] = "blocked_on_dependency"
            checkpoint["blockers"] = _collect_open_blockers(candidate)
        elif status == "blocked":
            status = "running"
            if str(checkpoint.get("stage", "")).strip().lower() == "blocked_on_dependency":
                checkpoint["stage"] = "resumed"
        updated = _update_run(
            run_id,
            status=status,
            dependencies=dependencies,
            blockers=blockers,
            checkpoint=checkpoint,
        )
        return {"run": updated}

    @app.post("/v1/runs/{run_id}/resume")
    async def resume_run(run_id: str, _: None = Depends(require_auth)):
        row = runs.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        status = str(row.get("status", "")).strip().lower()
        stale, stale_age = _run_stale_info(row)
        if status in {"running", "resuming", "waiting", "paused"} and not stale:
            raise HTTPException(status_code=409, detail="run is still active; wait for completion or a stale heartbeat")
        endpoint = str(row.get("endpoint", "")).strip().lower()
        request = row.get("request")
        if not isinstance(request, dict):
            raise HTTPException(status_code=400, detail="run checkpoint request is missing")
        replay = dict(request)
        replay["run_id"] = run_id
        replay["stream"] = False
        resume_count = int(row.get("resume_count", 0)) + 1
        checkpoint = row.get("checkpoint") if isinstance(row.get("checkpoint"), dict) else {}
        checkpoint = dict(checkpoint)
        checkpoint["stage"] = "resumed" if not stale else "resumed_from_stale"
        if stale:
            checkpoint["stalled_seconds"] = stale_age
        _update_run(run_id, status="resuming", checkpoint=checkpoint, resume_count=resume_count, resumed_at=_now_iso())
        if endpoint == "chat":
            response = await chat(replay, None)
            return {"run": runs.get(run_id, {}), "resume": response}
        if endpoint == "ask":
            response = await ask(replay, None)
            if isinstance(response, JSONResponse):
                try:
                    payload = json.loads(response.body.decode("utf-8"))
                except Exception:
                    payload = {}
            else:
                payload = response
            return {"run": runs.get(run_id, {}), "resume": payload}
        raise HTTPException(status_code=400, detail=f"run endpoint '{endpoint}' cannot be resumed")

    @app.get("/v1/server/run-triggers")
    async def list_run_triggers(request: Request, _: None = Depends(require_auth)):
        rows = _normalize_run_triggers(ui_settings.get("runTriggers"))
        return {"triggers": [_decorate_run_trigger(row, request=request, ui_settings=ui_settings) for row in rows]}

    @app.put("/v1/server/run-triggers")
    async def put_run_triggers(request: Request, payload: dict[str, Any], _: None = Depends(require_auth)):
        _require_admin_password_if_configured(payload, audit_reason="run_trigger_invalid_password")
        raw_rows = payload.get("triggers", payload)
        if not isinstance(raw_rows, list):
            raise HTTPException(status_code=400, detail="triggers array is required")
        rows = _normalize_run_triggers(raw_rows)
        ui_settings["runTriggers"] = rows
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        audit_logger.write("run_trigger_save", {"count": len(rows)})
        return {"triggers": [_decorate_run_trigger(row, request=request, ui_settings=ui_settings) for row in rows]}

    @app.post("/v1/server/run-triggers/{trigger_id}/rotate-secret")
    async def rotate_run_trigger_secret(trigger_id: str, request: Request, payload: dict[str, Any], _: None = Depends(require_auth)):
        _require_admin_password_if_configured(payload, audit_reason="run_trigger_invalid_password")
        rows = _normalize_run_triggers(ui_settings.get("runTriggers"))
        updated = False
        for row in rows:
            if str(row.get("trigger_id", "")) != str(trigger_id):
                continue
            row["secret"] = secrets.token_urlsafe(24)
            row["updated_at"] = _now_iso()
            updated = True
            break
        if not updated:
            raise HTTPException(status_code=404, detail="trigger not found")
        ui_settings["runTriggers"] = rows
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        audit_logger.write("run_trigger_rotate_secret", {"trigger_id": str(trigger_id)})
        selected = next(row for row in rows if str(row.get("trigger_id", "")) == str(trigger_id))
        return {"trigger": _decorate_run_trigger(selected, request=request, ui_settings=ui_settings)}

    @app.post("/v1/server/run-triggers/sweep")
    async def sweep_run_triggers(_: None = Depends(require_auth)):
        return await _sweep_due_run_triggers(source="manual_sweep")

    @app.post("/v1/hooks/run/{trigger_id}/{secret}")
    async def fire_run_trigger(trigger_id: str, secret: str, request: Request):
        rows = _normalize_run_triggers(ui_settings.get("runTriggers"))
        trigger = next((row for row in rows if str(row.get("trigger_id", "")) == str(trigger_id)), None)
        if trigger is None or not bool(trigger.get("enabled", False)):
            raise HTTPException(status_code=404, detail="trigger not found")
        if str(trigger.get("secret", "")) != str(secret):
            audit_logger.write("run_trigger_fire_failed", {"trigger_id": str(trigger_id), "reason": "invalid_secret"})
            raise HTTPException(status_code=403, detail="invalid trigger secret")
        body = await request.body()
        payload_data: dict[str, Any] = {}
        if body:
            try:
                loaded = json.loads(body.decode("utf-8"))
                if isinstance(loaded, dict):
                    payload_data = loaded
            except Exception:
                payload_data = {}
        result = await _execute_run_trigger(trigger, payload_data=payload_data, source="webhook")
        ui_settings["runTriggers"] = rows
        _save_ui_settings_to_disk(ui_settings, ui_settings_file, sessions_cipher)
        return result

    @app.get("/v1/tool-approvals")
    async def list_tool_approvals(status: str | None = None, limit: int = 100, _: None = Depends(require_auth)):
        return {"approvals": approvals.list(status=status, limit=limit)}

    @app.post("/v1/tool-approvals/{approval_id}/approve")
    async def approve_tool_approval(approval_id: str, _: None = Depends(require_auth)):
        record = approvals.approve(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return {"approval": record}

    @app.post("/v1/tool-approvals/{approval_id}/deny")
    async def deny_tool_approval(approval_id: str, _: None = Depends(require_auth)):
        record = approvals.deny(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return {"approval": record}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str, project_id: str | None = None, _: None = Depends(require_auth)):
        stored_project = _session_project(session_id)
        removed = sessions.get(session_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="session not found")
        if project_id is not None and stored_project != _normalize_project_id(project_id):
            raise HTTPException(status_code=404, detail="session not found")
        sessions.pop(session_id, None)
        session_projects.pop(session_id, None)
        _save_sessions_to_disk(sessions, sessions_file, sessions_cipher, session_projects=session_projects)
        return {"deleted": True, "session_id": session_id}

    @app.get("/v1/memory")
    async def memory_list(project_id: str = "default", _: None = Depends(require_auth)):
        normalized_project = _normalize_project_id(project_id)
        records = _memory_list_records(project_id=normalized_project, limit=100)
        serialized: list[dict[str, Any]] = []
        for row in records:
            try:
                serialized.append(asdict(row))
            except TypeError:
                if isinstance(row, dict):
                    serialized.append(dict(row))
                else:
                    serialized.append(dict(getattr(row, "__dict__", {})))
        return {"memories": serialized}

    @app.post("/v1/memory/suggest")
    async def memory_suggest(payload: dict[str, Any], _: None = Depends(require_auth)):
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        project_id = _normalize_project_id(payload.get("project_id"))
        session_project = _session_project(session_id)
        if session_project != project_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"session '{session_id}' belongs to project '{session_project}', "
                    f"not '{project_id}'"
                ),
            )
        messages = session.export()
        last_user = ""
        last_assistant = ""
        for item in reversed(messages):
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role == "assistant" and not last_assistant and content:
                last_assistant = content
            elif role == "user" and not last_user and content:
                last_user = content
            if last_user and last_assistant:
                break
        if not last_user:
            raise HTTPException(status_code=400, detail="session has no user message for memory suggestion")
        if not last_assistant:
            raise HTTPException(status_code=400, detail="session has no assistant response for memory suggestion")

        provider_raw = payload.get("provider")
        provider = str(provider_raw).strip() if provider_raw is not None else None
        prompt = (
            "MEMORY_EXTRACT_V1\n"
            "Extract at most one durable user memory from the conversation below.\n"
            "Only extract stable preferences/profile facts/plans that improve future responses.\n"
            "Return STRICT JSON object only with keys: save(boolean), statement(string), source_type(string), confidence(number 0..1), reason(string).\n"
            "If nothing should be saved, set save=false and statement=\"\".\n\n"
            f"User:\n{last_user}\n\nAssistant:\n{last_assistant}"
        )
        try:
            result = await _run_orchestrator_ask(
                query=prompt,
                mode="single",
                provider=provider,
                fact_check=False,
                tools=None,
                verbose=False,
                project_id=project_id,
                web_assist_mode="off",
            )
        except HTTPException as exc:
            raise HTTPException(status_code=int(exc.status_code), detail=f"memory suggestion failed: {exc.detail}")

        parsed = _parse_first_json_object(result.answer)
        if not isinstance(parsed, dict):
            return {"suggested": False, "reason": "model_output_not_json"}

        save = bool(parsed.get("save", False))
        statement = str(parsed.get("statement", "")).strip()
        source_type = str(parsed.get("source_type", "chat_inferred")).strip() or "chat_inferred"
        reason = str(parsed.get("reason", "")).strip()
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        if not save or not statement:
            return {"suggested": False, "reason": reason or "not_durable"}

        duplicate = _memory_find_duplicate(statement, project_id=project_id)
        if duplicate is not None:
            return {"suggested": False, "reason": "already_stored", "existing_id": duplicate.id}

        decision = orchestrator.memory_governance.evaluate_write(
            statement=statement,
            source_type=source_type,
            source_ref=f"server.memory.suggest:{session_id}",
            is_model_inferred=False,
            confirm_fn=None,
        )
        if not decision.allowed:
            return {"suggested": False, "reason": decision.reason}

        return {
            "suggested": True,
            "candidate": {
                "statement": decision.redacted_statement,
                "source_type": source_type,
                "confidence": confidence,
                "reason": reason or "durable_memory_candidate",
                "session_id": session_id,
                "project_id": project_id,
            },
        }

    @app.post("/v1/memory")
    async def memory_add(payload: dict[str, Any], _: None = Depends(require_auth)):
        statement = str(payload.get("statement", "")).strip()
        if not statement:
            raise HTTPException(status_code=400, detail="statement is required")
        source_type = str(payload.get("source_type", "api"))
        source_ref = str(payload.get("source_ref", "server.memory"))
        confidence = float(payload.get("confidence", 0.7))
        ttl_days = int(payload.get("ttl_days", 30))
        project_id = _normalize_project_id(payload.get("project_id"))
        reviewed_by = payload.get("reviewed_by")
        model_inferred = bool(payload.get("model_inferred", False))

        decision = orchestrator.memory_governance.evaluate_write(
            statement=statement,
            source_type=source_type,
            source_ref=source_ref,
            is_model_inferred=model_inferred,
            confirm_fn=None,
        )
        if not decision.allowed:
            raise HTTPException(status_code=400, detail=f"Memory write denied: {decision.reason}")
        duplicate = _memory_find_duplicate(decision.redacted_statement, project_id=project_id)
        if duplicate is not None:
            return {"id": duplicate.id, "duplicate": True}
        record_id = _memory_add(
            project_id=project_id,
            statement=decision.redacted_statement,
            source_type=source_type,
            source_ref=source_ref,
            confidence=confidence,
            ttl_days=ttl_days,
            reviewed_by=str(reviewed_by) if reviewed_by is not None else None,
            redaction_status="redacted",
        )
        return {"id": record_id}

    @app.delete("/v1/memory/{record_id}")
    async def memory_delete(record_id: int, project_id: str = "default", _: None = Depends(require_auth)):
        normalized_project = _normalize_project_id(project_id)
        deleted = _memory_delete(record_id, project_id=normalized_project)
        return {"deleted": bool(deleted), "id": record_id}

    @app.get("/v1/projects")
    async def list_projects(_: None = Depends(require_auth)):
        project_ids = {_normalize_project_id(value) for value in session_projects.values()}
        list_projects = getattr(orchestrator.memory_store, "list_projects", None)
        if callable(list_projects):
            try:
                for item in list_projects():
                    project_ids.add(_normalize_project_id(item))
            except Exception:
                pass
        if not project_ids:
            project_ids.add("default")
        return {"projects": [{"project_id": value} for value in sorted(project_ids)]}

    @app.get("/v1/artifacts")
    async def artifacts_list(limit: int = 50, _: None = Depends(require_auth)):
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            return {"artifacts": []}
        rows = store.list_summaries(limit=limit)
        return {
            "artifacts": [
                {
                    "id": row.request_id,
                    "date": row.started_at,
                    "query": row.query_preview,
                    "mode": row.mode,
                    "cost": row.cost,
                    "guardian_flags": [],
                }
                for row in rows
            ]
        }

    @app.get("/v1/artifacts/encryption/status")
    async def artifacts_encryption_status(_: None = Depends(require_auth)):
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            return {"enabled": False, "directory": "", "sampled_files": 0, "encrypted_files": 0}
        root = getattr(store, "root", None)
        cipher = getattr(store, "cipher", None)
        if not isinstance(root, Path):
            return {"enabled": cipher is not None, "directory": "", "sampled_files": 0, "encrypted_files": 0}
        sampled = 0
        encrypted = 0
        for path in sorted(root.glob("*.json"))[:50]:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sampled += 1
                if isinstance(raw, dict) and raw.get("encrypted") is True:
                    encrypted += 1
            except Exception:
                continue
        return {
            "enabled": cipher is not None,
            "directory": str(root),
            "sampled_files": sampled,
            "encrypted_files": encrypted,
        }

    @app.get("/v1/artifacts/{request_id}")
    async def artifacts_get(request_id: str, _: None = Depends(require_auth)):
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            raise HTTPException(status_code=404, detail="artifact store unavailable")
        try:
            payload = store.load(request_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact not found")
        artifact = payload.get("artifact", {}) if isinstance(payload, dict) else {}
        result = artifact.get("result", {}) if isinstance(artifact, dict) else {}
        steps: list[dict[str, Any]] = []
        if isinstance(artifact, dict):
            steps.append({"name": "User Query", "content": str(artifact.get("query", ""))})
            draft = result.get("draft")
            if draft:
                steps.append({"name": "Draft", "content": str(draft)})
            critique = result.get("critique")
            if critique:
                steps.append({"name": "Critique", "content": str(critique)})
            refined = result.get("refined")
            if refined:
                steps.append({"name": "Refine", "content": str(refined)})
            citations = result.get("citations")
            if isinstance(citations, list) and citations:
                citation_lines: list[str] = []
                for idx, item in enumerate(citations, start=1):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title", "Untitled")).strip() or "Untitled"
                    url = str(item.get("url", "")).strip()
                    retrieved_at = str(item.get("retrieved_at", "")).strip()
                    snippet = str(item.get("snippet", "")).strip()
                    line = f"[{idx}] {title}\nurl: {url}"
                    if retrieved_at:
                        line += f"\nretrieved_at: {retrieved_at}"
                    if snippet:
                        line += f"\nsnippet: {snippet}"
                    citation_lines.append(line)
                if citation_lines:
                    steps.append({"name": "Citations", "content": "\n\n".join(citation_lines)})
            shared_state = result.get("shared_state")
            if isinstance(shared_state, dict):
                diagnostics_step: dict[str, Any] | None = None
                stages = shared_state.get("stages")
                if isinstance(stages, list):
                    for stage in stages:
                        if not isinstance(stage, dict):
                            continue
                        if str(stage.get("name", "")) != "diagnostics":
                            continue
                        data = stage.get("data")
                        if isinstance(data, dict):
                            diagnostics_step = data
                            break
                if diagnostics_step:
                    timings = diagnostics_step.get("timings_ms")
                    metadata: dict[str, Any] = {}
                    if isinstance(timings, dict):
                        for key in ("search_ms", "fetch_ms", "synthesis_ms", "total_ms"):
                            value = timings.get(key)
                            if isinstance(value, int):
                                metadata[key] = value
                    steps.append(
                        {
                            "name": "Diagnostics",
                            "content": json.dumps(diagnostics_step, indent=2, sort_keys=True),
                            "metadata": metadata,
                        }
                    )
            answer = result.get("answer")
            if answer:
                steps.append({"name": "Final Response", "content": str(answer)})
        return {
            "run": {
                "id": request_id,
                "date": str(artifact.get("started_at", datetime.now(timezone.utc).isoformat())) if isinstance(artifact, dict) else "",
                "query": str(artifact.get("query", "")) if isinstance(artifact, dict) else "",
                "mode": str(result.get("mode", artifact.get("mode", ""))) if isinstance(artifact, dict) else "",
                "cost": float(result.get("cost", 0.0)) if isinstance(result, dict) else 0.0,
                "guardian_flags": list(result.get("warnings", [])) if isinstance(result, dict) else [],
                "steps": steps,
                "raw": payload,
            }
        }

    @app.post("/v1/artifacts/{request_id}/export")
    async def artifacts_export_one(request_id: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        audit_logger.write("artifact_export_attempt", {"scope": "single", "request_id": request_id})
        _require_admin_password_if_configured(payload, audit_reason="artifact_export_invalid_password")
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            raise HTTPException(status_code=404, detail="artifact store unavailable")
        try:
            data = store.load(request_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact not found")
        audit_logger.write("artifact_export_success", {"scope": "single", "request_id": request_id, "count": 1})
        return {"request_id": request_id, "artifact": data}

    @app.post("/v1/artifacts/export-all")
    async def artifacts_export_all(payload: dict[str, Any], _: None = Depends(require_auth)):
        audit_logger.write("artifact_export_attempt", {"scope": "all"})
        _require_admin_password_if_configured(payload, audit_reason="artifact_export_invalid_password")
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            return {"count": 0, "artifacts": []}
        limit_raw = payload.get("limit", 200)
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 200
        limit = max(1, min(limit, 500))
        rows: list[dict[str, Any]] = []
        for summary in store.list_summaries(limit=limit):
            try:
                data = store.load(summary.request_id)
                rows.append({"request_id": summary.request_id, "artifact": data})
            except Exception:
                continue
        audit_logger.write("artifact_export_success", {"scope": "all", "count": len(rows)})
        return {"count": len(rows), "artifacts": rows}

    @app.post("/v1/artifacts/{request_id}/delete")
    async def artifacts_delete_one(request_id: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        audit_logger.write("artifact_delete_attempt", {"scope": "single", "request_id": request_id})
        _require_admin_password_if_configured(payload, audit_reason="artifact_delete_invalid_password")
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            raise HTTPException(status_code=404, detail="artifact store unavailable")
        deleted = bool(store.delete(request_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="artifact not found")
        audit_logger.write("artifact_delete_success", {"scope": "single", "request_id": request_id, "count": 1})
        return {"deleted": 1, "request_ids": [request_id]}

    @app.post("/v1/artifacts/delete-all")
    async def artifacts_delete_all(payload: dict[str, Any], _: None = Depends(require_auth)):
        audit_logger.write("artifact_delete_attempt", {"scope": "all"})
        _require_admin_password_if_configured(payload, audit_reason="artifact_delete_invalid_password")
        store = getattr(orchestrator, "artifacts", None)
        if store is None:
            return {"deleted": 0, "request_ids": []}
        older_than_raw = payload.get("older_than_days")
        older_than_days: int | None
        if older_than_raw in (None, "", 0, "0"):
            older_than_days = None
        else:
            try:
                older_than_days = max(1, int(older_than_raw))
            except Exception:
                raise HTTPException(status_code=400, detail="older_than_days must be an integer > 0")
        limit_raw = payload.get("limit", 1000)
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 1000
        limit = max(1, min(limit, 5000))
        request_ids = store.delete_many(older_than_days=older_than_days, limit=limit)
        audit_logger.write("artifact_delete_success", {"scope": "all", "count": len(request_ids), "older_than_days": older_than_days})
        return {"deleted": len(request_ids), "request_ids": request_ids, "older_than_days": older_than_days}

    @app.get("/v1/skills")
    async def skills_list(_: None = Depends(require_auth)):
        try:
            from orchestrator.skills.registry import discover_skills
        except Exception:
            return {"skills": []}
        skills = discover_skills()
        rows: list[dict[str, Any]] = []
        for name in sorted(skills.keys()):
            record = skills[name]
            description = ""
            risk_level = "low"
            manifest: dict[str, Any] = {}
            try:
                import yaml

                path = Path(record.path).expanduser()
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    description = str(raw.get("description", ""))
                    risk_level = str(raw.get("risk_level", "low"))
                    manifest_raw = raw.get("manifest", {})
                    if isinstance(manifest_raw, dict):
                        manifest = manifest_raw
            except Exception:
                pass
            rows.append(
                {
                    "id": name,
                    "name": name,
                    "description": description,
                    "risk_level": risk_level if risk_level in {"low", "medium", "high"} else "low",
                    "enabled": bool(record.enabled),
                    "manifest": manifest,
                    "checksum": record.checksum,
                    "signature_verified": record.signature_verified,
                }
            )
        return {"skills": rows}

    @app.get("/v1/skills/catalog")
    async def skills_catalog(_: None = Depends(require_auth)):
        try:
            from orchestrator.skills.catalog import curated_skill_catalog
            from orchestrator.skills.registry import discover_skills
        except Exception:
            return {"catalog": []}
        discovered = discover_skills()
        try:
            return {"catalog": curated_skill_catalog(discovered)}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"curated catalog unavailable: {exc}")

    @app.post("/v1/skills/{name}/enable")
    async def skills_enable(name: str, _: None = Depends(require_auth)):
        try:
            from orchestrator.skills.registry import set_skill_enabled

            record = set_skill_enabled(name, True)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"name": record.name, "enabled": True}

    @app.post("/v1/skills/{name}/disable")
    async def skills_disable(name: str, _: None = Depends(require_auth)):
        try:
            from orchestrator.skills.registry import set_skill_enabled

            record = set_skill_enabled(name, False)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"name": record.name, "enabled": False}

    @app.delete("/v1/skills/{name}")
    async def skills_delete(name: str, _: None = Depends(require_auth)):
        try:
            from orchestrator.skills.registry import delete_skill

            deleted = delete_skill(name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"name": name, "deleted": bool(deleted)}

    @app.get("/v1/skills/{name}/export")
    async def skills_export(name: str, _: None = Depends(require_auth)):
        from orchestrator.skills.registry import discover_skills

        discovered = discover_skills()
        record = discovered.get(name)
        if record is None:
            raise HTTPException(status_code=404, detail="skill not found")
        skill_path = Path(str(getattr(record, "path", ""))).expanduser()
        try:
            workflow_text = skill_path.read_text(encoding="utf-8")
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"skill workflow file not found: {exc}")
        return {
            "skill": {
                "name": name,
                "path": str(skill_path),
                "enabled": bool(getattr(record, "enabled", False)),
                "checksum": str(getattr(record, "checksum", "")),
                "signature_verified": bool(getattr(record, "signature_verified", False)),
                "workflow_text": workflow_text,
            }
        }

    @app.post("/v1/skills/import")
    async def skills_import(payload: dict[str, Any], _: None = Depends(require_auth)):
        workflow_text = str(payload.get("workflow_text", "")).strip()
        if not workflow_text:
            skill_payload = payload.get("skill")
            if isinstance(skill_payload, dict):
                workflow_text = str(skill_payload.get("workflow_text", "")).strip()
        if not workflow_text:
            raise HTTPException(status_code=400, detail="workflow_text is required")
        overwrite = bool(payload.get("overwrite", False))
        save_result = await skills_draft_save(
            {
                "workflow_text": workflow_text,
                "overwrite": overwrite,
            },
            None,
        )
        if isinstance(save_result, JSONResponse):
            return save_result
        return {
            "imported": bool(save_result.get("saved", False)),
            "name": str(save_result.get("name", "")),
            "path": str(save_result.get("path", "")),
            "enabled": bool(save_result.get("enabled", False)),
            "overwrote": bool(save_result.get("overwrote", False)),
        }

    @app.post("/v1/skills/{name}/test")
    async def skills_test(name: str, payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.skills.registry import discover_skills, validate_workflow_file
        from orchestrator.skills.testing import run_skill_adversarial_tests
        from orchestrator.skills.workflow import run_workflow_skill

        discovered = discover_skills()
        record = discovered.get(name)
        if record is None:
            raise HTTPException(status_code=404, detail="skill not found")
        if not bool(getattr(record, "enabled", False)):
            raise HTTPException(status_code=400, detail="skill is disabled")
        skill_path = Path(str(getattr(record, "path", ""))).expanduser()
        if not skill_path.exists():
            raise HTTPException(status_code=404, detail="skill workflow file not found")

        valid, errors, _workflow = validate_workflow_file(str(skill_path))
        response: dict[str, Any] = {
            "skill": name,
            "path": str(skill_path),
            "validation": {"valid": bool(valid), "errors": list(errors or [])},
        }
        if not valid:
            return JSONResponse(response, status_code=400)

        mode = str(payload.get("mode", "single"))
        provider_raw = payload.get("provider")
        provider = str(provider_raw).strip() if provider_raw is not None else None
        budget_raw = payload.get("budget_cap_usd")
        budget_cap_usd: float | None = None
        if budget_raw is not None:
            try:
                budget_cap_usd = float(budget_raw)
            except Exception:
                raise HTTPException(status_code=400, detail="budget_cap_usd must be numeric")
        input_data = payload.get("input", {})
        if input_data is None:
            input_data = {}
        if not isinstance(input_data, dict):
            raise HTTPException(status_code=400, detail="input must be a mapping")

        if bool(payload.get("run", False)):
            try:
                run_result = await run_workflow_skill(
                    orchestrator,
                    skill_path=str(skill_path),
                    input_data={str(k): v for k, v in input_data.items()},
                    mode=mode,
                    provider=provider,
                    budget_cap_usd=budget_cap_usd,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"skill test run failed: {exc}")
            response["run"] = {
                "skill_name": run_result.skill_name,
                "steps_executed": run_result.steps_executed,
                "total_cost": run_result.total_cost,
                "outputs": run_result.outputs,
            }

        if bool(payload.get("adversarial", False)):
            fixtures_raw = payload.get("fixtures_path")
            if fixtures_raw is None:
                default = Path("evaluation/skills_adversarial") / f"{skill_path.stem}.yaml"
                fixtures_path = str(default)
            else:
                fixtures_path = str(fixtures_raw).strip()
            if not fixtures_path:
                raise HTTPException(status_code=400, detail="fixtures_path is required for adversarial test")
            try:
                summary = await run_skill_adversarial_tests(
                    orchestrator,
                    skill_path=str(skill_path),
                    fixtures_path=fixtures_path,
                    mode=mode,
                    provider=provider,
                    budget_cap_usd=budget_cap_usd,
                )
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"adversarial skill test failed: {exc}")
            response["adversarial"] = summary

        return response

    @app.post("/v1/skills/draft/validate")
    async def skills_draft_validate(payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.skills.registry import validate_skill_manifest
        import yaml

        workflow_text = str(payload.get("workflow_text", "")).strip()
        if not workflow_text:
            raise HTTPException(status_code=400, detail="workflow_text is required")
        try:
            raw = yaml.safe_load(workflow_text)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to parse YAML: {exc}")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Skill root must be a mapping")

        errors: list[str] = []
        name = str(raw.get("name", "")).strip()
        if not name:
            errors.append("Missing non-empty skill name")
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("Skill must define a non-empty steps list")
        else:
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    errors.append(f"Step {index} must be a mapping")
                    continue
                if "tool" not in step and "model_call" not in step:
                    errors.append(f"Step {index} must include 'tool' or 'model_call'")
        errors.extend(validate_skill_manifest(raw, steps=steps if isinstance(steps, list) else None))
        manifest = raw.get("manifest", {})
        risk_level = str(raw.get("risk_level", "low"))
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "name": name,
            "risk_level": risk_level if risk_level in {"low", "medium", "high"} else "low",
            "manifest": manifest if isinstance(manifest, dict) else {},
        }

    @app.post("/v1/skills/draft/save")
    async def skills_draft_save(payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.skills.registry import set_skill_enabled, skills_root_dir
        import re
        import yaml

        workflow_text = str(payload.get("workflow_text", "")).strip()
        if not workflow_text:
            raise HTTPException(status_code=400, detail="workflow_text is required")
        overwrite = bool(payload.get("overwrite", False))

        try:
            validate_result = await skills_draft_validate({"workflow_text": workflow_text}, None)
        except HTTPException:
            raise
        if not bool(validate_result.get("valid", False)):
            return JSONResponse(
                {
                    "saved": False,
                    "errors": list(validate_result.get("errors", [])),
                },
                status_code=400,
            )

        raw = yaml.safe_load(workflow_text)
        assert isinstance(raw, dict)
        name = str(raw.get("name", "")).strip()
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-._")
        if not safe_name:
            raise HTTPException(status_code=400, detail="skill name must include alphanumeric characters")

        root = skills_root_dir()
        target_dir = root / safe_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "workflow.yaml"
        existed = target_path.exists()
        if existed and not overwrite:
            raise HTTPException(status_code=409, detail="skill already exists; set overwrite=true to replace")

        normalized = dict(raw)
        normalized["name"] = safe_name
        target_path.write_text(yaml.safe_dump(normalized, sort_keys=False), encoding="utf-8")

        try:
            set_skill_enabled(safe_name, False)
        except Exception:
            pass
        return {
            "saved": True,
            "name": safe_name,
            "path": str(target_path),
            "enabled": False,
            "overwrote": existed,
        }

    @app.post("/v1/skill-drafts/test")
    async def skills_draft_test(payload: dict[str, Any], _: None = Depends(require_auth)):
        from tempfile import TemporaryDirectory
        from orchestrator.skills.testing import run_skill_adversarial_tests
        from orchestrator.skills.workflow import run_workflow_skill
        import yaml

        workflow_text = str(payload.get("workflow_text", "")).strip()
        if not workflow_text:
            raise HTTPException(status_code=400, detail="workflow_text is required")
        mode = str(payload.get("mode", "single"))
        provider_raw = payload.get("provider")
        provider = str(provider_raw).strip() if provider_raw is not None else None
        run_enabled = bool(payload.get("run", True))
        adversarial_enabled = bool(payload.get("adversarial", False))
        budget_raw = payload.get("budget_cap_usd")
        budget_cap_usd: float | None = None
        if budget_raw is not None:
            try:
                budget_cap_usd = float(budget_raw)
            except Exception:
                raise HTTPException(status_code=400, detail="budget_cap_usd must be numeric")
        raw_input_data = payload.get("input", {})
        if raw_input_data is None:
            raw_input_data = {}
        if not isinstance(raw_input_data, dict):
            raise HTTPException(status_code=400, detail="input must be a mapping")

        validate_result = await skills_draft_validate({"workflow_text": workflow_text}, None)
        response: dict[str, Any] = {
            "validation": {
                "valid": bool(validate_result.get("valid", False)),
                "errors": list(validate_result.get("errors", [])),
            }
        }
        if not bool(validate_result.get("valid", False)):
            return JSONResponse(response, status_code=400)

        with TemporaryDirectory(prefix="mmo_skill_draft_") as td:
            raw = yaml.safe_load(workflow_text)
            assert isinstance(raw, dict)
            draft_name = str(raw.get("name", "draft_skill")).strip() or "draft_skill"
            path = Path(td) / f"{draft_name}.workflow.yaml"
            path.write_text(workflow_text, encoding="utf-8")
            input_data, used_defaults = _build_skill_draft_test_inputs(raw, raw_input_data)

            if run_enabled:
                try:
                    run_result = await run_workflow_skill(
                        orchestrator,
                        skill_path=str(path),
                        input_data={str(k): v for k, v in input_data.items()},
                        mode=mode,
                        provider=provider,
                        budget_cap_usd=budget_cap_usd,
                    )
                except Exception as exc:
                    if used_defaults and _is_network_resolution_error(exc):
                        response["run"] = {
                            "skill_name": draft_name,
                            "skipped": True,
                            "reason": (
                                "Draft run skipped: network name resolution failed while using auto-filled "
                                "test inputs. Provide explicit input URLs or retry when network is available."
                            ),
                            "inputs_used": input_data,
                        }
                        return response
                    raise HTTPException(status_code=400, detail=f"skill draft test run failed: {exc}")
                response["run"] = {
                    "skill_name": run_result.skill_name,
                    "steps_executed": run_result.steps_executed,
                    "total_cost": run_result.total_cost,
                    "outputs": run_result.outputs,
                    "inputs_used": input_data,
                }

            if adversarial_enabled:
                fixtures_raw = payload.get("fixtures_path")
                if fixtures_raw is None:
                    default = Path("evaluation/skills_adversarial") / f"{path.stem}.yaml"
                    fixtures_path = str(default)
                else:
                    fixtures_path = str(fixtures_raw).strip()
                if not fixtures_path:
                    raise HTTPException(status_code=400, detail="fixtures_path is required for adversarial test")
                try:
                    summary = await run_skill_adversarial_tests(
                        orchestrator,
                        skill_path=str(path),
                        fixtures_path=fixtures_path,
                        mode=mode,
                        provider=provider,
                        budget_cap_usd=budget_cap_usd,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=f"adversarial draft test failed: {exc}")
                response["adversarial"] = summary

        return response

    @app.post("/v1/tools/simulate")
    async def tools_simulate(payload: dict[str, Any], _: None = Depends(require_auth)):
        from orchestrator.security.taint import TaintedString
        from orchestrator.tools.registry import load_tool_registry
        from orchestrator.tools.simulated import execute_simulated_tool

        tool_name = str(payload.get("tool_name", "")).strip()
        if not tool_name:
            raise HTTPException(status_code=400, detail="tool_name is required")
        registry = load_tool_registry()
        manifest = registry.get(tool_name)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")
        raw_args = payload.get("args", {})
        if raw_args is None:
            raw_args = {}
        if not isinstance(raw_args, dict):
            raise HTTPException(status_code=400, detail="args must be a mapping")
        raw_tainted = {
            str(key): TaintedString(
                value=str(value),
                source="user_input",
                source_id=f"server.tools.simulate:{tool_name}:{key}",
                taint_level="untrusted",
            )
            for key, value in raw_args.items()
        }
        guardian = getattr(orchestrator, "guardian", None)
        if guardian is None:
            validated = {key: str(value.value) for key, value in raw_tainted.items()}
        else:
            validated = guardian.validate_tool_arguments(
                tool_name=tool_name,
                args=raw_tainted,
                arg_patterns={},
            )
        result = execute_simulated_tool(tool_name, validated)
        return {
            "ok": True,
            "tool_name": tool_name,
            "validated_args": validated,
            "result": result,
        }

    return app


def run_server(orchestrator, *, host: str, port: int) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("uvicorn is required for daemon mode (install optional server dependencies).") from exc
    app = create_app(orchestrator)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _server_token_file() -> Path:
    raw = os.getenv("MMO_SERVER_API_KEY_FILE", "~/.mmo/server_api_key.txt")
    return Path(raw).expanduser()


def _load_server_api_key(api_key_env: str, token_file: Path) -> str:
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    env_token = os.getenv(api_key_env, "").strip()
    if env_token:
        return env_token
    return ""


def _write_server_api_key(token_file: Path, token: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token.strip(), encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass


def _sessions_store_file() -> Path:
    raw = os.getenv("MMO_SESSIONS_FILE", "~/.mmo/sessions_store.json")
    return Path(raw).expanduser()


def _ui_settings_store_file() -> Path:
    raw = os.getenv("MMO_UI_SETTINGS_FILE", "~/.mmo/ui_settings.json")
    return Path(raw).expanduser()


def _runs_store_file() -> Path:
    raw = os.getenv("MMO_RUNS_FILE", "~/.mmo/runs_store.json")
    return Path(raw).expanduser()


def _audit_store_file(orchestrator) -> Path:
    usage_file = getattr(getattr(orchestrator, "config", object()), "budgets", None)
    usage_path = getattr(usage_file, "usage_file", "")
    if usage_path:
        return Path(str(usage_path)).expanduser().with_name("audit.jsonl")
    state_dir = os.getenv("MMO_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / "audit.jsonl"
    raw = os.getenv("MMO_AUDIT_FILE", "~/.mmo/audit.jsonl")
    return Path(raw).expanduser()


def _read_recent_audit_events(
    path: Path,
    cipher,
    *,
    limit: int = 50,
    event_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("encrypted") is True:
            payload = item.get("payload")
            if not isinstance(payload, str) or cipher is None:
                continue
            try:
                item = json.loads(cipher.decrypt_text(payload))
            except Exception:
                continue
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type", ""))
        if event_types is not None and event_type not in event_types:
            continue
        rows.append(
            {
                "timestamp": str(item.get("timestamp", "")),
                "event_type": event_type,
                "payload": item.get("payload", {}),
            }
        )
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows


def _sessions_cipher():
    raw = str(os.getenv("MMO_SESSIONS_ENCRYPT", "1")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return None
    config = DataProtectionConfig(
        encrypt_at_rest=True,
        key_provider="os_keyring",
        passphrase_env="MMO_MASTER_PASSPHRASE",
    )
    return build_envelope_cipher(config)


def _normalize_ui_settings(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("apiBaseUrl", "assistantName", "assistantInstructions", "theme", "sessionPanelSide", "sessionSortOrder"):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value
    chat_mode = raw.get("chatMode")
    if isinstance(chat_mode, str) and chat_mode in {"single", "critique", "debate", "consensus", "council", "retrieval"}:
        out["chatMode"] = chat_mode
    web_assist_mode = raw.get("webAssistMode")
    if isinstance(web_assist_mode, str) and web_assist_mode in {"off", "auto", "confirm"}:
        out["webAssistMode"] = web_assist_mode
    retrieval_answer_style = raw.get("retrievalAnswerStyle")
    if isinstance(retrieval_answer_style, str) and retrieval_answer_style in {
        "concise_ranked",
        "full_details",
        "source_first",
    }:
        out["retrievalAnswerStyle"] = retrieval_answer_style
    for key in (
        "assistantStrictProfile",
        "debugRetrievalWarnings",
        "chatToolsEnabled",
        "chatFactCheckEnabled",
        "chatAutoMemoryEnabled",
    ):
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value
    web_max_sources = raw.get("webMaxSources")
    if isinstance(web_max_sources, int):
        out["webMaxSources"] = max(1, min(10, web_max_sources))
    providers = raw.get("providers")
    if isinstance(providers, list):
        rows: list[dict[str, Any]] = []
        for item in providers:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            model = str(item.get("model", "")).strip()
            enabled = bool(item.get("enabled", False))
            if not name:
                continue
            rows.append({"name": name, "model": model, "enabled": enabled})
        out["providers"] = rows
    provider_monthly_budgets = raw.get("providerMonthlyBudgets")
    if isinstance(provider_monthly_budgets, dict):
        budget_rows: dict[str, float] = {}
        for key, value in provider_monthly_budgets.items():
            name = str(key).strip()
            if not name:
                continue
            amount: float
            if isinstance(value, (int, float)):
                amount = float(value)
            elif isinstance(value, str):
                try:
                    amount = float(value.strip())
                except Exception:
                    continue
            else:
                continue
            budget_rows[name] = max(0.0, min(amount, 1_000_000.0))
        out["providerMonthlyBudgets"] = budget_rows
    if "mcpServers" in raw:
        out["mcpServers"] = _normalize_mcp_servers(raw.get("mcpServers"))
    if "remoteAccess" in raw:
        out["remoteAccess"] = _normalize_remote_access_profile(raw.get("remoteAccess"))
    if "runTriggers" in raw:
        out["runTriggers"] = _normalize_run_triggers(raw.get("runTriggers"))
    return out


def _normalize_remote_access_profile(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    mode = str(source.get("mode", "lan")).strip().lower()
    if mode not in {"lan", "tailscale", "cloudflare", "manual_proxy"}:
        mode = "lan"
    bind_host = str(source.get("bind_host", source.get("bindHost", "127.0.0.1"))).strip() or "127.0.0.1"
    public_base_url = str(source.get("public_base_url", source.get("publicBaseUrl", ""))).strip()
    notes = str(source.get("notes", "")).strip()
    try:
        bind_port = int(source.get("bind_port", source.get("bindPort", 8100)))
    except Exception:
        bind_port = 8100
    bind_port = max(1, min(bind_port, 65535))
    enabled = bool(source.get("enabled", bool(source)))
    updated_at = str(source.get("updated_at", "")).strip() or datetime.now(timezone.utc).isoformat()
    return {
        "enabled": enabled,
        "mode": mode,
        "bind_host": bind_host[:255],
        "bind_port": bind_port,
        "public_base_url": public_base_url[:512],
        "notes": notes[:800],
        "updated_at": updated_at,
    }


def _normalize_run_triggers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        message = str(item.get("message", "")).strip()
        if not name or not message:
            continue
        mode = str(item.get("mode", "single")).strip().lower()
        if mode not in {"single", "critique", "debate", "consensus", "council", "retrieval"}:
            mode = "single"
        web_assist_mode = str(item.get("web_assist_mode", item.get("webAssistMode", "off"))).strip().lower()
        if web_assist_mode not in {"off", "auto", "confirm"}:
            web_assist_mode = "off"
        interval_raw = item.get("interval_minutes", item.get("intervalMinutes", 0))
        try:
            interval_minutes = int(interval_raw)
        except Exception:
            interval_minutes = 0
        interval_minutes = max(0, min(interval_minutes, 60 * 24 * 30))
        trigger_id = str(item.get("trigger_id", item.get("triggerId", ""))).strip() or f"trigger-{uuid4()}"
        secret = str(item.get("secret", "")).strip() or secrets.token_urlsafe(24)
        next_run_at = str(item.get("next_run_at", item.get("nextRunAt", ""))).strip()
        if interval_minutes > 0 and not next_run_at:
            next_run_at = _schedule_next_run_at(interval_minutes)
        rows.append(
            {
                "trigger_id": trigger_id,
                "name": name[:120],
                "enabled": bool(item.get("enabled", True)),
                "project_id": str(item.get("project_id", "default") or "default").strip() or "default",
                "session_id": str(item.get("session_id", "")).strip(),
                "mode": mode,
                "provider": str(item.get("provider", "")).strip(),
                "message": message[:4000],
                "tools": bool(item.get("tools", False)),
                "fact_check": bool(item.get("fact_check", item.get("factCheck", False))),
                "assistant_name": str(item.get("assistant_name", item.get("assistantName", ""))).strip()[:120],
                "assistant_instructions": str(item.get("assistant_instructions", item.get("assistantInstructions", ""))).strip()[:2000],
                "strict_profile": bool(item.get("strict_profile", item.get("strictProfile", False))),
                "web_assist_mode": web_assist_mode,
                "interval_minutes": interval_minutes,
                "next_run_at": next_run_at,
                "secret": secret,
                "last_triggered_at": str(item.get("last_triggered_at", "")).strip(),
                "last_run_id": str(item.get("last_run_id", "")).strip(),
                "updated_at": str(item.get("updated_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
            }
        )
    return rows


def _decorate_run_trigger(row: dict[str, Any], *, request, ui_settings: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    secret = str(row.get("secret", "")).strip()
    trigger_id = str(row.get("trigger_id", "")).strip()
    payload["webhook_path"] = f"/v1/hooks/run/{trigger_id}/{secret}"
    payload["webhook_url"] = _run_trigger_webhook_url(trigger_id=trigger_id, secret=secret, request=request, ui_settings=ui_settings)
    return payload


def _run_trigger_webhook_url(*, trigger_id: str, secret: str, request, ui_settings: dict[str, Any]) -> str:
    remote_access = _normalize_remote_access_profile(ui_settings.get("remoteAccess"))
    public_base_url = str(remote_access.get("public_base_url", "")).strip()
    if public_base_url:
        return f"{public_base_url.rstrip('/')}/v1/hooks/run/{trigger_id}/{secret}"
    base = str(request.base_url).rstrip("/")
    return f"{base}/v1/hooks/run/{trigger_id}/{secret}"


def _schedule_next_run_at(interval_minutes: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(minutes=max(1, interval_minutes))).isoformat()


def _trigger_due(trigger: dict[str, Any], *, now: datetime | None = None) -> bool:
    interval_minutes = int(trigger.get("interval_minutes", 0) or 0)
    if interval_minutes <= 0 or not bool(trigger.get("enabled", False)):
        return False
    next_run_at = str(trigger.get("next_run_at", "")).strip()
    if not next_run_at:
        return True
    try:
        due_at = datetime.fromisoformat(next_run_at.replace("Z", "+00:00"))
    except Exception:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return due_at <= current


def _build_remote_access_response(raw: Any, *, setup_status: dict[str, Any]) -> dict[str, Any]:
    profile = _normalize_remote_access_profile(raw)
    bind_host = str(profile.get("bind_host", "127.0.0.1"))
    bind_port = int(profile.get("bind_port", 8100))
    mode = str(profile.get("mode", "lan"))
    public_base_url = str(profile.get("public_base_url", ""))
    enabled = bool(profile.get("enabled", False))

    mode_labels = {
        "lan": "LAN / direct port exposure",
        "tailscale": "Tailscale / private mesh",
        "cloudflare": "Cloudflare Tunnel / public edge",
        "manual_proxy": "Manual reverse proxy",
    }
    mode_guidance = {
        "lan": [
            "Restrict to a trusted LAN or VPN before binding beyond localhost.",
            "Prefer an OS firewall rule scoped to your own subnet.",
            "Do not reuse the bearer token across shared devices.",
        ],
        "tailscale": [
            "Keep the daemon bound to 127.0.0.1 when using a local mesh forwarder.",
            "Restrict access to approved Tailnet users only.",
            "Verify the public base URL points to the private mesh address, not a public IP.",
        ],
        "cloudflare": [
            "Prefer a tunnel that points to localhost instead of binding the daemon on 0.0.0.0.",
            "Require admin password and rotate the bearer token before first exposure.",
            "Review edge access policy before sharing the URL.",
        ],
        "manual_proxy": [
            "Terminate TLS and auth at the reverse proxy before forwarding to the daemon.",
            "Restrict allowed source IPs where possible.",
            "Keep the daemon on localhost unless the proxy requires otherwise.",
        ],
    }

    if not public_base_url:
        public_base_url = f"http://{bind_host}:{bind_port}"
    launch_command = f"python3 -m mmctl serve --host {bind_host} --port {bind_port}"
    rollback_command = f"python3 -m mmctl serve --host 127.0.0.1 --port {bind_port}"
    steps = [
        "Confirm bearer token access in the dashboard before exposing the daemon.",
        f"Launch or rebind the daemon with: {launch_command}",
        f"Validate setup using: python3 -m mmctl doctor",
        f"Open the dashboard via the intended remote URL: {public_base_url}",
        f"Rollback to localhost-only with: {rollback_command}",
    ]
    return {
        "enabled": enabled,
        "profile": profile,
        "admin_password_configured": bool(setup_status.get("configured", False)),
        "mode_label": mode_labels.get(mode, mode),
        "public_url": public_base_url,
        "launch_command": launch_command,
        "rollback_command": rollback_command,
        "steps": steps,
        "warnings": mode_guidance.get(mode, []),
        "summary": (
            f"Remote access plan active via {mode_labels.get(mode, mode)}."
            if enabled
            else "Remote access plan is not active."
        ),
    }


def _validate_remote_access_profile(profile: dict[str, Any]) -> str | None:
    mode = str(profile.get("mode", "lan"))
    bind_host = str(profile.get("bind_host", "")).strip().lower()
    public_base_url = str(profile.get("public_base_url", "")).strip()
    if not bind_host:
        return "bind_host is required"
    if mode in {"cloudflare", "tailscale"} and bind_host == "0.0.0.0":
        return "Use 127.0.0.1 for cloudflare or tailscale mode; expose it through the tunnel/mesh layer instead"
    if mode in {"cloudflare", "manual_proxy", "tailscale"} and not public_base_url:
        return "public_base_url is required for cloudflare, tailscale, and manual_proxy mode"
    return None


async def _probe_remote_access(*, profile: dict[str, Any], bearer_token: str) -> dict[str, Any]:
    import httpx

    bind_host = str(profile.get("bind_host", "127.0.0.1")).strip() or "127.0.0.1"
    bind_port = int(profile.get("bind_port", 8100))
    public_base_url = str(profile.get("public_base_url", "")).strip()
    bind_url = f"http://{bind_host}:{bind_port}/v1/health"
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}

    async def _probe_url(url: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
                response = await client.get(url, headers=headers or None)
            latency_ms = int((time.monotonic() - started) * 1000)
            ok = response.status_code < 400
            detail = ""
            if not ok:
                detail = f"HTTP {response.status_code}"
            remediation = _remote_access_probe_remediation(url=url, detail=detail or "ok", status_code=response.status_code if not ok else 200)
            return {
                "url": url,
                "reachable": ok,
                "status": "PASS" if ok else "FAIL",
                "detail": detail or "ok",
                "remediation": remediation,
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            detail = f"{type(exc).__name__}: {exc}"
            return {
                "url": url,
                "reachable": False,
                "status": "FAIL",
                "detail": detail,
                "remediation": _remote_access_probe_remediation(url=url, detail=detail, status_code=None),
                "latency_ms": latency_ms,
            }

    checks = [await _probe_url(bind_url)]
    if public_base_url:
        checks.append(await _probe_url(f"{public_base_url.rstrip('/')}/v1/health"))
    else:
        checks.append(
            {
                "url": "",
                "reachable": False,
                "status": "SKIP",
                "detail": "public_base_url not configured",
                "remediation": "Set a public base URL for the selected remote mode, then rerun the probe.",
                "latency_ms": 0,
            }
        )
    summary = {
        "passed": sum(1 for row in checks if row["status"] == "PASS"),
        "failed": sum(1 for row in checks if row["status"] == "FAIL"),
        "skipped": sum(1 for row in checks if row["status"] == "SKIP"),
        "total": len(checks),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(profile.get("enabled", False)),
        "summary": summary,
        "checks": [
            {"name": "bind_target", **checks[0]},
            {"name": "public_url", **checks[1]},
        ],
    }


def _remote_access_probe_remediation(*, url: str, detail: str, status_code: int | None) -> str:
    lowered = detail.lower()
    if status_code == 401:
        return "Bearer token was rejected. Rotate or recover the server token, update the dashboard token, and retry."
    if status_code == 403:
        return "The target is reachable but access is denied. Check upstream auth or access policy, then retry."
    if status_code == 404:
        return "The target responded without /v1/health. Verify the base URL points at the MMO daemon or proxy path."
    if "connecterror" in lowered or "connection refused" in lowered:
        return "The target is not accepting connections. Rebind the daemon or confirm the proxy/tunnel is forwarding correctly."
    if "timeout" in lowered:
        return "The target did not respond in time. Check tunnel/proxy health and retry once latency is stable."
    if "name or service not known" in lowered or "nodename nor servname provided" in lowered:
        return "DNS or hostname resolution failed. Verify the public hostname and local proxy destination."
    if status_code and status_code >= 500:
        return "The target is reachable but unhealthy. Check daemon logs or upstream proxy error logs."
    return "If this target should be reachable, verify bind host/port, proxy routing, and bearer token configuration."


def _normalize_mcp_servers(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        transport = str(item.get("transport", "stdio")).strip().lower()
        if transport not in {"stdio", "http", "sse", "ws"}:
            transport = "stdio"
        enabled = bool(item.get("enabled", True))
        command = str(item.get("command", "")).strip()
        args_raw = item.get("args", [])
        args: list[str] = []
        if isinstance(args_raw, list):
            args = [str(arg).strip() for arg in args_raw if str(arg).strip()]
        elif isinstance(args_raw, str):
            args = [chunk.strip() for chunk in args_raw.split() if chunk.strip()]
        url = str(item.get("url", "")).strip()
        headers_raw = item.get("headers", {})
        headers: dict[str, str] = {}
        if isinstance(headers_raw, dict):
            for key, value in headers_raw.items():
                k = str(key).strip()
                v = str(value).strip()
                if k and v:
                    headers[k] = v
        declared_raw = item.get("declared_tools", item.get("declaredTools", []))
        declared_tools: list[str] = []
        if isinstance(declared_raw, list):
            declared_tools = [str(tool).strip() for tool in declared_raw if str(tool).strip()]
        elif isinstance(declared_raw, str):
            declared_tools = [chunk.strip() for chunk in declared_raw.split(",") if chunk.strip()]
        header_refs_raw = item.get("header_env_refs", item.get("headerEnvRefs", {}))
        header_env_refs: dict[str, str] = {}
        if isinstance(header_refs_raw, dict):
            for key, env_name in header_refs_raw.items():
                k = str(key).strip()
                v = str(env_name).strip()
                if k and v:
                    header_env_refs[k] = v
        rows.append(
            {
                "name": name[:64],
                "transport": transport,
                "enabled": enabled,
                "command": command,
                "args": args[:32],
                "url": url,
                "headers": headers,
                "header_env_refs": header_env_refs,
                "declared_tools": declared_tools[:64],
            }
        )
    return rows


def _load_ui_settings_from_disk(path: Path, cipher) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and data.get("format") == "mmo-ui-settings-v1" and isinstance(data.get("encrypted"), str):
        if cipher is None:
            return {}
        try:
            payload = json.loads(cipher.decrypt_text(str(data["encrypted"])))
        except Exception:
            return {}
    elif isinstance(data, dict):
        payload = data
    else:
        return {}
    if not isinstance(payload, dict):
        return {}
    return _normalize_ui_settings(payload)


def _save_ui_settings_to_disk(settings: dict[str, Any], path: Path, cipher) -> None:
    payload = _normalize_ui_settings(settings)
    if cipher is not None:
        token = cipher.encrypt_text(
            json.dumps(payload, sort_keys=True),
            aad={"purpose": "ui-settings", "path": str(path)},
        )
        to_write: dict[str, Any] = {"format": "mmo-ui-settings-v1", "encrypted": token}
    else:
        to_write = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_write, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _build_skill_draft_test_inputs(workflow_raw: dict[str, Any], provided: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    merged: dict[str, Any] = {str(k): v for k, v in dict(provided).items()}
    used_defaults = False
    try:
        scan_text = yaml.safe_dump(workflow_raw, sort_keys=False)
    except Exception:
        scan_text = json.dumps(workflow_raw, ensure_ascii=True)
    keys = set(re.findall(r"\$input\.([A-Za-z0-9_]+)", scan_text))
    for key in sorted(keys):
        if key in merged:
            continue
        used_defaults = True
        lowered = key.lower()
        if "url" in lowered:
            merged[key] = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status"
        elif "pattern" in lowered:
            merged[key] = ".*"
        elif "objective" in lowered:
            merged[key] = "Test objective for draft run."
        elif "topic" in lowered or "query" in lowered or "message" in lowered or "prompt" in lowered:
            merged[key] = "test"
        else:
            merged[key] = "test"
    return merged, used_defaults


def _is_network_resolution_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "name or service not known",
        "temporary failure in name resolution",
        "nodename nor servname provided",
        "failed to resolve",
        "urlopen error",
    )
    return any(marker in text for marker in markers)


def _load_runs_from_disk(path: Path, cipher) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and data.get("format") == "mmo-runs-v1" and isinstance(data.get("encrypted"), str):
        if cipher is None:
            return {}
        try:
            payload = json.loads(cipher.decrypt_text(str(data["encrypted"])))
        except Exception:
            return {}
    elif isinstance(data, dict):
        payload = data
    else:
        return {}
    rows = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id", "")).strip()
        if not run_id:
            continue
        out[run_id] = dict(item)
    return out


def _save_runs_to_disk(runs: dict[str, dict[str, Any]], path: Path, cipher) -> None:
    payload = {"runs": [dict(row) for row in runs.values()]}
    if cipher is not None:
        token = cipher.encrypt_text(
            json.dumps(payload, sort_keys=True),
            aad={"purpose": "runs", "path": str(path)},
        )
        to_write: dict[str, Any] = {"format": "mmo-runs-v1", "encrypted": token}
    else:
        to_write = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_write, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_sessions_state_from_disk(path: Path, cipher) -> tuple[dict[str, SessionManager], dict[str, str]]:
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    if isinstance(data, dict) and data.get("format") == "mmo-sessions-v1" and isinstance(data.get("encrypted"), str):
        if cipher is None:
            return {}, {}
        try:
            payload = json.loads(cipher.decrypt_text(str(data["encrypted"])))
        except Exception:
            return {}, {}
    elif isinstance(data, dict):
        payload = data
    else:
        return {}, {}
    raw_sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
    if not isinstance(raw_sessions, list):
        return {}, {}
    restored: dict[str, SessionManager] = {}
    restored_projects: dict[str, str] = {}
    for item in raw_sessions:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id", "")).strip()
        if not session_id:
            continue
        project_id = str(item.get("project_id", "")).strip() or "default"
        manager = SessionManager(max_context_tokens=8000)
        messages = item.get("messages", [])
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", "user"))
                content = str(msg.get("content", ""))
                metadata = msg.get("metadata")
                manager.add(role=role, content=content, metadata=metadata if isinstance(metadata, dict) else None)
        manager.trim()
        restored[session_id] = manager
        restored_projects[session_id] = project_id
    return restored, restored_projects


def _load_sessions_from_disk(path: Path, cipher) -> dict[str, SessionManager]:
    sessions, _ = _load_sessions_state_from_disk(path, cipher)
    return sessions


def _save_sessions_to_disk(
    sessions: dict[str, SessionManager],
    path: Path,
    cipher,
    *,
    session_projects: dict[str, str] | None = None,
) -> None:
    rows = []
    for session_id, manager in sessions.items():
        project_id = "default"
        if session_projects is not None:
            project_id = str(session_projects.get(session_id, "default")).strip() or "default"
        rows.append({"session_id": session_id, "project_id": project_id, "messages": manager.export()})
    payload = {"sessions": rows}
    if cipher is not None:
        token = cipher.encrypt_text(
            json.dumps(payload, sort_keys=True),
            aad={"purpose": "sessions", "path": str(path)},
        )
        to_write: dict[str, Any] = {"format": "mmo-sessions-v1", "encrypted": token}
    else:
        to_write = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_write, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _delegate_socket_path() -> Path:
    override = os.getenv("MMO_DELEGATE_SOCKET", "").strip()
    if override:
        return Path(override).expanduser()
    state_dir = os.getenv("MMO_STATE_DIR", "~/.mmo")
    return Path(state_dir).expanduser() / "delegate" / "run" / "delegate.sock"


async def _probe_delegate_socket(socket_path: Path) -> dict[str, Any]:
    if not socket_path.exists():
        return {
            "status": "missing",
            "reachable": False,
            "socket_path": str(socket_path),
            "detail": "socket file not found",
        }
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(socket_path)), timeout=0.75)
    except Exception as exc:
        return {
            "status": "unreachable",
            "reachable": False,
            "socket_path": str(socket_path),
            "detail": str(exc),
        }
    try:
        writer.write(b'{"op":"health"}\n')
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=1.0)
        if not raw:
            return {
                "status": "unhealthy",
                "reachable": False,
                "socket_path": str(socket_path),
                "detail": "empty response from delegate daemon",
            }
        parsed = json.loads(raw.decode("utf-8"))
        ok = bool(isinstance(parsed, dict) and parsed.get("ok") is True)
        data = parsed.get("data", {}) if isinstance(parsed, dict) else {}
        return {
            "status": "ok" if ok else "unhealthy",
            "reachable": ok,
            "socket_path": str(socket_path),
            "detail": "" if ok else str(parsed.get("error", "invalid response")) if isinstance(parsed, dict) else "invalid response",
            "daemon": data if isinstance(data, dict) else {},
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "reachable": False,
            "socket_path": str(socket_path),
            "detail": str(exc),
        }
    finally:
        writer.close()
        await writer.wait_closed()


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for idx, ch in enumerate(raw[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    if end < 0:
        return None
    candidate = raw[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _default_provider_catalog() -> dict[str, list[str]]:
    return {
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-5", "gpt-5-mini"],
        "anthropic": ["claude-3-5-sonnet-latest", "claude-3-7-sonnet-latest"],
        "google": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "xai": ["grok-3", "grok-3-mini"],
        "groq": ["llama-3.3-70b", "deepseek-r1-distill-llama-70b"],
        "local": ["ollama/qwen2.5", "ollama/llama3.1", "ollama/mistral"],
    }


def _normalize_model_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "/" in value:
        value = value.split("/", 1)[1].strip()
    return value


def _apply_assistant_profile(query: str, assistant_name: str, assistant_instructions: str, *, strict_profile: bool) -> str:
    name = assistant_name.strip()
    instructions = assistant_instructions.strip()
    if not name and not instructions:
        return query
    profile_lines = ["Assistant profile (follow these instructions exactly for this response):"]
    if name:
        profile_lines.append(f"- Name: {name}")
    if instructions:
        profile_lines.append(f"- Behavior: {instructions}")
    if strict_profile:
        profile_lines.append("- Strict mode: If format constraints are specified, comply exactly.")
    profile_block = "\n".join(profile_lines)
    return f"{profile_block}\n\nUser request:\n{query}"


def _profile_compliance_error(answer: str, instructions: str) -> str | None:
    text = instructions.strip().lower()
    if not text:
        return None
    if _looks_like_instruction_echo(answer):
        return "Response leaked instruction/meta text instead of final answer"
    if _looks_like_meta_response(answer):
        return "Response leaked meta commentary instead of final answer"
    expected = _expected_bullet_count(instructions)
    if expected is not None:
        bullets = [line for line in answer.splitlines() if line.strip().startswith(("-", "*"))]
        if len(bullets) != expected:
            return f"Expected exactly {expected} bullet lines, got {len(bullets)}"
    m = re.search(r'start first bullet with\s+"([^"]+)"', instructions, flags=re.IGNORECASE)
    if m:
        prefix = m.group(1)
        first_bullet = next((line.strip() for line in answer.splitlines() if line.strip().startswith(("-", "*"))), "")
        if not first_bullet:
            return "Missing first bullet line"
        cleaned = first_bullet[1:].lstrip()
        if not cleaned.startswith(prefix):
            return f'First bullet must start with "{prefix}"'
    return None


def _strict_retry_query(base_query: str, compliance_error: str) -> str:
    return (
        f"{base_query}\n\n"
        f"Strict profile compliance check failed: {compliance_error}.\n"
        "Regenerate the final answer and strictly satisfy the assistant profile format constraints."
    )


def _expected_bullet_count(instructions: str) -> int | None:
    text = instructions.lower()
    numeric = re.search(r"exactly\s+(\d+)\s+bullet", text)
    if numeric:
        return int(numeric.group(1))
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    word_match = re.search(r"exactly\s+(one|two|three|four|five|six|seven|eight|nine|ten)\s+bullet", text)
    if word_match:
        return words[word_match.group(1)]
    return None


def _has_profile_constraints(instructions: str) -> bool:
    if _expected_bullet_count(instructions) is not None:
        return True
    lowered = instructions.lower()
    return ("start first bullet with" in lowered) or ("start second bullet with" in lowered)


def _coerce_profile_output(answer: str, instructions: str) -> str:
    expected = _expected_bullet_count(instructions)
    if expected is None:
        return answer

    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    lines = [line for line in lines if not _is_instruction_echo_line(line)]
    lines = [line for line in lines if not _is_meta_line(line)]
    lines = [_normalize_meta_like_line(line) for line in lines]
    if not lines:
        lines = ["Hello! I am ready to help.", "Ready for your next request."]
    if len(lines) < expected:
        lines.extend([lines[-1]] * (expected - len(lines)))
    lines = lines[:expected]

    first_prefix_match = re.search(r'start first bullet with\s+"([^"]+)"', instructions, flags=re.IGNORECASE)
    second_prefix_match = re.search(r'start second bullet with\s+"([^"]+)"', instructions, flags=re.IGNORECASE)
    if first_prefix_match:
        prefix = first_prefix_match.group(1)
        lines[0] = _force_prefix(lines[0], prefix)
    if second_prefix_match and len(lines) > 1:
        prefix = second_prefix_match.group(1)
        lines[1] = _force_prefix(lines[1], prefix)

    if _looks_like_meta_response("\n".join(lines)):
        line_one = "CerbiBot: Hello! I'm ready to assist."
        line_two = "Status: Ready."
        if first_prefix_match:
            line_one = _force_prefix(line_one, first_prefix_match.group(1))
        if second_prefix_match:
            line_two = _force_prefix(line_two, second_prefix_match.group(1))
        lines = [line_one, line_two]

    return "\n".join(f"- {line}" for line in lines)


def _force_prefix(text: str, prefix: str) -> str:
    cleaned = text.lstrip("-* ").strip()
    if cleaned.startswith(prefix):
        return cleaned
    return f"{prefix} {cleaned}".strip()


def _looks_like_instruction_echo(answer: str) -> bool:
    lowered = answer.lower()
    markers = (
        "we are given a strict instruction",
        "the instruction says",
        "exactly 2 bullet points",
        "first bullet point",
        "second bullet point",
        "must answer in exactly",
    )
    return any(marker in lowered for marker in markers)


def _is_instruction_echo_line(line: str) -> bool:
    lowered = line.lower()
    markers = (
        "strict instruction",
        "the instruction says",
        "exactly 2 bullet points",
        "first bullet",
        "second bullet",
        "must answer",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_meta_response(answer: str) -> bool:
    lowered = answer.lower()
    markers = (
        "we are given",
        "we are to",
        "example",
        "example structure",
        "example:",
        "[greeting and confirmation]",
        "remember:",
        "however, note",
        "context says",
        "the context says",
        "the user request",
        "the user has previously asked",
        "assistant responded",
        "system told",
        "previous response",
        "strict profile",
        "assistant must",
        "the assistant must",
        "two bullet points",
        "must not leak",
        "we must not include",
        "meta text",
        "meta commentary",
        "instructions in the response",
        "status: steps:",
        "steps:",
    )
    return any(marker in lowered for marker in markers)


def _is_meta_line(line: str) -> bool:
    lowered = line.lower()
    markers = (
        "we are given",
        "we are to",
        "example",
        "example structure",
        "example:",
        "[greeting and confirmation]",
        "remember:",
        "however, note",
        "context says",
        "the context says",
        "the user request",
        "the user has previously asked",
        "assistant responded",
        "system told",
        "previous response",
        "strict profile",
        "assistant must",
        "the assistant must",
        "two bullet points",
        "must not leak",
        "we must not include",
        "meta text",
        "meta commentary",
        "instructions in the response",
        "steps:",
    )
    return any(marker in lowered for marker in markers)


def _normalize_meta_like_line(line: str) -> str:
    lowered = line.lower()
    if any(
        marker in lowered
        for marker in (
            "we are given",
            "we are to",
            "example",
            "example structure",
            "example:",
            "[greeting and confirmation]",
            "remember:",
            "however, note",
            "context says",
            "the context says",
            "the user request",
            "the user has previously asked",
            "assistant responded",
            "strict profile",
            "assistant must",
            "must not leak",
            "two bullet points",
            "instruction",
            "meta text",
            "meta commentary",
            "steps:",
        )
    ):
        if lowered.startswith("cerbibot:"):
            return "CerbiBot: Hello! I'm ready to assist."
        if lowered.startswith("status:") or re.match(r"^status:\s*\d+\.", lowered):
            return "Status: Ready."
    return line


def _strip_profile_echo_preamble(answer: str, *, assistant_name: str, assistant_instructions: str) -> str:
    if not str(answer or "").strip():
        return answer
    if not (assistant_name.strip() or assistant_instructions.strip()):
        return answer
    text = str(answer)
    patterns = (
        r"(?is)^\s*okay,\s*here(?:'|’)s your profile:\s*(?:\n+\s*name:\s*[^\n]+)?\s*\n+",
        r"(?is)^\s*here(?:'|’)s your profile:\s*(?:\n+\s*name:\s*[^\n]+)?\s*\n+",
        r"(?is)^\s*assistant profile:\s*(?:\n+\s*-?\s*name:\s*[^\n]+)?\s*\n+",
        r"(?is)^\s*assistant profile\s*\([^)]+\)\s*:\s*(?:\n+\s*-\s*name:\s*[^\n]+)?\s*\n+",
    )
    stripped = text
    for pattern in patterns:
        stripped = re.sub(pattern, "", stripped, count=1)
    if stripped.strip():
        return stripped.strip()
    return text
