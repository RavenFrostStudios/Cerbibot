from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.config import SecurityConfig


@dataclass(slots=True)
class ToolPolicy:
    name: str
    max_calls_per_request: int = 5
    requires_human_approval: bool = False
    allowed_arg_patterns: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalPolicy:
    domain_allowlist: list[str]
    domain_denylist: list[str]


@dataclass(slots=True)
class SecurityPolicy:
    tool_allowlist: list[str]
    tool_policies: dict[str, ToolPolicy]
    retrieval_policy: RetrievalPolicy
    high_impact_actions: list[str]


def is_tool_allowed(policy: SecurityPolicy, tool_name: str) -> bool:
    return tool_name in policy.tool_allowlist


def build_security_policy(config: SecurityConfig) -> SecurityPolicy:
    default_policies = {
        name: ToolPolicy(name=name, max_calls_per_request=5, requires_human_approval=False)
        for name in config.tool_allowlist
    }
    high_impact = [
        "external_contact",
        "file_write",
        "credential_use",
        "payment_transfer",
    ]
    return SecurityPolicy(
        tool_allowlist=list(config.tool_allowlist),
        tool_policies=default_policies,
        retrieval_policy=RetrievalPolicy(
            domain_allowlist=list(config.retrieval_domain_allowlist),
            domain_denylist=list(config.retrieval_domain_denylist),
        ),
        high_impact_actions=high_impact,
    )
