"""Tests for the custom-group workflow: image capture, draft store, and the new MCP tools."""

from __future__ import annotations

import asyncio
import json

import lark_oapi as lark

from feishu2agents.bot_handler import should_process
from feishu2agents.group_draft_store import GroupDraftStore
from feishu2agents.message_context import (
    Mention,
    MessageContext,
    SenderIdentifiers,
    normalize_message_event,
)
from feishu2agents.requester_registry import RequesterRegistry

# --- message_context: image message ---


def make_image_event(image_key: str = "img_v2_x", parent_id: str | None = "om_bot_reply") -> dict:
    return {
        "header": {"event_id": "evt-1", "tenant_key": "tenant-a"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": "user",
                "tenant_key": "tenant-a",
            },
            "message": {
                "message_id": "om_img",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "image",
                "content": json.dumps({"image_key": image_key}),
                "mentions": [
                    {
                        "key": "@_user_1",
                        "name": "Bot",
                        "id": {"open_id": "ou_bot"},
                    }
                ],
                "parent_id": parent_id,
            },
        },
    }


def test_image_message_parses_image_keys() -> None:
    context = normalize_message_event(
        make_image_event(), bot_app_id="cli_test", bot_open_id="ou_bot"
    )
    assert context.message_type == "image"
    assert context.image_keys == ("img_v2_x",)
    assert context.reply_to_message_id == "om_bot_reply"


def test_text_message_has_no_image_keys() -> None:
    event = make_image_event()
    event["event"]["message"]["message_type"] = "text"
    event["event"]["message"]["content"] = json.dumps({"text": "hello"})
    context = normalize_message_event(event, bot_app_id="cli_test", bot_open_id="ou_bot")
    assert context.image_keys == ()


# --- bot_handler: image policy ---


def image_context(
    *, reply_to: str | None = "om_bot_reply", image_keys: tuple[str, ...] = ("img_x",)
) -> MessageContext:
    return MessageContext(
        message_id="om_img",
        event_id="evt_1",
        chat_id="oc_1",
        chat_type="group",
        sender_id="ou_user",
        sender_ids=SenderIdentifiers(open_id="ou_user"),
        sender_type="user",
        sender_tenant_key="tenant-a",
        event_tenant_key="tenant-a",
        message_type="image",
        text="",
        mentions=(
            Mention(
                key="@_user_1",
                name="Bot",
                ids=SenderIdentifiers(open_id="ou_bot"),
                tenant_key="tenant-a",
                is_bot=True,
            ),
        ),
        create_time=None,
        bot_app_id="cli_test",
        reply_to_message_id=reply_to,
        image_keys=image_keys,
    )


def test_image_quote_reply_is_processed() -> None:
    assert should_process(image_context()) is True


def test_image_without_quote_is_ignored() -> None:
    assert should_process(image_context(reply_to=None)) is False


# --- GroupDraftStore ---


def test_group_draft_store_avatar_and_group_map(tmp_path) -> None:
    store = GroupDraftStore(tmp_path / "g.sqlite")

    assert store.has_avatar("ck-1") is False
    assert store.avatar_size("ck-1") is None

    store.save_avatar("ck-1", b"first")
    store.save_avatar("ck-1", b"latest")  # overwrite = latest wins
    assert store.has_avatar("ck-1") is True
    assert store.read_avatar("ck-1") == b"latest"
    assert store.avatar_size("ck-1") == len(b"latest")

    assert store.get_group("ck-1") is None
    store.save_group("ck-1", "oc_g", name="项目群", member_open_ids=["ou_a", "ou_b"])
    group = store.get_group("ck-1")
    assert group["chat_id"] == "oc_g"
    assert group["name"] == "项目群"
    assert group["member_open_ids"] == ["ou_a", "ou_b"]

    # update only name keeps members/chat_id
    store.update_group("ck-1", name="新名字")
    group = store.get_group("ck-1")
    assert group["chat_id"] == "oc_g"
    assert group["name"] == "新名字"
    assert group["member_open_ids"] == ["ou_a", "ou_b"]


# --- feishu new API methods (via a request-based fake client) ---


class _FakeApiClient:
    def __init__(self, *, request_response=None, chat_create_chat_id="oc_created"):
        self.request_response = request_response
        self.chat_create_chat_id = chat_create_chat_id
        self.last_request = None
        self.im = type("Im", (), {"v1": type("V1", (), {"chat": _FakeChat(self)})()})()

    def request(self, request):
        self.last_request = request
        return self.request_response


class _FakeChat:
    def __init__(self, client):
        self._client = client
        self.last_request = None

    def create(self, request):
        self.last_request = request
        data = type("D", (), {"chat_id": self._client.chat_create_chat_id})()
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.data = data
        return resp


def _success_response(content: bytes):
    resp = type("R", (), {})()
    resp.success = lambda: True
    resp.raw = type("Raw", (), {"content": content})()
    return resp


def _bot(client=None):
    from feishu2agents.feishu import FeishuBot

    bot = FeishuBot.__new__(FeishuBot)
    bot._api_client = client or _FakeApiClient()
    return bot


def test_download_message_image_returns_bytes() -> None:
    client = _FakeApiClient(request_response=_success_response(b"\x89PNGdata"))
    bot = _bot(client)
    data = bot.download_message_image("om_1", "img_x")
    assert data == b"\x89PNGdata"
    req = bot._api_client.last_request
    assert req.http_method is lark.HttpMethod.GET


def test_upload_avatar_image_returns_image_key() -> None:
    resp = _success_response(json.dumps({"data": {"image_key": "im_avatar"}}).encode())
    bot = _bot(_FakeApiClient(request_response=resp))
    key = bot.upload_avatar_image(b"imgbytes")
    assert key == "im_avatar"
    req = bot._api_client.last_request
    assert req.headers.get("Content-Type", "").startswith("multipart/form-data")


def test_update_chat_sends_put() -> None:
    bot = _bot(_FakeApiClient(request_response=_success_response(b"{}")))
    bot.update_chat("oc_1", name="新群名")
    req = bot._api_client.last_request
    assert req.http_method is lark.HttpMethod.PUT
    assert req.body == {"name": "新群名"}


def test_create_chat_returns_chat_id() -> None:
    bot = _bot(_FakeApiClient())
    chat_id = bot.create_chat("项目群", ["ou_a", "ou_b"], avatar_image_key="im_avatar")
    assert chat_id == "oc_created"
    req = bot._api_client.im.v1.chat.last_request
    assert req.request_body.name == "项目群"
    assert req.request_body.user_id_list == ["ou_a", "ou_b"]
    assert req.request_body.avatar == "im_avatar"


def test_search_contacts_parses_candidates() -> None:
    payload = {"data": {"user_list": [{"user_id": "ou_z", "mobile": "13812345678"}]}}
    bot = _bot(_FakeApiClient(request_response=_success_response(json.dumps(payload).encode())))
    candidates = bot.search_contacts("张三，手机号：13812345678")
    assert candidates == [{"name": "", "open_id": "ou_z", "mobile": "13812345678"}]
    req = bot._api_client.last_request
    assert req.uri == "/open-apis/contact/v3/users/batch_get_id"
    assert req.queries == [("user_id_type", "open_id")]
    assert req.body == {"emails": [], "mobiles": ["13812345678"]}


# --- feishu_mcp new tools ---


class ToolCapture:
    def __init__(self, captured) -> None:
        self._captured = captured

    def tool(self, **opts):
        name = opts["name"]

        def deco(func):
            self._captured[name] = func
            return func

        return deco


def register(bot, *, store=None, registry=None):
    import tempfile
    from pathlib import Path

    from feishu2agents.feishu_mcp import register_feishu_tools

    captured = {}
    if store is None:
        store = GroupDraftStore(str(Path(tempfile.mkdtemp()) / "g.sqlite"))
    reg = registry or RequesterRegistry()
    reg.register("ck-1", open_id="ou_requester")
    register_feishu_tools(ToolCapture(captured), bot, reg, store)
    return captured, store, reg


def test_get_stored_image_tool() -> None:
    bot = _bot(_FakeApiClient())
    captured, store, _ = register(bot)
    assert asyncio.run(captured["get_stored_image"]("ck-1"))["has_avatar"] is False
    store.save_avatar("ck-1", b"x")
    assert asyncio.run(captured["get_stored_image"]("ck-1"))["has_avatar"] is True


def test_create_group_tool_creates_and_persists() -> None:
    bot = _bot(_FakeApiClient())
    captured, store, reg = register(bot)
    out = asyncio.run(captured["create_group"]("ck-1", "项目群", ["ou_friend"]))
    assert out["success"] is True
    assert out["chat_id"] == "oc_created"
    assert out["member_open_ids"] == ["ou_requester", "ou_friend"]
    assert store.get_group("ck-1")["chat_id"] == "oc_created"
    assert reg.get("ck-1")["created_group_chat_id"] == "oc_created"


def test_get_group_status_tool() -> None:
    bot = _bot(_FakeApiClient())
    captured, store, _ = register(bot)
    assert asyncio.run(captured["get_group_status"]("ck-1"))["created"] is False
    store.save_group("ck-1", "oc_g", name="群")
    status = asyncio.run(captured["get_group_status"]("ck-1"))
    assert status["created"] is True
    assert status["chat_id"] == "oc_g"


def test_update_group_tool_uses_persisted_chat() -> None:
    bot = _bot(_FakeApiClient(request_response=_success_response(b"{}")))
    captured, store, _ = register(bot)
    store.save_group("ck-1", "oc_persisted", name="旧名")
    out = asyncio.run(captured["update_group"]("ck-1", name="新名"))
    assert out["success"] is True
    assert out["chat_id"] == "oc_persisted"
    assert store.get_group("ck-1")["name"] == "新名"


def test_update_group_without_group_errors() -> None:
    bot = _bot(_FakeApiClient())
    captured, _, _ = register(bot)
    out = asyncio.run(captured["update_group"]("ck-1", name="新名"))
    assert out["success"] is False
    assert out["error"]["code"] == "group_missing"
