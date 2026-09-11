from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feishu2agents.config import Settings
from feishu2agents.feishu import FeishuBot
from feishu2agents.message_context import normalize_message_event


class _StubHandler:
    def __init__(self) -> None:
        self.received = None

    def handle(self, context) -> str | None:
        self.received = context
        return None


def _make_event_body(message_id: str = "om_1", sender_type: str = "user") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-a",
            "token": "verify-abc",
            "app_id": "cli_test",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": sender_type,
                "tenant_key": "tenant-a",
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "name": "Bot",
                        "id": {"open_id": "ou_bot"},
                        "tenant_key": "tenant-a",
                    }
                ],
                "create_time": "1700000000000",
            },
        },
    }


def _make_bot(handler, *, event_mode="webhook", verify_token=""):
    settings = Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_event_mode=event_mode,
        feishu_verify_token=verify_token,
    )
    bot = FeishuBot(settings, handler)
    # Bypass the network bot-identity lookup; long-connection start is not used here.
    bot._bot_open_id = "ou_bot"
    return bot


def test_settings_parses_event_mode() -> None:
    settings = Settings(
        feishu_app_id="a", feishu_app_secret="b", feishu_event_mode="webhook"
    )
    assert settings.feishu_event_mode == "webhook"

    default = Settings(feishu_app_id="a", feishu_app_secret="b")
    assert default.feishu_event_mode == "long_connection"


def test_challenge_echo() -> None:
    """A challenge body must be echoed back verbatim."""
    bot = _make_bot(_StubHandler())
    app = Starlette(routes=[bot.webhook_route()])
    with TestClient(app) as client:
        resp = client.post("/feishu/event", json={"challenge": "xyz-challenge"})
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "xyz-challenge"}


def test_message_event_ack() -> None:
    """An im.message.receive_v1 push must be acked with code:0."""
    bot = _make_bot(_StubHandler())
    app = Starlette(routes=[bot.webhook_route()])
    with TestClient(app) as client:
        resp = client.post("/feishu/event", json=_make_event_body())
        assert resp.status_code == 200
        assert resp.json() == {"code": 0}


def test_verify_token_mismatch_rejected() -> None:
    """A configured FEISHU_VERIFY_TOKEN that does not match must 403."""
    bot = _make_bot(_StubHandler(), verify_token="expected-token")
    app = Starlette(routes=[bot.webhook_route()])
    with TestClient(app) as client:
        resp = client.post("/feishu/event", json=_make_event_body())
        assert resp.status_code == 403
        assert resp.json() == {"code": 1}


def test_normalize_accepts_push_body_dict() -> None:
    """normalize_message_event must accept the raw pushed body dict directly."""
    context = normalize_message_event(
        _make_event_body(), bot_app_id="cli_test", bot_open_id="ou_bot"
    )
    assert context.message_id == "om_1"
    assert context.sender_id == "ou_user"
    assert context.mentions_bot is True