from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]


def _load_worker_module():
    workers = types.ModuleType("workers")

    class Response:
        pass

    class WorkerEntrypoint:
        pass

    workers.Response = Response
    workers.WorkerEntrypoint = WorkerEntrypoint
    sys.modules.setdefault("workers", workers)
    spec = importlib.util.spec_from_file_location(
        "cloudflare_worker_app", ROOT / "cloudflare_worker/src/worker_app.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(
    message_type: str,
    content: dict,
    *,
    text_mention: bool = True,
    parent_id: str = "",
) -> dict:
    mention = {
        "key": "@_user_bot",
        "id": {"open_id": "ou_bot"},
    }
    return {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_requester"},
            },
            "message": {
                "message_id": "om_123",
                "chat_id": "oc_123",
                "chat_type": "group",
                "message_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
                "mentions": [mention] if text_mention else [],
                "parent_id": parent_id,
            },
        },
    }


def test_worker_normalizes_text_mentions_without_echoing_the_mention():
    worker = _load_worker_module()
    event = worker._normalize_event(_event("text", {"text": "@_user_bot 测试"}), "ou_bot")

    assert event is not None
    assert event["text"] == "测试"
    assert event["open_id"] == "ou_requester"
    assert event["image_keys"] == []


def test_worker_keeps_image_key_for_queued_r2_storage():
    worker = _load_worker_module()
    event = worker._normalize_event(_event("image", {"image_key": "img_v2_abc"}), "ou_bot")

    assert event is not None
    assert event["image_keys"] == ["img_v2_abc"]
    assert "必要文件" in event["text"]


def test_worker_requires_an_mention_even_when_replying_to_a_bot_message():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=False, parent_id="om_bot_card"),
        "ou_bot",
    )

    assert event is None


def test_worker_keeps_parent_for_an_mentioned_reply():
    worker = _load_worker_module()

    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=True, parent_id="om_bot_card"),
        "ou_bot",
    )

    assert event is not None
    assert event["parent_id"] == "om_bot_card"
    assert event["mentioned_bot"] is True


def test_agent_input_uses_text_relay_envelope():
    worker = _load_worker_module()

    input_text = worker._conversation_input(
        request_id="req_1",
        conversation_key="feishu:app:chat:1",
        text="测试",
        continuation=False,
    )

    assert input_text.startswith(
        "request_id: req_1\n"
        "conversation_key: feishu:app:chat:1\n"
        "relay_mcp: workspace-agent-relay-mcp-prd\n"
        "protocol: local-agent-shell/v1\n"
        "turn_mode: initial\n"
    )
    assert "Completion contract:" in input_text
    assert "User task:\n测试" in input_text


def test_worker_config_points_directly_to_python_entrypoint():
    config = json.loads((ROOT / "wrangler.jsonc").read_text())

    assert config["main"] == "cloudflare_worker/src/entry.py"
    assert "python_workers" in config["compatibility_flags"]
    assert {item["binding"] for item in config["d1_databases"]} == {"DB"}
    assert config["queues"]["producers"][0]["binding"] == "AGENT_QUEUE"
    assert "r2_buckets" not in config
    assert "PYTHON_ORIGIN" not in (ROOT / "wrangler.jsonc").read_text()


def test_result_delivery_updates_a_plain_text_placeholder_in_place():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            assert request_id == "req_1"
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "done",
                "title": "完成",
                "markdown": "结果正文",
                "delivered": 0,
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.updates = []

        async def update(self, message_id, text):
            self.updates.append((message_id, text))

    async def noop_db_run(*args, **kwargs):
        return None

    worker._db_run = noop_db_run
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.updates == [("om_placeholder", "完成\n结果正文")]
    assert state.saved == ("om_placeholder", "feishu:app:chat:x")


def test_result_delivery_falls_back_to_a_new_reply_when_edit_fails():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "done",
                "title": "完成",
                "markdown": "结果正文",
                "delivered": 0,
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []

        async def update(self, message_id, text):
            raise RuntimeError("Feishu update temporarily failed")

        async def reply(self, message_id, text):
            self.replies.append((message_id, text))
            return "om_result"

    async def noop_db_run(*args, **kwargs):
        return None

    worker._db_run = noop_db_run
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.replies == [("om_source", "完成\n结果正文")]
    assert state.saved == ("om_result", "feishu:app:chat:x")


def test_feishu_text_update_uses_put_message_edit_api():
    worker = _load_worker_module()

    class FakeFeishu(worker.FeishuAPI):
        def __init__(self):
            super().__init__(SimpleNamespace())
            self.call = None

        async def _request(self, method, path, **kwargs):
            self.call = (method, path, kwargs)
            return {"code": 0}

    api = FakeFeishu()
    asyncio.run(api.update("om_message", "已完成"))

    assert api.call == (
        "PUT",
        "/open-apis/im/v1/messages/om_message",
        {
            "params": {"user_id_type": "open_id"},
            "json": {"msg_type": "text", "content": '{"text":"已完成"}'},
        },
    )


def test_feishu_contact_search_uses_tenant_contact_search_api():
    worker = _load_worker_module()

    class FakeFeishu(worker.FeishuAPI):
        def __init__(self):
            super().__init__(SimpleNamespace())
            self.call = None

        async def _request(self, method, path, **kwargs):
            self.call = (method, path, kwargs)
            return {
                "code": 0,
                "data": {
                    "user_list": [
                        {
                            "user_id": "ou_z",
                            "mobile": "13812345678",
                        }
                    ]
                },
            }

    api = FakeFeishu()
    candidates = asyncio.run(api.search_contacts("姓名：张三，手机号：13812345678"))

    assert candidates == [{"name": "", "open_id": "ou_z", "mobile": "13812345678"}]
    assert api.call == (
        "POST",
        "/open-apis/contact/v3/users/batch_get_id",
        {
            "params": {"user_id_type": "open_id"},
            "json": {"emails": [], "mobiles": ["13812345678"]},
        },
    )


def test_feishu_create_group_uses_tenant_chat_api():
    worker = _load_worker_module()

    class FakeFeishu(worker.FeishuAPI):
        def __init__(self):
            super().__init__(SimpleNamespace())
            self.call = None

        async def _request(self, method, path, **kwargs):
            self.call = (method, path, kwargs)
            return {"code": 0, "data": {"chat_id": "oc_group"}}

    api = FakeFeishu()
    chat_id = asyncio.run(api.create_group("项目群", ["ou_requester", "ou_member"]))

    assert chat_id == "oc_group"
    assert api.call == (
        "POST",
        "/open-apis/im/v1/chats",
        {
            "params": {"user_id_type": "open_id"},
            "json": {
                "name": "项目群",
                "chat_mode": "group",
                "user_id_list": ["ou_requester", "ou_member"],
            },
        },
    )


def test_cloudflare_mcp_exposes_and_dispatches_search_contacts():
    worker = _load_worker_module()

    class FakeState:
        db = object()

    class FakeFeishu:
        async def search_contacts(self, query):
            assert query == "张三"
            return [{"name": "张三", "open_id": "ou_z"}]

    relay = worker.CloudflareRelay(SimpleNamespace(), None, FakeState())
    relay.feishu = FakeFeishu()
    names = {tool["name"] for tool in relay.tool_definitions()}
    result = asyncio.run(
        relay.call_tool(
            "search_contacts",
            {"conversation_key": "feishu:app:chat:1", "query": "张三"},
        )
    )

    assert "search_contacts" in names
    assert result["isError"] is False
    assert result["structuredContent"]["candidates"] == [
        {"name": "张三", "open_id": "ou_z"}
    ]


def test_cloudflare_mcp_marks_non_mutating_tools_read_only():
    worker = _load_worker_module()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, SimpleNamespace())

    annotations = {
        tool["name"]: tool.get("annotations", {}) for tool in relay.tool_definitions()
    }

    assert {
        name for name, value in annotations.items() if value.get("readOnlyHint") is True
    } == {
        "server_info",
        "get_run_context",
        "get_requester_info",
        "search_contacts",
        "get_group_status",
        "get_stored_image",
    }
    assert all(
        not annotations[name].get("readOnlyHint", False)
        for name in {
            "record_plan",
            "record_progress",
            "record_result",
            "update_conversation_title",
            "ask_user",
            "create_private_group",
            "create_group",
        }
    )


def test_cloudflare_mcp_exposes_and_dispatches_create_group():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        def __init__(self):
            self.bound = None

        async def requester(self, conversation_key):
            assert conversation_key == "feishu:app:chat:1"
            return {"open_id": "ou_requester"}

        async def bind_group(self, conversation_key, chat_id):
            self.bound = (conversation_key, chat_id)

    class FakeFeishu:
        def __init__(self):
            self.call = None

        async def create_group(self, name, member_open_ids):
            self.call = (name, member_open_ids)
            return "oc_group"

    state = FakeState()
    feishu = FakeFeishu()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = feishu
    names = {tool["name"] for tool in relay.tool_definitions()}
    result = asyncio.run(
        relay.call_tool(
            "create_group",
            {
                "conversation_key": "feishu:app:chat:1",
                "name": "项目群",
                "member_open_ids": ["ou_member", "ou_requester", ""],
            },
        )
    )

    assert "create_group" in names
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "success": True,
        "conversation_key": "feishu:app:chat:1",
        "chat_id": "oc_group",
        "member_open_ids": ["ou_requester", "ou_member"],
    }
    assert feishu.call == ("项目群", ["ou_requester", "ou_member"])
    assert state.bound == ("feishu:app:chat:1", "oc_group")
