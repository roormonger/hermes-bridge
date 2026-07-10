"""Simple SQLite-backed chat history for the standalone Hermes web UI."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .config import data_dir


class ChatHistoryStore:
    """Store chat titles and messages in a local SQLite database."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or (data_dir() / "chat_history.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        # Migration: add user_id column to older databases that predate multi-user support.
        try:
            conn.execute("ALTER TABLE chats ADD COLUMN user_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists.
            pass
        conn.executescript(
            """

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_chats_updated_at ON chats(updated_at);
            CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id);
            """
        )
        # Migration: add images column to older databases.
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN images TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.commit()

    def _user_where(self, user_id: Optional[str]) -> str:
        """Return a WHERE clause that matches chats owned by the user or unowned (legacy)."""
        return "user_id = ? OR user_id IS NULL" if user_id else "user_id IS NULL"

    def _user_params(self, user_id: Optional[str], extra: tuple = ()) -> tuple:
        return (user_id, *extra) if user_id else extra

    def create_chat(self, chat_id: str, user_id: Optional[str], title: str = "New chat") -> None:
        now = time.time()
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO chats (chat_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, title, now, now),
        )
        conn.commit()

    def rename_chat(self, chat_id: str, user_id: Optional[str], title: str) -> None:
        conn = self._conn()
        conn.execute(
            f"UPDATE chats SET title = ?, updated_at = ? WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (title, time.time(), chat_id, *self._user_params(user_id)),
        )
        conn.commit()

    def delete_chat(self, chat_id: str, user_id: Optional[str]) -> None:
        conn = self._conn()
        conn.execute(
            f"DELETE FROM chats WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (chat_id, *self._user_params(user_id)),
        )
        conn.commit()

    def list_chats(self, user_id: Optional[str], limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            f"SELECT chat_id, user_id, title, created_at, updated_at FROM chats WHERE ({self._user_where(user_id)}) ORDER BY updated_at DESC LIMIT ?",
            (*self._user_params(user_id), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_chat(self, chat_id: str, user_id: Optional[str]) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            f"SELECT chat_id, user_id, title, created_at, updated_at FROM chats WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (chat_id, *self._user_params(user_id)),
        ).fetchone()
        return dict(row) if row else None

    def add_message(self, chat_id: str, user_id: Optional[str], role: str, content: str, images: list[str] | None = None) -> int:
        conn = self._conn()
        # Ensure the chat belongs to the user (or is unowned) before adding a message.
        chat = self.get_chat(chat_id, user_id)
        if chat is None:
            raise PermissionError("chat not found or access denied")
        images_json = json.dumps(images) if images else None
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, images, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, images_json, time.time()),
        )
        conn.execute(
            f"UPDATE chats SET updated_at = ? WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (time.time(), chat_id, *self._user_params(user_id)),
        )
        conn.commit()
        return cur.lastrowid

    def update_message(self, message_id: int, user_id: Optional[str], content: str) -> None:
        conn = self._conn()
        conn.execute(
            f"""
            UPDATE messages SET content = ?
            WHERE id = ? AND chat_id IN (
                SELECT chat_id FROM chats WHERE {self._user_where(user_id)}
            )
            """,
            (content, message_id, *self._user_params(user_id)),
        )
        conn.commit()

    def get_messages(self, chat_id: str, user_id: Optional[str]) -> list[dict]:
        conn = self._conn()
        # Ensure the chat belongs to the user (or is unowned) before returning messages.
        chat = self.get_chat(chat_id, user_id)
        if chat is None:
            return []
        rows = conn.execute(
            "SELECT id, role, content, images, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            raw = d.pop("images", None)
            d["images"] = json.loads(raw) if raw else []
            result.append(d)
        return result

    def delete_messages(self, chat_id: str, user_id: Optional[str]) -> None:
        conn = self._conn()
        conn.execute(
            f"""
            DELETE FROM messages WHERE chat_id = ? AND chat_id IN (
                SELECT chat_id FROM chats WHERE {self._user_where(user_id)}
            )
            """,
            (chat_id, *self._user_params(user_id)),
        )
        conn.commit()
