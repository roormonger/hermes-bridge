"""User accounts and JWT authentication for the standalone web UI."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import bcrypt
import jwt

from .config import data_dir


ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7


def _row_to_user(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["free_models_only"] = bool(data.get("free_models_only") or 0)
    return data


class UserStore:
    """SQLite-backed user accounts and authentication tokens."""

    def __init__(self, secret: str, db_path: Optional[Path] = None) -> None:
        self._secret = secret
        self._db_path = db_path or (data_dir() / "users.db")
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
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                free_models_only INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "free_models_only" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN free_models_only INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()

    def create_user(
        self,
        username: str,
        password: str,
        *,
        free_models_only: bool = False,
    ) -> dict:
        """Create a new user. Raises ValueError if the username is taken."""
        username = username.strip().lower()
        if not username or not password:
            raise ValueError("username and password are required")
        if len(password) < 4:
            raise ValueError("password must be at least 4 characters")

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        user_id = str(uuid.uuid4())
        now = time.time()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, password_hash, created_at, free_models_only) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username, password_hash, now, 1 if free_models_only else 0),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"username '{username}' is already taken") from exc
        return {
            "user_id": user_id,
            "username": username,
            "free_models_only": free_models_only,
        }

    def verify_user(self, username: str, password: str) -> Optional[dict]:
        """Verify credentials and return the user record, or None."""
        username = username.strip().lower()
        conn = self._conn()
        row = conn.execute(
            "SELECT user_id, username, password_hash, free_models_only FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        stored = row["password_hash"].encode("utf-8")
        if not bcrypt.checkpw(password.encode("utf-8"), stored):
            return None
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "free_models_only": bool(row["free_models_only"] or 0),
        }

    def get_by_id(self, user_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT user_id, username, created_at, free_models_only FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT user_id, username, created_at, free_models_only FROM users "
            "ORDER BY username ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_user(row) for row in rows]

    def set_free_models_only(self, user_id: str, free_models_only: bool) -> Optional[dict]:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE users SET free_models_only = ? WHERE user_id = ?",
            (1 if free_models_only else 0, user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_by_id(user_id)

    def delete_user(self, user_id: str) -> bool:
        conn = self._conn()
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0

    def create_token(self, user_id: str) -> str:
        """Create a JWT access token for the user."""
        now = time.time()
        payload = {
            "sub": user_id,
            "iat": now,
            "exp": now + (ACCESS_TOKEN_EXPIRE_DAYS * 86400),
        }
        return jwt.encode(payload, self._secret, algorithm=ALGORITHM)

    def decode_token(self, token: str) -> Optional[dict]:
        """Decode a JWT and return the user record, or None if invalid."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=[ALGORITHM])
        except jwt.PyJWTError:
            return None
        user_id = payload.get("sub")
        if not user_id:
            return None
        return self.get_by_id(user_id)
