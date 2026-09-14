"""Normalize Feishu SDK message events for the business layer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class MessageNormalizationError(ValueError):
    """Raised when a message event cannot be safely normalized."""


@dataclass(frozen=True)
class SenderIdentifiers:
    open_id: str | None = None
    user_id: str | None = None
    union_id: str | None = None

    @property
    def preferred(self) -> str:
        return self.open_id or self.union_id or self.user_id or "unknown"


@dataclass(frozen=True)
class Mention:
    key: str
    name: str | None
    ids: SenderIdentifiers
    tenant_key: str | None
    is_bot: bool


@dataclass(frozen=True)
class MessageContext:
    message_id: str
    event_id: str | None
    chat_id: str
    chat_type: str
    sender_id: str
    sender_ids: SenderIdentifiers
    sender_type: str | None
    sender_tenant_key: str | None
    event_tenant_key: str | None
    message_type: str
    text: str
    mentions: tuple[Mention, ...]
    create_time: datetime | None
    bot_app_id: str
    # 该消息"引用/回复"的目标消息 id（飞书 parent_id）。非回复消息为 None。
    reply_to_message_id: str | None = None
    # image/post 消息 content 里的 image_key 列表。其他消息为空元组。
    image_keys: tuple[str, ...] = ()

    @property
    def mentions_bot(self) -> bool:
        return any(mention.is_bot for mention in self.mentions)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _identifier_set(identifier: Any) -> SenderIdentifiers:
    return SenderIdentifiers(
        open_id=_get(identifier, "open_id"),
        user_id=_get(identifier, "user_id"),
        union_id=_get(identifier, "union_id"),
    )


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(value)
        # Feishu message timestamps are normally milliseconds.
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)  # noqa: UP017
    except (TypeError, ValueError, OSError):
        return None


def _clean_bot_mentions(text: str, mentions: tuple[Mention, ...]) -> str:
    for mention in mentions:
        if not mention.is_bot or not mention.key:
            continue
        # Mention keys are literal tokens such as @_user_1. Keep other mentions.
        text = re.sub(re.escape(mention.key), "", text)
    return text.strip()


def _message_parts(message_type: str, content: Any) -> tuple[str, tuple[str, ...]]:
    """Extract text and image keys from Feishu text/image/post payloads."""
    try:
        decoded = json.loads(content) if isinstance(content, str) else content
    except (TypeError, json.JSONDecodeError) as exc:
        raise MessageNormalizationError("Invalid JSON in message content") from exc
    if not isinstance(decoded, dict):
        raise MessageNormalizationError("Message content must be an object")
    if message_type == "text":
        text = decoded.get("text", "")
        if not isinstance(text, str):
            raise MessageNormalizationError(
                "Text message content does not contain a string text field"
            )
        return text, ()
    if message_type == "image":
        image_key = decoded.get("image_key")
        return "", (image_key,) if isinstance(image_key, str) and image_key else ()
    if message_type != "post":
        return "", ()

    # A caption plus an image is delivered as a localized rich-text post.
    payload: dict[str, Any] = decoded
    for value in decoded.values():
        if isinstance(value, dict) and isinstance(value.get("content"), list):
            payload = value
            break
    text_parts: list[str] = []
    image_keys: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        text_parts.append(title)
    rows = payload.get("content")
    if not isinstance(rows, list):
        return "", ()
    for row in rows:
        elements = row if isinstance(row, list) else [row]
        for element in elements:
            if not isinstance(element, dict):
                continue
            tag = str(element.get("tag") or "")
            if tag in {"text", "a", "at"}:
                value = element.get("text") or element.get("user_name") or ""
                if isinstance(value, str):
                    text_parts.append(value)
            elif tag == "img":
                image_key = element.get("image_key")
                if isinstance(image_key, str) and image_key:
                    image_keys.append(image_key)
    return "".join(text_parts), tuple(image_keys)


def normalize_message_event(
    event: Any,
    *,
    bot_app_id: str,
    bot_open_id: str,
) -> MessageContext:
    """Convert a P2ImMessageReceiveV1-like SDK object or dict to MessageContext."""
    payload = _get(event, "event")
    header = _get(event, "header")
    message = _get(payload, "message")
    sender = _get(payload, "sender")
    sender_id_obj = _get(sender, "sender_id")

    message_id = _get(message, "message_id")
    chat_id = _get(message, "chat_id")
    if not message_id or not chat_id:
        missing = [
            name for name, value in (("message_id", message_id), ("chat_id", chat_id)) if not value
        ]
        raise MessageNormalizationError("Missing required event field(s): " + ", ".join(missing))

    message_type = _get(message, "message_type", "unknown") or "unknown"
    text, image_keys = _message_parts(message_type, _get(message, "content", ""))

    normalized_mentions: list[Mention] = []
    for raw_mention in _get(message, "mentions", []) or []:
        mention_ids = _identifier_set(_get(raw_mention, "id"))
        normalized_mentions.append(
            Mention(
                key=_get(raw_mention, "key", "") or "",
                name=_get(raw_mention, "name"),
                ids=mention_ids,
                tenant_key=_get(raw_mention, "tenant_key"),
                is_bot=bool(mention_ids.open_id and mention_ids.open_id == bot_open_id),
            )
        )
    mentions = tuple(normalized_mentions)
    sender_ids = _identifier_set(sender_id_obj)

    return MessageContext(
        message_id=message_id,
        event_id=_get(header, "event_id"),
        chat_id=chat_id,
        chat_type=_get(message, "chat_type", "unknown") or "unknown",
        sender_id=sender_ids.preferred,
        sender_ids=sender_ids,
        sender_type=_get(sender, "sender_type"),
        sender_tenant_key=_get(sender, "tenant_key"),
        event_tenant_key=_get(header, "tenant_key"),
        message_type=message_type,
        text=_clean_bot_mentions(text, mentions),
        mentions=mentions,
        create_time=_parse_time(_get(message, "create_time")),
        bot_app_id=bot_app_id,
        reply_to_message_id=(
            _get(message, "parent_id") or _get(message, "root_id") or None
        ),
        image_keys=image_keys,
    )
