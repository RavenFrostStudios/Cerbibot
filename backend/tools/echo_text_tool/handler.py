from __future__ import annotations


def run(args: dict[str, str]) -> dict:
    text = args.get("text", "")
    return {
        "status": "ok",
        "tool": "echo_text",
        "stdout": text,
        "stderr": "",
        "result": {"echoed": text},
    }
