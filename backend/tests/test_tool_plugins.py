from __future__ import annotations

from pathlib import Path

from orchestrator.config import SecurityConfig
from orchestrator.security.guardian import Guardian
from orchestrator.tools.registry import (
    build_policy_overrides_from_manifest,
    discover_plugin_manifests,
    execute_tool,
    load_tool_registry,
)


def test_discover_plugin_manifests(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "custom_tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.yaml").write_text(
        """
name: custom_echo
description: test plugin
arg_schema: { text: string }
required_capabilities: [low_risk]
sandbox_config: { network_enabled: false, cpu_limit: 1.0, memory_limit_mb: 128, timeout_seconds: 5 }
max_calls_per_request: 2
""",
        encoding="utf-8",
    )
    (plugin_dir / "handler.py").write_text(
        """
def run(args):
    return {"status": "ok", "stdout": args.get("text", ""), "stderr": ""}
""",
        encoding="utf-8",
    )
    manifests = discover_plugin_manifests(str(tmp_path))
    assert len(manifests) == 1
    assert manifests[0].name == "custom_echo"
    registry = load_tool_registry(str(tmp_path))
    assert "python_exec" in registry
    assert "custom_echo" in registry


def test_execute_plugin_tool(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "custom_tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.yaml").write_text(
        """
name: custom_echo
description: test plugin
arg_schema: { text: string }
required_capabilities: [low_risk]
sandbox_config: { network_enabled: false, cpu_limit: 1.0, memory_limit_mb: 128, timeout_seconds: 5 }
max_calls_per_request: 2
""",
        encoding="utf-8",
    )
    handler = plugin_dir / "handler.py"
    handler.write_text(
        """
def run(args):
    return {"status": "ok", "tool": "custom_echo", "stdout": args.get("text", ""), "stderr": ""}
""",
        encoding="utf-8",
    )
    manifest = discover_plugin_manifests(str(tmp_path))[0]
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    result = execute_tool(manifest, {"text": "hello"}, guardian)
    assert result["tool"] == "custom_echo"
    assert result["stdout"] == "hello"
    assert result["status"] == "ok"


def test_execute_plugin_tool_nonzero_exit_code_marks_failed(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "failing_tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.yaml").write_text(
        """
name: failing_echo
description: test plugin
arg_schema: { text: string }
required_capabilities: [low_risk]
sandbox_config: { network_enabled: false, cpu_limit: 1.0, memory_limit_mb: 128, timeout_seconds: 5 }
max_calls_per_request: 2
""",
        encoding="utf-8",
    )
    (plugin_dir / "handler.py").write_text(
        """
def run(args):
    return {"status": "ok", "tool": "failing_echo", "stdout": args.get("text", ""), "stderr": "boom", "exit_code": 1}
""",
        encoding="utf-8",
    )
    manifest = discover_plugin_manifests(str(tmp_path))[0]
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    result = execute_tool(manifest, {"text": "hello"}, guardian)
    assert result["tool"] == "failing_echo"
    assert result["status"] == "failed"
    assert result["exit_code"] == 1


def test_execute_plugin_tool_explicit_error_marks_failed(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "error_tool"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.yaml").write_text(
        """
name: error_echo
description: test plugin
arg_schema: { text: string }
required_capabilities: [low_risk]
sandbox_config: { network_enabled: false, cpu_limit: 1.0, memory_limit_mb: 128, timeout_seconds: 5 }
max_calls_per_request: 2
""",
        encoding="utf-8",
    )
    (plugin_dir / "handler.py").write_text(
        """
def run(args):
    return {"status": "error", "tool": "error_echo", "stdout": "", "stderr": args.get("text", "")}
""",
        encoding="utf-8",
    )
    manifest = discover_plugin_manifests(str(tmp_path))[0]
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    result = execute_tool(manifest, {"text": "nope"}, guardian)
    assert result["tool"] == "error_echo"
    assert result["status"] == "failed"


def test_manifest_policy_overrides() -> None:
    registry = load_tool_registry("tools")
    manifest = registry["echo_text"]
    overrides = build_policy_overrides_from_manifest(manifest)
    assert overrides["max_calls_per_request"] == manifest.max_calls_per_request
    assert overrides["requires_human_approval"] is False
