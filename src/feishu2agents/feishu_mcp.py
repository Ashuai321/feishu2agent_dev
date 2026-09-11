"""Register Feishu group-creation tools on the shared relay MCP server.

The relay MCP (``workspace-agent-relay-mcp``, served at ``/mcp`` on port 8799)
is the same server the Workspace Agent already calls. We register two extra
tools on it so they live alongside ``record_*`` on the existing endpoint — the
agent-side enable/disable is just checking that MCP server connection in the
app. No separate port or server is needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .feishu import FeishuApiError, FeishuBot
from .requester_registry import RequesterRegistry

logger = logging.getLogger(__name__)


def register_feishu_tools(mcp: Any, bot: FeishuBot, registry: RequesterRegistry) -> None:
    """Attach ``get_requester_info`` and ``create_private_group`` to ``mcp``."""
    @mcp.tool(
        name="get_requester_info",
        title="Get Requester Info",
        description=(
            "Return the Feishu identity (open_id + source chat) of the user who @'d the "
            "bot for a given conversation_key. Read-only; call this before create_private_group."
        ),
    )
    async def get_requester_info(conversation_key: str) -> dict[str, Any]:
        info = registry.get(conversation_key)
        if info is None:
            return {
                "success": False,
                "error": {
                    "code": "requester_not_found",
                    "message": f"no requester registered for conversation_key={conversation_key}",
                },
            }
        return {"success": True, "conversation_key": conversation_key, "requester": info}

    @mcp.tool(
        name="create_private_group",
        title="Create Private Feishu Group",
        description=(
            "Create a Feishu group that contains ONLY this bot and the single user who "
            "mentioned the bot in the given conversation. Pass the conversation_key from "
            "the trigger header; the requester's Feishu identity is resolved automatically. "
            "Returns the new chat_id. If success is false, preserve and report the returned "
            "error.message; do not replace it with a generic permission error."
        ),
    )
    async def create_private_group(
        conversation_key: str, chat_name: str | None = None
    ) -> dict[str, Any]:
        info = registry.get(conversation_key)
        if info is None:
            return {
                "success": False,
                "error": {
                    "code": "requester_not_found",
                    "message": (
                        f"no requester registered for conversation_key={conversation_key}"
                    ),
                },
            }
        open_id = info.get("open_id") or ""
        if not open_id:
            return {
                "success": False,
                "error": {
                    "code": "requester_no_open_id",
                    "message": (
                        f"requester for {conversation_key} has no open_id"
                    ),
                },
            }
        try:
            chat_id = await asyncio.to_thread(
                bot.create_private_group,
                open_id,
                chat_name,
                source_chat_id=info.get("source_chat_id") or None,
            )
        except FeishuApiError as exc:
            logger.exception("create_private_group failed conversation_key=%s", conversation_key)
            message = str(exc)
            code = (
                "external_requester_not_supported"
                if "external-tenant" in message
                else "create_failed"
            )
            return {"success": False, "error": {"code": code, "message": message}}
        except Exception as exc:
            logger.exception("create_private_group failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "create_failed", "message": str(exc)}}
        registry.bind_group(conversation_key, chat_id)
        return {
            "success": True,
            "chat_id": chat_id,
            "conversation_key": conversation_key,
            "user_open_id": open_id,
        }
