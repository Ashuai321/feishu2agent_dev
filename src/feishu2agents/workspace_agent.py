"""Submit mentioned group text to a published ChatGPT Workspace Agent.

Every processed Feishu message is routed straight to the Workspace Agent using
the built-in relay protocol (``feishu2agents.relay``): a trigger injects
a ``request_id`` + ``conversation_key`` header, the agent finishes by calling
the relay's MCP ``record_result`` tool, a run is created in the reference
relay store, and :class:`~feishu2agents.relay_worker.RelayWorker` posts the
final answer back to Feishu.

The old two-step routing (DeepSeek intent classification → OpenAI direct answer)
has been removed: it now lives only as reusable :mod:`feishu2agents.utils`
utilities and is no longer used to branch message handling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from feishu2agents.relay.api.validation import (
    resolve_agent_token,
    validate_trigger_url,
)
from feishu2agents.relay.store.relay_store import RelayStore
from feishu2agents.relay.trigger import (
    TriggerClient,
    build_trigger_input,
    generate_request_id,
)

from .bot_handler import should_process
from .feishu_bridge import FeishuBridge
from .message_context import MessageContext
from .trigger_dispatch import FeishuTriggerDispatcher

logger = logging.getLogger(__name__)


# trim a conversation placeholder name from the user's first message
def _conversation_title(text: str) -> str:
    line = " ".join(text.split())
    return line[:32] if line else "飞书消息"


@dataclass(frozen=True)
class WorkspaceAgentSettings:
    """Relay-side settings required to trigger the Workspace Agent.

    The actual trigger URL and access token are resolved per-agent from the
    built-in relay store/config (``resolve_agent_token``), matching the
    dashboard's ``/api/conversations/.../runs`` flow. This structure keeps
    construction explicit without re-validating CHATGPT_AGENT_TOKEN.
    """

    store: RelayStore
    bridge: FeishuBridge
    dispatcher: FeishuTriggerDispatcher
    relay_config: object
    trigger_client: TriggerClient = TriggerClient()
    group_draft_store: Any | None = None


class WorkspaceAgentMessageHandler:
    def __init__(
        self,
        settings: WorkspaceAgentSettings,
        *,
        store: RelayStore | None = None,
        bridge: FeishuBridge | None = None,
    ) -> None:
        self._settings = settings
        self._store = store or settings.store
        self._bridge = bridge or settings.bridge
        self._group_draft_store = settings.group_draft_store
        # Optional callable(user_message_id, text) -> outbound_message_id, used to
        # post the "processing" placeholder that the final answer overrides in place.
        self.post_placeholder: Callable[[str, str], str] | None = None
        # Optional callable(message_id, image_key) -> bytes: used to persist an
        # image quote-reply as the conversation's pending group avatar.
        self.download_image: Callable[[str, str], bytes] | None = None
        # Optional callable(context, conversation_key): fired once a message is
        # claimed for dispatch, so the caller can record who @'d the bot.
        self.on_dispatch: Callable[[MessageContext, str], None] | None = None

    def _store_avatar_images(self, context: MessageContext, conversation_key: str) -> bool:
        """Download any image in the message and keep the latest bytes per conversation."""
        if self._group_draft_store is None or self.download_image is None:
            return False
        stored = False
        for image_key in context.image_keys:
            try:
                data = self.download_image(context.message_id, image_key)
            except Exception:
                logger.exception(
                    "Failed to download image image_key=%s message_id=%s",
                    image_key,
                    context.message_id,
                )
                continue
            try:
                self._group_draft_store.save_avatar(conversation_key, data)
                stored = True
            except Exception:
                logger.exception(
                    "Failed to persist avatar conversation_key=%s", conversation_key
                )
        return stored

    def _post_processing_placeholder(self, context: MessageContext, request_id: str) -> None:
        if self.post_placeholder is None:
            return
        try:
            outbound = self.post_placeholder(
                context.message_id, "正在处理，Agent 完成后会回复到这条消息。"
            )
        except Exception:
            logger.exception(
                "Failed to post processing placeholder request_id=%s message_id=%s",
                request_id,
                context.message_id,
            )
            return
        if outbound:
            try:
                self._bridge.record_outbound_message(request_id, outbound)
            except ValueError:
                logger.warning("Could not record placeholder outbound message")

    def _conversation_key(self, context: MessageContext) -> str:
        # Quoting a former bot reply continues that conversation; anything else
        # (a fresh @ without a quote) starts a brand-new conversation.
        if context.reply_to_message_id:
            continued = self._bridge.resolve_reply_conversation(
                context.reply_to_message_id
            )
            if continued:
                return continued
        return f"feishu:{context.bot_app_id}:{context.chat_id}:{uuid4().hex[:12]}"

    def _find_agent(self, conversation: dict) -> dict:
        for agent in self._store.list_agents():
            if int(agent["id"]) == int(conversation["agent_id"]):
                return agent
        raise KeyError("conversation agent was not found")

    @staticmethod
    def _agent_conversation_key(base_key: str, agent_id: int, *, fanout: bool) -> str:
        """Keep the old key unchanged for one Agent; isolate fan-out histories."""
        if not fanout:
            return base_key
        # A quoted reply may already contain the suffix from a prior fan-out.
        base = base_key.split("|agent:", 1)[0]
        return f"{base}|agent:{agent_id}"

    def handle(self, context: MessageContext) -> str | None:
        if not should_process(context):
            return None
        base_conversation_key = self._conversation_key(context)
        first_request_id = generate_request_id("feishu")

        # Exactly-once gate per Feishu message: only the first handler call owns
        # the message. Redeliveries (WS reconnect, duplicate instances) skip.
        if not self._bridge.claim(
            feishu_message_id=context.message_id,
            request_id=first_request_id,
        ):
            logger.info(
                "skip already-claimed feishu message message_id=%s",
                context.message_id,
            )
            return None
        # Persist any image quote-reply as the conversation's pending avatar and
        # tell the agent an image is available via a marker line.
        user_text = context.text
        if context.message_type == "image":
            marker = "[用户发送了一张图片，已保存为群头像候选]"
            user_text = (user_text + "\n" + marker).strip() if user_text else marker

        try:
            selected_ids = self._store.resolve_enabled_agent_ids()
        except (KeyError, ValueError) as exc:
            logger.error("Agent selection invalid: %s", exc)
            return "Agent 触发配置无效，请先配置可用的 Agent。"
        if not selected_ids:
            return "Agent 触发配置无效，请先配置可用的 Agent。"
        fanout = len(selected_ids) > 1
        workspace_id = self._store.resolve_default_workspace_id()
        idempotency_base = f"{context.bot_app_id}:{context.message_id}"

        # Validate every selected target before creating any run. This prevents
        # a partial fan-out when one selected Agent has a bad URL/token.
        targets: list[tuple[dict[str, Any], str]] = []
        for agent_id in selected_ids:
            try:
                agent = self._store.get_agent(int(agent_id))
                trigger_url = str(agent["trigger_url"])
                validate_trigger_url(trigger_url)
                access_token = resolve_agent_token(
                    self._settings.relay_config, self._store, str(agent["token_ref"])
                )
            except (KeyError, ValueError) as exc:
                logger.error("Agent trigger config invalid: %s", exc)
                return "Agent 触发配置无效，请检查后重试。"
            targets.append((agent, access_token))

        dispatched_request_ids: list[str] = []
        for index, (agent, access_token) in enumerate(targets):
            request_id = first_request_id if index == 0 else generate_request_id("feishu")
            conversation_key = self._agent_conversation_key(
                base_conversation_key, int(agent["id"]), fanout=fanout
            )
            if self.on_dispatch is not None:
                try:
                    self.on_dispatch(context, conversation_key)
                except Exception:
                    logger.exception(
                        "requester registration failed message_id=%s", context.message_id
                    )
            if context.message_type == "image":
                self._store_avatar_images(context, conversation_key)
            try:
                conversation = self._store.get_conversation_by_key(conversation_key)
            except KeyError:
                conversation = self._store.create_conversation(
                    agent_id=int(agent["id"]),
                    workspace_id=workspace_id,
                    name=_conversation_title(user_text),
                    conversation_key=conversation_key,
                )
            is_continuation = bool(
                self._store.list_runs_for_conversation(int(conversation["id"]))
            )
            idempotency_key = (
                idempotency_base if not fanout else f"{idempotency_base}:{int(agent['id'])}"
            )
            run = self._store.create_run(
                agent_id=int(agent["id"]),
                conversation_id=int(conversation["id"]),
                conversation_key=conversation_key,
                input_markdown=user_text,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            if index > 0:
                self._bridge.register_run(
                    feishu_message_id=context.message_id,
                    request_id=request_id,
                )
            trigger_input = build_trigger_input(
                request_id=request_id,
                conversation_key=conversation_key,
                user_input=user_text,
                is_continuation=is_continuation,
                working_directory=run.get("working_directory_snapshot"),
                local_context=run.get("local_context"),
            )
            self._store.mark_run_trigger_sent(request_id)
            self._settings.dispatcher.dispatch(
                store=self._store,
                trigger_client=self._settings.trigger_client,
                trigger_url=str(agent["trigger_url"]),
                access_token=access_token,
                conversation_key=conversation_key,
                input_text=trigger_input,
                idempotency_key=idempotency_key,
                request_id=request_id,
            )
            dispatched_request_ids.append(request_id)
            logger.info(
                "Workspace Agent trigger scheduled message_id=%s request_id=%s agent_id=%s",
                context.message_id,
                request_id,
                agent["id"],
            )
        # One processing placeholder is enough for a fan-out message; the
        # worker replies with each selected Agent's terminal result.
        self._post_processing_placeholder(context, dispatched_request_ids[0])
        return None
