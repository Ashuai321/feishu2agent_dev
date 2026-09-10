"""Feishu SDK adapter: identity lookup, event dispatch, and replies."""

from __future__ import annotations

import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)

from .bot_handler import MessageHandler
from .config import Settings
from .dedupe import DedupeCache
from .message_context import MessageNormalizationError, normalize_message_event

logger = logging.getLogger(__name__)


class FeishuApiError(RuntimeError):
    """A sanitized failure returned by a Feishu API."""


class FeishuBot:
    def __init__(
        self,
        settings: Settings,
        handler: MessageHandler,
        *,
        dedupe: DedupeCache | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._dedupe = dedupe or DedupeCache()
        self._api_client = (
            lark.Client.builder()
            # .domain(lark.LARK_DOMAIN) ---切换飞书和lark
            .domain(lark.FEISHU_DOMAIN)
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self._bot_open_id = ""

    def start(self) -> None:
        self._bot_open_id = self._fetch_bot_open_id()
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        ws_client = lark.ws.Client(
            self._settings.feishu_app_id,
            self._settings.feishu_app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=event_handler,
            # domain=lark.LARK_DOMAIN, ---切换飞书和lark
            domain=lark.FEISHU_DOMAIN,
        )
        logger.info("Starting Feishu long connection bot_app_id=%s", self._settings.feishu_app_id)
        ws_client.start()

    def _fetch_bot_open_id(self) -> str:
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        response = self._api_client.request(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("get bot identity", response))
        try:
            payload = json.loads(response.raw.content)
            open_id = payload["bot"]["open_id"]
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FeishuApiError("get bot identity returned an invalid response") from exc
        if not isinstance(open_id, str) or not open_id:
            raise FeishuApiError("get bot identity returned no open_id")
        logger.info("Resolved Feishu bot identity")
        return open_id

    def _on_message(self, event: Any) -> None:
        context = None
        reserved = False
        try:
            context = normalize_message_event(
                event,
                bot_app_id=self._settings.feishu_app_id,
                bot_open_id=self._bot_open_id,
            )
            if not self._dedupe.begin(context.message_id):
                logger.info("Ignored duplicate message message_id=%s", context.message_id)
                return
            reserved = True

            reply_text = self._handler.handle(context)
            if reply_text is None:
                self._dedupe.complete(context.message_id)
                logger.info(
                    "Ignored ineligible message message_id=%s chat_id=%s sender_id=%s type=%s",
                    context.message_id,
                    context.chat_id,
                    context.sender_id,
                    context.message_type,
                )
                return

            self._reply(context.message_id, reply_text)
            after_reply = getattr(self._handler, "after_reply", None)
            if after_reply is not None:
                after_reply(context)
            self._dedupe.complete(context.message_id)
            logger.info(
                "Replied to message message_id=%s chat_id=%s sender_id=%s",
                context.message_id,
                context.chat_id,
                context.sender_id,
            )
        except MessageNormalizationError as exc:
            logger.warning("Ignored invalid message event reason=%s", exc)
        except Exception:
            if reserved and context is not None:
                self._dedupe.fail(context.message_id)
            logger.exception(
                "Failed to process Feishu message message_id=%s",
                context.message_id if context else "unknown",
            )
            # Let the SDK report a failed handler execution so Feishu can retry.
            raise

    def _reply(self, message_id: str, text: str) -> str:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.reply(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("reply to message", response))
        # The outbound message id lets us map "quote this reply -> conversation".
        outbound = getattr(getattr(response, "data", None), "message_id", None)
        if not outbound:
            raise FeishuApiError("reply to message returned no message_id")
        return outbound

    def _update(self, message_id: str, text: str) -> None:
        """Edit a bot-sent message's content in place (overwrite placeholder)."""
        request = (
            UpdateMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                UpdateMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.update(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("update message", response))

    @staticmethod
    def _api_failure(action: str, response: Any) -> str:
        code = getattr(response, "code", "unknown")
        log_id = response.get_log_id() if hasattr(response, "get_log_id") else "unknown"
        return f"{action} failed: code={code}, log_id={log_id}"
