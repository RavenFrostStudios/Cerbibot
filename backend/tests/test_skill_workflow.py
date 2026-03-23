from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config import SecurityConfig
from orchestrator.security.guardian import Guardian
from orchestrator.skills.registry import SkillRecord
from orchestrator.skills.workflow import run_workflow_skill


@dataclass
class _AskResult:
    answer: str
    cost: float


class _Budgets:
    def check_would_fit(self, _estimated_cost: float) -> None:
        return None


class _FakeOrchestrator:
    def __init__(self, usage_file: str, answer: str = "ok", cost: float = 0.2):
        self.config = SimpleNamespace(
            security=SecurityConfig(
                block_on_secrets=True,
                redact_logs=True,
                tool_allowlist=[],
                retrieval_domain_allowlist=[],
                retrieval_domain_denylist=[],
            ),
            budgets=SimpleNamespace(usage_file=usage_file),
        )
        self.guardian = Guardian(self.config.security)
        self.budgets = _Budgets()
        self.cipher = None
        self._answer = answer
        self._cost = cost
        self.ask_queries: list[str] = []

    async def ask(
        self,
        query: str,
        mode: str | None = None,
        provider: str | None = None,
        verbose: bool = False,
        context_messages=None,
        fact_check: bool = False,
        tools: str | None = None,
    ) -> _AskResult:
        _ = (mode, provider, verbose, context_messages, fact_check, tools)
        self.ask_queries.append(query)
        return _AskResult(answer=self._answer, cost=self._cost)


@pytest.mark.asyncio
async def test_workflow_skill_runs_tool_and_model_steps(tmp_path: Path) -> None:
    skill = tmp_path / "sample.workflow.yaml"
    skill.write_text(
        """
name: sample
manifest:
  purpose: "Test workflow with tool and model call."
  tools: [regex_test]
  data_scope: ["test_input"]
  permissions: ["read", "model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
budget_cap_usd: 1.0
steps:
  - tool: regex_test
    args:
      pattern: "needle"
      text: "$input.text"
    output: rx
  - model_call: "Summarize tool result: $rx"
    mode: single
    output: summary
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="done", cost=0.2)
    result = await run_workflow_skill(
        orch,
        skill_path=str(skill),
        input_data={"text": "needle in haystack"},
        mode="single",
        provider=None,
        budget_cap_usd=None,
    )
    assert result.skill_name == "sample"
    assert result.steps_executed == 2
    assert result.total_cost == 0.2
    assert result.outputs["rx"]["tool"] == "regex_test"
    assert result.outputs["summary"] == "done"
    assert orch.ask_queries and "regex_test" in orch.ask_queries[0]


@pytest.mark.asyncio
async def test_workflow_skill_budget_cap_enforced(tmp_path: Path) -> None:
    skill = tmp_path / "budget.workflow.yaml"
    skill.write_text(
        """
name: budget_check
manifest:
  purpose: "Budget cap enforcement test workflow."
  tools: [system_info]
  data_scope: ["none"]
  permissions: ["model_call"]
  approval_policy: draft_only
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 0.1
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
budget_cap_usd: 0.1
steps:
  - model_call: "expensive call"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="done", cost=0.5)
    with pytest.raises(ValueError, match="Workflow budget exceeded"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_requires_manifest(tmp_path: Path) -> None:
    skill = tmp_path / "missing-manifest.workflow.yaml"
    skill.write_text(
        """
name: missing_manifest
steps:
  - model_call: "hello"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="Invalid skill manifest"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_denies_high_risk_when_auto_low_only(tmp_path: Path) -> None:
    skill = tmp_path / "deny-high.workflow.yaml"
    skill.write_text(
        """
name: deny_high
manifest:
  purpose: "Deny high-risk actions in auto-low policy."
  tools: [python_exec]
  data_scope: ["workspace"]
  permissions: ["execute"]
  approval_policy: auto_execute_low_risk
  rate_limits:
    actions_per_hour: 10
  budgets:
    usd_cap: 1.0
  kill_switch:
    enabled: true
  audit_sink: "audit.jsonl"
  failure_mode: "safe_abort"
steps:
  - tool: python_exec
    args:
      code: "print('hello')"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="denied by approval policy"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_denies_medium_risk_when_draft_only(tmp_path: Path) -> None:
    skill = tmp_path / "deny-medium.workflow.yaml"
    skill.write_text(
        """
name: deny_medium
manifest:
  purpose: "Deny medium-risk actions in draft-only policy."
  tools: [web_retrieve]
  data_scope: ["network_fetch"]
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
  - tool: web_retrieve
    args:
      url: "https://example.com/update-config"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="denied by approval policy"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_requires_shadow_confirm_for_high_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This test targets shadow-confirm behavior specifically, not certification.
    # Disable cert gate to keep scope focused.
    monkeypatch.setenv("MMO_REQUIRE_SKILL_CERT_FOR_ELEVATED", "0")
    skill = tmp_path / "shadow-required.workflow.yaml"
    skill.write_text(
        """
name: shadow_required
manifest:
  purpose: "Require shadow confirmation for risky action."
  tools: [python_exec]
  data_scope: ["workspace"]
  permissions: ["execute"]
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
  - tool: python_exec
    args:
      code: "print('shadow')"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="Shadow run confirmation required"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_blocks_elevated_when_uncertified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_REQUIRE_SKILL_CERT_FOR_ELEVATED", "1")
    skill = tmp_path / "elevated-uncertified.workflow.yaml"
    skill.write_text(
        """
name: elevated_uncertified
manifest:
  purpose: "Should fail certification gate."
  tools: [python_exec]
  data_scope: ["workspace"]
  permissions: ["execute"]
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
  - tool: python_exec
    args:
      code: "print('x')"
    output: out
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="requires a certified installed skill"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_workflow_skill_allows_elevated_when_certified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_REQUIRE_SKILL_CERT_FOR_ELEVATED", "1")
    skill = tmp_path / "elevated-certified.workflow.yaml"
    skill.write_text(
        """
name: elevated_certified
manifest:
  purpose: "Certified elevated execution."
  tools: [python_exec]
  data_scope: ["workspace"]
  permissions: ["execute"]
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
  - tool: python_exec
    args:
      code: "print('x')"
    output: out
""",
        encoding="utf-8",
    )
    resolved = str(skill.expanduser().resolve())
    monkeypatch.setattr(
        "orchestrator.skills.workflow.discover_skills",
        lambda: {
            "elevated_certified": SkillRecord(
                name="elevated_certified",
                path=resolved,
                enabled=True,
                checksum="sha256:test",
                signature_verified=True,
                signature_file="sig.json",
            )
        },
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"), answer="ok", cost=0.01)
    with pytest.raises(ValueError, match="Shadow run confirmation required"):
        await run_workflow_skill(
            orch,
            skill_path=str(skill),
            input_data={},
            mode="single",
            provider=None,
            budget_cap_usd=None,
        )
