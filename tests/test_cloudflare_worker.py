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


def _event(message_type: str, content: dict, *, text_mention: bool = True) -> dict:
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


def test_agent_input_is_json_relay_envelope():
    worker = _load_worker_module()

    payload = json.loads(
        worker._conversation_input(
            request_id="req_1",
            conversation_key="feishu:app:chat:1",
            text="测试",
            continuation=False,
        )
    )

    assert payload == {
        "request_id": "req_1",
        "conversation_key": "feishu:app:chat:1",
        "relay_mcp": "workspace-agent-relay-mcp-prd",
        "protocol": "local-agent-shell/v1",
        "turn_mode": "initial",
        "user_input": "测试",
    }


def test_worker_config_points_directly_to_python_entrypoint():
    config = json.loads((ROOT / "wrangler.jsonc").read_text())

    assert config["main"] == "cloudflare_worker/src/entry.py"
    assert "python_workers" in config["compatibility_flags"]
    assert {item["binding"] for item in config["d1_databases"]} == {"DB"}
    assert config["queues"]["producers"][0]["binding"] == "AGENT_QUEUE"
    assert "r2_buckets" not in config
    assert "PYTHON_ORIGIN" not in (ROOT / "wrangler.jsonc").read_text()


def test_result_delivery_falls_back_when_placeholder_is_plain_text():
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
            self.replies = []

        async def update(self, message_id, text):
            raise RuntimeError(
                "Feishu API failed (400): Your request contains an invalid request parameter, "
                "ext=This message is NOT a card."
            )

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
