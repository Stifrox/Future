"""Durable recent-chat sessions for Future.

The repository intentionally has a small interface so the storage backend can
be replaced later without changing the FastAPI or browser-facing code.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_RECENT_SESSIONS = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_path() -> Path:
    return Path(os.getenv("FUTURE_CHAT_SESSIONS_DB", "data/chat_sessions.sqlite3"))


class ChatSessionStore:
    """SQLite-backed chat sessions with protected starred sessions."""

    def __init__(self, database_path: Optional[Path] = None, max_sessions: int = MAX_RECENT_SESSIONS):
        self.database_path = Path(database_path) if database_path else _database_path()
        self.max_sessions = max(1, int(max_sessions))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    starred INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    messages TEXT NOT NULL DEFAULT '[]'
                )"""
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags") or "[]")
        result["messages"] = json.loads(result.pop("messages") or "[]")
        result["starred"] = bool(result["starred"])
        result["message_count"] = len(result["messages"])
        return result

    def create(self, title: str = "New conversation", description: str = "", tags: Optional[List[str]] = None) -> Dict[str, Any]:
        now = _utc_now()
        session = {
            "id": uuid.uuid4().hex,
            "title": (title or "New conversation").strip()[:120],
            "description": (description or "").strip()[:500],
            "tags": sorted({str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()}),
            "starred": False,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session["id"], session["title"], session["description"], json.dumps(session["tags"]), 0,
                 now, now, "[]"),
            )
        self.prune()
        return self._row_from_dict(session)

    @staticmethod
    def _row_from_dict(session: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(session)
        result["message_count"] = len(result["messages"])
        return result

    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row(row) if row else None

    def add_messages(self, session_id: str, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        session = self.get(session_id)
        if not session:
            raise KeyError(session_id)
        history = session["messages"] + [
            {"role": str(item.get("role", "user")), "content": str(item.get("content", ""))}
            for item in messages if str(item.get("content", "")).strip()
        ]
        if session["title"] == "New conversation" and history:
            first = history[0]["content"].strip().replace("\n", " ")
            session["title"] = first[:80] + ("..." if len(first) > 80 else "")
        updated = _utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ?, messages = ? WHERE id = ?",
                (session["title"], updated, json.dumps(history), session_id),
            )
        session["messages"] = history
        session["updated_at"] = updated
        return self._row_from_dict(session)

    def list(self, query: str = "") -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM chat_sessions ORDER BY starred DESC, updated_at DESC").fetchall()
        sessions = [self._row(row) for row in rows]
        if query.strip():
            needle = query.lower().strip()
            sessions = [item for item in sessions if needle in json.dumps(item).lower()]
        return [self._summary(item) for item in sessions]

    @staticmethod
    def _summary(session: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in session.items() if key != "messages"}

    def update(self, session_id: str, title: Optional[str] = None, description: Optional[str] = None,
               tags: Optional[List[str]] = None, starred: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        session = self.get(session_id)
        if not session:
            return None
        if title is not None:
            session["title"] = title.strip()[:120] or "Untitled conversation"
        if description is not None:
            session["description"] = description.strip()[:500]
        if tags is not None:
            session["tags"] = sorted({str(tag).strip()[:40] for tag in tags if str(tag).strip()})
        if starred is not None:
            session["starred"] = bool(starred)
        with self._connect() as connection:
            connection.execute(
                "UPDATE chat_sessions SET title=?, description=?, tags=?, starred=?, updated_at=? WHERE id=?",
                (session["title"], session["description"], json.dumps(session["tags"]), int(session["starred"]),
                 _utc_now(), session_id),
            )
        return self.get(session_id)

    def prune(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM chat_sessions WHERE starred = 0 AND id NOT IN
                   (SELECT id FROM chat_sessions WHERE starred = 0 ORDER BY updated_at DESC LIMIT ?)""",
                (self.max_sessions,),
            )