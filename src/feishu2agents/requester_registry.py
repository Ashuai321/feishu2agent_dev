"""Thread-safe registry mapping a relay conversation_key to the Feishu user
who mentioned the bot in that conversation.

The Workspace Agent does not know the Feishu identity of the person who sends
a message. When feishu2agents dispatches a message it registers the sender here
keyed by the same ``conversation_key`` it puts in the trigger header. The
Feishu MCP tools (``get_requester_info`` / ``create_private_group``) then look
the requester up so the agent can, e.g., create a private group for exactly
that person.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any


class RequesterRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    def register(
        self,
        conversation_key: str,
        *,
        open_id: str | None = None,
        name: str = "",
        source_chat_id: str = "",
        platform: str = "feishu",
    ) -> None:
        """Record who mentioned the bot for a given conversation_key."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._entries[conversation_key] = {
                "open_id": open_id or "",
                "name": name,
                "source_chat_id": source_chat_id,
                "platform": str(platform or "feishu").strip().lower() or "feishu",
                "created_at": now,
            }

    def get(self, conversation_key: str) -> dict[str, Any] | None:
        """Return a copy of the requester entry (or None)."""
        with self._lock:
            entry = self._entries.get(conversation_key)
            return dict(entry) if entry else None

    def bind_group(self, conversation_key: str, group_chat_id: str) -> None:
        """Attach the created private-group chat_id to an existing entry."""
        with self._lock:
            entry = self._entries.get(conversation_key)
            if entry is not None:
                entry["created_group_chat_id"] = group_chat_id

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(entry) for key, entry in self._entries.items()}
