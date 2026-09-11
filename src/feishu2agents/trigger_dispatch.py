"""Background dispatcher for ChatGPT Workspace Agent trigger calls.

The reference project schedules its trigger HTTP dispatch on the ASGI event
loop (so ``asyncio.create_task`` works). Feishu messages are handled on the
lark SDK's WebSocket worker thread, which has no running loop, so we run the
blocking trigger call on a thread pool instead and then update the shared relay
store. The store write is the same ``update_run_trigger_result`` transition the
reference dispatcher performs, so the dashboard's SSE stream and run status stay
consistent.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from feishu2agents.relay.trigger import TriggerClient

logger = logging.getLogger(__name__)


class FeishuTriggerDispatcher:
    def __init__(self, *, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="feishu-trigger"
        )

    def dispatch(
        self,
        *,
        store: Any,
        trigger_client: TriggerClient,
        trigger_url: str,
        access_token: str,
        conversation_key: str,
        input_text: str,
        idempotency_key: str,
        request_id: str,
    ) -> None:
        self._pool.submit(
            self._run,
            store=store,
            trigger_client=trigger_client,
            trigger_url=trigger_url,
            access_token=access_token,
            conversation_key=conversation_key,
            input_text=input_text,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    def _run(self, **kwargs: Any) -> None:
        store = kwargs["store"]
        trigger_client = kwargs["trigger_client"]
        request_id = kwargs["request_id"]
        try:
            result = trigger_client.trigger(
                trigger_url=kwargs["trigger_url"],
                access_token=kwargs["access_token"],
                conversation_key=kwargs["conversation_key"],
                input_text=kwargs["input_text"],
                idempotency_key=kwargs["idempotency_key"],
            )
        except Exception as exc:  # trigger() normally self-catches; belt-and-suspenders
            logger.error(
                "trigger dispatch raised for request_id=%s type=%s",
                request_id,
                type(exc).__name__,
            )
            store.update_run_trigger_result(
                request_id=request_id,
                trigger_http_status=0,
                trigger_x_request_id=None,
                conversation_url=None,
                trigger_error=f"{type(exc).__name__}: {exc}",
            )
            return
        store.update_run_trigger_result(
            request_id=request_id,
            trigger_http_status=result.http_status,
            trigger_x_request_id=result.x_request_id,
            conversation_url=result.conversation_url,
            trigger_error=result.error,
        )
        logger.info("trigger dispatch done request_id=%s http=%s", request_id, result.http_status)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)