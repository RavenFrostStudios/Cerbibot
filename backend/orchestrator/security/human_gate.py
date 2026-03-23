from __future__ import annotations

import select
import sys
from typing import Callable


class HumanGate:
    """CLI human approval gate for high-impact actions."""

    def __init__(self, input_fn: Callable[[str], str] | None = None):
        self.input_fn = input_fn or input

    def request_approval(
        self,
        *,
        tool_name: str,
        args: dict[str, str],
        reason: str,
        timeout_seconds: int = 60,
    ) -> bool:
        prompt = (
            f"Approve tool execution? tool={tool_name} reason={reason} args={args} "
            f"[y/N] (timeout {timeout_seconds}s): "
        )

        if self.input_fn is not input:
            answer = self.input_fn(prompt).strip().lower()
            return answer in {"y", "yes"}

        sys.stderr.write(prompt)
        sys.stderr.flush()
        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
        if not ready:
            sys.stderr.write("\nApproval timeout reached. Denied.\n")
            sys.stderr.flush()
            return False

        answer = sys.stdin.readline().strip().lower()
        return answer in {"y", "yes"}
