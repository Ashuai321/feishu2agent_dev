"""Feishu SDK adapter: identity lookup, event dispatch, and replies."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import lark_oapi as lark
import requests
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
from requests_toolbelt import MultipartEncoder
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .bot_handler import MessageHandler
from .config import Settings
from .dedupe import DedupeCache
from .message_context import MessageNormalizationError, normalize_message_event

logger = logging.getLogger(__name__)


class FeishuApiError(RuntimeError):
    """A sanitized failure returned by a Feishu API."""


def _image_upload_metadata(data: bytes) -> tuple[str, str]:
    """Return a filename and MIME type matching the actual image bytes."""
    signatures: tuple[tuple[bytes, str, str], ...] = (
        (b"\x89PNG\r\n\x1a\n", "avatar.png", "image/png"),
        (b"GIF87a", "avatar.gif", "image/gif"),
        (b"GIF89a", "avatar.gif", "image/gif"),
        (b"BM", "avatar.bmp", "image/bmp"),
    )
    for signature, filename, content_type in signatures:
        if data.startswith(signature):
            return filename, content_type
    if data.startswith(b"\xff\xd8\xff"):
        return "avatar.jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "avatar.webp", "image/webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "avatar.tiff", "image/tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "avatar.ico", "image/x-icon"
    raise FeishuApiError(
        "upload avatar image failed: image format is unsupported or cannot be detected"
    )


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
        # Pending OAuth state nonces: state -> {"target": str, "expiry": float}
        self._oauth_states: dict[str, dict[str, Any]] = {}

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

    def ensure_identity(self) -> str:
        """Resolve and cache the bot open id (needed by webhook normalization)."""
        if not self._bot_open_id:
            self._bot_open_id = self._fetch_bot_open_id()
        return self._bot_open_id

    def webhook_routes(self) -> list[Route]:
        """Return Starlette routes implementing Feishu developer-server mode.

        Handles the URL verification ``challenge`` sent during event-subscription setup
        and forwards ``im.message.receive_v1`` events to the same pipeline as the long
        connection (normalize -> dedupe -> handler.handle -> reply / placeholder).

        ``/feishu/events`` is the public route used by the Cloudflare Worker. The
        singular ``/feishu/event`` alias remains available for existing deployments.
        """

        async def handler(request: Request) -> JSONResponse:
            try:
                body = await self._read_json(request)
            except Exception:
                logger.exception("Feishu webhook: failed to read request body")
                return JSONResponse({"code": 0})

            if body is None:
                return JSONResponse({"code": 0})

            verify_token = self._settings.feishu_verify_token
            if verify_token:
                header = body.get("header") if isinstance(body, dict) else None
                token = header.get("token") if isinstance(header, dict) else None
                if token != verify_token:
                    logger.warning("Feishu webhook: verify token mismatch")
                    return JSONResponse({"code": 1}, status_code=403)

            if isinstance(body, dict) and body.get("challenge"):
                # URL verification: echo the challenge back verbatim.
                return JSONResponse({"challenge": body["challenge"]})

            event_type = ""
            if isinstance(body, dict):
                header = body.get("header") if isinstance(body, dict) else None
                if isinstance(header, dict):
                    event_type = header.get("event_type") or ""

            if event_type == "im.message.receive_v1":
                try:
                    self.ensure_identity()
                    # _on_message already accepts a dict event (header/event), so pass
                    # the whole pushed body through and reuse the full existing pipeline.
                    await asyncio.to_thread(self._on_message, body)
                except Exception:
                    # Swallow and still ack so Feishu does not retry forever; the same
                    # message is also protected server-side by dedupe on message_id.
                    logger.exception("Feishu webhook: failed to process message event")

            return JSONResponse({"code": 0})

        return [
            Route("/feishu/events", endpoint=handler, methods=["POST"]),
            Route("/feishu/event", endpoint=handler, methods=["POST"]),
        ]

    def webhook_route(self) -> Route:
        """Backward-compatible accessor for the singular webhook route."""
        return self.webhook_routes()[1]

    def oauth_routes(self) -> list[Route]:
        """Routes for user-identity OAuth used to act on Bitable with the
        authorizing user's own permissions (not the bot's).

        - ``GET /feishu/oauth/authorize`` builds a Feishu authorization link
          (with a fresh ``state`` nonce) and redirects the user to it.
        - ``GET /feishu/oauth/callback`` exchanges the returned ``code`` for a
          ``user_access_token`` and renders the token (plus refresh token) so
          it can be wired into Bitable calls.
        """
        return [
            Route("/feishu/oauth/authorize", endpoint=self._oauth_authorize, methods=["GET"]),
            Route("/feishu/oauth/callback", endpoint=self._oauth_callback, methods=["GET"]),
        ]

    def _oauth_authorize(self, request: Request) -> RedirectResponse:
        settings = self._settings
        callback_uri = (request.query_params.get("callback_uri") or "").strip()
        default_redirect = settings.feishu_oauth_redirect_uri.strip()
        if not callback_uri:
            callback_uri = default_redirect
        if not callback_uri:
            msg = (
                "Missing OAuth redirect target. Pass ?callback_uri=... or set "
                "FEISHU_OAUTH_REDIRECT_URI in the environment."
            )
            return HTMLResponse(msg, status_code=400)

        state = secrets.token_urlsafe(32)
        expires_at = time.time() + settings.feishu_oauth_state_ttl_seconds
        self._oauth_states[state] = {"target": callback_uri, "expiry": expires_at}

        authorize_query = urlencode(
            {
                "app_id": settings.feishu_app_id,
                "redirect_uri": callback_uri,
                "scope": settings.feishu_oauth_scope,
                "state": state,
            }
        )
        authorize_url = (
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
            + authorize_query
        )
        logger.info("OAuth authorize: state=%s target=%s", state, callback_uri)
        return RedirectResponse(authorize_url, status_code=302)

    async def _oauth_callback(self, request: Request) -> JSONResponse:
        params = request.query_params
        code = (params.get("code") or "").strip()
        state = (params.get("state") or "").strip()
        if not code:
            return JSONResponse(
                {"success": False, "error": "missing 'code' parameter"}, status_code=400
            )

        redirect_uri = None
        if state:
            record = self._oauth_states.pop(state, None)
            if record is None:
                return JSONResponse(
                    {"success": False, "error": "unknown or already-consumed 'state'"},
                    status_code=400,
                )
            if record.get("expiry", 0) < time.time():
                return JSONResponse(
                    {"success": False, "error": "expired 'state'"},
                    status_code=400,
                )
            redirect_uri = str(record.get("target") or "").strip() or None
        else:
            logger.warning("OAuth callback received no state nonce")

        token = await asyncio.to_thread(
            self.exchange_user_access_token,
            code,
            state=state or None,
            redirect_uri=redirect_uri,
        )
        if not token:
            return JSONResponse(
                {"success": False, "error": "failed to exchange authorization code"},
                status_code=502,
            )
        logger.info(
            "OAuth callback: issued user_access_token open_id=%s",
            token.get("open_id"),
        )
        return JSONResponse({"success": True, "token": token})

    def exchange_user_access_token(
        self,
        code: str,
        *,
        state: str | None = None,
        redirect_uri: str | None = None,
    ) -> dict[str, Any] | None:
        """Exchange a user-authorization ``code`` for ``user_access_token``.

        Returns the raw token payload (``access_token``, ``refresh_token``,
        ``expires_in``, ``scope``, ``open_id`` ...) or ``None`` on failure.
        """
        settings = self._settings
        payload: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.feishu_app_id,
            "client_secret": settings.feishu_app_secret,
        }
        payload["redirect_uri"] = (
            redirect_uri or settings.feishu_oauth_redirect_uri
        )
        try:
            response = requests.post(
                "https://accounts.feishu.cn/oauth/v3/token",
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=20,
            )
        except requests.RequestException:
            logger.exception("OAuth token exchange network failure")
            return None
        if response.status_code != 200:
            logger.error(
                "OAuth token exchange failed status=%s body=%s",
                response.status_code,
                response.text[:1000],
            )
            return None
        try:
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            # authen/v3/oauth/token returns token fields at the top level;
            # accept the older nested form as a compatibility fallback.
            if not isinstance(data, dict):
                data = body if isinstance(body, dict) else {}
        except (ValueError, AttributeError):
            logger.exception("OAuth token exchange returned invalid JSON")
            return None
        if not data.get("access_token"):
            logger.error("OAuth token exchange missing access_token body=%s", body)
            return None
        return data

    @staticmethod
    async def _read_json(request: Request) -> dict[str, Any] | None:
        raw = await request.body()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

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

    def download_message_image(self, message_id: str, file_key: str) -> bytes:
        """Download a message's image resource bytes via a tenant token."""
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/im/v1/messages/{message_id}/resources/{file_key}")
            .paths({"message_id": message_id, "file_key": file_key})
            .queries([("type", "image")])
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        response = self._api_client.request(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("download message image", response))
        return response.raw.content

    def upload_avatar_image(self, data: bytes) -> str:
        """Upload an avatar image and return its image_key. ≤10MB enforced."""
        if not data:
            raise FeishuApiError("upload avatar image failed: image is empty")
        if len(data) > 10 * 1024 * 1024:
            raise FeishuApiError("upload avatar image failed: image exceeds the 10MB limit")
        filename, content_type = _image_upload_metadata(data)
        fields = {
            "image_type": "avatar",
            "image": (filename, data, content_type),
        }
        encoder = MultipartEncoder(fields=fields)
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.POST)
            .uri("/open-apis/im/v1/images")
            .headers({"Content-Type": encoder.content_type})
            .token_types({lark.AccessTokenType.TENANT})
            .body(encoder)
            .build()
        )
        response = self._api_client.request(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("upload avatar image", response))
        try:
            payload = json.loads(response.raw.content)
            image_key = payload["data"]["image_key"]
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FeishuApiError("upload avatar image returned an invalid response") from exc
        if not isinstance(image_key, str) or not image_key:
            raise FeishuApiError("upload avatar image returned no image_key")
        return image_key

    def create_chat(
        self,
        name: str,
        user_id_list: list[str],
        avatar_image_key: str | None = None,
    ) -> str:
        """Create a general Feishu group and return its chat_id."""
        body_builder = (
            CreateChatRequestBody.builder()
            .name(name)
            .chat_mode("group")
            .user_id_list(list(user_id_list))
        )
        if avatar_image_key:
            body_builder.avatar(avatar_image_key)
        request = (
            CreateChatRequest.builder()
            .user_id_type("open_id")
            .request_body(body_builder.build())
            .build()
        )
        response = self._api_client.im.v1.chat.create(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("create chat", response))
        chat_id = getattr(getattr(response, "data", None), "chat_id", None)
        if not chat_id:
            raise FeishuApiError("create chat returned no chat_id")
        logger.info("Created Feishu group chat_id=%s", chat_id)
        return chat_id

    def update_chat(
        self,
        chat_id: str,
        *,
        name: str | None = None,
        avatar_image_key: str | None = None,
        description: str | None = None,
        i18n_names: dict[str, str] | None = None,
        add_member_permission: str | None = None,
        share_card_permission: str | None = None,
        at_all_permission: str | None = None,
        edit_permission: str | None = None,
        owner_id: str | None = None,
        join_message_visibility: str | None = None,
        leave_message_visibility: str | None = None,
        membership_approval: str | None = None,
        chat_type: str | None = None,
        group_message_type: str | None = None,
        urgent_setting: str | None = None,
        video_conference_setting: str | None = None,
        pin_manage_setting: str | None = None,
        hide_member_count_setting: str | None = None,
    ) -> None:
        """Update a group's name and/or avatar in place."""
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if avatar_image_key:
            body["avatar"] = avatar_image_key
        for key, value in (
            ("description", description),
            ("i18n_names", i18n_names),
            ("add_member_permission", add_member_permission),
            ("share_card_permission", share_card_permission),
            ("at_all_permission", at_all_permission),
            ("edit_permission", edit_permission),
            ("owner_id", owner_id),
            ("join_message_visibility", join_message_visibility),
            ("leave_message_visibility", leave_message_visibility),
            ("membership_approval", membership_approval),
            ("chat_type", chat_type),
            ("group_message_type", group_message_type),
            ("urgent_setting", urgent_setting),
            ("video_conference_setting", video_conference_setting),
            ("pin_manage_setting", pin_manage_setting),
            ("hide_member_count_setting", hide_member_count_setting),
        ):
            if value is not None:
                body[key] = value
        if not body:
            return
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.PUT)
            .uri("/open-apis/im/v1/chats/{chat_id}")
            .paths({"chat_id": chat_id})
            .token_types({lark.AccessTokenType.TENANT})
            .body(body)
            .build()
        )
        response = self._api_client.request(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("update chat", response))

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Return the current group information using the bot identity."""
        request = (
            GetChatRequest.builder()
            .chat_id(chat_id)
            .user_id_type("open_id")
            .build()
        )
        response = self._api_client.im.v1.chat.get(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("get chat", response))
        try:
            payload = json.loads(response.raw.content)
            data = payload.get("data") or {}
        except (AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise FeishuApiError("get chat returned an invalid response") from exc
        return data if isinstance(data, dict) else {}

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        """Resolve Feishu contacts from a mobile number or email address.

        The tenant-supported ``batch_get_id`` endpoint is used because the
        legacy name-search endpoint requires a user access token.  The group
        workflow therefore asks for a mobile number or email when it needs to
        identify a member.
        """
        text = str(query or "").strip()
        emails = sorted(
            set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text))
        )
        mobiles = sorted(
            set(re.findall(r"(?<!\d)(?:\+?86[\s-]*)?(1\d{10})(?!\d)", text))
        )
        if not emails and not mobiles:
            raise FeishuApiError(
                "Feishu contact lookup requires a mobile number or email; "
                "name-only search is not available with the tenant token"
            )
        request = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.POST)
            .uri("/open-apis/contact/v3/users/batch_get_id")
            .queries([("user_id_type", "open_id")])
            .token_types({lark.AccessTokenType.TENANT})
            .body({"emails": emails, "mobiles": mobiles})
            .build()
        )
        response = self._api_client.request(request)
        if not response.success():
            raise FeishuApiError(self._api_failure("search contacts", response))
        try:
            payload = json.loads(response.raw.content)
            users = (payload.get("data") or {}).get("user_list") or []
        except (AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise FeishuApiError("search contacts returned an invalid response") from exc
        candidates: list[dict[str, Any]] = []
        for user in users or []:
            open_id = user.get("user_id") or user.get("open_id") or ""
            if not open_id:
                continue
            entry: dict[str, Any] = {
                "name": user.get("name") or "",
                "open_id": open_id,
            }
            for field in ("email", "enterprise_email", "mobile", "department_ids", "title"):
                if user.get(field) is not None:
                    entry[field] = user.get(field)
            candidates.append(entry)
        return candidates

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
