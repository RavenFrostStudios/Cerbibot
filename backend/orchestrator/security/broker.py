from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from orchestrator.budgets import BudgetTracker
from orchestrator.observability.audit import AuditLogger
from orchestrator.security.guardian import Guardian
from orchestrator.security.human_gate import HumanGate
from orchestrator.security.policy import SecurityPolicy, is_tool_allowed
from orchestrator.security.taint import TaintedString


@dataclass(slots=True)
class RequestContext:
    request_id: str
    requester: str
    estimated_cost: float = 0.0
    approved_plan_tools: list[str] | None = None


@dataclass(slots=True)
class CapabilityToken:
    capability_id: str
    request_id: str
    tool_name: str
    scope: dict[str, str]
    ttl_seconds: int
    requires_human_approval: bool
    created_at: str
    policy_hash: str


@dataclass(slots=True)
class Denial:
    request_id: str
    tool_name: str
    reason: str
    details: dict[str, Any]


class CapabilityBroker:
    """Deterministic authorization layer for tool execution."""

    def __init__(
        self,
        *,
        policy: SecurityPolicy,
        guardian: Guardian,
        budgets: BudgetTracker,
        audit_logger: AuditLogger,
        human_gate: HumanGate,
    ):
        self.policy = policy
        self.guardian = guardian
        self.budgets = budgets
        self.audit = audit_logger
        self.human_gate = human_gate
        self._calls_per_request: dict[str, dict[str, int]] = {}

    def request_capability(
        self,
        *,
        tool_name: str,
        args: dict[str, TaintedString | str],
        request_context: RequestContext,
        ttl_seconds: int = 120,
    ) -> CapabilityToken | Denial:
        if not is_tool_allowed(self.policy, tool_name):
            return self._deny(request_context.request_id, tool_name, "Tool is not in allowlist", {"tool_name": tool_name})

        if request_context.approved_plan_tools and tool_name not in request_context.approved_plan_tools:
            return self._deny(
                request_context.request_id,
                tool_name,
                "Tool not present in approved plan",
                {"approved_plan_tools": request_context.approved_plan_tools},
            )

        per_tool = self.policy.tool_policies.get(tool_name)
        if per_tool is None:
            return self._deny(request_context.request_id, tool_name, "Missing tool policy", {})

        usage = self._calls_per_request.setdefault(request_context.request_id, {})
        current_calls = usage.get(tool_name, 0)
        if current_calls >= per_tool.max_calls_per_request:
            return self._deny(
                request_context.request_id,
                tool_name,
                "Tool call limit exceeded for request",
                {"max_calls_per_request": per_tool.max_calls_per_request},
            )

        try:
            validated_scope = self.guardian.validate_tool_arguments(
                tool_name,
                args,
                arg_patterns=per_tool.allowed_arg_patterns,
            )
        except Exception as exc:
            return self._deny(
                request_context.request_id,
                tool_name,
                "Tool argument validation failed",
                {"error": str(exc)},
            )

        try:
            self.budgets.check_would_fit(request_context.estimated_cost)
        except Exception as exc:
            return self._deny(
                request_context.request_id,
                tool_name,
                "Budget check failed",
                {"error": str(exc), "estimated_cost": request_context.estimated_cost},
            )

        requires_human = per_tool.requires_human_approval or tool_name in self.policy.high_impact_actions
        if requires_human:
            approved = self.human_gate.request_approval(
                tool_name=tool_name,
                args=validated_scope,
                reason="high-impact action",
            )
            if not approved:
                return self._deny(
                    request_context.request_id,
                    tool_name,
                    "Human approval denied",
                    {"requires_human_approval": True},
                )

        usage[tool_name] = current_calls + 1
        token = CapabilityToken(
            capability_id=str(uuid4()),
            request_id=request_context.request_id,
            tool_name=tool_name,
            scope=validated_scope,
            ttl_seconds=ttl_seconds,
            requires_human_approval=requires_human,
            created_at=datetime.now(timezone.utc).isoformat(),
            policy_hash=self._policy_hash(),
        )
        self.audit.write(
            "capability_granted",
            {
                "request_id": request_context.request_id,
                "tool_name": tool_name,
                "scope": validated_scope,
                "requires_human_approval": requires_human,
                "capability_id": token.capability_id,
            },
        )
        return token

    def execute_with_capability(
        self,
        *,
        token: CapabilityToken,
        executor: Callable[[dict[str, str]], dict[str, Any]],
    ) -> dict[str, Any] | Denial:
        created = datetime.fromisoformat(token.created_at)
        if datetime.now(timezone.utc) > created + timedelta(seconds=token.ttl_seconds):
            return self._deny(token.request_id, token.tool_name, "Capability token expired", {"capability_id": token.capability_id})

        result = executor(token.scope)
        self.audit.write(
            "capability_executed",
            {
                "request_id": token.request_id,
                "tool_name": token.tool_name,
                "scope": token.scope,
                "capability_id": token.capability_id,
                "result": result,
            },
        )
        return result

    def _policy_hash(self) -> str:
        payload = json.dumps(asdict(self.policy), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _deny(self, request_id: str, tool_name: str, reason: str, details: dict[str, Any]) -> Denial:
        denial = Denial(request_id=request_id, tool_name=tool_name, reason=reason, details=details)
        self.audit.write(
            "capability_denied",
            {
                "request_id": request_id,
                "tool_name": tool_name,
                "reason": reason,
                "details": details,
            },
        )
        return denial
