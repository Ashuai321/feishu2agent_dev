from __future__ import annotations

import json

import pytest

from feishu2agents.message_context import MessageNormalizationError, normalize_message_event


def make_event(
    *,
    text: str = "@_user_1 hello",
    message_type: str = "text",
    mentions: list[dict] | None = None,
    sender_type: str = "user",
) -> dict:
    if mentions is None:
        mentions = [
            {
                "key": "@_user_1",
                "name": "Echo Bot",
                "id": {"open_id": "ou_bot"},
                "tenant_key": "tenant-a",
            }
        ]
    return {
        "header": {"event_id": "evt-1", "tenant_key": "tenant-a"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user", "union_id": "on_user"},
                "sender_type": sender_type,
                "tenant_key": "tenant-external",
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": message_type,
                "content": json.dumps({"text": text}),
                "mentions": mentions,
                "create_time": "1700000000000",
            },
        },
    }


def normalize(event: dict):
    return normalize_message_event(event, bot_app_id="cli_test", bot_open_id="ou_bot")


def test_normalizes_text_and_sender_context() -> None:
    context = normalize(make_event())

    assert context.text == "hello"
    assert context.message_id == "om_1"
    assert context.chat_id == "oc_1"
    assert context.sender_id == "ou_user"
    assert context.sender_ids.union_id == "on_user"
    assert context.sender_tenant_key == "tenant-external"
    assert context.mentions_bot is True
    assert context.create_time is not None


def test_removes_only_bot_mention() -> None:
    mentions = [
        {"key": "@_user_1", "name": "Bot", "id": {"open_id": "ou_bot"}},
        {"key": "@_user_2", "name": "Alice", "id": {"open_id": "ou_alice"}},
    ]
    context = normalize(make_event(text="@_user_1 @_user_2 hello", mentions=mentions))

    assert context.text == "@_user_2 hello"
    assert context.mentions[1].is_bot is False


def test_external_sender_may_have_only_open_id() -> None:
    event = make_event()
    event["event"]["sender"]["sender_id"] = {"open_id": "ou_external"}

    context = normalize(event)

    assert context.sender_id == "ou_external"
    assert context.sender_ids.user_id is None


def test_non_text_message_has_empty_text() -> None:
    context = normalize(make_event(message_type="image"))
    assert context.text == ""


def test_invalid_content_is_rejected() -> None:
    event = make_event()
    event["event"]["message"]["content"] = "not-json"

    with pytest.raises(MessageNormalizationError, match="Invalid JSON"):
        normalize(event)


def test_missing_required_identifier_is_rejected() -> None:
    event = make_event()
    event["event"]["message"]["message_id"] = ""

    with pytest.raises(MessageNormalizationError, match="message_id"):
        normalize(event)
