from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from orchestrator.skills.signing import compute_skill_checksum, default_signature_path, verify_skill_signature


@dataclass(slots=True)
class SkillRecord:
    name: str
    path: str
    enabled: bool = True
    checksum: str = ""
    signature_verified: bool = False
    signature_file: str = ""


_APPROVAL_POLICIES = {
    "draft_only",
    "approve_actions",
    "approve_high_risk",
    "auto_execute_low_risk",
}


def skills_root_dir() -> Path:
    env_dir = os.getenv("MMO_STATE_DIR")
    state_dir = Path(env_dir).expanduser() if env_dir else Path("~/.mmo").expanduser()
    root = state_dir / "skills"
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except PermissionError:
        fallback = Path("/tmp/mmo/skills")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def registry_file_path() -> Path:
    return skills_root_dir() / "registry.json"


def load_registry() -> dict[str, SkillRecord]:
    path = registry_file_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    out: dict[str, SkillRecord] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        name = str(value.get("name", key))
        skill_path = str(value.get("path", ""))
        enabled = bool(value.get("enabled", True))
        checksum = str(value.get("checksum", ""))
        signature_verified = bool(value.get("signature_verified", False))
        signature_file = str(value.get("signature_file", ""))
        if name and skill_path:
            out[name] = SkillRecord(
                name=name,
                path=skill_path,
                enabled=enabled,
                checksum=checksum,
                signature_verified=signature_verified,
                signature_file=signature_file,
            )
    return out


def save_registry(registry: dict[str, SkillRecord]) -> None:
    path = registry_file_path()
    serializable = {name: asdict(record) for name, record in sorted(registry.items())}
    path.write_text(json.dumps(serializable, indent=2, sort_keys=True), encoding="utf-8")


def discover_skills() -> dict[str, SkillRecord]:
    discovered: dict[str, SkillRecord] = {}
    root = skills_root_dir()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        if path.name not in {"workflow.yaml", "workflow.yml"} and not path.name.endswith(".workflow.yaml"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name", path.stem)).strip()
        if not name:
            continue
        discovered[name] = SkillRecord(
            name=name,
            path=str(path),
            enabled=True,
            checksum=compute_skill_checksum(str(path)),
        )
    registry = load_registry()
    for name, record in registry.items():
        if name in discovered:
            discovered[name].enabled = record.enabled
        else:
            discovered[name] = record
    return discovered


def validate_workflow_file(path: str) -> tuple[bool, list[str], dict[str, Any] | None]:
    errors: list[str] = []
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return False, [f"Skill file not found: {path}"], None
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"Failed to parse YAML: {exc}"], None
    if not isinstance(raw, dict):
        return False, ["Skill root must be a mapping"], None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("Missing non-empty skill name")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("Skill must define a non-empty steps list")
    else:
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(f"Step {index} must be a mapping")
                continue
            if "tool" not in step and "model_call" not in step:
                errors.append(f"Step {index} must include 'tool' or 'model_call'")
    errors.extend(validate_skill_manifest(raw, steps=steps if isinstance(steps, list) else None))
    return not errors, errors, raw


def validate_skill_manifest(raw: dict[str, Any], *, steps: list[Any] | None = None) -> list[str]:
    errors: list[str] = []
    manifest = raw.get("manifest")
    if not isinstance(manifest, dict):
        return ["Skill must define a manifest mapping"]

    purpose = manifest.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        errors.append("manifest.purpose must be a non-empty string")

    tools = manifest.get("tools")
    if not isinstance(tools, list) or not tools or any(not isinstance(item, str) or not item.strip() for item in tools):
        errors.append("manifest.tools must be a non-empty list of tool names")
    declared_tools = {item.strip() for item in tools if isinstance(item, str) and item.strip()} if isinstance(tools, list) else set()

    data_scope = manifest.get("data_scope")
    if (
        not isinstance(data_scope, list)
        or not data_scope
        or any(not isinstance(item, str) or not item.strip() for item in data_scope)
    ):
        errors.append("manifest.data_scope must be a non-empty list of scope strings")

    permissions = manifest.get("permissions")
    if (
        not isinstance(permissions, list)
        or not permissions
        or any(not isinstance(item, str) or not item.strip() for item in permissions)
    ):
        errors.append("manifest.permissions must be a non-empty list of permission strings")

    approval_policy = manifest.get("approval_policy")
    if not isinstance(approval_policy, str) or approval_policy not in _APPROVAL_POLICIES:
        errors.append(
            "manifest.approval_policy must be one of: "
            + ", ".join(sorted(_APPROVAL_POLICIES))
        )

    rate_limits = manifest.get("rate_limits")
    if not isinstance(rate_limits, dict):
        errors.append("manifest.rate_limits must be a mapping")
    else:
        actions_per_hour = rate_limits.get("actions_per_hour")
        if not isinstance(actions_per_hour, int) or actions_per_hour <= 0:
            errors.append("manifest.rate_limits.actions_per_hour must be a positive integer")

    budgets = manifest.get("budgets")
    if not isinstance(budgets, dict):
        errors.append("manifest.budgets must be a mapping")
    else:
        usd_cap = budgets.get("usd_cap")
        if not isinstance(usd_cap, (int, float)) or float(usd_cap) <= 0:
            errors.append("manifest.budgets.usd_cap must be a positive number")

    kill_switch = manifest.get("kill_switch")
    if not isinstance(kill_switch, dict):
        errors.append("manifest.kill_switch must be a mapping")
    else:
        if not isinstance(kill_switch.get("enabled"), bool):
            errors.append("manifest.kill_switch.enabled must be a boolean")

    audit_sink = manifest.get("audit_sink")
    if not isinstance(audit_sink, str) or not audit_sink.strip():
        errors.append("manifest.audit_sink must be a non-empty string")

    failure_mode = manifest.get("failure_mode")
    if not isinstance(failure_mode, str) or not failure_mode.strip():
        errors.append("manifest.failure_mode must be a non-empty string")

    if steps is not None and declared_tools:
        workflow_tools = {
            str(step.get("tool", "")).strip()
            for step in steps
            if isinstance(step, dict) and "tool" in step
        }
        workflow_tools.discard("")
        missing = sorted(workflow_tools - declared_tools)
        if missing:
            errors.append(
                "manifest.tools is missing tool(s) used in steps: " + ", ".join(missing)
            )

    return errors


def install_skill(
    source: str,
    *,
    require_signature: bool = False,
    trusted_public_keys: list[str] | None = None,
) -> SkillRecord:
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise ValueError(f"Skill source not found: {source}")

    if source_path.is_dir():
        workflow_candidates = [
            source_path / "workflow.yaml",
            source_path / "workflow.yml",
        ]
        src_workflow = next((p for p in workflow_candidates if p.exists()), None)
        if src_workflow is None:
            raise ValueError("Skill directory must contain workflow.yaml or workflow.yml")
    else:
        src_workflow = source_path
    ok, errors, data = validate_workflow_file(str(src_workflow))
    if not ok:
        raise ValueError("; ".join(errors))
    assert isinstance(data, dict)
    name = str(data.get("name")).strip()
    target_dir = skills_root_dir() / name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "workflow.yaml"
    shutil.copy2(src_workflow, target_path)
    source_sig = default_signature_path(str(src_workflow))
    target_sig = default_signature_path(str(target_path))
    if source_sig.exists():
        target_sig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_sig, target_sig)

    checksum = compute_skill_checksum(str(target_path))
    signature_file = str(target_sig) if target_sig.exists() else ""
    signature_verified = False
    if require_signature:
        ok_sig, reason = verify_skill_signature(
            str(target_path),
            public_key_paths=list(trusted_public_keys or []),
            signature_path=signature_file or None,
        )
        if not ok_sig:
            raise ValueError(f"Signature verification failed: {reason}")
        signature_verified = True

    registry = load_registry()
    record = SkillRecord(
        name=name,
        path=str(target_path),
        enabled=True,
        checksum=checksum,
        signature_verified=signature_verified,
        signature_file=signature_file,
    )
    registry[name] = record
    save_registry(registry)
    return record


def set_skill_enabled(name: str, enabled: bool) -> SkillRecord:
    all_skills = discover_skills()
    if name not in all_skills:
        raise ValueError(f"Skill not found: {name}")
    record = all_skills[name]
    record.enabled = enabled
    registry = load_registry()
    registry[name] = record
    save_registry(registry)
    return record


def delete_skill(name: str) -> bool:
    all_skills = discover_skills()
    record = all_skills.get(name)
    if record is None:
        raise ValueError(f"Skill not found: {name}")

    root = skills_root_dir().resolve()
    target_path = Path(record.path).expanduser().resolve()
    try:
        target_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Skill path is outside managed skills root: {target_path}") from exc

    deleted = False
    if target_path.exists():
        target_dir = target_path.parent
        if target_dir.exists() and target_dir.is_dir():
            shutil.rmtree(target_dir, ignore_errors=False)
            deleted = True

    registry = load_registry()
    if name in registry:
        registry.pop(name, None)
        save_registry(registry)
    return deleted
