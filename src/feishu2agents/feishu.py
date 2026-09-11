"""Feishu SDK adapter: identity lookup, event dispatch, and replies."""

from __future__ import annotations

import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateChatRequest,
    CreateChatRequestBody,
    GetChatMembersRequest,
    GetChatRequest,
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

    def create_private_group(
        self,
        user_open_id: str,
        name: str | None = None,
        *,
        source_chat_id: str | None = None,
    ) -> str:
        """Create a Feishu group containing ONLY this bot + the given user.

        The bot is automatically added as a member/owner when it creates the
        chat, so passing ``user_id_list=[user_open_id]`` yields a 2-member
        private group (just that user and the bot). Returns the new chat_id.

        Feishu does not allow an app-identity request to create this kind of
        two-member group for an external-tenant user.  When the originating
        chat id is available, check that condition before making the create
        request so the MCP tool can return an actionable error instead of the
        opaque ``232043`` response.
        """
        if source_chat_id:
            reason = self._external_requester_reason(source_chat_id, user_open_id)
            if reason:
                raise FeishuApiError(reason)
        request = (
            CreateChatRequest.builder()
            .user_id_type("open_id")
            .request_body(
                CreateChatRequestBody.builder()
                .name(name or "机器人与TA的私聊")
                .chat_mode("group")
                .user_id_list([user_open_id])
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.chat.create(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("create private group", response))
        chat_id = getattr(getattr(response, "data", None), "chat_id", None)
        if not chat_id:
            raise FeishuApiError("create private group returned no chat_id")
        logger.info(
            "Created private Feishu group chat_id=%s user_open_id=%s",
            chat_id,
            user_open_id,
        )
        return chat_id

    def _external_requester_reason(
        self, source_chat_id: str, user_open_id: str
    ) -> str | None:
        """Return a clear limitation message for an external requester.

        A message can arrive from an external group even when the target user
        is an internal member, so compare the member's tenant key with the
        source chat's tenant key.  If the metadata lookup is unavailable we
        fall back to the normal create call, preserving the existing behavior.
        """
        try:
            chat_request = (
                GetChatRequest.builder()
                .chat_id(source_chat_id)
                .user_id_type("open_id")
                .build()
            )
            chat_response = self._api_client.im.v1.chat.get(chat_request)
            if not chat_response.success():
                logger.warning(
                    "Could not inspect source chat before private-group creation code=%s",
                    getattr(chat_response, "code", "unknown"),
                )
                return None
            chat_payload = json.loads(chat_response.raw.content)
            chat_data = chat_payload.get("data") or {}
            if not chat_data.get("external"):
                return None

            members_request = (
                GetChatMembersRequest.builder()
                .chat_id(source_chat_id)
                .member_id_type("open_id")
                .page_size(100)
                .build()
            )
            members_response = self._api_client.im.v1.chat.list(members_request)
            if not members_response.success():
                logger.warning(
                    "Could not inspect source chat members before private-group creation code=%s",
                    getattr(members_response, "code", "unknown"),
                )
                return None
            members_payload = json.loads(members_response.raw.content)
            members = (members_payload.get("data") or {}).get("items") or []
            requester = next(
                (item for item in members if item.get("member_id") == user_open_id),
                None,
            )
            source_tenant = chat_data.get("tenant_key")
            requester_tenant = (requester or {}).get("tenant_key")
            if source_tenant and requester_tenant and source_tenant != requester_tenant:
                return (
                    "cannot create a two-member private group for an external-tenant "
                    "requester: Feishu requires an internal owner for external groups, "
                    "so the result would include another internal member. Use an internal "
                    "requester or switch to a direct-message/external-group workflow."
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.exception("Source-chat inspection failed before private-group creation")
        return None

    @staticmethod
    def _api_failure(action: str, response: Any) -> str:
        code = getattr(response, "code", "unknown")
        log_id = response.get_log_id() if hasattr(response, "get_log_id") else "unknown"
        if code in (232024, 232043):
            detail = "bot is not visible to the target user or the user id is unavailable"
        elif code == 232032:
            detail = "the external-group owner must be in the same tenant as the operator"
        else:
            detail = "Feishu rejected the request"
        return f"{action} failed: code={code}, {detail}, log_id={log_id}"
