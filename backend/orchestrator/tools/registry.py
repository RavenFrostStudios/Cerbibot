from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orchestrator.security.guardian import Guardian
from orchestrator.tools.read_only import (
    run_file_read,
    run_file_search,
    run_git_status,
    run_json_query,
    run_regex_test,
    run_system_info,
    run_web_retrieve,
)
from orchestrator.tools.sandbox import SandboxConfig, execute_python_code
from orchestrator.tools.validators import validate_post_execution_output, validate_python_exec_args


@dataclass(slots=True)
class ToolManifest:
    name: str
    description: str
    arg_schema: dict[str, str]
    required_capabilities: list[str]
    sandbox_config: SandboxConfig
    max_calls_per_request: int = 3
    requires_human_approval: bool = False
    source: str = "builtin"
    handler_path: str | None = None


def built_in_manifests() -> dict[str, ToolManifest]:
    return {
        "file_read": ToolManifest(
            name="file_read",
            description="Read a UTF-8 text file within the configured workspace root.",
            arg_schema={"path": "string"},
            required_capabilities=["fs.read.scoped"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=5),
            max_calls_per_request=5,
            source="builtin",
        ),
        "file_search": ToolManifest(
            name="file_search",
            description="Search for a plain-text pattern within workspace files.",
            arg_schema={"path": "string", "pattern": "string"},
            required_capabilities=["fs.search.scoped"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=256, timeout_seconds=8),
            max_calls_per_request=4,
            source="builtin",
        ),
        "git_status": ToolManifest(
            name="git_status",
            description="Run read-only git status in a scoped repository path.",
            arg_schema={"path": "string"},
            required_capabilities=["git.read.scoped"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=8),
            max_calls_per_request=3,
            source="builtin",
        ),
        "web_retrieve": ToolManifest(
            name="web_retrieve",
            description="Fetch and sanitize a web page from an http/https URL with SSRF checks.",
            arg_schema={"url": "string"},
            required_capabilities=["net.fetch.allowlisted"],
            sandbox_config=SandboxConfig(network_enabled=True, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=12),
            max_calls_per_request=2,
            source="builtin",
        ),
        "json_query": ToolManifest(
            name="json_query",
            description="Extract values from JSON using a dotted-path expression (supports [index]).",
            arg_schema={"json_text": "string", "query": "string"},
            required_capabilities=["compute.local"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=5),
            max_calls_per_request=5,
            source="builtin",
        ),
        "regex_test": ToolManifest(
            name="regex_test",
            description="Evaluate a regex against sample text and return match spans.",
            arg_schema={"pattern": "string", "text": "string"},
            required_capabilities=["compute.local"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=5),
            max_calls_per_request=5,
            source="builtin",
        ),
        "system_info": ToolManifest(
            name="system_info",
            description="Return non-sensitive local runtime and disk-space metadata.",
            arg_schema={},
            required_capabilities=["system.read.minimal"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=0.5, memory_limit_mb=128, timeout_seconds=5),
            max_calls_per_request=3,
            source="builtin",
        ),
        "python_exec": ToolManifest(
            name="python_exec",
            description="Execute Python code in a constrained sandbox and return stdout/stderr/exit_code.",
            arg_schema={"code": "string"},
            required_capabilities=["sandbox.exec.python"],
            sandbox_config=SandboxConfig(network_enabled=False, cpu_limit=1.0, memory_limit_mb=512, timeout_seconds=10),
            max_calls_per_request=2,
            source="builtin",
        )
    }


def validate_tool_args_against_manifest(manifest: ToolManifest, args: dict[str, str]) -> dict[str, str]:
    for required_key in manifest.arg_schema:
        if required_key not in args:
            raise ValueError(f"Missing tool arg: {required_key}")
    if manifest.name == "python_exec":
        return validate_python_exec_args(args)
    for key, schema_type in manifest.arg_schema.items():
        value = args.get(key)
        if schema_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"Tool arg '{key}' must be a string")
            if len(value) > 2000:
                raise ValueError(f"Tool arg '{key}' too long")
    return args


def execute_tool(manifest: ToolManifest, args: dict[str, str], guardian: Guardian) -> dict[str, Any]:
    validated = validate_tool_args_against_manifest(manifest, args)
    if manifest.name == "file_read":
        result = run_file_read(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "file_search":
        result = run_file_search(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "git_status":
        result = run_git_status(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "web_retrieve":
        result = run_web_retrieve(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "json_query":
        result = run_json_query(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "regex_test":
        result = run_regex_test(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "system_info":
        result = run_system_info(validated)
        return _scan_plugin_result(manifest, result, guardian)
    if manifest.name == "python_exec":
        sandbox_result = execute_python_code(validated["code"], config=manifest.sandbox_config)
        scanned = validate_post_execution_output(
            stdout=sandbox_result.stdout,
            stderr=sandbox_result.stderr,
            guardian=guardian,
        )
        return _finalize_tool_result(manifest.name, {
            "status": "ok",
            "tool": manifest.name,
            "stdout": scanned.stdout,
            "stderr": scanned.stderr,
            "exit_code": sandbox_result.exit_code,
            "timed_out": sandbox_result.timed_out,
            "backend": sandbox_result.backend,
            "warning": sandbox_result.warning,
            "security_warnings": scanned.warnings,
        })

    if manifest.handler_path is None:
        raise ValueError(f"Unknown tool: {manifest.name}")

    plugin_result = _run_plugin_handler(manifest, validated)
    return _scan_plugin_result(manifest, plugin_result, guardian)


def load_tool_registry(plugins_root: str = "tools") -> dict[str, ToolManifest]:
    registry = built_in_manifests()
    for manifest in discover_plugin_manifests(plugins_root):
        if manifest.name in registry:
            continue
        registry[manifest.name] = manifest
    return registry


def discover_plugin_manifests(plugins_root: str = "tools") -> list[ToolManifest]:
    root = Path(plugins_root)
    if not root.exists() or not root.is_dir():
        return []
    manifests: list[ToolManifest] = []
    for plugin_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = plugin_dir / "manifest.yaml"
        handler_path = plugin_dir / "handler.py"
        if not manifest_path.exists() or not handler_path.exists():
            continue
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", plugin_dir.name)).strip()
        description = str(raw.get("description", f"Plugin tool: {name}")).strip()
        arg_schema_raw = raw.get("arg_schema", {})
        arg_schema = {str(k): str(v) for k, v in arg_schema_raw.items()} if isinstance(arg_schema_raw, dict) else {}
        required_caps_raw = raw.get("required_capabilities", [])
        required_caps = [str(item) for item in required_caps_raw] if isinstance(required_caps_raw, list) else []
        sandbox_raw = raw.get("sandbox_config", {}) or {}
        if not isinstance(sandbox_raw, dict):
            sandbox_raw = {}
        manifests.append(
            ToolManifest(
                name=name,
                description=description,
                arg_schema=arg_schema,
                required_capabilities=required_caps,
                sandbox_config=SandboxConfig(
                    network_enabled=bool(sandbox_raw.get("network_enabled", False)),
                    cpu_limit=float(sandbox_raw.get("cpu_limit", 1.0)),
                    memory_limit_mb=int(sandbox_raw.get("memory_limit_mb", 512)),
                    timeout_seconds=int(sandbox_raw.get("timeout_seconds", 10)),
                ),
                max_calls_per_request=int(raw.get("max_calls_per_request", 3)),
                requires_human_approval=bool(raw.get("requires_human_approval", False)),
                source="plugin",
                handler_path=str(handler_path),
            )
        )
    return manifests


def parse_tool_args_json(args_json: str | dict[str, Any]) -> dict[str, str]:
    if isinstance(args_json, dict):
        return {str(k): str(v) for k, v in args_json.items()}
    if not isinstance(args_json, str) or not args_json.strip():
        return {}
    loaded = json.loads(args_json)
    if not isinstance(loaded, dict):
        raise ValueError("args_json must decode to an object")
    return {str(k): str(v) for k, v in loaded.items()}


def build_policy_overrides_from_manifest(manifest: ToolManifest) -> dict[str, Any]:
    allowed_arg_patterns: dict[str, str] = {}
    for key, schema_type in manifest.arg_schema.items():
        if key == "code":
            allowed_arg_patterns[key] = r"[\s\S]{1,12000}"
        elif schema_type == "string":
            allowed_arg_patterns[key] = r"[^\n\r\t]{1,2000}"
        else:
            allowed_arg_patterns[key] = r"[^\n\r\t]{1,1000}"

    high_impact = manifest.requires_human_approval or any(
        cap.lower() in {"external_contact", "file_write", "credential_use", "payment_transfer"}
        or cap.lower().startswith("high_impact")
        for cap in manifest.required_capabilities
    )
    return {
        "max_calls_per_request": manifest.max_calls_per_request,
        "requires_human_approval": high_impact,
        "allowed_arg_patterns": allowed_arg_patterns,
    }


def _run_plugin_handler(manifest: ToolManifest, args: dict[str, str]) -> dict[str, Any]:
    if manifest.handler_path is None:
        raise ValueError("Plugin handler path is not set")
    path = Path(manifest.handler_path)
    if not path.exists():
        raise ValueError(f"Plugin handler not found: {path}")
    module_name = f"mmo_plugin_{manifest.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Failed loading plugin spec: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        raise ValueError(f"Plugin handler must expose callable run(args): {path}")
    result = run_fn(deepcopy(args))
    if not isinstance(result, dict):
        raise ValueError("Plugin run(args) must return dict")
    return result


def _scan_plugin_result(manifest: ToolManifest, result: dict[str, Any], guardian: Guardian) -> dict[str, Any]:
    out = dict(result)
    warnings: list[str] = []
    stdout = str(out.get("stdout", ""))
    stderr = str(out.get("stderr", ""))
    if stdout or stderr:
        scanned = validate_post_execution_output(stdout=stdout, stderr=stderr, guardian=guardian)
        out["stdout"] = scanned.stdout
        out["stderr"] = scanned.stderr
        warnings.extend(scanned.warnings)
    full_scan = guardian.post_output(json.dumps(out, sort_keys=True))
    if not full_scan.passed:
        warnings.append(f"Tool payload flagged: {full_scan.flags}")
    out["security_warnings"] = warnings
    return _finalize_tool_result(manifest.name, out)


def _finalize_tool_result(default_tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out.setdefault("tool", default_tool_name)
    out["status"] = _normalized_tool_status(out)
    return out


def _normalized_tool_status(result: dict[str, Any]) -> str:
    explicit = str(result.get("status", "")).strip().lower()
    exit_code = result.get("exit_code")
    timed_out = bool(result.get("timed_out", False))

    if timed_out:
        return "failed"
    if isinstance(exit_code, int) and exit_code != 0:
        return "failed"
    if explicit in {"failed", "error", "denied", "rejected", "timed_out", "timeout"}:
        return "failed"
    if explicit in {"running", "pending", "queued", "waiting"}:
        return explicit
    if explicit in {"ok", "success", "completed", "done"}:
        return "ok"
    if explicit:
        return explicit
    return "ok"
