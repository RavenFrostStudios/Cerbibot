from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import mmctl.__main__ as mmctl_main


@dataclass
class _FakeAskResult:
    answer: str
    mode: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost: float
    draft: str | None = None
    critique: str | None = None
    refined: str | None = None
    warnings: list[str] | None = None
    citations: list | None = None
    verification_notes: list | None = None
    debate_a: str | None = None
    debate_b: str | None = None
    judge_decision: str | None = None
    consensus_answers: dict[str, str] | None = None
    consensus_confidence: float | None = None
    consensus_agreement: float | None = None
    consensus_adjudicated: bool | None = None
    tool_outputs: list | None = None
    council_outputs: dict[str, str] | None = None
    council_notes: str | None = None


class _FakeOrchestratorAsk:
    def __init__(self, result: _FakeAskResult):
        self._result = result
        self.budgets = _FakeBudgets("/tmp/nonexistent_usage_for_test.json")
        self.config = SimpleNamespace(default_mode="single")

    async def ask(
        self,
        query: str,
        mode: str | None = None,
        provider: str | None = None,
        verbose: bool = False,
        context_messages: list[dict[str, str]] | None = None,
        fact_check: bool = False,
        tools: str | None = None,
        force_full_debate: bool = False,
    ) -> _FakeAskResult:
        return self._result

    async def ask_stream(
        self,
        query: str,
        mode: str | None = None,
        provider: str | None = None,
        verbose: bool = False,
        context_messages: list[dict[str, str]] | None = None,
        fact_check: bool = False,
        tools: str | None = None,
        force_full_debate: bool = False,
    ):
        yield SimpleNamespace(type="chunk", text=self._result.answer, result=None)
        yield SimpleNamespace(type="result", text=None, result=self._result)


class _FakeBudgets:
    def __init__(self, usage_file: str):
        self.config = SimpleNamespace(usage_file=usage_file)

    def _today(self) -> str:
        return "2026-02-10"

    def _month(self) -> str:
        return "2026-02"

    def remaining(self) -> dict[str, float]:
        return {"session": 1.23, "daily": 4.56, "monthly": 7.89}


class _FakeOrchestratorCost:
    def __init__(self, usage_file: str, rate_snapshot: dict | None = None):
        self.budgets = _FakeBudgets(usage_file)
        if rate_snapshot is not None:
            self.rate_limiter = SimpleNamespace(snapshot=lambda: rate_snapshot)


class _FakeRouterWeights:
    def __init__(self, snapshot: dict):
        self._snapshot = snapshot
        self.reset_called = False

    def snapshot(self) -> dict:
        return self._snapshot

    def reset(self) -> None:
        self.reset_called = True


class _FakeArtifactStore:
    def __init__(self):
        self._rows = [
            SimpleNamespace(
                request_id="req-1",
                started_at="2026-02-10T00:00:00Z",
                mode="single",
                query_preview="hello",
                cost=0.01,
                path="/tmp/req-1.json",
            )
        ]

    def list_summaries(self, limit=20):
        return self._rows[:limit]

    def load(self, request_id: str):
        return {
            "artifact": {
                "request_id": request_id,
                "query": "hello",
                "mode": "single",
                "provider_override": None,
                "request_options": {"fact_check": False},
                "result": {"answer": "hello"},
            },
            "meta": {"integrity_hash": "x"},
        }

    def export(self, request_id: str, *, output_path: str, fmt: str):
        return output_path


class _FakeArtifactStoreReport(_FakeArtifactStore):
    def __init__(self):
        super().__init__()
        self._rows = [
            SimpleNamespace(
                request_id="req-r1",
                started_at="2026-02-10T00:00:00Z",
                mode="single",
                query_preview="hello",
                cost=0.02,
                path="/tmp/req-r1.json",
            )
        ]

    def load(self, request_id: str):
        return {
            "artifact": {
                "request_id": request_id,
                "started_at": "2026-02-10T00:00:00Z",
                "duration_ms": 900,
                "result": {
                    "mode": "single",
                    "provider": "openai",
                    "cost": 0.02,
                    "warnings": ["flag-x"],
                    "answer": "hello",
                },
                "fact_check": [{"conflicts": ["no-source"]}],
            },
            "meta": {"integrity_hash": "x"},
        }


class _FakeMemoryStore:
    def __init__(self):
        self.rows = []

    def add(self, **kwargs):
        row = SimpleNamespace(id=len(self.rows) + 1, **kwargs, created_at="2026-02-10T00:00:00+00:00")
        self.rows.append(row)
        return row.id

    def list_records(self, limit=50, min_confidence=0.0):
        return [r for r in self.rows if r.confidence >= min_confidence][:limit]

    def search(self, query: str, limit=20, min_confidence=0.0):
        return [r for r in self.rows if query.lower() in r.statement.lower() and r.confidence >= min_confidence][:limit]

    def delete(self, record_id: int):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r.id != record_id]
        return len(self.rows) != before

    def clear(self):
        count = len(self.rows)
        self.rows = []
        return count


class _FakeGovernance:
    def evaluate_write(self, **kwargs):
        return SimpleNamespace(allowed=True, reason="ok", redacted_statement=kwargs["statement"])


async def _async_false(*_args, **_kwargs):
    return False


async def _async_true(*_args, **_kwargs):
    return True


def test_cli_ask_renders_warnings(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="final answer",
        mode="critique",
        provider="multi",
        model="m1,m2",
        tokens_in=10,
        tokens_out=20,
        cost=0.123,
        warnings=["Critique step failed; returned draft-only answer."],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)

    result = runner.invoke(mmctl_main.main, ["ask", "hello"])
    assert result.exit_code == 0
    assert "final answer" in result.output
    assert "Warnings" in result.output
    assert "Critique step failed" in result.output


def test_cli_cost_no_usage_file(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    usage_file = tmp_path / "missing_usage.json"
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorCost(str(usage_file)))

    result = runner.invoke(mmctl_main.main, ["cost"])
    assert result.exit_code == 0
    assert "No usage data yet." in result.output


def test_cli_cost_renders_summary(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(
        json.dumps(
            {
                "daily_totals": {"cost": 0.3, "providers": {"openai": {"cost": 0.3, "tokens_in": 10, "tokens_out": 10}}},
                "monthly_totals": {"cost": 1.2, "providers": {"openai": {"cost": 1.2, "tokens_in": 40, "tokens_out": 30}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorCost(str(usage_file)))

    result = runner.invoke(mmctl_main.main, ["cost"])
    assert result.exit_code == 0
    assert "Usage Summary" in result.output
    assert "Today" in result.output
    assert "Month" in result.output


def test_cli_cost_renders_rate_limit_status(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(
        json.dumps(
            {
                "daily_totals": {"cost": 0.1, "providers": {}},
                "monthly_totals": {"cost": 0.2, "providers": {}},
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "openai": {
            "rpm_limit": 60,
            "tpm_limit": 120000,
            "rpm_used": 2,
            "tpm_used": 1500,
            "rpm_headroom": 0.97,
            "tpm_headroom": 0.99,
        }
    }
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorCost(str(usage_file), snapshot))

    result = runner.invoke(mmctl_main.main, ["cost"])
    assert result.exit_code == 0
    assert "Rate Limit Status" in result.output
    assert "openai" in result.output


def test_cli_router_show(monkeypatch) -> None:
    runner = CliRunner()
    fake = SimpleNamespace(
        router_weights=_FakeRouterWeights(
            {
                "openai": {
                    "coding": {
                        "score": 0.82,
                        "count": 5,
                        "p50_latency_ms": 900,
                        "p95_latency_ms": 1800,
                    }
                }
            }
        )
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: fake)
    result = runner.invoke(mmctl_main.main, ["router", "show"])
    assert result.exit_code == 0
    assert "Router Weights" in result.output
    assert "coding" in result.output


def test_cli_router_reset(monkeypatch) -> None:
    runner = CliRunner()
    weights = _FakeRouterWeights({})
    fake = SimpleNamespace(router_weights=weights)
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: fake)
    result = runner.invoke(mmctl_main.main, ["router", "reset"])
    assert result.exit_code == 0
    assert "Router weights reset." in result.output
    assert weights.reset_called is True


def test_cli_eval_run(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: object())

    async def _fake_run_eval(_orchestrator, _tasks, _out):
        return {"total": 2}

    import evaluation.harness as harness

    monkeypatch.setattr(harness, "run_eval", _fake_run_eval)
    result = runner.invoke(mmctl_main.main, ["eval", "run"])
    assert result.exit_code == 0
    assert "Eval complete: 2 tasks" in result.output


def test_cli_eval_adversarial(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: object())

    async def _fake_run_adv(_orchestrator, _fixtures_dir, _out_file):
        return {"total": 3, "passed": 3, "failed": 0}

    import evaluation.adversarial.runner as adv_runner

    monkeypatch.setattr(adv_runner, "run_adversarial_eval", _fake_run_adv)
    result = runner.invoke(mmctl_main.main, ["eval", "adversarial"])
    assert result.exit_code == 0
    assert "Adversarial eval complete" in result.output


def test_cli_ask_no_stream_path(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="non-stream answer",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=1,
        tokens_out=2,
        cost=0.01,
        warnings=[],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)
    result = runner.invoke(mmctl_main.main, ["ask", "--no-stream", "hello"])
    assert result.exit_code == 0
    assert "non-stream answer" in result.output


def test_cli_ask_consensus_verbose(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="Canberra",
        mode="consensus",
        provider="multi",
        model="m,m",
        tokens_in=10,
        tokens_out=11,
        cost=0.02,
        warnings=[],
        consensus_answers={"openai": "Canberra", "anthropic": "Canberra"},
        consensus_confidence=0.91,
        consensus_agreement=0.87,
        consensus_adjudicated=False,
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)
    result = runner.invoke(mmctl_main.main, ["ask", "--no-stream", "--mode", "consensus", "--verbose", "hello"])
    assert result.exit_code == 0
    assert "Consensus" in result.output
    assert "agreement=0.87" in result.output
    assert "confidence=0.91" in result.output


def test_cli_ask_renders_tool_output(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="tool-backed answer",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=5,
        tokens_out=7,
        cost=0.01,
        warnings=[],
        tool_outputs=[{"tool": "python_exec", "stdout": "13\n", "exit_code": 0}],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)
    result = runner.invoke(mmctl_main.main, ["ask", "--no-stream", "--tools", "run python", "hello"])
    assert result.exit_code == 0
    assert "Tool Output" in result.output
    assert "python_exec" in result.output


def test_cli_ask_council_verbose(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="final council answer",
        mode="council",
        provider="multi",
        model="m,m,m",
        tokens_in=20,
        tokens_out=30,
        cost=0.04,
        warnings=[],
        council_outputs={"coding": "coding view", "security": "security view"},
        council_notes="resolved tradeoffs",
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)
    result = runner.invoke(mmctl_main.main, ["ask", "--no-stream", "--mode", "council", "--verbose", "hello"])
    assert result.exit_code == 0
    assert "Council Specialists" in result.output
    assert "resolved tradeoffs" in result.output


def test_cli_memory_add_and_search(monkeypatch) -> None:
    runner = CliRunner()
    store = _FakeMemoryStore()
    monkeypatch.setattr(mmctl_main, "_load_memory_components", lambda _cfg: (store, _FakeGovernance()))

    add = runner.invoke(mmctl_main.main, ["memory", "add", "User prefers concise answers"])
    assert add.exit_code == 0
    assert "Stored memory id=1" in add.output

    search = runner.invoke(mmctl_main.main, ["memory", "search", "concise"])
    assert search.exit_code == 0
    assert "concise" in search.output


def test_cli_memory_clear_yes(monkeypatch) -> None:
    runner = CliRunner()
    store = _FakeMemoryStore()
    store.add(
        statement="x",
        source_type="summary",
        source_ref="r1",
        confidence=0.9,
        ttl_days=30,
        reviewed_by=None,
        redaction_status="redacted",
    )
    monkeypatch.setattr(mmctl_main, "_load_memory_components", lambda _cfg: (store, _FakeGovernance()))
    result = runner.invoke(mmctl_main.main, ["memory", "clear", "--yes"])
    assert result.exit_code == 0
    assert "Cleared memories: 1" in result.output


def test_cli_skill_install_list_enable_disable(tmp_path) -> None:
    runner = CliRunner()
    skill_file = tmp_path / "demo.workflow.yaml"
    skill_file.write_text(
        """
name: demo_skill
manifest:
  purpose: "CLI install/list skill fixture."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "say hi"
    output: out
""",
        encoding="utf-8",
    )
    env = {"MMO_STATE_DIR": str(tmp_path / "state")}

    installed = runner.invoke(mmctl_main.main, ["skill", "install", str(skill_file)], env=env)
    assert installed.exit_code == 0
    assert "Installed skill: demo_skill" in installed.output

    listed = runner.invoke(mmctl_main.main, ["skill", "list"], env=env)
    assert listed.exit_code == 0
    assert "demo_skill" in listed.output
    assert "yes" in listed.output

    disabled = runner.invoke(mmctl_main.main, ["skill", "disable", "demo_skill"], env=env)
    assert disabled.exit_code == 0
    assert "Disabled skill: demo_skill" in disabled.output

    listed2 = runner.invoke(mmctl_main.main, ["skill", "list"], env=env)
    assert listed2.exit_code == 0
    assert "demo_skill" in listed2.output
    assert "no" in listed2.output

    enabled = runner.invoke(mmctl_main.main, ["skill", "enable", "demo_skill"], env=env)
    assert enabled.exit_code == 0
    assert "Enabled skill: demo_skill" in enabled.output


def test_cli_skill_test_validation_only(tmp_path) -> None:
    runner = CliRunner()
    skill_file = tmp_path / "validate.workflow.yaml"
    skill_file.write_text(
        """
name: validate_skill
manifest:
  purpose: "CLI validation-only fixture."
  tools: [system_info]
  data_scope: ["local_runtime"]
  permissions: ["read"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - tool: system_info
    output: info
""",
        encoding="utf-8",
    )
    env = {"MMO_STATE_DIR": str(tmp_path / "state")}
    result = runner.invoke(mmctl_main.main, ["skill", "test", str(skill_file)], env=env)
    assert result.exit_code == 0
    assert "Skill validation passed" in result.output


def test_cli_skill_install_requires_manifest(tmp_path) -> None:
    runner = CliRunner()
    skill_file = tmp_path / "invalid.workflow.yaml"
    skill_file.write_text(
        """
name: invalid_skill
steps:
  - model_call: "hello"
    output: out
""",
        encoding="utf-8",
    )
    env = {"MMO_STATE_DIR": str(tmp_path / "state")}
    result = runner.invoke(mmctl_main.main, ["skill", "install", str(skill_file)], env=env)
    assert result.exit_code != 0
    assert "manifest" in result.output.lower()


def test_cli_skill_test_adversarial(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    skill_file = tmp_path / "adv.workflow.yaml"
    skill_file.write_text(
        """
name: adv_skill
manifest:
  purpose: "CLI adversarial fixture skill."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "hello"
    output: out
""",
        encoding="utf-8",
    )
    fixtures = tmp_path / "adv.yaml"
    fixtures.write_text(
        """
- id: c1
  input: {}
  expect_error: false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: SimpleNamespace())

    async def _fake_adv(*_args, **_kwargs):
        return {"total": 1, "passed": 1, "failed": 0, "results": [{"case_id": "c1", "passed": True}]}

    import orchestrator.skills.testing as skill_testing

    monkeypatch.setattr(skill_testing, "run_skill_adversarial_tests", _fake_adv)
    result = runner.invoke(
        mmctl_main.main,
        ["skill", "test", str(skill_file), "--adversarial", "--fixtures", str(fixtures)],
    )
    assert result.exit_code == 0
    assert "adversarial test summary" in result.output.lower()


def test_cli_skill_analyze_bloat_emits_artifacts(tmp_path) -> None:
    runner = CliRunner()
    env = {"MMO_STATE_DIR": str(tmp_path / "state")}

    skill_a = tmp_path / "alpha.workflow.yaml"
    skill_a.write_text(
        """
name: alpha_skill
risk_level: low
manifest:
  purpose: "Fetch and summarize web docs."
  tools: [web_retrieve]
  data_scope: ["public_web_content"]
  permissions: ["network_read"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - tool: web_retrieve
    args:
      url: "$input.url"
    output: source
  - model_call: "summarize $source"
    output: brief
""",
        encoding="utf-8",
    )
    skill_b = tmp_path / "beta.workflow.yaml"
    skill_b.write_text(
        """
name: beta_skill
risk_level: low
manifest:
  purpose: "Retrieve and summarize public web pages."
  tools: [web_retrieve]
  data_scope: ["public_web_content"]
  permissions: ["network_read"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - tool: web_retrieve
    args:
      url: "$input.url"
    output: page
  - model_call: "summarize $page"
    output: summary
""",
        encoding="utf-8",
    )

    assert runner.invoke(mmctl_main.main, ["skill", "install", str(skill_a)], env=env).exit_code == 0
    assert runner.invoke(mmctl_main.main, ["skill", "install", str(skill_b)], env=env).exit_code == 0

    out_dir = tmp_path / "gov"
    result = runner.invoke(
        mmctl_main.main,
        ["skill", "analyze-bloat", "--out-dir", str(out_dir)],
        env=env,
    )
    assert result.exit_code == 0
    assert "skill bloat analysis complete" in result.output.lower()
    assert (out_dir / "merge_candidates.json").exists()
    assert (out_dir / "crossover_candidates.json").exists()
    assert (out_dir / "skills_bloat_report.md").exists()
    assert (out_dir / "deprecation_plan.md").exists()


def test_cli_skill_sign_verify_and_install_require_signature(tmp_path) -> None:
    cryptography = pytest.importorskip("cryptography")
    _ = cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    runner = CliRunner()
    state_dir = tmp_path / "state"
    env = {"MMO_STATE_DIR": str(state_dir)}
    skill_file = tmp_path / "signed.workflow.yaml"
    skill_file.write_text(
        """
name: signed_cli_skill
manifest:
  purpose: "CLI signing fixture skill."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "hello"
    output: out
""",
        encoding="utf-8",
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_path = tmp_path / "private.pem"
    pub_path = tmp_path / "public.pem"
    priv_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    cfg = tmp_path / "skills-config.yaml"
    cfg.write_text(
        f"""
skills:
  require_signature: true
  trusted_public_keys:
    - {pub_path}
""",
        encoding="utf-8",
    )

    signed = runner.invoke(
        mmctl_main.main,
        ["skill", "sign", str(skill_file), "--private-key", str(priv_path)],
        env=env,
    )
    assert signed.exit_code == 0

    verified = runner.invoke(
        mmctl_main.main,
        ["skill", "verify", str(skill_file), "--public-key", str(pub_path)],
        env=env,
    )
    assert verified.exit_code == 0
    assert "Signature verified" in verified.output

    installed = runner.invoke(
        mmctl_main.main,
        ["skill", "install", str(skill_file), "--config", str(cfg)],
        env=env,
    )
    assert installed.exit_code == 0
    assert "signature_verified=True" in installed.output


def test_cli_skill_keygen(tmp_path) -> None:
    pytest.importorskip("cryptography")
    runner = CliRunner()
    priv = tmp_path / "generated_private.pem"
    pub = tmp_path / "generated_public.pem"
    result = runner.invoke(
        mmctl_main.main,
        ["skill", "keygen", "--private-key", str(priv), "--public-key", str(pub)],
    )
    assert result.exit_code == 0
    assert priv.exists()
    assert pub.exists()
    assert "BEGIN PRIVATE KEY" in priv.read_text(encoding="utf-8")
    assert "BEGIN PUBLIC KEY" in pub.read_text(encoding="utf-8")


def test_cli_skill_run_shadow_confirm_passes_flag(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    skill_file = tmp_path / "shadow.workflow.yaml"
    skill_file.write_text(
        """
name: shadow_cli_skill
manifest:
  purpose: "CLI shadow confirm plumbing."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: approve_high_risk
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - model_call: "hello"
    output: out
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: SimpleNamespace())
    captured: dict = {}

    async def _fake_run(_orch, **kwargs):
        captured.update(kwargs.get("input_data", {}))
        return SimpleNamespace(skill_name="shadow_cli_skill", steps_executed=1, total_cost=0.0, outputs={"out": "ok"})

    import orchestrator.skills.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod, "run_workflow_skill", _fake_run)
    result = runner.invoke(
        mmctl_main.main,
        ["skill", "run", str(skill_file), "--shadow-confirm"],
    )
    assert result.exit_code == 0
    assert captured.get("_shadow_confirm") is True


def test_cli_secret_set_and_list(tmp_path) -> None:
    runner = CliRunner()
    usage_file = tmp_path / "usage.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: {{ fast: gpt-4o-mini, deep: gpt-4.1 }}
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: {{ input: 0.1, output: 0.2 }}
      gpt-4.1: {{ input: 1.0, output: 2.0 }}
  anthropic:
    enabled: true
    api_key_env: ANTHROPIC_API_KEY
    models: {{ fast: a, deep: b }}
    pricing_usd_per_1m_tokens:
      a: {{ input: 0.1, output: 0.2 }}
      b: {{ input: 0.3, output: 0.4 }}
budgets:
  session_usd_cap: 10
  daily_usd_cap: 10
  monthly_usd_cap: 10
  usage_file: {usage_file}
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: [fetch_url, web_search]
  retrieval_domain_allowlist: [example.com]
  retrieval_domain_denylist: [localhost]
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
        encoding="utf-8",
    )
    env = {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "MMO_STATE_DIR": str(tmp_path / "state")}
    set_result = runner.invoke(
        mmctl_main.main,
        ["secret", "set", "--config", str(config_path), "api_token", "value-123"],
        env=env,
    )
    assert set_result.exit_code == 0
    assert "secret://keyring/api_token#value" in set_result.output

    list_result = runner.invoke(
        mmctl_main.main,
        ["secret", "list", "--config", str(config_path)],
        env=env,
    )
    assert list_result.exit_code == 0


def test_cli_chat_exit(monkeypatch) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="chat answer",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=1,
        tokens_out=2,
        cost=0.01,
        warnings=[],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_false)
    result = runner.invoke(mmctl_main.main, ["chat"], input="/exit\n")
    assert result.exit_code == 0
    assert "Entering chat mode" in result.output
    assert "Exiting chat." in result.output


def test_cli_tool_simulate(tmp_path) -> None:
    runner = CliRunner()
    usage_file = tmp_path / "usage.json"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: {{ fast: gpt-4o-mini, deep: gpt-4.1 }}
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: {{ input: 0.1, output: 0.2 }}
      gpt-4.1: {{ input: 1.0, output: 2.0 }}
  anthropic:
    enabled: true
    api_key_env: ANTHROPIC_API_KEY
    models: {{ fast: a, deep: b }}
    pricing_usd_per_1m_tokens:
      a: {{ input: 0.1, output: 0.2 }}
      b: {{ input: 0.3, output: 0.4 }}
budgets:
  session_usd_cap: 10
  daily_usd_cap: 10
  monthly_usd_cap: 10
  usage_file: {usage_file}
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: [fetch_url, web_search]
  retrieval_domain_allowlist: [example.com]
  retrieval_domain_denylist: [localhost]
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        mmctl_main.main,
        [
            "tool",
            "simulate",
            "--config",
            str(config_path),
            "--tool-name",
            "fetch_url",
            "--arg",
            "url=https://example.com/docs",
        ],
        env={"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"},
    )
    assert result.exit_code == 0
    assert "Capability granted and executed" in result.output


def test_cli_ask_proxies_to_daemon(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(mmctl_main, "_daemon_health_ok", _async_true)
    monkeypatch.setattr(mmctl_main, "_proxy_ask_to_daemon", _async_true)
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: (_ for _ in ()).throw(RuntimeError("unexpected")))
    result = runner.invoke(mmctl_main.main, ["ask", "hello"])
    assert result.exit_code == 0


def test_cli_serve_command(monkeypatch) -> None:
    runner = CliRunner()

    class _FakeOrchestrator:
        config = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=8100))

    called = {"host": None, "port": None}

    def _fake_run_server(_orchestrator, *, host: str, port: int):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestrator())
    import orchestrator.server as server_mod

    monkeypatch.setattr(server_mod, "run_server", _fake_run_server)
    result = runner.invoke(mmctl_main.main, ["serve", "--host", "127.0.0.1", "--port", "9100"])
    assert result.exit_code == 0
    assert called["port"] == 9100


def test_cli_dashboard_once_local(monkeypatch) -> None:
    runner = CliRunner()
    async def _none_daemon(_cfg):
        return None

    monkeypatch.setattr(mmctl_main, "_fetch_daemon_dashboard", _none_daemon)
    monkeypatch.setattr(
        mmctl_main,
        "_collect_local_dashboard",
        lambda _cfg: {
            "source": "local",
            "providers": ["openai"],
            "remaining": {"session": 1.0, "daily": 2.0, "monthly": 3.0},
            "state": {"daily_total_cost": 0.1, "monthly_total_cost": 0.2},
            "rate_limits": {"openai": {"rpm_used": 1, "rpm_limit": 60, "tpm_used": 10, "tpm_limit": 120000}},
            "router_weights": {"openai": {"general": {"score": 0.8, "p50_latency_ms": 1000, "p95_latency_ms": 2000}}},
            "audit_events": [],
            "memory_stats": {"count": 0, "size_bytes": 0, "oldest": "", "newest": ""},
        },
    )
    result = runner.invoke(mmctl_main.main, ["dashboard", "--once"])
    assert result.exit_code == 0
    assert "MMO Dashboard (local)" in result.output
    assert "Provider Status" in result.output


def test_cli_dashboard_once_daemon(monkeypatch) -> None:
    runner = CliRunner()

    async def _fake_daemon(_cfg):
        return {
            "source": "daemon",
            "health": {"providers": ["openai"], "budget_remaining": {"session": 1, "daily": 2, "monthly": 3}},
            "cost": {
                "remaining": {"session": 1, "daily": 2, "monthly": 3},
                "state": {"daily_spend": 0.2, "monthly_spend": 0.4},
                "rate_limits": {"openai": {"rpm_used": 1, "rpm_limit": 60, "tpm_used": 10, "tpm_limit": 120000}},
                "router_weights": {"openai": {"general": {"score": 0.7, "p50_latency_ms": 900, "p95_latency_ms": 1600}}},
            },
            "sessions": {"sessions": [{"session_id": "s1", "messages": 3}]},
            "memory": {"memories": []},
        }

    monkeypatch.setattr(mmctl_main, "_fetch_daemon_dashboard", _fake_daemon)
    result = runner.invoke(mmctl_main.main, ["dashboard", "--once"])
    assert result.exit_code == 0
    assert "MMO Dashboard (daemon)" in result.output


def test_cli_history_list_show_export(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(mmctl_main, "_load_artifact_store", lambda _cfg: _FakeArtifactStore())

    listed = runner.invoke(mmctl_main.main, ["history", "list"])
    assert listed.exit_code == 0
    assert "Run Artifacts" in listed.output

    shown = runner.invoke(mmctl_main.main, ["history", "show", "req-1"])
    assert shown.exit_code == 0
    assert "req-1" in shown.output

    exported = runner.invoke(mmctl_main.main, ["history", "export", "req-1", "--format", "json"])
    assert exported.exit_code == 0
    assert "Exported:" in exported.output


def test_cli_history_replay(monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr(mmctl_main, "_load_artifact_store", lambda _cfg: _FakeArtifactStore())
    result_obj = _FakeAskResult(
        answer="hello replay",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=1,
        tokens_out=2,
        cost=0.01,
        warnings=[],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    replayed = runner.invoke(mmctl_main.main, ["history", "replay", "req-1"])
    assert replayed.exit_code == 0
    assert "Replay mode=single provider=openai" in replayed.output


def test_cli_policy_check_and_diff(tmp_path) -> None:
    runner = CliRunner()
    policies = tmp_path / "policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / "a.yaml").write_text(
        """
tool_allowlist: [fetch_url]
tool_policies:
  fetch_url:
    max_calls_per_request: 3
    requires_human_approval: false
    allowed_arg_patterns: {}
retrieval_policy:
  domain_allowlist: []
  domain_denylist: [localhost]
high_impact_actions: []
""",
        encoding="utf-8",
    )
    (policies / "b.yaml").write_text(
        """
tool_allowlist: [fetch_url, web_search]
tool_policies:
  fetch_url:
    max_calls_per_request: 3
    requires_human_approval: false
    allowed_arg_patterns: {}
  web_search:
    max_calls_per_request: 3
    requires_human_approval: false
    allowed_arg_patterns: {}
retrieval_policy:
  domain_allowlist: [docs.python.org]
  domain_denylist: [localhost]
high_impact_actions: []
""",
        encoding="utf-8",
    )
    checked = runner.invoke(mmctl_main.main, ["policy", "check", "--path", str(policies)])
    assert checked.exit_code == 0
    assert "Policy files valid" in checked.output

    diffed = runner.invoke(mmctl_main.main, ["policy", "diff", str(policies / "a.yaml"), str(policies / "b.yaml")])
    assert diffed.exit_code == 0
    assert "Policy Diff" in diffed.output


def test_cli_batch_run_resume_report(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    input_file = tmp_path / "in.jsonl"
    output_file = tmp_path / "out.jsonl"
    input_file.write_text(
        "\n".join(
            [
                json.dumps({"id": "q1", "query": "hello", "mode": "single"}),
                json.dumps({"id": "q2", "query": "world", "mode": "single"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result_obj = _FakeAskResult(
        answer="ok",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=2,
        tokens_out=3,
        cost=0.01,
        warnings=[],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))

    run_res = runner.invoke(
        mmctl_main.main,
        ["batch", "run", str(input_file), "--output-file", str(output_file), "--parallel", "2"],
    )
    assert run_res.exit_code == 0
    assert "Batch complete:" in run_res.output
    assert output_file.exists()

    report_res = runner.invoke(mmctl_main.main, ["batch", "report", str(output_file)])
    assert report_res.exit_code == 0
    assert "Batch Report" in report_res.output

    resume_res = runner.invoke(mmctl_main.main, ["batch", "resume", str(output_file)])
    assert resume_res.exit_code == 0
    assert "Nothing to resume." in resume_res.output


def test_cli_prompts_commands(monkeypatch) -> None:
    runner = CliRunner()
    fake_tpl = SimpleNamespace(
        template_id="drafter_v1",
        role="drafter",
        variables=["query"],
        name="drafter",
        version=1,
        content_hash="abc",
        template="Q: {query}",
    )
    fake_lib = SimpleNamespace(
        list_templates=lambda: [fake_tpl],
        resolve=lambda selector, role=None: fake_tpl,
        render=lambda selector, role=None, variables=None: f"Q: {variables['query']}",
    )
    monkeypatch.setattr(mmctl_main, "_load_prompt_library", lambda _cfg: (fake_lib, {"drafter": "drafter_latest"}))

    listed = runner.invoke(mmctl_main.main, ["prompts", "list"])
    assert listed.exit_code == 0
    assert "Prompt Templates" in listed.output

    shown = runner.invoke(mmctl_main.main, ["prompts", "show", "drafter_v1"])
    assert shown.exit_code == 0
    assert "id=drafter_v1" in shown.output

    tested = runner.invoke(mmctl_main.main, ["prompts", "test", "drafter_v1", "--query", "hello"])
    assert tested.exit_code == 0
    assert "Q: hello" in tested.output


def test_cli_report_generate_and_exports(monkeypatch, tmp_path) -> None:
    runner = CliRunner()
    result_obj = _FakeAskResult(
        answer="ok",
        mode="single",
        provider="openai",
        model="m",
        tokens_in=1,
        tokens_out=1,
        cost=0.01,
        warnings=[],
    )
    monkeypatch.setattr(mmctl_main, "_load_orchestrator", lambda _cfg: _FakeOrchestratorAsk(result_obj))
    monkeypatch.setattr(mmctl_main, "_load_artifact_store", lambda _cfg: _FakeArtifactStoreReport())

    reported = runner.invoke(mmctl_main.main, ["report", "generate", "--period", "week", "--format", "terminal"])
    assert reported.exit_code == 0
    assert "Usage Report (week)" in reported.output

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
default_mode: single
providers: {}
budgets: {}
security:
  token: abc
routing: {}
""",
        encoding="utf-8",
    )
    exported_cfg = runner.invoke(mmctl_main.main, ["export", "config", "--format", "json", "--out", str(tmp_path / "cfg.json"), "--config", str(cfg)])
    assert exported_cfg.exit_code == 0
    assert "Exported:" in exported_cfg.output

    policies_dir = tmp_path / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "base.yaml").write_text(
        """
tool_allowlist: [fetch_url]
tool_policies:
  fetch_url:
    max_calls_per_request: 3
    requires_human_approval: false
    allowed_arg_patterns: {}
retrieval_policy:
  domain_allowlist: []
  domain_denylist: [localhost]
high_impact_actions: []
""",
        encoding="utf-8",
    )
    exported_pol = runner.invoke(mmctl_main.main, ["export", "policies", "--policy-dir", str(policies_dir)])
    assert exported_pol.exit_code == 0
    assert "base.yaml" in exported_pol.output

    store = _FakeMemoryStore()
    store.add(
        statement="my password is 123",
        source_type="summary",
        source_ref="src",
        confidence=0.9,
        ttl_days=30,
        reviewed_by=None,
        redaction_status="redacted",
    )
    monkeypatch.setattr(mmctl_main, "_load_memory_components", lambda _cfg: (store, _FakeGovernance()))
    exported_mem = runner.invoke(mmctl_main.main, ["export", "memories"])
    assert exported_mem.exit_code == 0
    assert "REDACTED" in exported_mem.output or "password" not in exported_mem.output.lower()

    exported_art = runner.invoke(mmctl_main.main, ["export", "artifacts", "--since", "2026-02-01"])
    assert exported_art.exit_code == 0
    assert "req-r1" in exported_art.output


def test_cli_discord_start(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr(
        "integrations.discord_bot.run_discord_bot",
        lambda _cfg: None,
    )
    ok = runner.invoke(mmctl_main.main, ["discord", "start"])
    assert ok.exit_code == 0

    monkeypatch.setattr(
        "integrations.discord_bot.run_discord_bot",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fail = runner.invoke(mmctl_main.main, ["discord", "start"])
    assert fail.exit_code != 0
    assert "boom" in fail.output
