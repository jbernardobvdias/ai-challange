"""Per-user answer preferences, in the same SQLite database as saved reports.

Values come from fixed lists rather than free text. Preferences are added to the system prompt on
every later turn, so free text would let one message plant instructions that persist.
"""
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

ResponseFormat = Literal["tables", "bullet_points", "short_paragraphs"]
Depth = Literal["brief", "detailed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""
_ALLOWED = {"response_format": get_args(ResponseFormat), "depth": get_args(Depth)}
_DESCRIPTIONS = {
    "tables": "Prefer tables for results where possible.",
    "bullet_points": "Prefer bullet points over tables and paragraphs where possible.",
    "short_paragraphs": "Prefer short paragraphs of plain prose where possible.",
    "brief": "Keep answers brief: the key result and at most a few supporting points.",
    "detailed": "Give detailed analysis: break results down and explain the reasoning.",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProfileStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _utcnow) -> None:
        self.path, self.now = path, clock
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        try:
            db.executescript(_SCHEMA)
        finally:
            db.close()

    def set(self, user: str, name: str, value: str) -> None:
        if value not in _ALLOWED.get(name, ()):
            raise ValueError(f"Invalid preference {name}={value}")
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute(
                    "INSERT INTO user_preferences (user_id, name, value, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (user_id, name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (user, name, value, self.now().isoformat()),
                )
        finally:
            db.close()

    def get(self, user: str) -> dict[str, str]:
        db = sqlite3.connect(self.path)
        try:
            rows = db.execute("SELECT name, value FROM user_preferences WHERE user_id = ?", (user,)).fetchall()
        finally:
            db.close()
        return dict(rows)

    def forget(self, user: str) -> None:
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute("DELETE FROM user_preferences WHERE user_id = ?", (user,))
        finally:
            db.close()

    def describe(self, user: str) -> list[str]:
        return [_DESCRIPTIONS[value] for value in self.get(user).values() if value in _DESCRIPTIONS]
