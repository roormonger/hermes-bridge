"""Simple SQLite-backed chat history for the standalone Hermes web UI."""

from __future__ import annotations

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
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

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
            """
        )
        conn.commit()

    def create_chat(self, chat_id: str, title: str = "New chat") -> None:
        now = time.time()
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO chats (chat_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (chat_id, title, now, now),
        )
        conn.commit()

    def rename_chat(self, chat_id: str, title: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE chats SET title = ?, updated_at = ? WHERE chat_id = ?",
            (title, time.time(), chat_id),
        )
        conn.commit()

    def delete_chat(self, chat_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        conn.commit()

    def list_chats(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chats ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_chat(self, chat_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def add_message(self, chat_id: str, role: str, content: str) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, time.time()),
        )
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE chat_id = ?",
            (time.time(), chat_id),
        )
        conn.commit()
        return cur.lastrowid

    def update_message(self, message_id: int, content: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (content, message_id),
        )
        conn.commit()

    def get_messages(self, chat_id: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_messages(self, chat_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.commit()
