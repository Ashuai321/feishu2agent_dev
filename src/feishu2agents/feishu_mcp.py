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


def register_feishu_tools(
    mcp: Any,
    bot: FeishuBot,
    registry: RequesterRegistry,
    group_draft_store: Any | None = None,
) -> None:
    """Attach Feishu group tools (private + custom group workflow) to ``mcp``."""

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

    @mcp.tool(
        name="search_contacts",
        title="Search Feishu Contacts",
        description=(
            "Search visible Feishu contacts by name/email/mobile and return candidate "
            "cards (name, open_id, email). Read-only. The agent must show these to the "
            "user and wait for explicit confirmation before inviting anyone."
        ),
    )
    async def search_contacts(conversation_key: str, query: str) -> dict[str, Any]:
        try:
            candidates = await asyncio.to_thread(bot.search_contacts, query)
        except FeishuApiError as exc:
            return {"success": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            logger.exception("search_contacts failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "search_failed", "message": str(exc)}}
        return {
            "success": True,
            "conversation_key": conversation_key,
            "query": query,
            "candidates": candidates,
        }

    @mcp.tool(
        name="get_stored_image",
        title="Get Stored Avatar Candidate",
        description=(
            "Return whether the conversation currently holds a pending avatar image "
            "(the user's latest image quote-reply). The bytes are not returned; the "
            "agent only sees presence + size and acts on it via create/update/apply."
        ),
    )
    async def get_stored_image(conversation_key: str) -> dict[str, Any]:
        if group_draft_store is None:
            return {
                "success": False,
                "error": {
                    "code": "store_unavailable",
                    "message": "group draft store is not configured",
                },
            }
        return {
            "success": True,
            "has_avatar": group_draft_store.has_avatar(conversation_key),
            "size": group_draft_store.avatar_size(conversation_key),
        }

    @mcp.tool(
        name="get_group_status",
        title="Get Created Group Status",
        description=(
            "Return the persisted state of the group created in this conversation "
            "(chat_id, name, member_open_ids, has_avatar), or 'not_created'. Used to "
            "decide whether to create or update, and to apply the latest fields."
        ),
    )
    async def get_group_status(conversation_key: str) -> dict[str, Any]:
        existing = (
            group_draft_store.get_group(conversation_key)
            if group_draft_store is not None
            else None
        )
        if existing is None:
            return {"success": True, "conversation_key": conversation_key, "created": False}
        return {
            "success": True,
            "conversation_key": conversation_key,
            "created": True,
            "chat_id": existing["chat_id"],
            "name": existing["name"],
            "member_open_ids": existing["member_open_ids"],
            "has_avatar": existing.get("has_avatar", False),
        }

    async def _avatar_image_key(conversation_key: str) -> str | None:
        """Upload the stored avatar bytes if present and return an image_key."""
        if group_draft_store is None or not group_draft_store.has_avatar(conversation_key):
            return None
        data = group_draft_store.read_avatar(conversation_key)
        if data is None:
            return None
        return await asyncio.to_thread(bot.upload_avatar_image, data)

    @mcp.tool(
        name="create_group",
        title="Create Feishu Group",
        description=(
            "Create a Feishu group whose members are the requester (auto) plus the "
            "explicitly-confirmed member_open_ids. Set set_avatar_from_stored=True only "
            "when the user sent an image and get_stored_image.confirmed it. Returns chat_id."
        ),
    )
    async def create_group(
        conversation_key: str,
        name: str,
        member_open_ids: list[str],
        set_avatar_from_stored: bool = False,
    ) -> dict[str, Any]:
        info = registry.get(conversation_key)
        if info is None or not (info.get("open_id") or ""):
            return {
                "success": False,
                "error": {
                    "code": "requester_not_found",
                    "message": "requester open_id unavailable",
                },
            }
        requester = info["open_id"]
        members: list[str] = []
        for m in [requester, *member_open_ids]:
            if m and m not in members:
                members.append(m)
        avatar_key: str | None = None
        if set_avatar_from_stored:
            avatar_key = await _avatar_image_key(conversation_key)
            if avatar_key is None and group_draft_store is not None:
                return {
                    "success": False,
                    "error": {
                        "code": "avatar_missing",
                        "message": "no stored avatar image; ask the user to send one",
                    },
                }
        try:
            chat_id = await asyncio.to_thread(
                bot.create_chat, name, members, avatar_key
            )
        except FeishuApiError as exc:
            return {"success": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            logger.exception("create_group failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "create_failed", "message": str(exc)}}
        registry.bind_group(conversation_key, chat_id)
        if group_draft_store is not None:
            group_draft_store.save_group(
                conversation_key,
                chat_id,
                name=name,
                member_open_ids=members,
            )
        return {
            "success": True,
            "chat_id": chat_id,
            "conversation_key": conversation_key,
            "member_open_ids": members,
        }

    @mcp.tool(
        name="apply_group_avatar",
        title="Apply Stored Avatar to Group",
        description=(
            "Upload the conversation's stored avatar image and set it as the avatar of "
            "an existing group. chat_id may be omitted to reuse the persisted group. "
            "Returns success + chat_id; error if no stored image."
        ),
    )
    async def apply_group_avatar(
        conversation_key: str, chat_id: str | None = None
    ) -> dict[str, Any]:
        if group_draft_store is None:
            return {
                "success": False,
                "error": {
                    "code": "store_unavailable",
                    "message": "group draft store is not configured",
                },
            }
        if not chat_id:
            existing = group_draft_store.get_group(conversation_key)
            chat_id = existing["chat_id"] if existing else None
        if not chat_id:
            return {
                "success": False,
                "error": {
                    "code": "group_missing",
                    "message": "no created group; create it first or pass chat_id",
                },
            }
        try:
            avatar_key = await _avatar_image_key(conversation_key)
            if avatar_key is None:
                return {
                    "success": False,
                    "error": {
                        "code": "avatar_missing",
                        "message": "no stored avatar image; ask the user to send one",
                    },
                }
            await asyncio.to_thread(bot.update_chat, chat_id, avatar_image_key=avatar_key)
        except FeishuApiError as exc:
            return {"success": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            logger.exception("apply_group_avatar failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "update_failed", "message": str(exc)}}
        group_draft_store.update_group(
            conversation_key,
            avatar_image_path=(
                (
                    group_draft_store.avatar_dir
                    / group_draft_store._avatar_filename(conversation_key)
                ).as_posix()
            ),
        )
        return {"success": True, "chat_id": chat_id}

    @mcp.tool(
        name="update_group",
        title="Update Feishu Group",
        description=(
            "Update an existing group's name and/or avatar. chat_id may be omitted to reuse "
            "the persisted group. Pass name and/or set_avatar_from_stored=True per the latest "
            "user quote-reply. Updates the SAME group; never creates a new one."
        ),
    )
    async def update_group(
        conversation_key: str,
        chat_id: str | None = None,
        name: str | None = None,
        set_avatar_from_stored: bool = False,
    ) -> dict[str, Any]:
        if group_draft_store is None:
            return {
                "success": False,
                "error": {
                    "code": "store_unavailable",
                    "message": "group draft store is not configured",
                },
            }
        existing = group_draft_store.get_group(conversation_key)
        if not chat_id:
            chat_id = existing["chat_id"] if existing else None
        if not chat_id:
            return {
                "success": False,
                "error": {
                    "code": "group_missing",
                    "message": "no created group found; create it first or pass chat_id",
                },
            }
        avatar_applied = False
        try:
            if name:
                await asyncio.to_thread(bot.update_chat, chat_id, name=name)
            if set_avatar_from_stored:
                avatar_key = await _avatar_image_key(conversation_key)
                if avatar_key is None:
                    return {
                        "success": False,
                        "error": {
                            "code": "avatar_missing",
                            "message": "set_avatar_from_stored requested, but no stored avatar",
                        },
                    }
                await asyncio.to_thread(bot.update_chat, chat_id, avatar_image_key=avatar_key)
                avatar_applied = True
        except FeishuApiError as exc:
            return {"success": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            logger.exception("update_group failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "update_failed", "message": str(exc)}}
        group_draft_store.update_group(
            conversation_key,
            name=name if name is not None else (existing["name"] if existing is not None else None),
            avatar_image_path=(
                (
                    group_draft_store.avatar_dir
                    / group_draft_store._avatar_filename(conversation_key)
                ).as_posix()
                if avatar_applied
                else None
            ),
        )
        return {"success": True, "chat_id": chat_id, "avatar_pending_overwrite": avatar_applied}
