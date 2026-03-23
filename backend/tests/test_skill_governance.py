from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.skills.governance import analyze_skill_bloat
from orchestrator.skills.registry import SkillRecord


def _write_skill(path: Path, body: str) -> str:
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_analyze_skill_bloat_normalizes_synonyms_and_io_names(monkeypatch, tmp_path: Path) -> None:
    alpha_path = _write_skill(
        tmp_path / "alpha.workflow.yaml",
        """
name: alpha_skill
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
    output: source_doc
  - model_call: "summarize $source_doc"
    output: brief
""",
    )
    beta_path = _write_skill(
        tmp_path / "beta.workflow.yaml",
        """
name: beta_skill
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
  - model_call: "summary $page"
    output: final_summary
""",
    )

    def _discover():
        return {
            "alpha_skill": SkillRecord(name="alpha_skill", path=alpha_path, enabled=True, checksum="a"),
            "beta_skill": SkillRecord(name="beta_skill", path=beta_path, enabled=True, checksum="b"),
        }

    monkeypatch.setattr("orchestrator.skills.governance.discover_skills", _discover)
    report = analyze_skill_bloat(out_dir=str(tmp_path / "out"), merge_threshold=0.55)
    merge = (tmp_path / "out" / "merge_candidates.json").read_text(encoding="utf-8")

    assert report["merge_candidates"] == 1
    assert '"recommendation": "merge_candidate"' in merge
    assert "retrieve" in merge
    assert "summarize" in merge


def test_analyze_skill_bloat_filters_false_positive_pairs_with_only_generic_overlap(monkeypatch, tmp_path: Path) -> None:
    alpha_path = _write_skill(
        tmp_path / "alpha.workflow.yaml",
        """
name: alpha_skill
manifest:
  purpose: "Run local repository checks."
  tools: [system_info]
  data_scope: ["local_repo_metadata"]
  permissions: ["read_repo"]
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
    output: repo_state
  - model_call: "summarize $repo_state"
    output: repo_report
""",
    )
    beta_path = _write_skill(
        tmp_path / "beta.workflow.yaml",
        """
name: beta_skill
manifest:
  purpose: "Fetch and summarize external docs."
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
    )

    def _discover():
        return {
            "alpha_skill": SkillRecord(name="alpha_skill", path=alpha_path, enabled=True, checksum="a"),
            "beta_skill": SkillRecord(name="beta_skill", path=beta_path, enabled=True, checksum="b"),
        }

    monkeypatch.setattr("orchestrator.skills.governance.discover_skills", _discover)
    report = analyze_skill_bloat(out_dir=str(tmp_path / "out"))

    assert report["merge_candidates"] == 0
    assert report["crossover_candidates"] == 0
