import pytest

from orchestrator.security.taint import TaintedString, merge_taint_level, validate_for_tool_arg
from orchestrator.security.taint_validator import validate_path_arg, validate_search_query_arg, validate_url_arg


def test_taint_propagates_on_concat_and_slice() -> None:
    left = TaintedString("hello", source="user_input", source_id="u1", taint_level="untrusted")
    right = TaintedString(" world", source="model_output", source_id="m1", taint_level="validated")
    combined = left + right
    assert combined.taint_level == "untrusted"
    assert str(combined) == "hello world"

    sliced = combined[:5]
    assert isinstance(sliced, TaintedString)
    assert str(sliced) == "hello"
    assert sliced.taint_level == "untrusted"


def test_taint_format_propagates() -> None:
    template = TaintedString("Hi {}", source="user_input", source_id="u1", taint_level="validated")
    val = TaintedString("Bob", source="memory", source_id="mem1", taint_level="untrusted")
    out = template.format(val)
    assert out.taint_level == "untrusted"
    assert str(out) == "Hi Bob"


def test_validate_for_tool_arg_untrusted_rejects_extra_text() -> None:
    value = TaintedString(
        "please fetch https://example.com now",
        source="user_input",
        source_id="u1",
        taint_level="untrusted",
    )
    with pytest.raises(ValueError):
        validate_for_tool_arg(value, r"https?://[^\s]+")


def test_taint_merge_order() -> None:
    assert merge_taint_level("trusted", "validated") == "validated"
    assert merge_taint_level("validated", "untrusted") == "untrusted"


def test_taint_validator_url_and_query() -> None:
    url = validate_url_arg(
        TaintedString("https://docs.python.org", source="retrieved_text", source_id="r1", taint_level="validated"),
        allowlist=["python.org"],
    )
    assert url == "https://docs.python.org"

    query = validate_search_query_arg(TaintedString("python release", source="user_input", source_id="u1", taint_level="validated"))
    assert query == "python release"


def test_taint_validator_path(tmp_path) -> None:
    allowed = tmp_path / "workspace"
    allowed.mkdir()
    target = allowed / "file.txt"
    target.write_text("x", encoding="utf-8")
    result = validate_path_arg(str(target), allowed_prefixes=[str(allowed)])
    assert result.endswith("file.txt")
