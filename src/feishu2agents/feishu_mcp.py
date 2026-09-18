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


_GROUP_UPDATE_ENUMS: dict[str, set[str]] = {
    "add_member_permission": {"all_members", "only_owner"},
    "share_card_permission": {"allowed", "not_allowed"},
    "at_all_permission": {"all_members", "only_owner"},
    "edit_permission": {"all_members", "only_owner"},
    "join_message_visibility": {"all_members", "only_owner", "not_anyone"},
    "leave_message_visibility": {"all_members", "only_owner", "not_anyone"},
    "membership_approval": {"no_approval_required", "approval_required"},
    "chat_type": {"private", "public"},
    "group_message_type": {"chat", "thread"},
    "urgent_setting": {"all_members", "only_owner"},
    "video_conference_setting": {"all_members", "only_owner"},
    "pin_manage_setting": {"all_members", "only_owner"},
    "hide_member_count_setting": {"all_members", "only_owner"},
}


def _validate_group_updates(changes: dict[str, Any]) -> str | None:
    """Validate the documented Feishu update-chat fields before sending them."""
    supported = {
        "name",
        "avatar_image_key",
        "description",
        "i18n_names",
        "add_member_permission",
        "share_card_permission",
        "at_all_permission",
        "edit_permission",
        "owner_id",
        "join_message_visibility",
        "leave_message_visibility",
        "membership_approval",
        "chat_type",
        "group_message_type",
        "urgent_setting",
        "video_conference_setting",
        "pin_manage_setting",
        "hide_member_count_setting",
    }
    unknown = sorted(set(changes) - supported)
    if unknown:
        return f"unsupported group update field(s): {', '.join(unknown)}"
    name = changes.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return "name must be a non-empty string"
    if isinstance(name, str) and len(name) > 60:
        return "name must be 60 characters or fewer"
    description = changes.get("description")
    if description is not None and not isinstance(description, str):
        return "description must be a string"
    if isinstance(description, str) and len(description) > 100:
        return "description must be 100 characters or fewer"
    i18n_names = changes.get("i18n_names")
    if i18n_names is not None and (
        not isinstance(i18n_names, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in i18n_names.items()
        )
    ):
        return "i18n_names must be an object of language codes to names"
    for field, allowed in _GROUP_UPDATE_ENUMS.items():
        value = changes.get(field)
        if value is not None and value not in allowed:
            return f"{field} must be one of: {', '.join(sorted(allowed))}"
    add_permission = changes.get("add_member_permission")
    share_permission = changes.get("share_card_permission")
    if add_permission == "only_owner" and share_permission == "allowed":
        return "share_card_permission must be not_allowed when add_member_permission is only_owner"
    if add_permission == "all_members" and share_permission == "not_allowed":
        return "share_card_permission must be allowed when add_member_permission is all_members"
    return None


def register_feishu_tools(
    mcp: Any,
    bot: FeishuBot | dict[str, FeishuBot],
    registry: RequesterRegistry,
    group_draft_store: Any | None = None,
) -> None:
    """Attach Feishu group tools (private + custom group workflow) to ``mcp``."""

    bots = bot if isinstance(bot, dict) else {getattr(bot, "_platform", "feishu"): bot}

    def bot_for(conversation_key: str, info: dict[str, Any] | None = None) -> FeishuBot:
        # The platform prefix is written at event dispatch time and is the
        # authoritative routing key.  The registry value is only a legacy
        # fallback for conversation keys created before platform routing.
        key_platform = str(conversation_key.split(":", 1)[0] or "").strip().lower()
        platform = key_platform if key_platform in bots else ""
        if not platform:
            platform = str((info or {}).get("platform") or "").strip().lower()
        return bots.get(platform) or bots.get("feishu") or next(iter(bots.values()))

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
            selected_bot = bot_for(conversation_key, info)
            chat_id = await asyncio.to_thread(
                selected_bot.create_private_group,
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
            "Resolve a visible Feishu contact by mobile number or email and return candidate "
            "cards (name, open_id, email). Name-only lookup requires a user token and is "
            "unavailable here. Read-only; the agent must show candidates to the user and "
            "wait for explicit confirmation before inviting anyone."
        ),
    )
    async def search_contacts(conversation_key: str, query: str) -> dict[str, Any]:
        try:
            candidates = await asyncio.to_thread(
                bot_for(conversation_key).search_contacts, query
            )
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
        return await asyncio.to_thread(
            bot_for(conversation_key).upload_avatar_image, data
        )

    @mcp.tool(
        name="create_group",
        title="Create Feishu Group",
        description=(
            "Create a Feishu group whose members are exactly the explicitly-confirmed "
            "member_open_ids. The requester who mentioned the bot is not added "
            "automatically. Set set_avatar_from_stored=True only when the user sent an "
            "image and get_stored_image.confirmed it. Returns chat_id."
        ),
    )
    async def create_group(
        conversation_key: str,
        name: str,
        member_open_ids: list[str],
        set_avatar_from_stored: bool = False,
    ) -> dict[str, Any]:
        info = registry.get(conversation_key)
        if info is None:
            return {
                "success": False,
                "error": {
                    "code": "requester_not_found",
                    "message": "no requester registered for this conversation",
                },
            }
        members: list[str] = []
        for m in member_open_ids:
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
            selected_bot = bot_for(conversation_key, info)
            chat_id = await asyncio.to_thread(
                selected_bot.create_chat, name, members, avatar_key
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
            await asyncio.to_thread(
                bot_for(conversation_key).update_chat, chat_id, avatar_image_key=avatar_key
            )
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
        name="get_group_info",
        title="Get Feishu Group Info",
        description=(
            "Read the current settings of an existing Feishu group. chat_id may be omitted "
            "to reuse the persisted group. Use this before changing permissions or other "
            "settings; it never creates or changes a group."
        ),
    )
    async def get_group_info(
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
        try:
            data = await asyncio.to_thread(bot_for(conversation_key).get_chat, chat_id)
        except FeishuApiError as exc:
            return {"success": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            logger.exception("get_group_info failed conversation_key=%s", conversation_key)
            return {"success": False, "error": {"code": "read_failed", "message": str(exc)}}
        return {"success": True, "chat_id": chat_id, "settings": data}

    @mcp.tool(
        name="update_group",
        title="Update Feishu Group",
        description=(
            "Update the SAME existing group; never create a new one. chat_id may be omitted "
            "to reuse the persisted group. Supported fields are name (<=60 chars), description "
            "(<=100), avatar_image_key, i18n_names, add_member_permission, "
            "share_card_permission, at_all_permission, edit_permission, owner_id, "
            "join_message_visibility, leave_message_visibility, membership_approval, "
            "chat_type, group_message_type, urgent_setting, video_conference_setting, "
            "pin_manage_setting, and hide_member_count_setting. Values are validated against "
            "Feishu's documented enum values; add_member_permission and share_card_permission "
            "must be consistent. set_avatar_from_stored=True uses the latest quoted image."
        ),
    )
    async def update_group(
        conversation_key: str,
        chat_id: str | None = None,
        name: str | None = None,
        avatar_image_key: str | None = None,
        set_avatar_from_stored: bool = False,
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
        changes: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "avatar_image_key": avatar_image_key,
                "description": description,
                "i18n_names": i18n_names,
                "add_member_permission": add_member_permission,
                "share_card_permission": share_card_permission,
                "at_all_permission": at_all_permission,
                "edit_permission": edit_permission,
                "owner_id": owner_id,
                "join_message_visibility": join_message_visibility,
                "leave_message_visibility": leave_message_visibility,
                "membership_approval": membership_approval,
                "chat_type": chat_type,
                "group_message_type": group_message_type,
                "urgent_setting": urgent_setting,
                "video_conference_setting": video_conference_setting,
                "pin_manage_setting": pin_manage_setting,
                "hide_member_count_setting": hide_member_count_setting,
            }.items()
            if value is not None
        }
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
            changes["avatar_image_key"] = avatar_key
        if not changes:
            return {
                "success": False,
                "error": {
                    "code": "no_updates",
                    "message": "provide at least one group field to update",
                },
            }
        invalid = _validate_group_updates(changes)
        if invalid:
            return {"success": False, "error": {"code": "invalid_setting", "message": invalid}}
        avatar_from_stored = set_avatar_from_stored
        try:
            await asyncio.to_thread(
                bot_for(conversation_key).update_chat, chat_id, **changes
            )
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
                if avatar_from_stored
                else None
            ),
        )
        return {
            "success": True,
            "chat_id": chat_id,
            "updated_fields": sorted(changes),
            "avatar_pending_overwrite": avatar_from_stored,
        }
