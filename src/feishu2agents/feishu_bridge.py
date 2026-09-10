"""SQLite bridge linking an agent run to the Feishu message that should reply.

The reference relay store (``workspace_agent_relay_mcp``) does not track which
Feishu message triggered a run, so a small table records that mapping. Delivery
is **exactly-once per Feishu message**: the table is keyed by
``feishu_message_id`` and the claim transitions ``delivered`` atomically, so
even when several workers/instances share the file, only one posts the answer.

A run is looked up by ``request_id``; rows older than the worker's freshness
TTL are ignored so a restart never flushes very old, stale @messages.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class FeishuBridge:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
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
                CREATE TABLE IF NOT EXISTS feishu_run_bridge (
                    feishu_message_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    delivered_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feishu_reply_conversation (
                    outbound_message_id TEXT PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            # outbound placeholder message id (the one the final answer overrides).
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(feishu_run_bridge)")}
            if "outbound_message_id" not in cols:
                conn.execute(
                    "ALTER TABLE feishu_run_bridge "
                    "ADD COLUMN outbound_message_id TEXT"
                )
            conn.commit()

    def claim(self, *, feishu_message_id: str, request_id: str) -> bool:
        """Atomically claim a Feishu message for a run.

        Returns ``True`` only if this call created the row (i.e. the message was
        not already claimed by another worker/instance). Used as an idempotency
        gate so the same @message never produces two agent runs.
        """
        if not feishu_message_id or not request_id:
            raise ValueError("feishu_message_id and request_id must not be empty")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO feishu_run_bridge
                    (feishu_message_id, request_id, created_at)
                VALUES (?, ?, ?)
                """,
                (feishu_message_id, request_id, int(time.time())),
            )
            conn.commit()
            return cur.rowcount == 1

    def pending(self) -> list[dict[str, Any]]:
        """Return (feishu_message_id, request_id, created_at, outbound_message_id)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT feishu_message_id, request_id, created_at, outbound_message_id "
                "FROM feishu_run_bridge WHERE delivered = 0 ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_outbound_message(self, request_id: str, outbound_message_id: str) -> None:
        """Remember which bot message is the placeholder for a run's answer."""
        if not request_id or not outbound_message_id:
            raise ValueError("request_id and outbound_message_id must not be empty")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE feishu_run_bridge SET outbound_message_id = ? WHERE request_id = ?",
                (outbound_message_id, request_id),
            )
            conn.commit()

    def resolve_outbound_message(self, request_id: str) -> str | None:
        """Return the placeholder message id for a run, if any."""
        if not request_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT outbound_message_id FROM feishu_run_bridge WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        value = row["outbound_message_id"] if row else None
        return str(value) if value else None

    def mark_delivered(self, request_id: str) -> bool:
        """Atomically mark a pending row delivered.

        Returns ``True`` only if this call performed the 0 -> 1 transition, i.e.
        the caller won the right to post the reply. Concurrent callers observing
        the same pending row will lose here and must skip.
        """
        if not request_id:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE feishu_run_bridge SET delivered = 1, delivered_at = ? "
                "WHERE request_id = ? AND delivered = 0",
                (int(time.time()), request_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def record_reply(self, outbound_message_id: str, conversation_key: str) -> bool:
        """Map a bot reply message id back to the conversation it belongs to.

        Users continue a conversation by quoting a bot message; this lets us
        recover that conversation from the quoted (outbound) message id.
        """
        if not outbound_message_id or not conversation_key:
            raise ValueError("outbound_message_id and conversation_key must not be empty")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO feishu_reply_conversation "
                "(outbound_message_id, conversation_key, created_at) VALUES (?, ?, ?)",
                (outbound_message_id, conversation_key, int(time.time())),
            )
            conn.commit()
            return cur.rowcount == 1

    def resolve_reply_conversation(self, outbound_message_id: str) -> str | None:
        """Return the conversation_key of a former bot reply, if known."""
        if not outbound_message_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT conversation_key FROM feishu_reply_conversation "
                "WHERE outbound_message_id = ?",
                (outbound_message_id,),
            ).fetchone()
        return str(row["conversation_key"]) if row else None