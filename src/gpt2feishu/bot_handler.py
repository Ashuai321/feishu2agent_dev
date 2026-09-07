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
    return (
        context.chat_type == "group"
        and context.message_type == "text"
        and context.mentions_bot
        and bool(context.text)
        and sender_type not in {"app", "bot"}
    )
