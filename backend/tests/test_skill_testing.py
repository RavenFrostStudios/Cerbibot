from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config import SecurityConfig
from orchestrator.security.guardian import Guardian
from orchestrator.skills.testing import run_skill_adversarial_tests


@dataclass
class _AskResult:
    answer: str
    cost: float


class _Budgets:
    def check_would_fit(self, _estimated_cost: float) -> None:
        return None


class _FakeOrchestrator:
    def __init__(self, usage_file: str):
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

    async def ask(self, **_kwargs):
        return _AskResult(answer="ok", cost=0.1)


@pytest.mark.asyncio
async def test_run_skill_adversarial_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    skill_path = tmp_path / "readme.workflow.yaml"
    skill_path.write_text(
        """
name: readme_check
manifest:
  purpose: "Adversarial fixture test for file read workflow."
  tools: [file_read]
  data_scope: ["workspace_readme"]
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
steps:
  - tool: file_read
    args:
      path: $input.path
    output: readme
  - model_call: "Summarize: $readme"
    output: out
""",
        encoding="utf-8",
    )
    fixtures = tmp_path / "fixtures.yaml"
    fixtures.write_text(
        """
- id: safe
  input:
    path: README.md
  expect_error: false
- id: escape
  input:
    path: ../etc/passwd
  expect_error: true
  error_contains: escapes workspace root
""",
        encoding="utf-8",
    )
    orch = _FakeOrchestrator(usage_file=str(tmp_path / "usage.json"))
    summary = await run_skill_adversarial_tests(
        orch,
        skill_path=str(skill_path),
        fixtures_path=str(fixtures),
    )
    assert summary["total"] == 2
    assert summary["failed"] == 0
