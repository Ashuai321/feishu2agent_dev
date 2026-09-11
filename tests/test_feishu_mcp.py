"""Tests for Feishu group-creation MCP tools and requester registry."""

from __future__ import annotations

import asyncio

import pytest

from feishu2agents.feishu import FeishuApiError
from feishu2agents.requester_registry import RequesterRegistry


class _FakeChatResource:
    def __init__(self, *, success=True, chat_id="oc_test_group", code=0) -> None:
        self._success = success
        self._chat_id = chat_id
        self._code = code
        self.last_request = None

    def create(self, request):
        self.last_request = request
        if not self._success:
            resp = type("R", (), {})()
            resp.success = lambda: False
            resp.code = self._code
            resp.get_log_id = lambda: "log"
            resp.data = None
            return resp
        data = type("D", (), {"chat_id": self._chat_id})()
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.data = data
        return resp

    def get(self, request):
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.raw = type("Raw", (), {"content": b'{"data":{"external":false}}'})()
        return resp

    def list(self, request):
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.raw = type("Raw", (), {"content": b'{"data":{"items":[]}}'})()
        return resp


class _FakeExternalChatResource(_FakeChatResource):
    def get(self, request):
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.raw = type(
            "Raw", (), {"content": b'{"data":{"external":true,"tenant_key":"tenant-a"}}'}
        )()
        return resp

    def list(self, request):
        resp = type("R", (), {})()
        resp.success = lambda: True
        resp.raw = type(
            "Raw",
            (),
            {
                "content": (
                    b'{"data":{"items":[{"member_id":"ou_external",'
                    b'"tenant_key":"tenant-b"}]}}'
                )
            },
        )()
        return resp


class _FakeV1:
    def __init__(self, chat) -> None:
        self.chat = chat


class _FakeIm:
    def __init__(self, chat) -> None:
        self.v1 = _FakeV1(chat)


class _FakeApiClient:
    def __init__(self, chat: _FakeChatResource) -> None:
        self.im = _FakeIm(chat)


def _bot(chat=None, **kw):
    from feishu2agents.feishu import FeishuBot

    chat = chat or _FakeChatResource(**kw)
    bot = FeishuBot.__new__(FeishuBot)  # skip __init__ (needs real creds)
    bot._api_client = _FakeApiClient(chat)
    return bot


def test_create_private_group_builds_request_and_returns_chat_id():
    bot = _bot(chat_id="oc_new_group")
    chat_id = bot.create_private_group("ou_abc", name="张三的私聊")
    assert chat_id == "oc_new_group"
    request = bot._api_client.im.v1.chat.last_request
    assert request.user_id_type == "open_id"
    assert request.request_body.user_id_list == ["ou_abc"]
    assert request.request_body.chat_mode == "group"
    assert request.request_body.name == "张三的私聊"


def test_create_private_group_default_name():
    bot = _bot()
    bot.create_private_group("ou_xyz")
    assert bot._api_client.im.v1.chat.last_request.request_body.name == "机器人与TA的私聊"


def test_create_private_group_raises_on_failure():
    bot = _bot(success=False, code=12)
    with pytest.raises(FeishuApiError):
        bot.create_private_group("ou_abc")


def test_external_requester_gets_actionable_limitation_before_create():
    bot = _bot(chat=_FakeExternalChatResource())
    with pytest.raises(FeishuApiError, match="external-tenant"):
        bot.create_private_group(
            "ou_external", source_chat_id="oc_external_source"
        )
    assert bot._api_client.im.v1.chat.last_request is None


def test_registry_register_get_bind():
    reg = RequesterRegistry()
    assert reg.get("ck-1") is None
    reg.register("ck-1", open_id="ou_1", name="张", source_chat_id="oc_g")
    assert reg.get("ck-1")["open_id"] == "ou_1"
    reg.bind_group("ck-1", "oc_new")
    assert reg.get("ck-1")["created_group_chat_id"] == "oc_new"
    assert reg.get("ck-2") is None


def test_get_requester_info_tool():
    from feishu2agents.feishu_mcp import register_feishu_tools

    reg = RequesterRegistry()
    reg.register("ck-1", open_id="ou_1")
    captured = {}
    register_feishu_tools(ToolCapture(captured), _bot(), reg)
    out = asyncio.run(captured["get_requester_info"]("ck-1"))
    assert out["success"] is True
    assert out["requester"]["open_id"] == "ou_1"


def test_create_private_group_tool_uses_registry():
    from feishu2agents.feishu_mcp import register_feishu_tools

    reg = RequesterRegistry()
    reg.register("ck-1", open_id="ou_1", source_chat_id="oc_source")
    bot = _bot(chat_id="oc_made")
    captured = {}
    register_feishu_tools(ToolCapture(captured), bot, reg)
    out = asyncio.run(captured["create_private_group"]("ck-1"))
    assert out["success"] is True
    assert out["chat_id"] == "oc_made"
    assert out["user_open_id"] == "ou_1"
    assert reg.get("ck-1")["created_group_chat_id"] == "oc_made"


def test_create_private_group_tool_unknown_conversation():
    from feishu2agents.feishu_mcp import register_feishu_tools

    captured = {}
    register_feishu_tools(ToolCapture(captured), _bot(), RequesterRegistry())
    out = asyncio.run(captured["create_private_group"]("ck-nope"))
    assert out["success"] is False
    assert out["error"]["code"] == "requester_not_found"


class ToolCapture:
    """Minimal stand-in for FastMCP that records registered tools by name."""

    def __init__(self, captured) -> None:
        self._captured = captured

    def tool(self, **opts):
        name = opts["name"]

        def deco(func):
            self._captured[name] = func
            return func

        return deco
