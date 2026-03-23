from __future__ import annotations

import sqlite3
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.security.encryption import EnvelopeCipher


@dataclass(slots=True)
class MemoryRecord:
    id: int
    project_id: str
    statement: str
    source_type: str
    source_ref: str
    confidence: float
    ttl_days: int
    created_at: str
    reviewed_by: str | None
    redaction_status: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self, db_path: str, cipher: EnvelopeCipher | None = None):
        self.db_path = Path(db_path).expanduser()
        self.cipher = cipher
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL DEFAULT 'default',
                    statement TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    ttl_days INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    redaction_status TEXT NOT NULL
                )
                """
            )
            cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
            if "project_id" not in cols:
                conn.execute("ALTER TABLE memories ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_statement ON memories(statement)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_created_at ON memories(project_id, created_at)")

    def add(
        self,
        *,
        project_id: str = "default",
        statement: str,
        source_type: str,
        source_ref: str,
        confidence: float,
        ttl_days: int,
        reviewed_by: str | None = None,
        redaction_status: str = "redacted",
    ) -> int:
        now = _utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO memories (
                    project_id, statement, source_type, source_ref, confidence, ttl_days,
                    created_at, reviewed_by, redaction_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id.strip() or "default",
                    self._enc(statement, f"memory_statement:{source_type}:{source_ref}"),
                    source_type,
                    self._enc(source_ref, f"memory_source_ref:{source_type}"),
                    float(confidence),
                    int(ttl_days),
                    now,
                    reviewed_by,
                    redaction_status,
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def normalize_statement(statement: str) -> str:
        # Normalize for duplicate detection without mutating stored user text.
        return re.sub(r"\s+", " ", statement.strip().lower())

    def find_duplicate_statement(self, statement: str, *, limit: int = 500, project_id: str = "default") -> MemoryRecord | None:
        target = self.normalize_statement(statement)
        if not target:
            return None
        for row in self.list_records(limit=limit, project_id=project_id):
            if self.normalize_statement(row.statement) == target:
                return row
        return None

    def expire_records(self) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """
                DELETE FROM memories
                WHERE datetime(created_at) <= datetime('now', '-' || ttl_days || ' day')
                """
            )
            return int(rows.rowcount)

    def list_records(
        self,
        *,
        limit: int = 100,
        min_confidence: float = 0.0,
        project_id: str = "default",
    ) -> list[MemoryRecord]:
        self.expire_records()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE confidence >= ? AND project_id = ?
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (float(min_confidence), project_id.strip() or "default", int(limit)),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        min_confidence: float = 0.0,
        project_id: str = "default",
    ) -> list[MemoryRecord]:
        self.expire_records()
        like = f"%{query.strip()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE statement LIKE ? AND confidence >= ? AND project_id = ?
                ORDER BY confidence DESC, datetime(created_at) DESC
                LIMIT ?
                """,
                (like, float(min_confidence), project_id.strip() or "default", int(limit)),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def retrieve_for_query(self, query: str, *, limit: int = 5, project_id: str = "default") -> list[MemoryRecord]:
        tokens = [token.strip() for token in query.lower().split() if len(token.strip()) > 2]
        if not tokens:
            return self.list_records(limit=limit, project_id=project_id)
        with self._connect() as conn:
            clauses = " OR ".join(["lower(statement) LIKE ?"] * len(tokens))
            params = [f"%{token}%" for token in tokens]
            sql = f"""
                SELECT * FROM memories
                WHERE ({clauses}) AND project_id = ?
                ORDER BY confidence DESC, datetime(created_at) DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (*params, project_id.strip() or "default", int(limit))).fetchall()
        return [self._row_to_record(row) for row in rows]

    def delete(self, record_id: int, *, project_id: str = "default") -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                "DELETE FROM memories WHERE id = ? AND project_id = ?",
                (int(record_id), project_id.strip() or "default"),
            )
            return rows.rowcount > 0

    def clear(self, *, project_id: str | None = None) -> int:
        with self._connect() as conn:
            if project_id is None:
                rows = conn.execute("DELETE FROM memories")
            else:
                rows = conn.execute(
                    "DELETE FROM memories WHERE project_id = ?",
                    (project_id.strip() or "default",),
                )
            return int(rows.rowcount)

    def list_projects(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT project_id FROM memories ORDER BY project_id ASC").fetchall()
        out = [str(row["project_id"]).strip() for row in rows if str(row["project_id"]).strip()]
        return out or ["default"]

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=int(row["id"]),
            project_id=str(row["project_id"]),
            statement=self._dec(str(row["statement"])),
            source_type=str(row["source_type"]),
            source_ref=self._dec(str(row["source_ref"])),
            confidence=float(row["confidence"]),
            ttl_days=int(row["ttl_days"]),
            created_at=str(row["created_at"]),
            reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] is not None else None,
            redaction_status=str(row["redaction_status"]),
        )

    def backdate_for_test(self, record_id: int, *, days_ago: int) -> None:
        created = (_utcnow() - timedelta(days=days_ago)).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (created, int(record_id)))

    def _enc(self, value: str, aad_ref: str) -> str:
        if self.cipher is None:
            return value
        return self.cipher.encrypt_text(
            value,
            aad={"record_type": "memory", "source_ref": aad_ref, "orchestrator_version": "0.1.0"},
        )

    def _dec(self, value: str) -> str:
        if self.cipher is None:
            return value
        return self.cipher.decrypt_text(value)
