from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


@dataclass(slots=True)
class SandboxConfig:
    network_enabled: bool = False
    cpu_limit: float = 1.0
    memory_limit_mb: int = 512
    timeout_seconds: int = 10


@dataclass(slots=True)
class SandboxExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    backend: str
    warning: str | None = None


def execute_python_code(code: str, *, config: SandboxConfig | None = None) -> SandboxExecutionResult:
    cfg = config or SandboxConfig()
    with TemporaryDirectory(prefix="mmo-sandbox-") as tmp_dir:
        workspace = Path(tmp_dir)
        workspace.chmod(0o755)
        script_path = workspace / "main.py"
        script_path.write_text(code, encoding="utf-8")
        script_path.chmod(0o644)

        docker_result = _run_in_docker(script_path=script_path, workspace=workspace, config=cfg)
        if docker_result is not None:
            return docker_result
        return _run_local_subprocess(script_path=script_path, workspace=workspace, config=cfg)


def _run_in_docker(
    *,
    script_path: Path,
    workspace: Path,
    config: SandboxConfig,
) -> SandboxExecutionResult | None:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return None

    image = "python:3.12-alpine"
    command = [
        docker_bin,
        "run",
        "--rm",
        "--user",
        "65534:65534",
        "--cpus",
        str(config.cpu_limit),
        "--memory",
        f"{config.memory_limit_mb}m",
        "--pids-limit",
        "128",
        "-v",
        f"{workspace}:/workspace:rw",
        "-w",
        "/workspace",
    ]
    if not config.network_enabled:
        command.extend(["--network", "none"])
    command.extend([image, "python", str(script_path.name)])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            env={},
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    combined_output = f"{completed.stdout}\n{completed.stderr}".lower()
    unavailable_markers = [
        "docker could not be found",
        "the command 'docker' could not be found",
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "permission denied while trying to connect to the docker api",
    ]
    if completed.returncode != 0 and any(marker in combined_output for marker in unavailable_markers):
        return None

    return SandboxExecutionResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
        timed_out=False,
        backend="docker",
    )


def _run_local_subprocess(
    *,
    script_path: Path,
    workspace: Path,
    config: SandboxConfig,
) -> SandboxExecutionResult:
    python_bin = shutil.which("python3") or shutil.which("python")
    if not python_bin:
        return SandboxExecutionResult(
            stdout="",
            stderr="python executable not found",
            exit_code=127,
            timed_out=False,
            backend="local",
            warning="Docker unavailable; local fallback used.",
        )

    env = {"PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", "")}
    try:
        completed = subprocess.run(
            [python_bin, script_path.name],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxExecutionResult(
            stdout=exc.stdout or "",
            stderr=exc.stderr or "Execution timed out",
            exit_code=124,
            timed_out=True,
            backend="local",
            warning="Docker unavailable; local fallback used.",
        )

    return SandboxExecutionResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
        timed_out=False,
        backend="local",
        warning="Docker unavailable; local fallback used.",
    )
