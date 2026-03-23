from __future__ import annotations

from dataclasses import dataclass

from orchestrator.security.guardian import Guardian


@dataclass(slots=True)
class ToolValidationResult:
    stdout: str
    stderr: str
    warnings: list[str]


def validate_python_exec_args(args: dict[str, str]) -> dict[str, str]:
    if "code" not in args:
        raise ValueError("python_exec requires 'code' argument")
    code = args["code"]
    if not isinstance(code, str):
        raise ValueError("python_exec code must be a string")
    if not code.strip():
        raise ValueError("python_exec code cannot be empty")
    if len(code) > 12_000:
        raise ValueError("python_exec code too long (max 12000 chars)")

    banned_patterns = [
        "subprocess.",
        "os.system(",
        "socket.",
        "requests.",
        "http.client",
        "```",
    ]
    lowered = code.lower()
    for pattern in banned_patterns:
        if pattern in lowered:
            raise ValueError(f"python_exec code contains blocked pattern: {pattern}")
    return {"code": code}


def validate_post_execution_output(*, stdout: str, stderr: str, guardian: Guardian) -> ToolValidationResult:
    warnings: list[str] = []
    clean_stdout = guardian.post_output(stdout)
    clean_stderr = guardian.post_output(stderr)
    if not clean_stdout.passed:
        warnings.append(f"Tool stdout flagged: {clean_stdout.flags}")
    if not clean_stderr.passed:
        warnings.append(f"Tool stderr flagged: {clean_stderr.flags}")
    return ToolValidationResult(
        stdout=clean_stdout.redacted_text,
        stderr=clean_stderr.redacted_text,
        warnings=warnings,
    )
