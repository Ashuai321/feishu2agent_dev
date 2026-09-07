from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gpt2feishu.bot_handler import EchoMessageHandler
from gpt2feishu.dedupe import DedupeCache
from gpt2feishu.feishu import FeishuBot


def event() -> dict:
    return {
        "header": {"event_id": "evt-1", "tenant_key": "tenant-a"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": "user",
                "tenant_key": "tenant-a",
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 hello"}),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "name": "Bot",
                        "id": {"open_id": "ou_bot"},
                    }
                ],
            },
        },
    }


def bot() -> FeishuBot:
    instance = FeishuBot.__new__(FeishuBot)
    instance._settings = SimpleNamespace(feishu_app_id="cli_test")
    instance._handler = EchoMessageHandler()
    instance._dedupe = DedupeCache()
    instance._bot_open_id = "ou_bot"
    return instance


def test_duplicate_event_replies_once() -> None:
    instance = bot()
    replies: list[tuple[str, str]] = []
    instance._reply = lambda message_id, text: replies.append((message_id, text))

    instance._on_message(event())
    instance._on_message(event())

    assert replies == [("om_1", "收到：hello")]


def test_reply_failure_releases_dedupe_for_retry() -> None:
    instance = bot()
    attempts = 0

    def reply(_message_id: str, _text: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("upstream failed")

    instance._reply = reply

    with pytest.raises(RuntimeError, match="upstream failed"):
        instance._on_message(event())
    instance._on_message(event())

    assert attempts == 2
