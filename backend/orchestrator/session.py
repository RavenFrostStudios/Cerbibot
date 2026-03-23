from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SessionMessage:
    role: str
    content: str
    metadata: dict | None = None


@dataclass(slots=True)
class SessionState:
    messages: list[SessionMessage] = field(default_factory=list)
    max_context_tokens: int = 8000


class SessionManager:
    """Maintains rolling chat context and trims history to token budget."""

    def __init__(self, max_context_tokens: int = 8000):
        self.state = SessionState(max_context_tokens=max_context_tokens)

    def add(self, role: str, content: str, metadata: dict | None = None) -> None:
        self.state.messages.append(SessionMessage(role=role, content=content, metadata=metadata))

    def clear(self) -> None:
        self.state.messages.clear()

    def export(self) -> list[dict]:
        exported: list[dict] = []
        for message in self.state.messages:
            item: dict = {"role": message.role, "content": message.content}
            if message.metadata:
                item["metadata"] = message.metadata
            exported.append(item)
        return exported

    def trim(self) -> None:
        while self._estimate_tokens(self.export()) > self.state.max_context_tokens and self.state.messages:
            self.state.messages.pop(0)

    def _estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        total = 0
        for message in messages:
            total += max(1, len(message.get("content", "").split())) + 4
        return total


def format_context_messages(messages: list[dict[str, str]]) -> str:
    if not messages:
        return ""
    lines = ["Conversation context:"]
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)
