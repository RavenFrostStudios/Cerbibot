from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.observability.audit import AuditLogger
from orchestrator.security.broker import CapabilityBroker, CapabilityToken, RequestContext
from orchestrator.security.human_gate import HumanGate
from orchestrator.security.policy import ToolPolicy, build_security_policy
from orchestrator.security.taint import TaintedString
from orchestrator.skills.registry import discover_skills, validate_skill_manifest
from orchestrator.tools.registry import build_policy_overrides_from_manifest, execute_tool, load_tool_registry


@dataclass(slots=True)
class WorkflowRunResult:
    skill_name: str
    outputs: dict[str, Any]
    steps_executed: int
    total_cost: float


async def run_workflow_skill(
    orchestrator: Any,
    *,
    skill_path: str,
    input_data: dict[str, Any] | None = None,
    mode: str = "single",
    provider: str | None = None,
    budget_cap_usd: float | None = None,
) -> WorkflowRunResult:
    raw = yaml.safe_load(Path(skill_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Skill file root must be a mapping")

    name = str(raw.get("name", Path(skill_path).stem))
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Skill must define a non-empty steps list")
    manifest_errors = validate_skill_manifest(raw, steps=steps)
    if manifest_errors:
        raise ValueError("Invalid skill manifest: " + "; ".join(manifest_errors))
    manifest = raw.get("manifest", {})
    assert isinstance(manifest, dict)
    approval_policy = str(manifest.get("approval_policy", "draft_only"))
    if _require_skill_cert_for_elevated() and _is_elevated_skill_manifest(manifest):
        if not _is_certified_skill(skill_path):
            raise ValueError(
                "Elevated skill execution requires a certified installed skill "
                "(signature verified). Install/enable the skill with signature verification."
            )

    budget_cap = float(raw.get("budget_cap_usd", budget_cap_usd if budget_cap_usd is not None else 2.0))
    if budget_cap <= 0:
        raise ValueError("budget_cap_usd must be > 0")

    rendered_inputs = dict(input_data or {})
    outputs: dict[str, Any] = {}
    total_cost = 0.0

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Invalid step at index {index}: expected mapping")
        if "tool" in step:
            result = _run_tool_step(
                orchestrator,
                step=step,
                inputs=rendered_inputs,
                outputs=outputs,
                step_index=index,
                skill_name=name,
                approval_policy=approval_policy,
                skill_manifest=manifest,
            )
            key = str(step.get("output", f"step_{index}"))
            outputs[key] = result
            continue
        if "model_call" in step:
            prompt_template = str(step["model_call"])
            prompt = _render_template(prompt_template, rendered_inputs, outputs)
            model_mode = str(step.get("mode", mode))
            result = await orchestrator.ask(
                query=prompt,
                mode=model_mode,
                provider=provider,
                verbose=False,
                fact_check=False,
                tools=None,
            )
            total_cost += float(getattr(result, "cost", 0.0))
            if total_cost > budget_cap:
                raise ValueError(
                    f"Workflow budget exceeded: {total_cost:.6f} > {budget_cap:.6f} "
                    f"(step {index})"
                )
            key = str(step.get("output", f"step_{index}"))
            outputs[key] = str(getattr(result, "answer", ""))
            continue
        raise ValueError(f"Step {index} must define either 'tool' or 'model_call'")

    return WorkflowRunResult(
        skill_name=name,
        outputs=outputs,
        steps_executed=len(steps),
        total_cost=total_cost,
    )


def _run_tool_step(
    orchestrator: Any,
    *,
    step: dict[str, Any],
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    step_index: int,
    skill_name: str,
    approval_policy: str,
    skill_manifest: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(step.get("tool", "")).strip()
    if not tool_name:
        raise ValueError(f"Tool step {step_index} missing tool name")

    registry = load_tool_registry()
    manifest = registry.get(tool_name)
    if manifest is None:
        raise ValueError(f"Tool step {step_index} references unknown tool: {tool_name}")

    raw_args = step.get("args", {})
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ValueError(f"Tool step {step_index} args must be a mapping")
    rendered_args = {
        str(key): _stringify(_render_value(value, inputs, outputs))
        for key, value in raw_args.items()
    }
    risk = _assess_tool_step_risk(tool_name, rendered_args, skill_manifest)
    audit_path = Path(orchestrator.config.budgets.usage_file).expanduser().with_name("audit.jsonl")
    audit_logger = AuditLogger(str(audit_path), cipher=getattr(orchestrator, "cipher", None))
    if approval_policy in {"approve_actions", "approve_high_risk"} and _should_require_shadow_run(risk):
        if not _shadow_confirmed(inputs):
            report = _build_shadow_run_report(
                skill_name=skill_name,
                step_index=step_index,
                tool_name=tool_name,
                args=rendered_args,
                risk=risk,
            )
            audit_logger.write(
                "skill_shadow_run_required",
                {
                    "skill_name": skill_name,
                    "tool_name": tool_name,
                    "step_index": step_index,
                    "approval_policy": approval_policy,
                    "report": report,
                },
            )
            raise ValueError(
                "Shadow run confirmation required before executing this step. "
                "Set input _shadow_confirm=true and rerun. "
                f"PredictedOutcomeReport={json.dumps(report, ensure_ascii=True)}"
            )
    human_required, denied = _risk_policy_decision(
        approval_policy=approval_policy,
        risk_level=risk["risk_level"],
    )
    if denied:
        raise ValueError(
            f"Tool step {step_index} denied by approval policy '{approval_policy}' "
            f"for risk={risk['risk_level']}: {risk['reason']}"
        )

    policy = build_security_policy(orchestrator.config.security)
    if tool_name not in policy.tool_allowlist:
        policy.tool_allowlist.append(tool_name)
    overrides = build_policy_overrides_from_manifest(manifest)
    policy.tool_policies[tool_name] = ToolPolicy(
        name=tool_name,
        max_calls_per_request=int(overrides["max_calls_per_request"]),
        requires_human_approval=bool(overrides["requires_human_approval"] or human_required),
        allowed_arg_patterns=dict(overrides["allowed_arg_patterns"]),
    )
    audit_logger.write(
        "skill_risk_assessed",
        {
            "skill_name": skill_name,
            "tool_name": tool_name,
            "step_index": step_index,
            "approval_policy": approval_policy,
            "risk_level": risk["risk_level"],
            "risk_score": risk["risk_score"],
            "risk_reason": risk["reason"],
            "human_required": human_required,
            "denied": denied,
        },
    )
    broker = CapabilityBroker(
        policy=policy,
        guardian=orchestrator.guardian,
        budgets=orchestrator.budgets,
        audit_logger=audit_logger,
        human_gate=HumanGate(),
    )
    tainted_args = {
        key: TaintedString(value=value, source="user_input", source_id=f"workflow:{step_index}:{key}", taint_level="untrusted")
        for key, value in rendered_args.items()
    }
    decision = broker.request_capability(
        tool_name=tool_name,
        args=tainted_args,
        request_context=RequestContext(
            request_id=f"skill-{uuid4()}",
            requester="mmctl.skill.run",
            estimated_cost=0.0,
            approved_plan_tools=[tool_name],
        ),
    )
    if not isinstance(decision, CapabilityToken):
        raise ValueError(f"Tool step {step_index} denied: {decision.reason}")

    executed = broker.execute_with_capability(
        token=decision,
        executor=lambda scope: execute_tool(manifest, scope, orchestrator.guardian),
    )
    if not isinstance(executed, dict):
        raise ValueError(f"Tool step {step_index} execution denied: {executed.reason}")
    return executed


def _render_value(value: Any, inputs: dict[str, Any], outputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_template(value, inputs, outputs)
    if isinstance(value, dict):
        return {str(k): _render_value(v, inputs, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(item, inputs, outputs) for item in value]
    return value


def _render_template(template: str, inputs: dict[str, Any], outputs: dict[str, Any]) -> str:
    text = template
    for key, value in inputs.items():
        text = text.replace(f"$input.{key}", _stringify(value))
    for key, value in outputs.items():
        text = text.replace(f"${key}", _stringify(value))
    return text


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=True)


def _assess_tool_step_risk(tool_name: str, args: dict[str, str], manifest: dict[str, Any]) -> dict[str, Any]:
    text = " ".join([tool_name] + [str(v) for v in args.values()]).lower()
    score = 0
    reasons: list[str] = []
    destructive_or_access = False

    # Base tool risk
    if tool_name in {"python_exec"}:
        score += 3
        reasons.append("sandboxed code execution tool")
        destructive_or_access = True
    elif tool_name in {"web_retrieve"}:
        score += 1
        reasons.append("network access tool")

    # Keyword risk
    high_markers = (
        "delete",
        "drop",
        "truncate",
        "root",
        "admin",
        "grant",
        "sudo",
        "credential",
        "token",
        "key",
        "password",
        "shutdown",
        "kill",
        "firewall",
        "global",
        "all users",
        "everyone",
    )
    medium_markers = ("write", "modify", "update", "config", "permission", "access")
    for marker in high_markers:
        if marker in text:
            score += 2
            reasons.append(f"high-risk marker: {marker}")
            destructive_or_access = True
            break
    for marker in medium_markers:
        if marker in text:
            score += 1
            reasons.append(f"medium-risk marker: {marker}")
            destructive_or_access = True
            break

    # Manifest-declared risk bias (if present)
    manifest_risk = str(manifest.get("risk_level", "")).strip().lower()
    if manifest_risk == "high":
        score += 1
        reasons.append("manifest risk_level=high")
    elif manifest_risk == "medium":
        score += 1
        reasons.append("manifest risk_level=medium")

    if score >= 3:
        level = "high"
    elif score >= 2:
        level = "medium"
    else:
        level = "low"
    reason = ", ".join(reasons) if reasons else "no elevated markers"
    return {
        "risk_level": level,
        "risk_score": score,
        "reason": reason,
        "destructive_or_access": destructive_or_access,
    }


def _risk_policy_decision(*, approval_policy: str, risk_level: str) -> tuple[bool, bool]:
    # Returns (human_required, denied)
    if risk_level == "low":
        return (False, False)

    if approval_policy == "approve_actions":
        return (True, False)
    if approval_policy == "approve_high_risk":
        if risk_level == "high":
            return (True, False)
        return (False, False)
    if approval_policy == "draft_only":
        return (False, True)
    if approval_policy == "auto_execute_low_risk":
        return (False, True)
    # Unknown policy should not happen after validation; fail closed.
    return (False, True)


def _should_require_shadow_run(risk: dict[str, Any]) -> bool:
    level = str(risk.get("risk_level", "low"))
    impactful = bool(risk.get("destructive_or_access"))
    return level in {"medium", "high"} and impactful


def _shadow_confirmed(inputs: dict[str, Any]) -> bool:
    raw = inputs.get("_shadow_confirm", False)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _build_shadow_run_report(
    *,
    skill_name: str,
    step_index: int,
    tool_name: str,
    args: dict[str, str],
    risk: dict[str, Any],
) -> dict[str, Any]:
    scope_targets = [key for key in args.keys() if key in {"path", "url", "code", "query"}]
    scope_count = len(scope_targets) if scope_targets else max(1, len(args))
    if tool_name == "python_exec":
        rollback = "Review script side effects and restore affected files/state from backup or VCS."
    elif tool_name in {"file_read", "file_search", "git_status", "json_query", "regex_test", "system_info", "web_retrieve"}:
        rollback = "No direct write expected; verify no downstream automation was triggered."
    else:
        rollback = "Review tool output impact and execute documented rollback procedure for affected target."
    return {
        "skill_name": skill_name,
        "step_index": step_index,
        "tool_name": tool_name,
        "risk_level": risk.get("risk_level", "low"),
        "risk_reason": risk.get("reason", ""),
        "predicted_scope_count": scope_count,
        "affected_targets": scope_targets,
        "rollback_hint": rollback,
    }


def _require_skill_cert_for_elevated() -> bool:
    raw = str(os.getenv("MMO_REQUIRE_SKILL_CERT_FOR_ELEVATED", "1")).strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _is_elevated_skill_manifest(manifest: dict[str, Any]) -> bool:
    policy = str(manifest.get("approval_policy", "")).strip().lower()
    return policy in {"approve_actions", "approve_high_risk"}


def _is_certified_skill(skill_path: str) -> bool:
    try:
        target = Path(skill_path).expanduser().resolve()
        rows = discover_skills()
    except Exception:
        return False
    for record in rows.values():
        if not bool(getattr(record, "enabled", False)):
            continue
        if not bool(getattr(record, "signature_verified", False)):
            continue
        record_path = Path(str(getattr(record, "path", ""))).expanduser()
        try:
            if record_path.resolve() == target:
                return True
        except Exception:
            continue
    return False
