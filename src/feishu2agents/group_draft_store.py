"""Persistent drafts and created-group state for the Feishu group workflow.

Two concerns live here, both keyed by the relay ``conversation_key`` of the
@bot conversation:

* Temporary avatar bytes — an image the user sent as a quote-reply. The most
  recent image overwrites the previous one (``"latest wins"``), and the bytes
  are kept on disk so the agent can upload them via MCP without ever routing
  them through ChatGPT.
* Created-group mapping — once a group is created we persist
  ``conversation_key -> chat_id`` (plus name/members) so later quote-replies
  that tweak the name or avatar update the *same* group even after a restart.

A per-process lock plus SQLite ``busy_timeout`` keeps access safe under the
single-instance assumption already mandated elsewhere in the project.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# Temp avatars older than this are treated as absent / pruned on write.
_AVATAR_TTL_SECONDS = 24 * 3600


class GroupDraftStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.avatar_dir = self.database_path.parent / "avatars"
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.database_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS avatar_tmp (
                    conversation_key TEXT PRIMARY KEY,
                    avatar_image_path TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS group_map (
                    conversation_key TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    member_open_ids TEXT NOT NULL DEFAULT '[]',
                    avatar_image_path TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _avatar_filename(conversation_key: str) -> str:
        digest = hashlib.sha256(conversation_key.encode("utf-8")).hexdigest()[:32]
        return f"{digest}.img"

    @staticmethod
    def _expired(updated_at: int) -> bool:
        return (time.time() - updated_at) > _AVATAR_TTL_SECONDS

    # --- temp avatar ---

    def save_avatar(self, conversation_key: str, image_bytes: bytes) -> None:
        """Persist the latest avatar bytes, overwriting any previous one."""
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = self.avatar_dir / self._avatar_filename(conversation_key)
        with contextlib.suppress(OSError):
            avatar_path.unlink()
        avatar_path.write_bytes(image_bytes)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO avatar_tmp
                    (conversation_key, avatar_image_path, updated_at)
                VALUES (?, ?, ?)
                """,
                (conversation_key, str(avatar_path), now),
            )
            conn.execute(
                "DELETE FROM avatar_tmp WHERE updated_at < ?",
                (now - _AVATAR_TTL_SECONDS,),
            )
            conn.commit()

    def _read_avatar(self, conversation_key: str) -> tuple[str, int] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT avatar_image_path, updated_at FROM avatar_tmp "
                "WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        if row is None or self._expired(int(row["updated_at"])):
            return None
        return str(row["avatar_image_path"]), int(row["updated_at"])

    def read_avatar(self, conversation_key: str) -> bytes | None:
        found = self._read_avatar(conversation_key)
        if found is None:
            return None
        try:
            return Path(found[0]).read_bytes()
        except OSError:
            return None

    def has_avatar(self, conversation_key: str) -> bool:
        return self._read_avatar(conversation_key) is not None

    def avatar_size(self, conversation_key: str) -> int | None:
        data = self.read_avatar(conversation_key)
        return len(data) if data is not None else None

    # --- created-group mapping ---

    def save_group(
        self,
        conversation_key: str,
        chat_id: str,
        *,
        name: str = "",
        member_open_ids: list[str] | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO group_map
                    (conversation_key, chat_id, name, member_open_ids,
                     avatar_image_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    conversation_key,
                    chat_id,
                    name,
                    json.dumps(member_open_ids or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_group(self, conversation_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM group_map WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        try:
            entry["member_open_ids"] = json.loads(entry.get("member_open_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            entry["member_open_ids"] = []
        if entry.get("avatar_image_path"):
            entry["has_avatar"] = Path(entry["avatar_image_path"]).exists()
        else:
            entry["has_avatar"] = False
        return entry

    def update_group(
        self,
        conversation_key: str,
        *,
        name: str | None = None,
        avatar_image_path: str | None = None,
    ) -> None:
        existing = self.get_group(conversation_key)
        if existing is None:
            return
        new_name = name if name is not None else existing["name"]
        new_avatar = (
            avatar_image_path
            if avatar_image_path is not None
            else (existing.get("avatar_image_path") or None)
        )
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE group_map
                SET name = ?, avatar_image_path = ?, updated_at = ?
                WHERE conversation_key = ?
                """,
                (new_name, new_avatar, now, conversation_key),
            )
            conn.commit()