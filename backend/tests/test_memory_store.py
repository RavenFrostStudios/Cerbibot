from __future__ import annotations

from orchestrator.memory.store import MemoryStore


def test_memory_store_crud_and_search(tmp_path) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    mem_id = store.add(
        statement="User prefers concise answers",
        source_type="user_preference",
        source_ref="session:1",
        confidence=0.9,
        ttl_days=30,
    )
    rows = store.list_records()
    assert len(rows) == 1
    assert rows[0].id == mem_id

    found = store.search("concise")
    assert len(found) == 1
    assert "concise" in found[0].statement

    deleted = store.delete(mem_id)
    assert deleted is True
    assert store.list_records() == []


def test_memory_store_expires_old_records(tmp_path) -> None:
    store = MemoryStore(str(tmp_path / "memory.db"))
    mem_id = store.add(
        statement="Old memory",
        source_type="summary",
        source_ref="run:abc",
        confidence=0.8,
        ttl_days=1,
    )
    store.backdate_for_test(mem_id, days_ago=2)
    expired = store.expire_records()
    assert expired >= 1
    assert store.list_records() == []
