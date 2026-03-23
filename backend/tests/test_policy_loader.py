from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.security.policy_loader import PolicyError, load_policy_file, policy_diff, policy_hash


def test_load_policy_file_and_hash(tmp_path: Path) -> None:
    p = tmp_path / "p.yaml"
    p.write_text(
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
high_impact_actions: [file_write]
""",
        encoding="utf-8",
    )
    policy = load_policy_file(str(p))
    assert policy.tool_allowlist == ["fetch_url"]
    assert policy_hash(policy)


def test_policy_diff_detects_widening(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    cur = tmp_path / "cur.yaml"
    base.write_text(
        """
tool_allowlist: [fetch_url]
tool_policies:
  fetch_url:
    max_calls_per_request: 2
    requires_human_approval: false
    allowed_arg_patterns: {}
retrieval_policy:
  domain_allowlist: []
  domain_denylist: [localhost]
high_impact_actions: []
""",
        encoding="utf-8",
    )
    cur.write_text(
        """
tool_allowlist: [fetch_url, web_search]
tool_policies:
  fetch_url:
    max_calls_per_request: 5
    requires_human_approval: false
    allowed_arg_patterns: {}
  web_search:
    max_calls_per_request: 5
    requires_human_approval: false
    allowed_arg_patterns: {}
retrieval_policy:
  domain_allowlist: [docs.python.org]
  domain_denylist: []
high_impact_actions: []
""",
        encoding="utf-8",
    )
    diff = policy_diff(load_policy_file(str(base)), load_policy_file(str(cur)))
    assert "web_search" in diff["widenings"]["new_tools"]
    assert "docs.python.org" in diff["widenings"]["new_allowed_domains"]


def test_load_policy_file_rejects_invalid(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("tool_allowlist: [a, a]\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy_file(str(p))
