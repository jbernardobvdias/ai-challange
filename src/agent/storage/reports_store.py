"""Saved reports and the audit log, in SQLite (Cloud SQL Postgres in production).

Every query filters on owner_id, which comes from the signed-in user, so one user can never read or
delete another user's reports. Deleting is a soft delete, written together with its audit entry.
"""
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.observability import current_trace_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    deleted_at      TEXT,
    delete_batch    TEXT
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports (owner_id, deleted_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    action          TEXT NOT NULL,
    report_ids      TEXT NOT NULL,
    conversation_id TEXT,
    trace_id        TEXT
);
"""


@dataclass(frozen=True)
class Report:
    """A saved report."""

    id: int
    title: str
    content: str
    conversation_id: str
    created_at: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _report(row: sqlite3.Row) -> Report:
    return Report(row["id"], row["title"], row["content"], row["conversation_id"], row["created_at"])


class ReportStore:
    """Stores reports and the audit log."""

    def __init__(self, path: Path, clock: Callable[[], datetime] = _utcnow) -> None:
        self.path, self.now = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # Databases created before trace IDs were added to the audit log.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(audit_log)")}
            if "trace_id" not in columns:
                db.execute("ALTER TABLE audit_log ADD COLUMN trace_id TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One connection per operation: tools may run on different threads.
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:  # commits on success, rolls back on error
                yield db
        finally:
            db.close()

    def _audit(self, db: sqlite3.Connection, user: str, action: str, ids: Sequence[int], conversation: str | None) -> None:
        db.execute(
            "INSERT INTO audit_log (at, user_id, action, report_ids, conversation_id, trace_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.now().isoformat(), user, action, json.dumps(list(ids)), conversation, current_trace_id()),
        )

    def audit(self, user: str, action: str, ids: Sequence[int], conversation: str | None = None) -> None:
        with self._connect() as db:
            self._audit(db, user, action, ids, conversation)

    def audit_entries(self, user: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM audit_log WHERE user_id = ? ORDER BY id", (user,)).fetchall()
        return [{**dict(r), "report_ids": json.loads(r["report_ids"])} for r in rows]

    def save(self, owner: str, conversation: str, title: str, content: str) -> Report:
        created_at = self.now().isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO reports (owner_id, conversation_id, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (owner, conversation, title, content, created_at),
            )
            self._audit(db, owner, "save", [cursor.lastrowid], conversation)
        return Report(cursor.lastrowid, title, content, conversation, created_at)

    def find(
        self,
        owner: str,
        search: str | None = None,
        conversation_id: str | None = None,
        report_ids: Sequence[int] | None = None,
    ) -> list[Report]:
        """The owner's live reports matching every filter given, newest first."""
        where, params = ["owner_id = ?", "deleted_at IS NULL"], [owner]
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            params += [pattern, pattern]
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if report_ids is not None:
            where.append(f"id IN ({', '.join('?' * len(report_ids))})")
            params += list(report_ids)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM reports WHERE {' AND '.join(where)} ORDER BY id DESC", params).fetchall()
        return [_report(r) for r in rows]

    def soft_delete(self, owner: str, ids: Sequence[int], conversation: str | None = None) -> list[Report]:
        """Delete exactly these reports if the owner still has them; returns what was deleted."""
        batch = uuid.uuid4().hex
        marks = ", ".join("?" * len(ids))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM reports WHERE owner_id = ? AND deleted_at IS NULL AND id IN ({marks})",
                [owner, *ids],
            ).fetchall()
            deleted = [_report(r) for r in rows]
            if deleted:
                db.execute(
                    f"UPDATE reports SET deleted_at = ?, delete_batch = ? "
                    f"WHERE owner_id = ? AND deleted_at IS NULL AND id IN ({marks})",
                    [self.now().isoformat(), batch, owner, *ids],
                )
                self._audit(db, owner, "delete", [r.id for r in deleted], conversation)
        return deleted

    def restore_last_delete(self, owner: str, window: timedelta, conversation: str | None = None) -> list[Report]:
        """Undo the owner's most recent deletion, if it happened within the window."""
        cutoff = (self.now() - window).isoformat()
        with self._connect() as db:
            latest = db.execute(
                "SELECT delete_batch FROM reports WHERE owner_id = ? AND deleted_at >= ? "
                "ORDER BY deleted_at DESC LIMIT 1",
                (owner, cutoff),
            ).fetchone()
            if latest is None:
                return []
            batch = latest["delete_batch"]
            rows = db.execute(
                "SELECT * FROM reports WHERE owner_id = ? AND delete_batch = ?", (owner, batch)
            ).fetchall()
            restored = [_report(r) for r in rows]
            db.execute(
                "UPDATE reports SET deleted_at = NULL, delete_batch = NULL WHERE owner_id = ? AND delete_batch = ?",
                (owner, batch),
            )
            self._audit(db, owner, "restore", [r.id for r in restored], conversation)
        return restored
