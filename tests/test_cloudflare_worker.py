from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

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


def test_worker_config_points_directly_to_python_entrypoint():
    config = json.loads((ROOT / "wrangler.jsonc").read_text())

    assert config["main"] == "cloudflare_worker/src/entry.py"
    assert "python_workers" in config["compatibility_flags"]
    assert {item["binding"] for item in config["d1_databases"]} == {"DB"}
    assert config["queues"]["producers"][0]["binding"] == "AGENT_QUEUE"
    assert config["r2_buckets"][0]["binding"] == "AVATARS"
    assert "PYTHON_ORIGIN" not in (ROOT / "wrangler.jsonc").read_text()
