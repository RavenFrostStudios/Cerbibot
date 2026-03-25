from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
_fv = tuple(int(part) for part in fastapi.__version__.split(".")[:2])
if _fv <= (0, 101):
    pytest.skip(
        "FastAPI dependency execution can hang with this stack; upgrade FastAPI/Starlette to run server API tests.",
        allow_module_level=True,
    )

from orchestrator.server import _load_server_api_key, create_app


@pytest.fixture(autouse=True)
def _isolate_server_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY_FILE", str(tmp_path / "server_api_key.txt"))
    monkeypatch.setenv("MMO_SESSIONS_FILE", str(tmp_path / "sessions_store.json"))
    monkeypatch.setenv("MMO_UI_SETTINGS_FILE", str(tmp_path / "ui_settings.json"))
    monkeypatch.setenv("MMO_RUNS_FILE", str(tmp_path / "runs_store.json"))
    monkeypatch.setenv("MMO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(tmp_path / "admin_auth.json"))


@dataclass
class _AskResult:
    answer: str
    mode: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost: float
    warnings: list[str] | None = None
    citations: list | None = None
    verification_notes: list | None = None
    tool_outputs: list | None = None
    pending_tool: dict | None = None


class _FakeBudgets:
    def remaining(self):
        return {"session": 1.0, "daily": 2.0, "monthly": 3.0}

    def state(self):
        return SimpleNamespace(session_spend=0.1, daily_spend=0.2, monthly_spend=0.3)

    def usage_totals(self):
        return {
            "daily_totals": {"cost": 0.2, "requests": 4, "providers": {"xai": {"requests": 4}}},
            "monthly_totals": {"cost": 0.3, "requests": 8, "providers": {"xai": {"requests": 8}}},
        }


class _FakeMemoryStore:
    def __init__(self):
        self._rows = []

    def list_records(self, limit=100, project_id: str = "default"):
        pid = str(project_id or "default")
        return [row for row in self._rows if str(getattr(row, "project_id", "default")) == pid][:limit]

    def add(self, **kwargs):
        new_id = len(self._rows) + 1
        project_id = str(kwargs.pop("project_id", "default") or "default")
        rec = SimpleNamespace(id=new_id, created_at="2026-02-10T00:00:00+00:00", **kwargs)
        setattr(rec, "project_id", project_id)
        self._rows = [rec, *self._rows]
        return new_id

    def find_duplicate_statement(self, statement: str, limit: int = 500, project_id: str = "default"):
        target = " ".join(statement.strip().lower().split())
        pid = str(project_id or "default")
        for row in self._rows[:limit]:
            if str(getattr(row, "project_id", "default")) != pid:
                continue
            candidate = " ".join(str(row.statement).strip().lower().split())
            if candidate == target:
                return row
        return None

    def delete(self, record_id: int, project_id: str = "default"):
        pid = str(project_id or "default")
        for idx, row in enumerate(self._rows):
            if int(row.id) == int(record_id) and str(getattr(row, "project_id", "default")) == pid:
                self._rows.pop(idx)
                return True
        return False

    def list_projects(self):
        return sorted({str(getattr(row, "project_id", "default")) for row in self._rows} or {"default"})


class _FakeMemoryGov:
    def evaluate_write(self, **kwargs):
        return SimpleNamespace(allowed=True, reason="ok", redacted_statement=kwargs["statement"])


class _FakeProviderAdapter:
    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float):
        _ = (prompt, model, max_tokens, temperature)
        return SimpleNamespace(text="OK")


class _FakeOrchestrator:
    def __init__(self):
        provider_cfg = {
            "openai": SimpleNamespace(
                enabled=True,
                api_key_env="OPENAI_API_KEY",
                models=SimpleNamespace(fast="gpt-4o-mini", deep="gpt-4o"),
            )
        }
        self.config = SimpleNamespace(
            server=SimpleNamespace(api_key_env="MMO_SERVER_API_KEY", cors_origins=[]),
            providers=provider_cfg,
        )
        self.providers = {"openai": _FakeProviderAdapter()}
        self.budgets = _FakeBudgets()
        self.memory_store = _FakeMemoryStore()
        self.memory_governance = _FakeMemoryGov()
        self.artifacts = SimpleNamespace(
            list_summaries=lambda limit=50: [
                SimpleNamespace(
                    request_id="req-1",
                    started_at="2026-02-10T00:00:00Z",
                    mode="single",
                    query_preview="hello",
                    cost=0.01,
                    path="/tmp/req-1.json",
                )
            ],
            load=lambda request_id: {
                "artifact": {
                    "request_id": request_id,
                    "started_at": "2026-02-10T00:00:00Z",
                    "query": "hello",
                    "mode": "single",
                    "result": {"answer": "hi", "mode": "single", "cost": 0.01, "warnings": []},
                },
                "meta": {"integrity_hash": "x"},
            },
        )
        self.runtime_rebuilds = 0
        self.last_query = ""
        self.last_ask_kwargs: dict[str, object] = {}
        self.ask_calls = 0
        self.force_noncompliant_retry = False
        self.force_instruction_echo_retry = False
        self.force_meta_echo_retry = False
        self.force_followup_meta_echo = False
        self.force_we_are_to_meta_echo = False
        self.force_remember_meta_echo = False
        self.force_example_meta_echo = False
        self.routing_roles = {
            "critique": {
                "drafter_provider": "openai",
                "critic_provider": "openai",
                "refiner_provider": "openai",
            },
            "debate": {
                "debater_a_provider": "openai",
                "debater_b_provider": "openai",
                "judge_provider": "openai",
                "synthesizer_provider": "openai",
            },
            "consensus": {"adjudicator_provider": "openai"},
            "council": {
                "specialist_roles": {"coding": "openai", "security": "openai", "writing": "", "factual": ""},
                "synthesizer_provider": "openai",
            },
        }

    def apply_provider_overrides(self, overrides):
        updated = []
        skipped = []
        for item in overrides:
            name = str(item.get("name", ""))
            if name not in self.config.providers:
                skipped.append({"name": name, "reason": "unknown provider"})
                continue
            cfg = self.config.providers[name]
            cfg.enabled = bool(item.get("enabled", cfg.enabled))
            model = str(item.get("model", "")).strip()
            if model:
                cfg.models.fast = model
                cfg.models.deep = model
            updated.append({"name": name, "enabled": cfg.enabled, "model": cfg.models.deep})
        return {"updated": updated, "skipped": skipped}

    def _rebuild_provider_runtime(self):
        self.runtime_rebuilds += 1

    def get_role_routes(self):
        return self.routing_roles

    def apply_role_routes(self, routes):
        critique = routes.get("critique", {}) if isinstance(routes, dict) else {}
        if critique.get("drafter_provider") == "missing":
            raise ValueError("critique.drafter_provider references unknown provider 'missing'")
        self.routing_roles = routes
        return self.routing_roles

    async def ask(self, **kwargs):
        self.ask_calls += 1
        self.last_ask_kwargs = dict(kwargs)
        self.last_query = str(kwargs.get("query", ""))
        if "MEMORY_EXTRACT_V1" in self.last_query:
            return _AskResult(
                answer=json.dumps(
                    {
                        "save": True,
                        "statement": "User codename is RAVEN-73-LIME.",
                        "source_type": "chat_inferred",
                        "confidence": 0.91,
                        "reason": "Stable user-provided profile fact",
                    }
                ),
                mode=kwargs.get("mode") or "single",
                provider="openai",
                model="m",
                tokens_in=1,
                tokens_out=2,
                cost=0.01,
                warnings=[],
                citations=[],
                verification_notes=[],
                tool_outputs=[],
            )
        if "Strict profile compliance check failed" in self.last_query:
            if self.force_example_meta_echo:
                return _AskResult(
                    answer=(
                        "CerbiBot: Example structure:\n"
                        "Status: CerbiBot: [greeting and confirmation]"
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_remember_meta_echo:
                return _AskResult(
                    answer=(
                        "CerbiBot: Remember:\n"
                        "Status: However, note the context says:"
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_we_are_to_meta_echo:
                return _AskResult(
                    answer=(
                        "CerbiBot: We are to greet briefly and confirm readiness.\n"
                        "Status: 1. We are to create two bullet points."
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_followup_meta_echo:
                return _AskResult(
                    answer=(
                        "CerbiBot: We are given a strict profile for the assistant: CerbiBot\n"
                        "Status: The assistant must:"
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_meta_echo_retry:
                return _AskResult(
                    answer=(
                        'CerbiBot: The user request: "Greet me briefly and confirm readiness."\n'
                        "Status: We must not include any meta text or instructions in the response."
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_instruction_echo_retry:
                return _AskResult(
                    answer=(
                        "We are given a strict instruction: exactly 2 bullet points.\n"
                        "First bullet point must start with CerbiBot.\n"
                        "Second bullet point must start with Status."
                    ),
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            if self.force_noncompliant_retry:
                return _AskResult(
                    answer="still not in bullet format",
                    mode=kwargs.get("mode") or "single",
                    provider="openai",
                    model="m",
                    tokens_in=1,
                    tokens_out=2,
                    cost=0.01,
                    warnings=[],
                    citations=[],
                    verification_notes=[],
                    tool_outputs=[],
                )
            return _AskResult(
                answer="- CerbiBot: compliant line one\n- compliant line two",
                mode=kwargs.get("mode") or "single",
                provider="openai",
                model="m",
                tokens_in=1,
                tokens_out=2,
                cost=0.01,
                warnings=[],
                citations=[],
                verification_notes=[],
                tool_outputs=[],
            )
        return _AskResult(
            answer="hello",
            mode=kwargs.get("mode") or "single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=[],
            citations=[],
            verification_notes=[],
            tool_outputs=[],
        )

    async def ask_stream(self, **kwargs):
        yield SimpleNamespace(type="chunk", text="he", result=None)
        yield SimpleNamespace(type="chunk", text="llo", result=None)
        yield SimpleNamespace(type="result", text=None, result=await self.ask(**kwargs))


class _QuotaErrorOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        raise RuntimeError("Google completion failed after retries: 429 RESOURCE_EXHAUSTED")


class _TimeoutErrorOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        raise RuntimeError("Local call timed out after 2 attempts (timeout=60s)")


class _ProfileEchoOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        return _AskResult(
            answer=(
                "Okay, here's your profile:\n\n"
                "Name: CerbiBot\n\n"
                "Use Hugging Face: https://huggingface.co/models"
            ),
            mode="single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=[],
            citations=[],
            verification_notes=[],
            tool_outputs=[],
        )


class _AssistantProfileEchoOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        return _AskResult(
            answer=(
                "Assistant profile:\n\n"
                "Name: CerbiBot\n"
                "Okay, I can definitely help you with that!\n\n"
                "Use Hugging Face Hub: https://huggingface.co/models"
            ),
            mode="single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=[],
            citations=[],
            verification_notes=[],
            tool_outputs=[],
        )


class _AssistantProfileBulletEchoOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        return _AskResult(
            answer=(
                "Assistant profile (follow these instructions exactly for this response):\n"
                "- Name: CerbiBot\n\n"
                "Okay, I can definitely help you with that!\n"
                "Use Hugging Face Hub: https://huggingface.co/models"
            ),
            mode="single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=[],
            citations=[],
            verification_notes=[],
            tool_outputs=[],
        )


class _FailedToolOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        return _AskResult(
            answer="I could not complete the requested tool step.",
            mode="single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=["tool execution failed"],
            citations=[],
            verification_notes=[],
            tool_outputs=[{"tool": "python_exec", "status": "ok", "exit_code": 1, "stderr": "boom"}],
        )


class _PendingToolOrchestrator(_FakeOrchestrator):
    async def ask(self, **kwargs):
        _ = kwargs
        return _AskResult(
            answer="Tool approval required before execution.",
            mode="single",
            provider="openai",
            model="m",
            tokens_in=1,
            tokens_out=2,
            cost=0.01,
            warnings=[],
            citations=[],
            verification_notes=[],
            tool_outputs=[],
            pending_tool={
                "approval_id": "approval-1",
                "tool_name": "web_retrieve",
                "arguments": {"url": "https://example.com"},
                "risk_level": "high",
                "status": "pending",
            },
        )


@pytest.mark.asyncio
async def test_server_health_and_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/v1/health", headers=headers)
        assert health.status_code == 200
        ask = await client.post("/v1/ask", headers=headers, json={"query": "hello", "stream": False})
        assert ask.status_code == 200
        assert ask.json()["answer"] == "hello"


@pytest.mark.asyncio
async def test_server_delegate_health_reports_missing_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    missing_socket = tmp_path / "nope" / "delegate.sock"
    monkeypatch.setenv("MMO_DELEGATE_SOCKET", str(missing_socket))
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/server/delegate/health", headers=headers)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "missing"
        assert payload["reachable"] is False
        assert payload["socket_path"] == str(missing_socket)


@pytest.mark.asyncio
async def test_server_maps_quota_errors_to_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_QuotaErrorOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ask = await client.post("/v1/ask", headers=headers, json={"query": "hello", "stream": False})
        assert ask.status_code == 429
        chat = await client.post("/v1/chat", headers=headers, json={"session_id": "s1", "message": "hello"})
        assert chat.status_code == 429


@pytest.mark.asyncio
async def test_chat_strips_profile_echo_preamble_in_non_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_ProfileEchoOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-profile-echo",
                "message": "Where can I find free models?",
                "assistant_name": "CerbiBot",
            },
        )
        assert chat.status_code == 200
        answer = chat.json()["result"]["answer"]
        assert "here's your profile" not in answer.lower()
        assert "name: cerbibot" not in answer.lower()
        assert "huggingface.co/models" in answer


@pytest.mark.asyncio
async def test_chat_strips_assistant_profile_echo_preamble_in_non_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_AssistantProfileEchoOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-assistant-profile-echo",
                "message": "Where can I find free models?",
                "assistant_name": "CerbiBot",
            },
        )
        assert chat.status_code == 200
        answer = chat.json()["result"]["answer"]
        assert "assistant profile:" not in answer.lower()
        assert "name: cerbibot" not in answer.lower()
        assert "huggingface.co/models" in answer


@pytest.mark.asyncio
async def test_chat_strips_assistant_profile_bullet_echo_in_non_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_AssistantProfileBulletEchoOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-assistant-profile-bullet-echo",
                "message": "Where can I find free models?",
                "assistant_name": "CerbiBot",
            },
        )
        assert chat.status_code == 200
        answer = chat.json()["result"]["answer"]
        assert "assistant profile" not in answer.lower()
        assert "name: cerbibot" not in answer.lower()
        assert "huggingface.co/models" in answer


@pytest.mark.asyncio
async def test_server_run_marked_failed_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_TimeoutErrorOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-timeout", "run_id": "run-timeout", "message": "hello"},
        )
        assert chat.status_code == 504
        run = await client.get("/v1/runs/run-timeout", headers=headers)
        assert run.status_code == 200
        payload = run.json()["run"]
        assert payload["status"] == "failed"
        assert payload.get("checkpoint", {}).get("stage") == "failed_timeout"


@pytest.mark.asyncio
async def test_server_memory_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/v1/memory", headers=headers, json={"statement": "remember this"})
        assert resp.status_code == 200
        assert resp.json()["id"] == 1


@pytest.mark.asyncio
async def test_server_memory_suggest_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-memory-suggest", "message": "Remember codename RAVEN-73-LIME."},
        )
        assert created.status_code == 200
        suggest = await client.post(
            "/v1/memory/suggest",
            headers=headers,
            json={"session_id": "s-memory-suggest"},
        )
        assert suggest.status_code == 200
        payload = suggest.json()
        assert payload["suggested"] is True
        candidate = payload["candidate"]
        assert "RAVEN-73-LIME" in candidate["statement"]
        assert candidate["source_type"] == "chat_inferred"
        assert fake.last_ask_kwargs.get("mode") == "single"
        assert fake.last_ask_kwargs.get("web_assist_mode") == "off"


@pytest.mark.asyncio
async def test_server_memory_suggest_dedupes_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-memory-dupe", "message": "Remember codename RAVEN-73-LIME."},
        )
        assert created.status_code == 200
        suggest = await client.post(
            "/v1/memory/suggest",
            headers=headers,
            json={"session_id": "s-memory-dupe"},
        )
        assert suggest.status_code == 200
        candidate = suggest.json()["candidate"]
        added = await client.post(
            "/v1/memory",
            headers=headers,
            json={"statement": candidate["statement"], "source_type": candidate["source_type"]},
        )
        assert added.status_code == 200

        again = await client.post(
            "/v1/memory/suggest",
            headers=headers,
            json={"session_id": "s-memory-dupe"},
        )
        assert again.status_code == 200
        payload = again.json()
        assert payload["suggested"] is False
        assert payload["reason"] == "already_stored"
        assert int(payload["existing_id"]) >= 1


@pytest.mark.asyncio
async def test_server_memory_add_dedupes_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        one = await client.post("/v1/memory", headers=headers, json={"statement": "Project codename is RAVEN-73-LIME"})
        assert one.status_code == 200
        two = await client.post(
            "/v1/memory",
            headers=headers,
            json={"statement": "  project  codename   is   raven-73-lime "},
        )
        assert two.status_code == 200
        payload = two.json()
        assert payload["duplicate"] is True
        assert int(payload["id"]) == int(one.json()["id"])


@pytest.mark.asyncio
async def test_server_memory_delete_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post("/v1/memory", headers=headers, json={"statement": "remove me"})
        assert created.status_code == 200
        memory_id = int(created.json()["id"])
        resp = await client.delete(f"/v1/memory/{memory_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True


@pytest.mark.asyncio
async def test_server_cost_endpoint_includes_request_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/cost", headers=headers)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["state"]["daily_spend"] == pytest.approx(0.2)
        assert payload["totals"]["daily_totals"]["requests"] == 4
        assert payload["totals"]["monthly_totals"]["requests"] == 8


@pytest.mark.asyncio
async def test_server_artifacts_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get("/v1/artifacts", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["artifacts"][0]["id"] == "req-1"
        run = await client.get("/v1/artifacts/req-1", headers=headers)
        assert run.status_code == 200
        assert run.json()["run"]["id"] == "req-1"
        enc = await client.get("/v1/artifacts/encryption/status", headers=headers)
        assert enc.status_code == 200
        assert "enabled" in enc.json()
        single_export = await client.post("/v1/artifacts/req-1/export", headers=headers, json={})
        assert single_export.status_code == 200
        assert "artifact" in single_export.json()
        all_export = await client.post("/v1/artifacts/export-all", headers=headers, json={"limit": 10})
        assert all_export.status_code == 200
        assert all_export.json()["count"] >= 1


@pytest.mark.asyncio
async def test_artifact_export_requires_admin_password_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(tmp_path / "admin_auth.json"))
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        blocked = await client.post(
            "/v1/artifacts/req-1/export",
            headers=headers,
            json={"admin_password": "wrong"},
        )
        assert blocked.status_code == 401

        allowed = await client.post(
            "/v1/artifacts/req-1/export",
            headers=headers,
            json={"admin_password": "LongerPassword123"},
        )
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_server_skills_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)

    def _discover():
        return {"s1": SimpleNamespace(name="s1", path="/tmp/s1.yaml", enabled=True, checksum="sha256:abc", signature_verified=True)}

    def _set(name: str, enabled: bool):
        return SimpleNamespace(name=name, enabled=enabled)

    def _delete(name: str):
        if name != "s1":
            raise ValueError("Skill not found")
        return True

    def _catalog(discovered):
        _ = discovered
        return [
            {
                "id": "repo_health_check",
                "title": "Repo Health Check",
                "description": "d",
                "trust": "mmy-curated",
                "tested": "smoke",
                "risk_level": "low",
                "workflow_text": "name: repo_health_check",
                "installed": False,
                "enabled": False,
                "signature_verified": False,
                "checksum": "",
            }
        ]

    monkeypatch.setattr("orchestrator.skills.registry.discover_skills", _discover)
    monkeypatch.setattr("orchestrator.skills.catalog.curated_skill_catalog", _catalog)
    monkeypatch.setattr("orchestrator.skills.registry.set_skill_enabled", _set)
    monkeypatch.setattr("orchestrator.skills.registry.delete_skill", _delete)
    monkeypatch.setattr("yaml.safe_load", lambda *_args, **_kwargs: {"description": "d", "risk_level": "low"})
    monkeypatch.setattr("pathlib.Path.read_text", lambda *_args, **_kwargs: "name: s1")

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get("/v1/skills", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["skills"][0]["name"] == "s1"
        catalog = await client.get("/v1/skills/catalog", headers=headers)
        assert catalog.status_code == 200
        assert catalog.json()["catalog"][0]["id"] == "repo_health_check"
        exported = await client.get("/v1/skills/s1/export", headers=headers)
        assert exported.status_code == 200
        assert exported.json()["skill"]["name"] == "s1"
        assert "name: s1" in exported.json()["skill"]["workflow_text"]
        en = await client.post("/v1/skills/s1/enable", headers=headers)
        assert en.status_code == 200
        assert en.json()["enabled"] is True
        dis = await client.post("/v1/skills/s1/disable", headers=headers)
        assert dis.status_code == 200
        assert dis.json()["enabled"] is False
        delete = await client.delete("/v1/skills/s1", headers=headers)
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True


@pytest.mark.asyncio
async def test_server_skills_test_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)

    skill_path = tmp_path / "demo.workflow.yaml"
    skill_path.write_text("name: demo\nsteps: []\n", encoding="utf-8")

    def _discover():
        return {
            "demo": SimpleNamespace(
                name="demo",
                path=str(skill_path),
                enabled=True,
                checksum="sha256:test",
                signature_verified=True,
            )
        }

    async def _run_skill(_orch, **_kwargs):
        return SimpleNamespace(skill_name="demo", outputs={"out": "ok"}, steps_executed=1, total_cost=0.0)

    async def _run_adv(_orch, **_kwargs):
        return {"total": 1, "passed": 1, "failed": 0, "results": []}

    monkeypatch.setattr("orchestrator.skills.registry.discover_skills", _discover)
    monkeypatch.setattr("orchestrator.skills.registry.validate_workflow_file", lambda _path: (True, [], {}))
    monkeypatch.setattr("orchestrator.skills.workflow.run_workflow_skill", _run_skill)
    monkeypatch.setattr("orchestrator.skills.testing.run_skill_adversarial_tests", _run_adv)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/skills/demo/test",
            headers=headers,
            json={"run": True, "adversarial": True, "fixtures_path": str(tmp_path / "fixtures.yaml")},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["validation"]["valid"] is True
        assert payload["run"]["skill_name"] == "demo"
        assert payload["adversarial"]["passed"] == 1


@pytest.mark.asyncio
async def test_server_skills_governance_analyze_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        out_dir = tmp_path / "gov"
        resp = await client.post(
            "/v1/skills/governance/analyze",
            headers=headers,
            json={"out_dir": str(out_dir), "include_disabled": False, "limit": 10},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert "summary" in payload
        assert "skills_analyzed" in payload["summary"]
        assert payload["artifacts"]["out_dir"] == str(out_dir)
        assert Path(payload["artifacts"]["merge_candidates_path"]).exists()
        assert Path(payload["artifacts"]["crossover_candidates_path"]).exists()
        assert Path(payload["artifacts"]["skills_bloat_report_path"]).exists()
        assert Path(payload["artifacts"]["deprecation_plan_path"]).exists()


@pytest.mark.asyncio
async def test_server_doctor_includes_governance_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/server/doctor",
            headers=headers,
            json={"governance": True, "governance_out_dir": str(tmp_path / "gov-doctor")},
        )
        assert resp.status_code == 200
        payload = resp.json()
        checks = payload.get("checks", [])
        gov = next((item for item in checks if item.get("name") == "skills:governance"), None)
        assert gov is not None
        assert gov.get("status") in {"PASS", "FAIL"}


@pytest.mark.asyncio
async def test_server_skills_draft_validate_and_save(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    workflow_text = """
name: repo_health_check
description: Run quick local checks
risk_level: low
manifest:
  purpose: Validate repository health
  tools: [python_eval]
  data_scope: [repo_metadata]
  permissions: [read_repo]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 20
  budgets:
    usd_cap: 1
  kill_switch:
    enabled: true
  audit_sink: server.audit
  failure_mode: fail_closed
steps:
  - id: step1
    tool: python_eval
    args:
      code: "print('ok')"
""".strip()
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        validate = await client.post(
            "/v1/skills/draft/validate",
            headers=headers,
            json={"workflow_text": workflow_text},
        )
        assert validate.status_code == 200
        v_payload = validate.json()
        assert v_payload["valid"] is True
        assert v_payload["name"] == "repo_health_check"

        save = await client.post(
            "/v1/skills/draft/save",
            headers=headers,
            json={"workflow_text": workflow_text},
        )
        assert save.status_code == 200
        s_payload = save.json()
        assert s_payload["saved"] is True
        assert s_payload["name"] == "repo_health_check"
        assert s_payload["enabled"] is False

        listing = await client.get("/v1/skills", headers=headers)
        assert listing.status_code == 200
        skills = listing.json()["skills"]
        match = next((row for row in skills if row["name"] == "repo_health_check"), None)
        assert match is not None
        assert match["enabled"] is False


@pytest.mark.asyncio
async def test_server_skills_draft_test_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    workflow_text = """
name: draft_echo
description: draft skill test
risk_level: low
manifest:
  purpose: Echo a simple value
  tools: [python_eval]
  data_scope: [repo_metadata]
  permissions: [read_repo]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 20
  budgets:
    usd_cap: 1
  kill_switch:
    enabled: true
  audit_sink: server.audit
  failure_mode: fail_closed
steps:
  - id: step1
    tool: python_eval
    args:
      code: "print('ok')"
""".strip()

    async def _run_skill(_orch, **_kwargs):
        return SimpleNamespace(skill_name="draft_echo", outputs={"result": "ok"}, steps_executed=1, total_cost=0.0)

    monkeypatch.setattr("orchestrator.skills.workflow.run_workflow_skill", _run_skill)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/skill-drafts/test",
            headers=headers,
            json={"workflow_text": workflow_text, "run": True},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["validation"]["valid"] is True
        assert payload["run"]["skill_name"] == "draft_echo"


@pytest.mark.asyncio
async def test_server_skills_import_bundle_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    workflow_text = """
name: import_demo_skill
description: import demo
risk_level: low
manifest:
  purpose: Import test
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
  - id: step1
    tool: system_info
    args: {}
""".strip()
    bundle = {
        "exported_at": "2026-02-14T00:00:00Z",
        "skill": {"name": "import_demo_skill", "enabled": True},
        "workflow_text": workflow_text,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        imported = await client.post("/v1/skills/import", headers=headers, json=bundle)
        assert imported.status_code == 200
        payload = imported.json()
        assert payload["imported"] is True
        assert payload["name"] == "import_demo_skill"
        assert payload["enabled"] is False


@pytest.mark.asyncio
async def test_server_tools_simulate_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/tools/simulate",
            headers=headers,
            json={"tool_name": "web_retrieve", "args": {"url": "https://example.com"}},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["tool_name"] == "web_retrieve"
        assert payload["result"]["tool"] == "web_retrieve"


@pytest.mark.asyncio
async def test_server_session_detail_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s1", "message": "hello", "tools": True},
        )
        assert created.status_code == 200
        detail = await client.get("/v1/sessions/s1", headers=headers)
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["session_id"] == "s1"
        assert payload["title"] == "hello"
        assert len(payload["messages"]) == 2
        assistant = payload["messages"][1]
        assert assistant["role"] == "assistant"
        assert "metadata" in assistant
        assert isinstance(assistant["metadata"]["warnings"], list)
        assert "shared_state" in assistant["metadata"]


@pytest.mark.asyncio
async def test_chat_result_status_marks_failed_tool_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FailedToolOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-tool-fail", "message": "run the tool"},
        )
        assert chat.status_code == 200
        payload = chat.json()
        assert payload["result"]["status"] == "failed"

        detail = await client.get("/v1/sessions/s-tool-fail", headers=headers)
        assert detail.status_code == 200
        assistant = detail.json()["messages"][1]
        assert assistant["metadata"]["status"] == "failed"


@pytest.mark.asyncio
async def test_chat_result_status_marks_pending_tool_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_PendingToolOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        chat = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-tool-pending", "message": "use web search"},
        )
        assert chat.status_code == 200
        payload = chat.json()
        assert payload["result"]["status"] == "pending"
        assert payload["result"]["pending_tool"]["status"] == "pending"

        detail = await client.get("/v1/sessions/s-tool-pending", headers=headers)
        assert detail.status_code == 200
        assistant = detail.json()["messages"][1]
        assert assistant["metadata"]["status"] == "pending"


@pytest.mark.asyncio
async def test_remote_access_plan_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        initial = await client.get("/v1/server/remote-access/status", headers=headers)
        assert initial.status_code == 200
        assert initial.json()["enabled"] is False

        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        configured = await client.post(
            "/v1/server/remote-access/configure",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "mode": "cloudflare",
                "bind_host": "127.0.0.1",
                "bind_port": 8100,
                "public_base_url": "https://assistant.example.com",
                "notes": "Use edge access policy.",
            },
        )
        assert configured.status_code == 200
        payload = configured.json()
        assert payload["enabled"] is True
        assert payload["profile"]["mode"] == "cloudflare"
        assert "mmctl serve --host 127.0.0.1 --port 8100" in payload["launch_command"]

        revoked = await client.post(
            "/v1/server/remote-access/revoke",
            headers=headers,
            json={"admin_password": "LongerPassword123"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["enabled"] is False


@pytest.mark.asyncio
async def test_remote_access_plan_requires_admin_password_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(tmp_path / "admin_auth_remote.json"))
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        bad = await client.post(
            "/v1/server/remote-access/configure",
            headers=headers,
            json={
                "admin_password": "wrong",
                "mode": "lan",
                "bind_host": "0.0.0.0",
                "bind_port": 8100,
            },
        )
        assert bad.status_code == 401

        ok = await client.post(
            "/v1/server/remote-access/configure",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "mode": "lan",
                "bind_host": "0.0.0.0",
                "bind_port": 8100,
            },
        )
        assert ok.status_code == 200
        assert ok.json()["enabled"] is True


@pytest.mark.asyncio
async def test_remote_access_plan_rejects_unsafe_bind_for_cloudflare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        bad = await client.post(
            "/v1/server/remote-access/configure",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "mode": "cloudflare",
                "bind_host": "0.0.0.0",
                "bind_port": 8100,
                "public_base_url": "https://assistant.example.com",
            },
        )
        assert bad.status_code == 400
        assert "127.0.0.1" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_remote_access_health_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200
        configured = await client.post(
            "/v1/server/remote-access/configure",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "mode": "lan",
                "bind_host": "127.0.0.1",
                "bind_port": 8100,
            },
        )
        assert configured.status_code == 200

        report = await client.post("/v1/server/remote-access/health", headers=headers)
        assert report.status_code == 200
        payload = report.json()
        assert payload["summary"]["total"] == 2
        assert payload["checks"][0]["name"] == "bind_target"
        assert payload["checks"][1]["name"] == "public_url"
        assert "remediation" in payload["checks"][0]


@pytest.mark.asyncio
async def test_run_trigger_lifecycle_and_fire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        saved = await client.put(
            "/v1/server/run-triggers",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "triggers": [
                    {
                        "name": "Nightly summary",
                        "project_id": "default",
                        "mode": "single",
                        "message": "Summarize the latest queue state.",
                    }
                ],
            },
        )
        assert saved.status_code == 200
        trigger = saved.json()["triggers"][0]
        assert trigger["trigger_id"]
        assert trigger["webhook_url"].endswith(trigger["webhook_path"])

        fired = await client.post(
            f"/v1/hooks/run/{trigger['trigger_id']}/{trigger['secret']}",
            json={"source": "test"},
        )
        assert fired.status_code == 200
        payload = fired.json()
        assert payload["ok"] is True
        assert str(payload["run_id"]).startswith("run-trigger-")

        listed = await client.get("/v1/server/run-triggers", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["triggers"][0]["last_run_id"] == payload["run_id"]


@pytest.mark.asyncio
async def test_run_trigger_rejects_bad_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200
        saved = await client.put(
            "/v1/server/run-triggers",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "triggers": [{"name": "Trigger", "message": "hello"}],
            },
        )
        assert saved.status_code == 200
        trigger_id = saved.json()["triggers"][0]["trigger_id"]
        bad = await client.post(f"/v1/hooks/run/{trigger_id}/wrong-secret", json={})
        assert bad.status_code == 403


@pytest.mark.asyncio
async def test_run_trigger_sweep_fires_due_interval_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200
        saved = await client.put(
            "/v1/server/run-triggers",
            headers=headers,
            json={
                "admin_password": "LongerPassword123",
                "triggers": [
                    {
                        "name": "Interval trigger",
                        "message": "Run on interval.",
                        "interval_minutes": 5,
                        "next_run_at": "2000-01-01T00:00:00+00:00",
                    }
                ],
            },
        )
        assert saved.status_code == 200

        swept = await client.post("/v1/server/run-triggers/sweep", headers=headers, json={})
        assert swept.status_code == 200
        payload = swept.json()
        assert payload["due"] == 1
        assert payload["fired"] == 1

        listed = await client.get("/v1/server/run-triggers", headers=headers)
        assert listed.status_code == 200
        trigger = listed.json()["triggers"][0]
        assert str(trigger["last_run_id"]).startswith("run-trigger-")
        assert trigger["next_run_at"]


@pytest.mark.asyncio
async def test_server_sessions_are_project_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-proj", "project_id": "alpha", "message": "hello"},
        )
        assert created.status_code == 200
        assert created.json()["project_id"] == "alpha"

        alpha = await client.get("/v1/sessions?project_id=alpha", headers=headers)
        assert alpha.status_code == 200
        alpha_ids = [row["session_id"] for row in alpha.json()["sessions"]]
        assert "s-proj" in alpha_ids

        default = await client.get("/v1/sessions?project_id=default", headers=headers)
        assert default.status_code == 200
        default_ids = [row["session_id"] for row in default.json()["sessions"]]
        assert "s-proj" not in default_ids

        mismatch = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-proj", "project_id": "beta", "message": "still hello"},
        )
        assert mismatch.status_code == 409


@pytest.mark.asyncio
async def test_server_session_dag_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-dag", "message": "hello", "mode": "single"},
        )
        assert created.status_code == 200

        dag = await client.get("/v1/sessions/s-dag/dag", headers=headers)
        assert dag.status_code == 200
        payload = dag.json()
        assert payload["session_id"] == "s-dag"
        assert "dag" in payload
        assert isinstance(payload["dag"]["nodes"], list)
        assert len(payload["dag"]["nodes"]) >= 1


@pytest.mark.asyncio
async def test_server_memory_and_projects_are_project_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        one = await client.post(
            "/v1/memory",
            headers=headers,
            json={"project_id": "alpha", "statement": "alpha memory"},
        )
        assert one.status_code == 200
        two = await client.post(
            "/v1/memory",
            headers=headers,
            json={"project_id": "beta", "statement": "beta memory"},
        )
        assert two.status_code == 200

        alpha = await client.get("/v1/memory?project_id=alpha", headers=headers)
        assert alpha.status_code == 200
        assert any(row["statement"] == "alpha memory" for row in alpha.json()["memories"])
        assert all(row["statement"] != "beta memory" for row in alpha.json()["memories"])

        projects = await client.get("/v1/projects", headers=headers)
        assert projects.status_code == 200
        ids = [row["project_id"] for row in projects.json()["projects"]]
        assert "alpha" in ids
        assert "beta" in ids

@pytest.mark.asyncio
async def test_server_run_checkpoint_and_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-run", "message": "checkpoint me"},
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]
        assert run_id

        loaded = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert loaded.status_code == 200
        run = loaded.json()["run"]
        assert run["status"] == "completed"
        assert run["endpoint"] == "chat"
        assert run["session_id"] == "s-run"

        hb = await client.post(
            f"/v1/runs/{run_id}/heartbeat",
            headers=headers,
            json={"stage": "waiting_approval", "note": "waiting", "progress": 0.5},
        )
        assert hb.status_code == 200
        assert hb.json()["run"]["checkpoint"]["stage"] == "waiting_approval"
        assert hb.json()["run"]["heartbeat_count"] >= 1
        assert hb.json()["run"]["last_heartbeat_at"]
        assert hb.json()["run"]["status"] == "running"

        listing = await client.get("/v1/runs?status=running", headers=headers)
        assert listing.status_code == 200
        assert any(row["run_id"] == run_id for row in listing.json()["runs"])


@pytest.mark.asyncio
async def test_server_run_heartbeat_marks_blocked_when_open_blockers_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-run-blocked", "message": "block me"},
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]

        hb = await client.post(
            f"/v1/runs/{run_id}/heartbeat",
            headers=headers,
            json={
                "status": "running",
                "stage": "waiting_dependency",
                "blockers": [{"code": "dep_missing", "message": "Need dependency", "status": "open"}],
            },
        )
        assert hb.status_code == 200
        payload = hb.json()["run"]
        assert payload["status"] == "blocked"
        assert payload["checkpoint"]["stage"] == "waiting_dependency"
        assert payload["checkpoint"]["blockers"][0]["code"] == "dep_missing"
        assert payload["heartbeat_count"] >= 1


@pytest.mark.asyncio
async def test_server_run_resume_replays_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ask = await client.post("/v1/ask", headers=headers, json={"query": "resume this", "stream": False})
        assert ask.status_code == 200
        run_id = ask.json()["run_id"]

        resumed = await client.post(f"/v1/runs/{run_id}/resume", headers=headers)
        assert resumed.status_code == 200
        payload = resumed.json()
        assert payload["run"]["resume_count"] >= 1
        assert payload["resume"]["answer"] == "hello"


@pytest.mark.asyncio
async def test_run_resume_rejects_when_still_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ask = await client.post("/v1/ask", headers=headers, json={"query": "active run", "stream": False})
        assert ask.status_code == 200
        run_id = ask.json()["run_id"]
        hb = await client.post(
            f"/v1/runs/{run_id}/heartbeat",
            headers=headers,
            json={"status": "running", "stage": "working"},
        )
        assert hb.status_code == 200
        resumed = await client.post(f"/v1/runs/{run_id}/resume", headers=headers)
        assert resumed.status_code == 409
        assert "still active" in resumed.json()["detail"].lower()


@pytest.mark.asyncio
async def test_run_stalled_filter_and_stale_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_RUN_STALE_SECONDS", "1")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ask = await client.post("/v1/ask", headers=headers, json={"query": "stale run", "stream": False})
        assert ask.status_code == 200
        run_id = ask.json()["run_id"]
        hb = await client.post(
            f"/v1/runs/{run_id}/heartbeat",
            headers=headers,
            json={"status": "running", "stage": "working"},
        )
        assert hb.status_code == 200
        time.sleep(1.2)
        stalled = await client.get("/v1/runs?stalled=true", headers=headers)
        assert stalled.status_code == 200
        matched = next((row for row in stalled.json()["runs"] if row["run_id"] == run_id), None)
        assert matched is not None
        assert matched["stalled"] is True
        assert matched["stalled_seconds"] >= 1

        resumed = await client.post(f"/v1/runs/{run_id}/resume", headers=headers)
        assert resumed.status_code == 200
        run = resumed.json()["run"]
        assert int(run.get("resume_count", 0)) >= 1
        assert isinstance(run.get("resumed_at"), str) and run.get("resumed_at")

@pytest.mark.asyncio
async def test_server_run_delete_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-runs", "run_id": "run-a", "message": "hello a"},
        )
        assert first.status_code == 200
        second = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-runs", "run_id": "run-b", "message": "hello b"},
        )
        assert second.status_code == 200

        deleted = await client.delete("/v1/runs/run-a", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        missing = await client.get("/v1/runs/run-a", headers=headers)
        assert missing.status_code == 404

        cleared = await client.post("/v1/runs/clear", headers=headers, json={"status": "completed"})
        assert cleared.status_code == 200
        assert cleared.json()["deleted"] >= 1

        listing = await client.get("/v1/runs", headers=headers)
        assert listing.status_code == 200
        ids = [row["run_id"] for row in listing.json()["runs"]]
        assert "run-b" not in ids


@pytest.mark.asyncio
async def test_server_run_dependency_and_blocker_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        base = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-deps", "run_id": "run-base", "message": "base"},
        )
        assert base.status_code == 200
        dep = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-deps", "run_id": "run-dep", "message": "dep"},
        )
        assert dep.status_code == 200

        patched = await client.post(
            "/v1/runs/run-dep/dependencies",
            headers=headers,
            json={
                "depends_on": ["run-base"],
                "blockers": [
                    {
                        "blocker_id": "b1",
                        "code": "awaiting_approval",
                        "message": "Need approval token",
                        "severity": "high",
                        "status": "open",
                    }
                ],
            },
        )
        assert patched.status_code == 200
        run = patched.json()["run"]
        assert run["status"] == "blocked"
        assert run["dependencies"] == ["run-base"]
        assert isinstance(run["blockers"], list) and len(run["blockers"]) == 1

        blocked_listing = await client.get("/v1/runs?blocked=true", headers=headers)
        assert blocked_listing.status_code == 200
        assert any(row["run_id"] == "run-dep" for row in blocked_listing.json()["runs"])

        by_dependency = await client.get("/v1/runs?dependency=run-base", headers=headers)
        assert by_dependency.status_code == 200
        assert any(row["run_id"] == "run-dep" for row in by_dependency.json()["runs"])

        dag = await client.get("/v1/runs/dag", headers=headers)
        assert dag.status_code == 200
        dag_payload = dag.json()["dag"]
        assert isinstance(dag_payload["nodes"], list)
        assert isinstance(dag_payload["edges"], list)
        assert any(edge["type"] == "depends_on" for edge in dag_payload["edges"])
        assert any(edge["type"] == "blocked_by" for edge in dag_payload["edges"])


@pytest.mark.asyncio
async def test_chat_blocks_when_dependency_not_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        blocked = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-dep-blocked",
                "run_id": "run-child",
                "message": "child",
                "depends_on": ["run-missing"],
            },
        )
        assert blocked.status_code == 409
        assert "dependency run 'run-missing'" in blocked.json()["detail"].lower()
        assert fake.ask_calls == 0

        run = await client.get("/v1/runs/run-child", headers=headers)
        assert run.status_code == 200
        payload = run.json()["run"]
        assert payload["status"] == "blocked"
        assert payload["checkpoint"]["stage"] == "blocked_on_dependency"
        assert payload["dependencies"] == ["run-missing"]


@pytest.mark.asyncio
async def test_chat_allows_when_dependency_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        base = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "s-dep-ok", "run_id": "run-parent", "message": "parent"},
        )
        assert base.status_code == 200

        child = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-dep-ok",
                "run_id": "run-child-ok",
                "message": "child",
                "depends_on": ["run-parent"],
            },
        )
        assert child.status_code == 200
        assert fake.ask_calls == 2


@pytest.mark.asyncio
async def test_chat_applies_assistant_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-profile",
                "message": "Summarize this repo.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": "Be concise and technical.",
            },
        )
        assert resp.status_code == 200
        assert "Assistant profile" in fake.last_query
        assert "CerbiBot" in fake.last_query
        assert "Be concise and technical." in fake.last_query


@pytest.mark.asyncio
async def test_chat_strict_profile_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict",
                "message": "Status update.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert fake.ask_calls == 2
        assert payload["result"]["answer"].startswith("- CerbiBot:")


@pytest.mark.asyncio
async def test_chat_strict_profile_coerces_after_failed_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_noncompliant_retry = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-coerce",
                "message": "Status update.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_instruction_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_instruction_echo_retry = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-echo",
                "message": "hello",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "strict instruction" not in answer.lower()


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_meta_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_meta_echo_retry = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-meta",
                "message": "hello",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "the user request" not in answer.lower()
        assert "meta text" not in answer.lower()


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_followup_meta_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_followup_meta_echo = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-followup-meta",
                "message": "Greet me briefly and confirm readiness.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "strict profile" not in answer.lower()
        assert "assistant must" not in answer.lower()
        assert "we are given" not in answer.lower()


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_we_are_to_meta_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_we_are_to_meta_echo = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-we-are-to-meta",
                "message": "Greet me briefly and confirm readiness.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "we are to" not in answer.lower()
        assert "two bullet points" not in answer.lower()


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_remember_context_meta_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_remember_meta_echo = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-remember-meta",
                "message": "Greet me briefly and confirm readiness.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "remember:" not in answer.lower()
        assert "context says" not in answer.lower()


@pytest.mark.asyncio
async def test_chat_strict_profile_sanitizes_example_structure_meta_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    fake = _FakeOrchestrator()
    fake.force_example_meta_echo = True
    app = create_app(fake)
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat",
            headers=headers,
            json={
                "session_id": "s-strict-example-meta",
                "message": "Greet me briefly and confirm readiness.",
                "assistant_name": "CerbiBot",
                "assistant_instructions": 'Always answer in exactly 2 bullet points. Start first bullet with "CerbiBot:" and start second bullet with "Status:"',
                "strict_profile": True,
            },
        )
        assert resp.status_code == 200
        answer = resp.json()["result"]["answer"]
        lines = [line for line in answer.splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0].startswith("- CerbiBot:")
        assert lines[1].startswith("- Status:")
        assert "example" not in answer.lower()
        assert "[greeting and confirmation]" not in answer.lower()


@pytest.mark.asyncio
async def test_server_sessions_persist_across_app_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_SESSIONS_FILE", str(tmp_path / "sessions_store.json"))
    app1 = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport1 = httpx.ASGITransport(app=app1)
    async with httpx.AsyncClient(transport=transport1, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/chat",
            headers=headers,
            json={"session_id": "persist-me", "message": "hello world"},
        )
        assert created.status_code == 200

    app2 = create_app(_FakeOrchestrator())
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://testserver") as client:
        listing = await client.get("/v1/sessions", headers=headers)
        assert listing.status_code == 200
        ids = [row["session_id"] for row in listing.json()["sessions"]]
        assert "persist-me" in ids


@pytest.mark.asyncio
async def test_server_ui_settings_persist_across_app_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_UI_SETTINGS_FILE", str(tmp_path / "ui_settings.json"))
    app1 = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport1 = httpx.ASGITransport(app=app1)
    async with httpx.AsyncClient(transport=transport1, base_url="http://testserver") as client:
        set_resp = await client.put(
            "/v1/server/ui-settings",
            headers=headers,
            json={
                "settings": {
                    "theme": "light",
                    "assistantName": "CerbiBot",
                    "assistantStrictProfile": True,
                    "providers": [{"name": "google", "model": "gemini-2.5-flash", "enabled": True}],
                    "providerMonthlyBudgets": {"google": 10, "xai": "12.5"},
                }
            },
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["settings"]["theme"] == "light"

    app2 = create_app(_FakeOrchestrator())
    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://testserver") as client:
        get_resp = await client.get("/v1/server/ui-settings", headers=headers)
        assert get_resp.status_code == 200
        payload = get_resp.json()["settings"]
        assert payload["theme"] == "light"
        assert payload["assistantName"] == "CerbiBot"
        assert payload["assistantStrictProfile"] is True
        assert payload["providers"][0]["name"] == "google"
        assert payload["providerMonthlyBudgets"]["google"] == pytest.approx(10.0)
        assert payload["providerMonthlyBudgets"]["xai"] == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_server_connectors_status_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/server/connectors", headers=headers)

    assert resp.status_code == 200
    payload = resp.json()
    assert "connectors" in payload
    assert payload["connectors"][0]["name"] == "discord"
    assert payload["connectors"][0]["status"] == "disabled"


@pytest.mark.asyncio
async def test_server_mcp_servers_endpoints_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_UI_SETTINGS_FILE", str(tmp_path / "ui_settings.json"))
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        put_resp = await client.put(
            "/v1/server/mcp/servers",
            headers=headers,
            json={
                "servers": [
                    {
                        "name": "local-fs",
                        "transport": "stdio",
                        "enabled": True,
                        "command": "npx",
                        "args": ["@modelcontextprotocol/server-filesystem"],
                        "header_env_refs": {"Authorization": "MCP_TEST_TOKEN"},
                        "declared_tools": ["read_file", "list_dir"],
                    },
                    {
                        "name": "docs-http",
                        "transport": "http",
                        "enabled": False,
                        "url": "http://127.0.0.1:3010",
                    },
                ]
            },
        )
        assert put_resp.status_code == 200
        servers = put_resp.json()["servers"]
        assert len(servers) == 2
        assert servers[0]["name"] == "local-fs"
        assert servers[1]["enabled"] is False

        get_resp = await client.get("/v1/server/mcp/servers", headers=headers)
        assert get_resp.status_code == 200
        rows = get_resp.json()["servers"]
        assert len(rows) == 2
        assert rows[0]["transport"] == "stdio"
        assert rows[0]["header_env_refs"]["Authorization"] == "MCP_TEST_TOKEN"
        assert rows[1]["transport"] == "http"


@pytest.mark.asyncio
async def test_server_mcp_health_endpoint_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_UI_SETTINGS_FILE", str(tmp_path / "ui_settings.json"))
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        save_resp = await client.put(
            "/v1/server/mcp/servers",
            headers=headers,
            json={
                "servers": [
                    {
                        "name": "bad-stdio",
                        "transport": "stdio",
                        "enabled": True,
                        "command": "definitely-not-a-real-command-xyz",
                    },
                    {
                        "name": "disabled-http",
                        "transport": "http",
                        "enabled": False,
                        "url": "http://127.0.0.1:39999",
                    },
                ]
            },
        )
        assert save_resp.status_code == 200

        report_resp = await client.post("/v1/server/mcp/health", headers=headers, json={"include_disabled": True})
        assert report_resp.status_code == 200
        payload = report_resp.json()
        assert payload["summary"]["total"] == 2
        assert payload["summary"]["failed"] >= 1
        checks = payload["checks"]
        assert isinstance(checks, list)
        names = [row["name"] for row in checks]
        assert "bad-stdio" in names
        assert "disabled-http" in names

        targeted_resp = await client.post(
            "/v1/server/mcp/health",
            headers=headers,
            json={"include_disabled": True, "server_names": ["disabled-http"]},
        )
        assert targeted_resp.status_code == 200
        targeted = targeted_resp.json()
        assert targeted["summary"]["total"] == 1
        assert targeted["checks"][0]["name"] == "disabled-http"


@pytest.mark.asyncio
async def test_server_tool_approval_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get("/v1/tool-approvals", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["approvals"] == []
        approved = await client.post("/v1/tool-approvals/missing/approve", headers=headers)
        assert approved.status_code == 404
        denied = await client.post("/v1/tool-approvals/missing/deny", headers=headers)
        assert denied.status_code == 404


@pytest.mark.asyncio
async def test_server_provider_config_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get("/v1/providers", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["providers"][0]["name"] == "openai"
        catalog = await client.get("/v1/providers/catalog", headers=headers)
        assert catalog.status_code == 200
        assert "google" in catalog.json()["catalog"]
        models = await client.get("/v1/providers/openai/models", headers=headers)
        assert models.status_code == 200
        models_payload = models.json()
        assert models_payload["provider"] == "openai"
        assert "gpt-4o" in models_payload["models"]
        assert models_payload["configured_model"]
        applied = await client.put(
            "/v1/providers",
            headers=headers,
            json={"providers": [{"name": "openai", "enabled": True, "model": "gpt-4o-mini"}]},
        )
        assert applied.status_code == 200
        assert applied.json()["updated"][0]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_server_role_routing_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        current = await client.get("/v1/routing/roles", headers=headers)
        assert current.status_code == 200
        assert current.json()["routing"]["critique"]["drafter_provider"] == "openai"

        updated = await client.put(
            "/v1/routing/roles",
            headers=headers,
            json={
                "routing": {
                    "critique": {
                        "drafter_provider": "openai",
                        "critic_provider": "openai",
                        "refiner_provider": "openai",
                    },
                    "debate": {
                        "debater_a_provider": "openai",
                        "debater_b_provider": "openai",
                        "judge_provider": "openai",
                        "synthesizer_provider": "openai",
                    },
                    "consensus": {"adjudicator_provider": "openai"},
                    "council": {
                        "specialist_roles": {"coding": "openai", "security": "", "writing": "", "factual": ""},
                        "synthesizer_provider": "openai",
                    },
                }
            },
        )
        assert updated.status_code == 200
        assert updated.json()["routing"]["consensus"]["adjudicator_provider"] == "openai"

        bad = await client.put(
            "/v1/routing/roles",
            headers=headers,
            json={
                "routing": {
                    "critique": {
                        "drafter_provider": "missing",
                        "critic_provider": "openai",
                        "refiner_provider": "openai",
                    }
                }
            },
        )
        assert bad.status_code == 400


@pytest.mark.asyncio
async def test_server_provider_key_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setattr("orchestrator.server.has_secret", lambda _name: False)
    monkeypatch.setattr("orchestrator.server.set_secret", lambda _name, _value: None)
    monkeypatch.setattr("orchestrator.server.delete_secret", lambda _name: True)
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        status = await client.get("/v1/providers/keys/status", headers=headers)
        assert status.status_code == 200
        row = status.json()["providers"][0]
        assert row["name"] == "openai"
        assert row["key_set"] is False

        set_resp = await client.post(
            "/v1/providers/keys",
            headers=headers,
            json={"provider": "openai", "api_key": "sk-test-value"},
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["provider"] == "openai"

        delete_resp = await client.delete("/v1/providers/keys/openai", headers=headers)
        assert delete_resp.status_code == 200
        assert delete_resp.json()["deleted"] is True


@pytest.mark.asyncio
async def test_server_provider_test_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    app = create_app(_FakeOrchestrator())
    headers = {"Authorization": "Bearer secret"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        tested = await client.post("/v1/providers/openai/test", headers=headers, json={})
        assert tested.status_code == 200
        payload = tested.json()
        assert payload["provider"] == "openai"
        assert payload["ok"] is True


@pytest.mark.asyncio
async def test_server_token_rotate_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    token_file = tmp_path / "server_api_key.txt"
    monkeypatch.setenv("MMO_SERVER_API_KEY_FILE", str(token_file))
    app = create_app(_FakeOrchestrator())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        old_headers = {"Authorization": "Bearer secret"}
        rotate = await client.post("/v1/server/token/rotate", headers=old_headers)
        assert rotate.status_code == 200
        payload = rotate.json()
        assert payload["token"]
        assert token_file.exists()
        assert token_file.read_text(encoding="utf-8").strip() == payload["token"]

        old_health = await client.get("/v1/health", headers=old_headers)
        assert old_health.status_code == 401

        new_headers = {"Authorization": f"Bearer {payload['token']}"}
        new_health = await client.get("/v1/health", headers=new_headers)
        assert new_health.status_code == 200


def test_load_server_api_key_prefers_file_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "server_api_key.txt"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("MMO_SERVER_API_KEY", "env-token")
    token = _load_server_api_key("MMO_SERVER_API_KEY", token_file)
    assert token == "file-token"


@pytest.mark.asyncio
async def test_admin_password_recover_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    token_file = tmp_path / "server_api_key.txt"
    admin_file = tmp_path / "admin_auth.json"
    monkeypatch.setenv("MMO_SERVER_API_KEY_FILE", str(token_file))
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(admin_file))
    app = create_app(_FakeOrchestrator())
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        status_resp = await client.get("/v1/server/admin-password/status", headers=headers)
        assert status_resp.status_code == 200
        assert status_resp.json()["configured"] is True

        bad_verify = await client.post(
            "/v1/server/admin-password/verify",
            headers=headers,
            json={"password": "wrong"},
        )
        assert bad_verify.status_code == 401

        verify = await client.post(
            "/v1/server/admin-password/verify",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert verify.status_code == 200
        assert verify.json()["ok"] is True

        bad_recover = await client.post("/v1/server/token/recover", json={"admin_password": "wrong"})
        assert bad_recover.status_code == 401

        recover = await client.post("/v1/server/token/recover", json={"admin_password": "LongerPassword123"})
        assert recover.status_code == 200
        new_token = recover.json()["token"]
        assert new_token

        old_health = await client.get("/v1/health", headers=headers)
        assert old_health.status_code == 401
        new_health = await client.get("/v1/health", headers={"Authorization": f"Bearer {new_token}"})
        assert new_health.status_code == 200


@pytest.mark.asyncio
async def test_admin_password_verify_lockout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_SERVER_API_KEY_FILE", str(tmp_path / "server_api_key.txt"))
    monkeypatch.setenv("MMO_ADMIN_AUTH_FILE", str(tmp_path / "admin_auth.json"))
    monkeypatch.setenv("MMO_ADMIN_VERIFY_MAX_FAILED", "2")
    monkeypatch.setenv("MMO_ADMIN_VERIFY_LOCKOUT_SECONDS", "60")
    app = create_app(_FakeOrchestrator())
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        set_resp = await client.post(
            "/v1/server/admin-password/set",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert set_resp.status_code == 200

        bad_one = await client.post(
            "/v1/server/admin-password/verify",
            headers=headers,
            json={"password": "wrong"},
        )
        assert bad_one.status_code == 401

        bad_two = await client.post(
            "/v1/server/admin-password/verify",
            headers=headers,
            json={"password": "wrong"},
        )
        assert bad_two.status_code == 401

        locked = await client.post(
            "/v1/server/admin-password/verify",
            headers=headers,
            json={"password": "LongerPassword123"},
        )
        assert locked.status_code == 429


@pytest.mark.asyncio
async def test_server_audit_event_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_SERVER_API_KEY", "secret")
    monkeypatch.setenv("MMO_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    app = create_app(_FakeOrchestrator())
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer secret"}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        bad = await client.post(
            "/v1/server/audit/event",
            headers=headers,
            json={"event_type": "bad event", "payload": {}},
        )
        assert bad.status_code == 400

        ok = await client.post(
            "/v1/server/audit/event",
            headers=headers,
            json={"event_type": "ui.export_success", "payload": {"scope": "all", "count": 2}},
        )
        assert ok.status_code == 200
        assert ok.json()["ok"] is True
