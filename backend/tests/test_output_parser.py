from orchestrator.collaboration.output_parser import parse_structured_output


def test_parse_structured_output_plain_json() -> None:
    parsed = parse_structured_output('{"answer":"ok","assumptions":[]}', ["answer", "assumptions"])
    assert parsed.valid
    assert parsed.data["answer"] == "ok"


def test_parse_structured_output_fenced_json() -> None:
    text = "notes\n```json\n{\"final_answer\":\"done\",\"citations\":[],\"confidence\":\"high\"}\n```"
    parsed = parse_structured_output(text, ["final_answer", "citations", "confidence"])
    assert parsed.valid
    assert parsed.data["final_answer"] == "done"


def test_parse_structured_output_missing_key() -> None:
    parsed = parse_structured_output('{"answer":"x"}', ["answer", "assumptions"])
    assert not parsed.valid
    assert parsed.error is not None


def test_parse_structured_output_nested_object_from_text() -> None:
    text = 'prefix {"final_answer":"ok","citations":[{"url":"https://x"}],"confidence":"high"} suffix'
    parsed = parse_structured_output(text, ["final_answer", "citations", "confidence"])
    assert parsed.valid
    assert parsed.data["citations"][0]["url"] == "https://x"


def test_parse_structured_output_fallback_structure() -> None:
    text = "this is not json at all"
    parsed = parse_structured_output(text, ["final_answer", "citations", "confidence"])
    assert not parsed.valid
    assert parsed.data["raw_text"] == text
    assert parsed.data["final_answer"] == text
