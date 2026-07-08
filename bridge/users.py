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
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """
        )
        conn.commit()

    def create_user(self, username: str, password: str) -> dict:
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
                "INSERT INTO users (user_id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, password_hash, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"username '{username}' is already taken") from exc
        return {"user_id": user_id, "username": username}

    def verify_user(self, username: str, password: str) -> Optional[dict]:
        """Verify credentials and return the user record, or None."""
        username = username.strip().lower()
        conn = self._conn()
        row = conn.execute(
            "SELECT user_id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        stored = row["password_hash"].encode("utf-8")
        if not bcrypt.checkpw(password.encode("utf-8"), stored):
            return None
        return {"user_id": row["user_id"], "username": row["username"]}

    def get_by_id(self, user_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT user_id, username, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_users(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT user_id, username, created_at FROM users ORDER BY username ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

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
