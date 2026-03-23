from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.budgets import BudgetTracker
from orchestrator.config import BudgetConfig, SecurityConfig
from orchestrator.observability.audit import AuditLogger
from orchestrator.security.broker import CapabilityBroker, CapabilityToken, RequestContext
from orchestrator.security.guardian import Guardian
from orchestrator.security.human_gate import HumanGate
from orchestrator.security.policy import ToolPolicy, build_security_policy
from orchestrator.security.taint import TaintedString


class _DenyGate(HumanGate):
    def __init__(self):
        super().__init__(input_fn=lambda _prompt: "n")


class _AllowGate(HumanGate):
    def __init__(self):
        super().__init__(input_fn=lambda _prompt: "y")


def _security_config() -> SecurityConfig:
    return SecurityConfig(
        block_on_secrets=True,
        redact_logs=True,
        tool_allowlist=["fetch_url", "web_search"],
        retrieval_domain_allowlist=["example.com"],
        retrieval_domain_denylist=["localhost"],
    )


def _budget_tracker(tmp_path) -> BudgetTracker:
    return BudgetTracker(
        BudgetConfig(
            session_usd_cap=10.0,
            daily_usd_cap=10.0,
            monthly_usd_cap=10.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )


def _make_broker(tmp_path, gate: HumanGate) -> CapabilityBroker:
    config = _security_config()
    policy = build_security_policy(config)
    return CapabilityBroker(
        policy=policy,
        guardian=Guardian(config),
        budgets=_budget_tracker(tmp_path),
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        human_gate=gate,
    )


def test_broker_grants_valid_capability(tmp_path) -> None:
    broker = _make_broker(tmp_path, _AllowGate())
    decision = broker.request_capability(
        tool_name="fetch_url",
        args={"url": TaintedString("https://example.com/docs", source="user_input", source_id="u1", taint_level="validated")},
        request_context=RequestContext(request_id="r1", requester="test", approved_plan_tools=["fetch_url"]),
    )
    assert isinstance(decision, CapabilityToken)
    assert decision.tool_name == "fetch_url"
    assert decision.scope["url"] == "https://example.com/docs"


def test_broker_denies_non_allowlisted_tool(tmp_path) -> None:
    broker = _make_broker(tmp_path, _AllowGate())
    decision = broker.request_capability(
        tool_name="shell.exec",
        args={"cmd": "ls"},
        request_context=RequestContext(request_id="r1", requester="test", approved_plan_tools=["shell.exec"]),
    )
    assert getattr(decision, "reason", "") == "Tool is not in allowlist"


def test_broker_denies_invalid_tainted_arg(tmp_path) -> None:
    broker = _make_broker(tmp_path, _AllowGate())
    decision = broker.request_capability(
        tool_name="fetch_url",
        args={"url": TaintedString("go to https://example.com now", source="user_input", source_id="u1", taint_level="untrusted")},
        request_context=RequestContext(request_id="r1", requester="test", approved_plan_tools=["fetch_url"]),
    )
    assert "validation failed" in getattr(decision, "reason", "").lower()


def test_broker_denies_when_human_gate_rejects(tmp_path) -> None:
    config = _security_config()
    policy = build_security_policy(config)
    policy.tool_policies["fetch_url"] = ToolPolicy(name="fetch_url", requires_human_approval=True)

    broker = CapabilityBroker(
        policy=policy,
        guardian=Guardian(config),
        budgets=_budget_tracker(tmp_path),
        audit_logger=AuditLogger(str(tmp_path / "audit.jsonl")),
        human_gate=_DenyGate(),
    )

    decision = broker.request_capability(
        tool_name="fetch_url",
        args={"url": TaintedString("https://example.com/docs", source="user_input", source_id="u1", taint_level="validated")},
        request_context=RequestContext(request_id="r1", requester="test", approved_plan_tools=["fetch_url"]),
    )
    assert getattr(decision, "reason", "") == "Human approval denied"


def test_broker_execute_with_capability_expired(tmp_path) -> None:
    broker = _make_broker(tmp_path, _AllowGate())
    token = CapabilityToken(
        capability_id="c1",
        request_id="r1",
        tool_name="fetch_url",
        scope={"url": "https://example.com"},
        ttl_seconds=1,
        requires_human_approval=False,
        created_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        policy_hash="h",
    )
    result = broker.execute_with_capability(token=token, executor=lambda _scope: {"ok": True})
    assert getattr(result, "reason", "") == "Capability token expired"


def test_broker_execute_success(tmp_path) -> None:
    broker = _make_broker(tmp_path, _AllowGate())
    decision = broker.request_capability(
        tool_name="fetch_url",
        args={"url": TaintedString("https://example.com/docs", source="user_input", source_id="u1", taint_level="validated")},
        request_context=RequestContext(request_id="r1", requester="test", approved_plan_tools=["fetch_url"]),
    )
    assert isinstance(decision, CapabilityToken)
    out = broker.execute_with_capability(token=decision, executor=lambda scope: {"url": scope["url"]})
    assert out["url"] == "https://example.com/docs"
