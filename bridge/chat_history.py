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
        # Migration: add last_usage_json column for persisted token baseline.
        try:
            conn.execute("ALTER TABLE chats ADD COLUMN last_usage_json TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: add usage_json column for per-message token usage.
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN usage_json TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: add tool_steps_json column for persisted chain-of-thought/tool calls.
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN tool_steps_json TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        # Migration: add pinned column for sidebar pinning.
        try:
            conn.execute("ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
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
            f"SELECT chat_id, user_id, title, created_at, updated_at, pinned FROM chats WHERE ({self._user_where(user_id)}) ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (*self._user_params(user_id), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_chat(self, chat_id: str, user_id: Optional[str]) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            f"SELECT chat_id, user_id, title, created_at, updated_at, pinned FROM chats WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (chat_id, *self._user_params(user_id)),
        ).fetchone()
        return dict(row) if row else None

    def pin_chat(self, chat_id: str, user_id: Optional[str], pinned: bool) -> None:
        conn = self._conn()
        conn.execute(
            f"UPDATE chats SET pinned = ? WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (1 if pinned else 0, chat_id, *self._user_params(user_id)),
        )
        conn.commit()

    def add_message(
        self,
        chat_id: str,
        user_id: Optional[str],
        role: str,
        content: str,
        images: list[str] | None = None,
        tool_steps: list[dict] | None = None,
    ) -> int:
        conn = self._conn()
        # Ensure the chat belongs to the user (or is unowned) before adding a message.
        chat = self.get_chat(chat_id, user_id)
        if chat is None:
            raise PermissionError("chat not found or access denied")
        images_json = json.dumps(images) if images else None
        tool_steps_json = json.dumps(tool_steps) if tool_steps else None
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, images, tool_steps_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, role, content, images_json, tool_steps_json, time.time()),
        )
        conn.execute(
            f"UPDATE chats SET updated_at = ? WHERE chat_id = ? AND ({self._user_where(user_id)})",
            (time.time(), chat_id, *self._user_params(user_id)),
        )
        conn.commit()
        return cur.lastrowid

    def update_message(
        self, message_id: int, user_id: Optional[str], content: str, tool_steps: list[dict] | None = None
    ) -> None:
        conn = self._conn()
        tool_steps_json = json.dumps(tool_steps) if tool_steps else None
        conn.execute(
            f"""
            UPDATE messages SET content = ?, tool_steps_json = ?
            WHERE id = ? AND chat_id IN (
                SELECT chat_id FROM chats WHERE {self._user_where(user_id)}
            )
            """,
            (content, tool_steps_json, message_id, *self._user_params(user_id)),
        )
        conn.commit()

    def update_message_usage(self, message_id: int, user_id: Optional[str], usage: dict) -> None:
        conn = self._conn()
        conn.execute(
            f"""
            UPDATE messages SET usage_json = ?
            WHERE id = ? AND chat_id IN (
                SELECT chat_id FROM chats WHERE {self._user_where(user_id)}
            )
            """,
            (json.dumps(usage), message_id, *self._user_params(user_id)),
        )
        conn.commit()

    def get_messages(self, chat_id: str, user_id: Optional[str]) -> list[dict]:
        conn = self._conn()
        # Ensure the chat belongs to the user (or is unowned) before returning messages.
        chat = self.get_chat(chat_id, user_id)
        if chat is None:
            return []
        rows = conn.execute(
            "SELECT id, role, content, images, usage_json, tool_steps_json, created_at FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            raw = d.pop("images", None)
            d["images"] = json.loads(raw) if raw else []
            raw_usage = d.pop("usage_json", None)
            d["usage"] = json.loads(raw_usage) if raw_usage else None
            raw_tool_steps = d.pop("tool_steps_json", None)
            d["tool_steps"] = json.loads(raw_tool_steps) if raw_tool_steps else []
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

    def delete_last_turn(self, chat_id: str, user_id: Optional[str]) -> int:
        """Delete the last assistant message and the preceding user message.

        Returns the number of rows deleted.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, role FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 2",
            (chat_id,)
        ).fetchall()
        if not rows:
            return 0
        # If the latest message is an assistant, delete it and the user before it.
        # If the latest is a user (e.g. no response yet), delete just that user message.
        ids_to_delete = []
        if rows[0]["role"] == "assistant":
            ids_to_delete.append(rows[0]["id"])
            if len(rows) > 1 and rows[1]["role"] == "user":
                ids_to_delete.append(rows[1]["id"])
        elif rows[0]["role"] == "user":
            ids_to_delete.append(rows[0]["id"])
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            conn.execute(
                f"""
                DELETE FROM messages WHERE id IN ({placeholders})
                  AND chat_id IN (SELECT chat_id FROM chats WHERE {self._user_where(user_id)})
                """,
                (*ids_to_delete, *self._user_params(user_id)),
            )
            conn.commit()
        return len(ids_to_delete)

    def set_chat_usage(self, chat_id: str, user_id: Optional[str], usage: dict) -> None:
        conn = self._conn()
        conn.execute(
            f"""
            UPDATE chats SET last_usage_json = ?
            WHERE chat_id = ? AND ({self._user_where(user_id)})
            """,
            (json.dumps(usage), chat_id, *self._user_params(user_id)),
        )
        conn.commit()

    def get_chat_usage(self, chat_id: str, user_id: Optional[str]) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            f"""
            SELECT last_usage_json FROM chats
            WHERE chat_id = ? AND ({self._user_where(user_id)})
            """,
            (chat_id, *self._user_params(user_id)),
        ).fetchone()
        if row and row["last_usage_json"]:
            try:
                return json.loads(row["last_usage_json"])
            except json.JSONDecodeError:
                return None
        return None
