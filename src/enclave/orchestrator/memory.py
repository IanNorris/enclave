"""Cross-session memory store.

Per-user SQLite database for persistent memories that survive across
agent sessions. Supports key memories (injected into every system
prompt), manual storage/recall, and auto-dreaming extraction.

The orchestrator owns the database — agents interact via IPC.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from enclave.common.logging import get_logger

log = get_logger("memory")

# Valid memory categories
CATEGORIES = {"personal", "technical", "project", "workflow", "debug", "other"}


@dataclass
class Memory:
    """A single memory entry."""

    id: str
    category: str
    content: str
    source_session: str
    created_at: float
    last_accessed: float
    access_count: int
    is_key_memory: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "content": self.content,
            "source_session": self.source_session,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "is_key_memory": self.is_key_memory,
        }


class MemoryStore:
    """Per-user SQLite memory database.

    Each user gets their own database file at:
        {data_dir}/memory/{user_id_sanitized}.db

    Thread-safety: SQLite in WAL mode supports concurrent reads.
    The orchestrator is the sole writer.
    """

    def __init__(self, data_dir: str | Path, user_id: str) -> None:
        self._dir = Path(data_dir) / "memory"
        self._dir.mkdir(parents=True, exist_ok=True)

        # Sanitize matrix user ID for filename
        safe_name = user_id.replace("@", "").replace(":", "_")
        self._db_path = self._dir / f"{safe_name}.db"
        self._user_id = user_id

        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

        log.info("Memory store opened: %s (%s)", self._db_path, user_id)

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'other',
                content TEXT NOT NULL,
                source_session TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0,
                is_key_memory INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memories_key
                ON memories(is_key_memory) WHERE is_key_memory = 1;
            CREATE INDEX IF NOT EXISTS idx_memories_category
                ON memories(category);
        """)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def store(
        self,
        content: str,
        category: str = "other",
        source_session: str = "",
        is_key_memory: bool = False,
    ) -> Memory:
        """Store a new memory. Returns the created Memory."""
        if category not in CATEGORIES:
            category = "other"

        now = time.time()
        mem_id = uuid.uuid4().hex[:12]

        self._conn.execute(
            """INSERT INTO memories
               (id, category, content, source_session, created_at,
                last_accessed, access_count, is_key_memory)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (mem_id, category, content, source_session, now, now,
             1 if is_key_memory else 0),
        )
        self._conn.commit()

        mem = Memory(
            id=mem_id,
            category=category,
            content=content,
            source_session=source_session,
            created_at=now,
            last_accessed=now,
            access_count=0,
            is_key_memory=is_key_memory,
        )
        log.debug("Stored memory %s [%s] key=%s", mem_id, category, is_key_memory)
        return mem

    def query(
        self,
        keyword: str = "",
        category: str = "",
        limit: int = 20,
    ) -> list[Memory]:
        """Search memories by keyword and/or category.

        Keyword search is case-insensitive substring match on content.
        Results ordered by last_accessed descending.
        """
        conditions = []
        params: list[Any] = []

        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if category and category in CATEGORIES:
            conditions.append("category = ?")
            params.append(category)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        rows = self._conn.execute(
            f"""SELECT id, category, content, source_session, created_at,
                       last_accessed, access_count, is_key_memory
                FROM memories {where}
                ORDER BY last_accessed DESC
                LIMIT ?""",
            params + [limit],
        ).fetchall()

        # Update access timestamps
        now = time.time()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            self._conn.execute(
                f"""UPDATE memories SET last_accessed = ?, access_count = access_count + 1
                    WHERE id IN ({placeholders})""",
                [now] + ids,
            )
            self._conn.commit()

        return [self._row_to_memory(r) for r in rows]

    def list_key_memories(self) -> list[Memory]:
        """Get all key memories (for system prompt injection)."""
        rows = self._conn.execute(
            """SELECT id, category, content, source_session, created_at,
                      last_accessed, access_count, is_key_memory
               FROM memories WHERE is_key_memory = 1
               ORDER BY created_at ASC""",
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_recent(self, limit: int = 20) -> list[Memory]:
        """Get recent memories."""
        rows = self._conn.execute(
            """SELECT id, category, content, source_session, created_at,
                      last_accessed, access_count, is_key_memory
               FROM memories
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def delete(self, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            log.debug("Deleted memory %s", memory_id)
        return deleted

    def count(self) -> int:
        """Total number of memories."""
        row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0] if row else 0

    def key_memories_as_prompt(self, max_lines: int = 200) -> str:
        """Format key memories for system prompt injection.

        Returns a markdown-formatted block, capped at max_lines.
        """
        memories = self.list_key_memories()
        if not memories:
            return ""

        lines = ["## Your Memories", ""]
        lines.append(
            "These are things you've learned about this user across sessions:"
        )
        lines.append("")

        for mem in memories:
            prefix = f"[{mem.category}]" if mem.category != "other" else ""
            entry = f"- {prefix} {mem.content}".strip()
            lines.append(entry)

            if len(lines) >= max_lines:
                lines.append(f"- _(truncated — {len(memories)} total memories)_")
                break

        return "\n".join(lines)

    def store_from_dreaming(
        self,
        extracted: list[dict[str, Any]],
        source_session: str = "",
    ) -> int:
        """Store memories extracted by auto-dreaming.

        Each entry in `extracted` should have:
          - content: str
          - category: str (optional, defaults to 'other')
          - is_key: bool (optional, defaults to False)

        Deduplicates against existing memories (exact content match).
        Returns count of new memories stored.
        """
        stored = 0
        for entry in extracted:
            content = entry.get("content", "").strip()
            if not content:
                continue

            # Check for exact duplicate
            existing = self._conn.execute(
                "SELECT id FROM memories WHERE content = ?", (content,)
            ).fetchone()
            if existing:
                continue

            category = entry.get("category", "other")
            is_key = entry.get("is_key", False)
            self.store(
                content=content,
                category=category,
                source_session=source_session,
                is_key_memory=is_key,
            )
            stored += 1

        log.info(
            "Auto-dreaming: stored %d/%d memories (session %s)",
            stored, len(extracted), source_session,
        )
        return stored

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_memory(row: tuple) -> Memory:
        return Memory(
            id=row[0],
            category=row[1],
            content=row[2],
            source_session=row[3],
            created_at=row[4],
            last_accessed=row[5],
            access_count=row[6],
            is_key_memory=bool(row[7]),
        )
