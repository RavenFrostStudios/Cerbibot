from orchestrator.session import SessionManager, format_context_messages


def test_session_trim_enforces_token_budget() -> None:
    session = SessionManager(max_context_tokens=10)
    session.add("user", "one two three four five")
    session.add("assistant", "six seven eight nine ten")
    session.trim()
    assert len(session.export()) <= 1


def test_format_context_messages() -> None:
    context = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    text = format_context_messages(context)
    assert "Conversation context:" in text
    assert "- user: hello" in text
    assert "- assistant: hi" in text
