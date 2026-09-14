"""Business-level message policies and the phase-one Echo handler."""

from __future__ import annotations

from typing import Protocol

from .message_context import MessageContext


class MessageHandler(Protocol):
    def handle(self, context: MessageContext) -> str | None: ...


class EchoMessageHandler:
    def handle(self, context: MessageContext) -> str | None:
        if not should_process(context):
            return None
        return f"收到：{context.text}"


def should_process(context: MessageContext) -> bool:
    sender_type = (context.sender_type or "").lower()
    if context.chat_type != "group" or not context.mentions_bot:
        return False
    if sender_type in {"app", "bot"}:
        return False
    if context.message_type == "text":
        return bool(context.text)
    if context.message_type == "image":
        # 图片消息可直接 @ 开启新会话，也可在引用回复中继续已有会话。
        return bool(context.image_keys)
    if context.message_type == "post":
        return bool(context.image_keys) or bool(context.text)
    return False
