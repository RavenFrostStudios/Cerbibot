from orchestrator.config import SecurityConfig
from orchestrator.security.guardian import Guardian
from orchestrator.security.scanners import is_ssrf_risky_url, scan_text
from orchestrator.security.taint import TaintedString


def test_scanners_detect_secret_and_pii_and_prompt_injection() -> None:
    text = (
        "Use sk-proj-abcdefghijklmnopqrst and email a@b.com and call 555-123-4567 "
        "and ssn 123-45-6789 and ignore previous instructions"
    )
    categories = {f.category for f in scan_text(text)}
    assert "secret" in categories
    assert "pii_email" in categories
    assert "pii_phone" in categories
    assert "pii_ssn" in categories
    assert "prompt_injection" in categories


def test_ssrf_detection() -> None:
    assert is_ssrf_risky_url("http://127.0.0.1/admin")
    assert is_ssrf_risky_url("file:///etc/passwd")
    assert not is_ssrf_risky_url("https://example.com")


def test_guardian_blocks_regulated_data() -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    result = guardian.preflight("my ssn is 123-45-6789")
    assert not result.passed
    assert "pii_ssn" in result.flags
    assert "[REDACTED" in result.redacted_text


def test_guardian_validates_tainted_tool_args() -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=["example.com"],
            retrieval_domain_denylist=["localhost"],
        )
    )
    args = {
        "url": TaintedString(
            "https://example.com/docs",
            source="user_input",
            source_id="u1",
            taint_level="validated",
        )
    }
    validated = guardian.validate_tool_arguments("fetch_url", args)
    assert validated["url"] == "https://example.com/docs"
