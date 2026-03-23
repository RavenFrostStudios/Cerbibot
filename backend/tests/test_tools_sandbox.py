from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.config import SecurityConfig
from orchestrator.security.guardian import Guardian
from orchestrator.tools.registry import built_in_manifests, execute_tool
from orchestrator.tools.sandbox import execute_python_code
from orchestrator.tools.validators import validate_python_exec_args


def test_execute_python_code_basic() -> None:
    result = execute_python_code("print('ok')")
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_python_exec_validator_blocks_dangerous_patterns() -> None:
    try:
        validate_python_exec_args({"code": "import os\nos.system('id')"})
    except ValueError as exc:
        assert "blocked pattern" in str(exc)
        return
    raise AssertionError("Expected validator to reject os.system")


def test_registry_executes_python_tool() -> None:
    manifest = built_in_manifests()["python_exec"]
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    result = execute_tool(manifest, {"code": "print(2 + 3)"}, guardian)
    assert result["tool"] == "python_exec"
    assert result["exit_code"] == 0
    assert "5" in result["stdout"]


def test_builtins_include_readonly_d1_tools() -> None:
    manifests = built_in_manifests()
    expected = {
        "file_read",
        "file_search",
        "git_status",
        "web_retrieve",
        "json_query",
        "regex_test",
        "system_info",
    }
    assert expected.issubset(manifests.keys())


def test_registry_executes_file_read_and_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_WORKSPACE_ROOT", str(tmp_path))
    source = tmp_path / "notes.txt"
    source.write_text("alpha\nbeta needle\ngamma needle\n", encoding="utf-8")
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    read_manifest = built_in_manifests()["file_read"]
    read_result = execute_tool(read_manifest, {"path": "notes.txt"}, guardian)
    assert "beta needle" in read_result["stdout"]

    search_manifest = built_in_manifests()["file_search"]
    search_result = execute_tool(search_manifest, {"path": ".", "pattern": "needle"}, guardian)
    payload = json.loads(search_result["stdout"])
    assert payload["count"] == 2


def test_registry_blocks_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MMO_WORKSPACE_ROOT", str(tmp_path))
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    read_manifest = built_in_manifests()["file_read"]
    with pytest.raises(ValueError, match="escapes workspace root"):
        execute_tool(read_manifest, {"path": str(outside)}, guardian)


def test_registry_executes_json_query_regex_and_system_info(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MMO_WORKSPACE_ROOT", str(tmp_path))
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    jq_manifest = built_in_manifests()["json_query"]
    jq_result = execute_tool(
        jq_manifest,
        {"json_text": '{"a":{"b":[{"c":7}]}}', "query": "a.b[0].c"},
        guardian,
    )
    assert json.loads(jq_result["stdout"])["value"] == 7

    rx_manifest = built_in_manifests()["regex_test"]
    rx_result = execute_tool(rx_manifest, {"pattern": "n..dle", "text": "needle haystack needle"}, guardian)
    assert json.loads(rx_result["stdout"])["count"] == 2

    sys_manifest = built_in_manifests()["system_info"]
    sys_result = execute_tool(sys_manifest, {}, guardian)
    assert str(tmp_path.resolve()) in sys_result["stdout"]
    assert os.sys.version.split(" ", 1)[0] in sys_result["stdout"]


def test_registry_executes_web_retrieve_with_mocked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _n: int) -> bytes:
            return b"<html><head><title>T</title></head><body>Hello from test</body></html>"

    def _fake_urlopen(*_args, **_kwargs):
        return _Resp()

    monkeypatch.setattr("orchestrator.tools.read_only.urlopen", _fake_urlopen)
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    manifest = built_in_manifests()["web_retrieve"]
    result = execute_tool(manifest, {"url": "https://example.com/page"}, guardian)
    assert "UNTRUSTED_SOURCE_BEGIN" in result["stdout"]
    assert "Hello from test" in result["stdout"]
