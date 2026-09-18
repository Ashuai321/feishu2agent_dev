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


def test_plural_webhook_route_is_available() -> None:
    bot = _make_bot(_StubHandler())
    app = Starlette(routes=bot.webhook_routes())
    with TestClient(app) as client:
        resp = client.post("/feishu/events", json={"challenge": "plural-challenge"})
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "plural-challenge"}


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


def test_normalize_preserves_the_platform_that_delivered_event() -> None:
    context = normalize_message_event(
        _make_event_body(),
        bot_app_id="cli_test",
        bot_open_id="ou_bot",
        platform="lark",
    )
    assert context.platform == "lark"


def test_lark_webhook_and_oauth_routes_use_lark_configuration() -> None:
    settings = Settings(
        feishu_app_id="cli_feishu",
        feishu_app_secret="feishu-secret",
        lark_app_id="cli_lark",
        lark_app_secret="lark-secret",
        lark_event_mode="webhook",
        lark_oauth_redirect_uri="https://bot.boooe.com/lark/oauth/callback",
        lark_oauth_scope="bitable:app",
    )
    bot = FeishuBot(settings, _StubHandler(), platform="lark")
    bot._bot_open_id = "ou_lark_bot"
    app = Starlette(routes=bot.webhook_routes() + bot.oauth_routes())

    with TestClient(app) as client:
        challenge = client.post("/lark/events", json={"challenge": "lark-challenge"})
        assert challenge.status_code == 200
        assert challenge.json() == {"challenge": "lark-challenge"}

        redirect = client.get("/lark/oauth/authorize", follow_redirects=False)
        assert redirect.status_code == 302
        location = redirect.headers["location"]
        assert location.startswith(
            "https://accounts.larksuite.com/open-apis/authen/v1/authorize?"
        )
        assert "app_id=cli_lark" in location
        assert "scope=bitable%3Aapp" in location


def _make_oauth_bot():
    settings = Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_event_mode="webhook",
        feishu_oauth_redirect_uri="https://example.com/feishu/oauth/callback",
        feishu_oauth_scope="bitable:app",
    )
    bot = FeishuBot(settings, _StubHandler())
    bot._bot_open_id = "ou_bot"
    return bot


def test_oauth_authorize_redirects_with_state(monkeypatch) -> None:
    bot = _make_oauth_bot()
    app = Starlette(routes=bot.oauth_routes())
    with TestClient(app) as client:
        resp = client.get("/feishu/oauth/authorize", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith(
            "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
        )
        assert "app_id=cli_test" in location
        assert "scope=bitable%3Aapp" in location
        assert "state=" in location
        assert "redirect_uri=" in location
        assert bot._oauth_states, "a state nonce should be recorded"
        captured_state = next(iter(bot._oauth_states))
        assert captured_state in location


def test_oauth_authorize_requires_target() -> None:
    bot = _make_oauth_bot()
    bot._settings = Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        feishu_event_mode="webhook",
    )
    app = Starlette(routes=bot.oauth_routes())
    with TestClient(app) as client:
        resp = client.get("/feishu/oauth/authorize")
        assert resp.status_code == 400


def test_oauth_callback_exchanges_code(monkeypatch) -> None:
    bot = _make_oauth_bot()
    # Simulate a fresh, valid state issued by the authorize endpoint.
    bot._oauth_states["abc123"] = {
        "target": "https://example.com/feishu/oauth/callback",
        "expiry": __import__("time").time() + 300,
    }
    token_payload = {
        "access_token": "u-xxx",
        "refresh_token": "r-xxx",
        "expires_in": 7200,
        "scope": "bitable:app",
        "open_id": "ou_user",
    }
    monkeypatch.setattr(
        bot,
        "exchange_user_access_token",
        lambda code, state=None, redirect_uri=None: token_payload,
    )

    app = Starlette(routes=bot.oauth_routes())
    with TestClient(app) as client:
        resp = client.get(
            "/feishu/oauth/callback", params={"code": "code-1", "state": "abc123"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["token"]["access_token"] == "u-xxx"
    # state must be consumed
    assert "abc123" not in bot._oauth_states


def test_oauth_callback_rejects_unknown_state() -> None:
    bot = _make_oauth_bot()
    app = Starlette(routes=bot.oauth_routes())
    with TestClient(app) as client:
        resp = client.get(
            "/feishu/oauth/callback", params={"code": "code-1", "state": "nope"}
        )
        assert resp.status_code == 400


def test_oauth_callback_requires_code() -> None:
    bot = _make_oauth_bot()
    app = Starlette(routes=bot.oauth_routes())
    with TestClient(app) as client:
        resp = client.get("/feishu/oauth/callback")
        assert resp.status_code == 400
