from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from orchestrator.delegation.daemon import DelegationBrokerDaemon
from orchestrator.delegation.gateway import DelegationGateway, DelegationJobSpec


def _run(cmd: list[str], cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"command failed: {' '.join(cmd)}")


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=path)
    _run(["git", "commit", "-m", "init"], cwd=path)


def test_delegate_gateway_submit_generates_patch_bundle(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="add notes file",
        repo_root=str(repo),
        files=["notes.txt"],
        checks=["test -f notes.txt"],
        executor_cmd="printf 'delegated\\n' > {workspace}/notes.txt",
    )
    record = gateway.submit(spec)
    assert record["status"] == "completed"
    artifacts = Path(record["artifacts_dir"])
    patch = (artifacts / "changes.patch").read_text(encoding="utf-8")
    assert "notes.txt" in patch
    checks = (artifacts / "checks.json").read_text(encoding="utf-8")
    assert "test -f notes.txt" in checks


def test_delegate_gateway_apply_patch_to_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="append readme",
        repo_root=str(repo),
        files=["README.md"],
        executor_cmd="printf 'extra\\n' >> {workspace}/README.md",
    )
    record = gateway.submit(spec)
    assert record["status"] == "completed"
    result = gateway.apply_patch(record["job_id"])
    assert result["ok"] is True
    assert (repo / "README.md").read_text(encoding="utf-8").endswith("extra\n")


def test_delegate_gateway_submit_async_and_events(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="async write",
        repo_root=str(repo),
        files=["async.txt"],
        executor_cmd="sleep 0.2; printf 'ok\\n' > {workspace}/async.txt",
    )
    record = gateway.submit_async(spec)
    final = gateway.wait_for_completion(str(record["job_id"]), timeout_s=10, poll_s=0.05)
    assert final["status"] == "completed"
    events, _ = gateway.read_events(str(record["job_id"]))
    names = [str(item.get("event_type", "")) for item in events]
    assert "submitted" in names
    assert "running" in names
    assert "completed" in names


def test_delegate_gateway_respects_deny_globs(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".delegate.yaml").write_text("deny_globs:\n  - \"**/*.secret\"\n", encoding="utf-8")
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="write denied file",
        repo_root=str(repo),
        files=["data.secret"],
        executor_cmd="printf 's\\n' > {workspace}/data.secret",
    )
    with pytest.raises(RuntimeError, match="denied by policy glob"):
        gateway.submit(spec)


def test_delegate_daemon_dispatch_health(tmp_path) -> None:
    gateway = DelegationGateway(root=tmp_path / "state")
    daemon = DelegationBrokerDaemon(gateway=gateway, socket_path=tmp_path / "delegate.sock")
    out = daemon._dispatch({"op": "health"})  # noqa: SLF001 - direct unit coverage of dispatch table
    assert out["status"] == "ok"


def test_delegate_gateway_non_git_fallback_and_apply(tmp_path) -> None:
    if shutil.which("patch") is None:
        pytest.skip("patch command not available")
    repo = tmp_path / "plain_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "note.txt").write_text("hello\n", encoding="utf-8")

    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="update note",
        repo_root=str(repo),
        files=["note.txt"],
        executor_cmd="printf 'hello\\nworld\\n' > {workspace}/note.txt",
    )
    record = gateway.submit(spec)
    assert record["status"] == "completed"
    assert record["execution_mode"] == "temp_copy"

    check = gateway.apply_patch(str(record["job_id"]), check_only=True)
    assert check["ok"] is True

    result = gateway.apply_patch(str(record["job_id"]))
    assert result["ok"] is True
    assert (repo / "note.txt").read_text(encoding="utf-8") == "hello\nworld\n"


def test_delegate_gateway_rejects_executor_command_intent_drift(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="update readme docs",
        repo_root=str(repo),
        files=["README.md"],
        executor_cmd="rm -rf /tmp/build-cache",
    )
    record = gateway.submit(spec)
    assert record["status"] == "failed"
    assert "intent drift detected for executor command" in str(record.get("error") or "")


def test_delegate_gateway_rejects_changed_files_intent_drift_without_file_scope(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    gateway = DelegationGateway(root=tmp_path / "state")
    spec = DelegationJobSpec(
        objective="update readme docs",
        repo_root=str(repo),
        files=[],
        executor_cmd="printf 'readme docs update\\n' > {workspace}/credentials.secret",
    )
    record = gateway.submit(spec)
    assert record["status"] == "failed"
    assert "intent drift detected for changed files" in str(record.get("error") or "")
