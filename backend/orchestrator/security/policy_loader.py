from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from orchestrator.security.policy import RetrievalPolicy, SecurityPolicy, ToolPolicy


class PolicyError(ValueError):
    pass


def load_policy_file(path: str) -> SecurityPolicy:
    p = Path(path).expanduser()
    if not p.exists():
        raise PolicyError(f"Policy file not found: {path}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PolicyError("Policy root must be a mapping")

    allow = [str(v) for v in list(raw.get("tool_allowlist", []))]
    high_impact = [str(v) for v in list(raw.get("high_impact_actions", []))]
    retrieval = raw.get("retrieval_policy", {}) or {}
    if not isinstance(retrieval, dict):
        raise PolicyError("retrieval_policy must be a mapping")
    policies_raw = raw.get("tool_policies", {}) or {}
    if not isinstance(policies_raw, dict):
        raise PolicyError("tool_policies must be a mapping")

    tool_policies: dict[str, ToolPolicy] = {}
    for name, item in policies_raw.items():
        if not isinstance(item, dict):
            raise PolicyError(f"tool_policies.{name} must be a mapping")
        tool_policies[str(name)] = ToolPolicy(
            name=str(name),
            max_calls_per_request=int(item.get("max_calls_per_request", 5)),
            requires_human_approval=bool(item.get("requires_human_approval", False)),
            allowed_arg_patterns={str(k): str(v) for k, v in dict(item.get("allowed_arg_patterns", {}) or {}).items()},
        )
    for tool in allow:
        tool_policies.setdefault(tool, ToolPolicy(name=tool))

    policy = SecurityPolicy(
        tool_allowlist=allow,
        tool_policies=tool_policies,
        retrieval_policy=RetrievalPolicy(
            domain_allowlist=[str(v) for v in list(retrieval.get("domain_allowlist", []))],
            domain_denylist=[str(v) for v in list(retrieval.get("domain_denylist", []))],
        ),
        high_impact_actions=high_impact,
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: SecurityPolicy) -> None:
    if len(set(policy.tool_allowlist)) != len(policy.tool_allowlist):
        raise PolicyError("tool_allowlist contains duplicates")
    for name in policy.tool_allowlist:
        if name not in policy.tool_policies:
            raise PolicyError(f"Missing tool policy for allowlisted tool: {name}")
    for name, item in policy.tool_policies.items():
        if item.max_calls_per_request <= 0:
            raise PolicyError(f"tool_policies.{name}.max_calls_per_request must be > 0")
    overlap = set(policy.retrieval_policy.domain_allowlist).intersection(policy.retrieval_policy.domain_denylist)
    if overlap:
        raise PolicyError(f"retrieval_policy allow/deny overlap: {sorted(overlap)}")


def policy_hash(policy: SecurityPolicy) -> str:
    payload = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def policy_diff(baseline: SecurityPolicy, current: SecurityPolicy) -> dict[str, Any]:
    base_tools = set(baseline.tool_allowlist)
    cur_tools = set(current.tool_allowlist)
    base_allow = set(baseline.retrieval_policy.domain_allowlist)
    cur_allow = set(current.retrieval_policy.domain_allowlist)
    base_deny = set(baseline.retrieval_policy.domain_denylist)
    cur_deny = set(current.retrieval_policy.domain_denylist)

    widened_tools = sorted(cur_tools - base_tools)
    tightened_tools = sorted(base_tools - cur_tools)
    widened_domains = sorted(cur_allow - base_allow)
    tightened_domains = sorted(base_allow - cur_allow)
    relaxed_denies = sorted(base_deny - cur_deny)
    stricter_denies = sorted(cur_deny - base_deny)

    relaxed_calls: list[str] = []
    stricter_calls: list[str] = []
    for name, cur in current.tool_policies.items():
        base = baseline.tool_policies.get(name)
        if base is None:
            continue
        if cur.max_calls_per_request > base.max_calls_per_request:
            relaxed_calls.append(name)
        elif cur.max_calls_per_request < base.max_calls_per_request:
            stricter_calls.append(name)

    return {
        "widenings": {
            "new_tools": widened_tools,
            "new_allowed_domains": widened_domains,
            "relaxed_denylists": relaxed_denies,
            "relaxed_tool_call_limits": sorted(relaxed_calls),
        },
        "tightenings": {
            "removed_tools": tightened_tools,
            "removed_allowed_domains": tightened_domains,
            "stricter_denylists": stricter_denies,
            "stricter_tool_call_limits": sorted(stricter_calls),
        },
    }
