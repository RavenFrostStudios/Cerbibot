import json

from orchestrator.observability.audit import AuditLogger
from orchestrator.security.taint import TaintedString


def test_audit_logger_records_taint_metadata(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))
    logger.write(
        "event",
        {
            "input": TaintedString("hello", source="user_input", source_id="u1", taint_level="untrusted"),
            "plain": "ok",
        },
    )
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["payload"]["input"] == "hello"
    assert record["taint"][0]["field"] == "input"
    assert record["taint"][0]["source"] == "user_input"
