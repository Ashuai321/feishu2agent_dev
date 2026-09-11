"""Background worker that posts completed agent runs back into Feishu.

The agent finishes a turn by calling the relay's ``record_result`` MCP tool,
which writes a terminal status + markdown answer into the relay store. This
worker periodically claims finished-but-undelivered runs (tracked by
:class:`~feishu2agents.feishu_bridge.FeishuBridge`) and posts their answer as a
reply to the original Feishu message.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from feishu2agents.relay.store.relay_store import TERMINAL_STATUSES, RelayStore

from .feishu_bridge import FeishuBridge

logger = logging.getLogger(__name__)

# A reply receiver takes a Feishu message_id and the text to post, and returns
# the outbound message id of the posted reply (used to map quote->conversation).
ReplyCallable = Callable[[str, str], str]
# An optional in-place editor that overwrites a bot-sent message's content.
UpdateCallable = Callable[[str, str], None]


def format_result_for_feishu(store: RelayStore, run: dict[str, Any]) -> str:
    """Render a finished run's ``record_result`` answer as Feishu text."""
    status = str(run.get("status") or "done")
    title = ""
    markdown = ""
    try:
        events = store.list_events(int(run["id"]))
        result = next((e for e in events if e.get("event_type") == "result"), None)
        if result is not None:
            title = str(result.get("title") or "").strip()
            markdown = str(result.get("markdown") or "").strip()
    except (KeyError, TypeError, ValueError):
        pass
    body = "\n".join(part for part in (title, markdown) if part).strip()
    if status == "done":
        return body or "(Agent 已完成，但未返回内容。)"
    if status == "blocked":
        return f"Agent 任务被阻塞：{body or '(未提供原因)'}"
    return f"Agent 任务失败：{body or '(未提供原因)'}"


class RelayWorker:
    def __init__(
        self,
        store: RelayStore,
        bridge: FeishuBridge,
        reply: ReplyCallable,
        *,
        update_message: UpdateCallable | None = None,
        interval: float = 2.0,
        freshness_ttl_seconds: int = 3600,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._reply = reply
        self._update_message = update_message
        self._interval = interval
        # Ignore pending rows older than this: prevents a restart from flushing
        # very old @messages (some of which were already replied to).
        self._freshness_ttl = freshness_ttl_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="relay-worker", daemon=True)
        self._thread.start()
        logger.info("Relay worker started interval=%s", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        logger.info("Relay worker stopped")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.deliver_once()
            except Exception:
                logger.exception("Relay worker deliver pass failed")

    def deliver_once(self) -> int:
        """Deliver any completed-and-fresh runs. Returns number delivered."""
        delivered = 0
        now = int(time.time())
        for row in self._bridge.pending():
            request_id = row["request_id"]
            message_id = row["feishu_message_id"]
            created_at = int(row.get("created_at") or 0)
            # Skip stale @messages so a restart never replies to very old ones.
            if created_at and now - created_at > self._freshness_ttl:
                continue
            try:
                run = self._store.get_run_by_request_id(request_id)
            except KeyError:
                continue
            if run.get("status") not in TERMINAL_STATUSES or not message_id:
                continue
            # Atomically claim the delivery: only one worker/instance may post.
            if not self._bridge.mark_delivered(request_id):
                continue
            text = format_result_for_feishu(self._store, run)
            try:
                # Prefer editing the "processing" placeholder message in place.
                placeholder_id = self._bridge.resolve_outbound_message(request_id)
                if placeholder_id and self._update_message is not None:
                    self._update_message(placeholder_id, text)
                    self._bridge.record_reply(
                        placeholder_id, run.get("conversation_key") or ""
                    )
                else:
                    outbound_id = self._reply(message_id, text)
                    self._bridge.record_reply(
                        outbound_id, run.get("conversation_key") or ""
                    )
                delivered += 1
                logger.info(
                    "Posted agent result to Feishu request_id=%s message_id=%s",
                    request_id,
                    message_id,
                )
            except Exception:
                logger.exception(
                    "Failed to post agent result request_id=%s message_id=%s",
                    request_id,
                    message_id,
                )
        return delivered