from __future__ import annotations

import asyncio
import base64
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


def test_worker_preserves_cross_platform_sender_identity_fields():
    worker = _load_worker_module()
    body = _event("text", {"text": "@_user_bot 测试"})
    body["header"]["tenant_key"] = "tenant_feishu"
    body["event"]["sender"]["tenant_key"] = "tenant_lark"
    body["event"]["sender"]["sender_id"].update(
        {"union_id": "on_union", "user_id": "ou_user_id"}
    )

    event = worker._normalize_event(body, "ou_bot")

    assert event is not None
    assert event["union_id"] == "on_union"
    assert event["user_id"] == "ou_user_id"
    assert event["tenant_key"] == "tenant_feishu"
    assert event["sender_tenant_key"] == "tenant_lark"


def test_worker_keeps_image_key_for_queued_r2_storage():
    worker = _load_worker_module()
    event = worker._normalize_event(_event("image", {"image_key": "img_v2_abc"}), "ou_bot")

    assert event is not None
    assert event["image_keys"] == ["img_v2_abc"]
    assert "必要文件" in event["text"]


def test_feishu_webhook_ack_schedules_queue_without_running_d1_work():
    worker = _load_worker_module()
    worker.Response.json = staticmethod(lambda payload, **kwargs: payload)

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    class FakeContext:
        def __init__(self):
            self.tasks = []

        def wait_until(self, task):
            self.tasks.append(task)

    class FakeRequest:
        method = "POST"
        headers = {}
        url = "https://example.test/feishu/events"

        async def json(self):
            return {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_requester"}},
                    "message": {
                        "message_id": "om_123",
                        "chat_id": "oc_123",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": json.dumps({"text": "@_user_bot hello"}),
                        "mentions": [
                            {"key": "@_user_bot", "id": {"open_id": "ou_bot"}}
                        ],
                    },
                },
            }

    queue = FakeQueue()
    context = FakeContext()
    relay = worker.CloudflareRelay(
        types.SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot", AGENT_QUEUE=queue),
        context,
        types.SimpleNamespace(),
    )
    asyncio.run(relay.handle_feishu(FakeRequest(), "feishu"))

    # The HTTP handler only schedules the Queue hand-off.  It must not await
    # D1/API work before returning the Feishu acknowledgement.
    assert len(context.tasks) == 1
    assert queue.messages == []
    asyncio.run(context.tasks.pop())
    assert queue.messages[0]["kind"] == "feishu_event"
    assert queue.messages[0]["platform"] == "feishu"


def test_worker_detects_actual_image_format_for_avatar_upload():
    worker = _load_worker_module()

    assert worker._image_upload_metadata(b"\x89PNG\r\n\x1a\nbytes") == (
        "avatar.png",
        "image/png",
    )
    assert worker._image_upload_metadata(b"\xff\xd8\xffbytes") == (
        "avatar.jpg",
        "image/jpeg",
    )


def test_bitable_automation_formatter_prefers_ai_result_text():
    worker = _load_worker_module()

    assert worker._format_bitable_automation_text(
        {"result": "本周任务完成率为 80%", "trace_id": "ignored"}
    ) == "[多维表格 AI 分析]\n本周任务完成率为 80%"


def test_bitable_automation_formatter_preserves_arbitrary_json():
    worker = _load_worker_module()

    text = worker._format_bitable_automation_text({"rows": 3, "status": "ok"})
    assert text.startswith("[多维表格 AI 分析]\n")
    assert '"rows": 3' in text
    assert '"status": "ok"' in text


def test_bitable_automation_webhook_reads_request_body_once(monkeypatch):
    worker = _load_worker_module()
    responses = []
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: responses.append(
            (payload, status, headers)
        )
        or responses[-1],
    )

    class Request:
        method = "POST"
        headers = {}

        def __init__(self):
            self.reads = 0

        async def text(self):
            self.reads += 1
            return json.dumps({"text": "来自 AI 分析"}, ensure_ascii=False)

        async def json(self):
            raise AssertionError("the single-use body must not be read as JSON first")

    class FakeFeishu:
        async def send_text(self, chat_id, text):
            assert chat_id == worker.BITABLE_WORKFLOW_GROUP_CHAT_ID
            assert text == "[多维表格 AI 分析]\n来自 AI 分析"
            return "om_forwarded"

    relay = worker.CloudflareRelay(
        SimpleNamespace(
            BITABLE_WORKFLOW_GROUP_CHAT_ID=worker.BITABLE_WORKFLOW_GROUP_CHAT_ID,
            BITABLE_AUTOMATION_WEBHOOK_TOKEN="",
        ),
        None,
        SimpleNamespace(),
    )
    relay.feishu = FakeFeishu()
    request = Request()

    result = asyncio.run(relay.bitable_automation_webhook(request))

    assert request.reads == 1
    assert result == ({"success": True, "chat_id": worker.BITABLE_WORKFLOW_GROUP_CHAT_ID, "message_id": "om_forwarded"}, 200, None)


def test_worker_parses_caption_and_image_post_message():
    worker = _load_worker_module()
    content = {
        "zh_cn": {
            "title": "",
            "content": [
                [
                    {"tag": "text", "text": "@_user_bot 将群头像改成这个："},
                    {"tag": "img", "image_key": "img_v3_rocket"},
                ]
            ],
        }
    }
    event = worker._normalize_event(_event("post", content), "ou_bot")

    assert event is not None
    assert event["text"].startswith("将群头像改成这个")
    assert event["image_keys"] == ["img_v3_rocket"]


def test_worker_requires_an_mention_even_when_replying_to_a_bot_message():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=False, parent_id="om_bot_card"),
        "ou_bot",
    )

    assert event is None


def test_target_group_can_continue_with_a_quoted_reply_without_new_mention():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=False, parent_id="om_bot_card"),
        "ou_bot",
        allow_unmentioned_reply=True,
    )

    assert event is not None
    assert event["text"] == "继续刚才的问题"
    assert event["parent_id"] == "om_bot_card"


def test_worker_keeps_parent_for_an_mentioned_reply():
    worker = _load_worker_module()

    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=True, parent_id="om_bot_card"),
        "ou_bot",
    )

    assert event is not None
    assert event["parent_id"] == "om_bot_card"
    assert event["mentioned_bot"] is True


def test_target_group_workflow_uses_the_configured_group_and_platform():
    worker = _load_worker_module()

    class Relay:
        env = type("Env", (), {})()

        def base_url(self):
            return "https://bot.boooe.com"

    workflow = worker.BitableGroupWorkflow(Relay())
    assert workflow.is_target_group("oc_5e9132f3638772d53d92d6fc5e953abc") is True
    assert workflow.is_target_group("oc_other") is False
    assert workflow._auth_base("feishu") == "https://accounts.feishu.cn"
    assert workflow._auth_base("lark") == "https://accounts.larksuite.com"


def test_target_group_document_commands_select_mode_and_optional_payload():
    worker = _load_worker_module()

    assert worker.BitableGroupWorkflow.parse_document_command("[飞书文档]") == (
        "feishu",
        "",
    )
    assert worker.BitableGroupWorkflow.parse_document_command("[lark文档] 要写入") == (
        "lark",
        "要写入",
    )
    assert worker.BitableGroupWorkflow.parse_document_command("lark文档：测试") == (
        "lark",
        "测试",
    )
    assert worker.BitableGroupWorkflow.parse_document_command("其他内容") is None


def test_document_mode_reply_uses_prompt_binding_before_latest_user_mode():
    worker = _load_worker_module()

    class State:
        async def bitable_group_mode_for_prompt(self, **kwargs):
            return {
                "om_prompt_lark": "lark",
                "om_prompt_feishu": "feishu",
            }.get(kwargs["prompt_message_id"])

        async def bitable_group_mode(self, **kwargs):
            # The latest per-user choice is deliberately the wrong value for
            # the first prompt; threaded prompt binding must win.
            return "feishu"

    relay = worker.CloudflareRelay(SimpleNamespace(), None, State())
    event = {
        "chat_id": "oc_group",
        "open_id": "ou_requester",
        "parent_id": "om_prompt_lark",
    }
    assert asyncio.run(
        relay._bitable_document_mode(source_platform="feishu", event=event)
    ) == "lark"

    event["parent_id"] = "om_prompt_feishu"
    assert asyncio.run(
        relay._bitable_document_mode(source_platform="feishu", event=event)
    ) == "feishu"


def test_external_sender_seen_by_feishu_is_routed_to_lark_oauth():
    worker = _load_worker_module()

    class Relay:
        env = type("Env", (), {"LARK_APP_ID": "cli_lark", "LARK_APP_SECRET": "secret"})()
        lark = object()

    relay = Relay()
    # The detector only needs the configured Lark client; no API request is
    # made in the webhook path.
    workflow = worker.CloudflareRelay.detect_user_platform
    assert asyncio.run(
        workflow(
            relay,
            {
                "tenant_key": "tenant_feishu",
                "sender_tenant_key": "tenant_lark",
            },
            "feishu",
        )
    ) == "lark"


def test_bitable_group_workflow_builds_platform_specific_authorization_link():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self):
            self.pending = None

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            return None

        async def save_bitable_pending(self, **kwargs):
            self.pending = kwargs

    class FakeAPI:
        def __init__(self):
            self.replies = []
            self.card_replies = []

        async def reply(self, message_id, text):
            self.replies.append((message_id, text))
            return "om_reply"

        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            self.card_replies.append((chat_id, open_id, card))
            return "om_card_reply"

    class Relay:
        env = type("Env", (), {"FEISHU_APP_ID": "cli_feishu"})()

        def __init__(self):
            self.state = FakeState()
            self.api = FakeAPI()

        def base_url(self):
            return "https://bot.boooe.com"

        def platform_oauth_scope(self, platform):
            return "bitable:app wiki:wiki:readonly"

        def feishu_oauth_ttl(self, platform):
            return 600

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    workflow = worker.BitableGroupWorkflow(relay)
    asyncio.run(
        workflow.handle_event(
            platform="feishu",
            conversation_key="feishu:chat:1",
            event={
                "message_id": "om_source",
                "chat_id": "oc_5e9132f3638772d53d92d6fc5e953abc",
                "open_id": "ou_requester",
                "text": "测试任务",
            },
        )
    )
    assert relay.state.pending["platform"] == "feishu"
    assert relay.state.pending["requester_open_id"] == "ou_requester"
    assert relay.api.card_replies[0][0:2] == (
        "oc_5e9132f3638772d53d92d6fc5e953abc",
        "ou_requester",
    )
    card = relay.api.card_replies[0][2]
    assert card["elements"][1]["actions"][0]["text"]["content"] == "授权并继续"
    auth_url = card["elements"][1]["actions"][0]["url"]
    assert "accounts.feishu.cn/open-apis/authen/v1/authorize" in auth_url
    assert "app_id=cli_feishu" in auth_url


def test_bitable_group_workflow_auth_card_uses_lark_authorization_url():
    worker = _load_worker_module()

    class FakeState:
        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            return None

        async def save_bitable_pending(self, **kwargs):
            self.pending = kwargs

    class FakeAPI:
        def __init__(self):
            self.card_replies = []

        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            self.card_replies.append((chat_id, open_id, card))
            return "om_card_reply"

    class Relay:
        env = type("Env", (), {"LARK_APP_ID": "cli_lark"})()

        def __init__(self):
            self.state = FakeState()
            self.api = FakeAPI()

        def base_url(self):
            return "https://bot.boooe.com"

        def platform_oauth_scope(self, platform):
            return "bitable:app wiki:wiki:readonly"

        def feishu_oauth_ttl(self, platform):
            return 600

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    asyncio.run(
        worker.BitableGroupWorkflow(relay).handle_event(
            platform="lark",
            source_platform="feishu",
            conversation_key="feishu:chat:1",
            event={
                "message_id": "om_source",
                "chat_id": "oc_5e9132f3638772d53d92d6fc5e953abc",
                "open_id": "ou_requester",
                "text": "测试任务",
            },
        )
    )
    assert relay.api.card_replies[0][0:2] == (
        "oc_5e9132f3638772d53d92d6fc5e953abc",
        "ou_requester",
    )
    card = relay.api.card_replies[0][2]
    action = card["elements"][1]["actions"][0]
    assert "accounts.larksuite.com/open-apis/authen/v1/authorize" in action["url"]
    assert "app_id=cli_lark" in action["url"]
    assert "授权平台：**Lark**" in card["elements"][0]["text"]["content"]


def test_lark_document_selection_keeps_feishu_requester_on_feishu_oauth():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self):
            self.pending = None
            self.token_lookup = None

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            self.token_lookup = (platform, open_id)
            return None

        async def save_bitable_pending(self, **kwargs):
            self.pending = kwargs

    class FakeAPI:
        def __init__(self):
            self.card_replies = []

        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            self.card_replies.append((chat_id, open_id, card))
            return "om_card_reply"

    class Relay:
        env = type("Env", (), {"FEISHU_APP_ID": "cli_feishu"})()

        def __init__(self):
            self.state = FakeState()
            self.api = FakeAPI()

        def base_url(self):
            return "https://bot.boooe.com"

        def platform_oauth_scope(self, platform):
            return "bitable:app wiki:wiki:readonly"

        def feishu_oauth_ttl(self, platform):
            return 600

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    asyncio.run(
        worker.BitableGroupWorkflow(relay).handle_event(
            # The requester is Feishu; only the destination document is Lark.
            platform="feishu",
            document_mode="lark",
            source_platform="feishu",
            conversation_key="feishu:chat:1",
            event={
                "message_id": "om_source",
                "chat_id": "oc_5e9132f3638772d53d92d6fc5e953abc",
                "open_id": "ou_requester",
                "text": "测试任务",
            },
        )
    )

    assert relay.state.token_lookup == ("feishu", "ou_requester")
    assert relay.state.pending["platform"] == "feishu"
    assert relay.state.pending["document_mode"] == "lark"
    card = relay.api.card_replies[0][2]
    auth_url = card["elements"][1]["actions"][0]["url"]
    assert "accounts.feishu.cn/open-apis/authen/v1/authorize" in auth_url
    assert "accounts.larksuite.com" not in auth_url
    assert "授权平台：**Feishu**" in card["elements"][0]["text"]["content"]


def test_pending_authorization_sends_one_card_and_queues_later_requests():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self):
            self.pending = None
            self.items = []

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            return None

        async def save_bitable_pending(self, **kwargs):
            self.pending = dict(kwargs)

        async def bitable_pending_authorization(self, *, platform, requester_open_id):
            if (
                self.pending
                and self.pending["platform"] == platform
                and self.pending["requester_open_id"] == requester_open_id
            ):
                return self.pending
            return None

        async def append_bitable_pending_item(self, *, state, item):
            assert self.pending and self.pending["state"] == state
            self.items.append(dict(item))

    class FakeAPI:
        def __init__(self):
            self.card_replies = []

        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            self.card_replies.append((chat_id, open_id, card))
            return "om_card_reply"

    class Relay:
        env = type("Env", (), {"LARK_APP_ID": "cli_lark"})()

        def __init__(self):
            self.state = FakeState()
            self.api = FakeAPI()

        def base_url(self):
            return "https://bot.boooe.com"

        def platform_oauth_scope(self, platform):
            return "bitable:app wiki:wiki:readonly"

        def feishu_oauth_ttl(self, platform):
            return 600

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    workflow = worker.BitableGroupWorkflow(relay)
    common = {
        "chat_id": worker.BITABLE_WORKFLOW_GROUP_CHAT_ID,
        "open_id": "ou_requester",
    }
    asyncio.run(
        workflow.handle_event(
            platform="lark",
            document_mode="lark",
            source_platform="feishu",
            conversation_key="feishu:chat:1",
            event={**common, "message_id": "om_lark", "text": "Lark请求"},
        )
    )
    asyncio.run(
        workflow.handle_event(
            platform="lark",
            document_mode="feishu",
            source_platform="feishu",
            conversation_key="feishu:chat:1",
            event={**common, "message_id": "om_feishu", "text": "飞书请求"},
        )
    )

    assert len(relay.api.card_replies) == 1
    assert [item["document_mode"] for item in relay.state.items] == [
        "lark",
        "feishu",
    ]
    assert [item["source_message_id"] for item in relay.state.items] == [
        "om_lark",
        "om_feishu",
    ]


def test_complete_oauth_processes_all_queued_document_requests_once():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self):
            self.saved_tokens = []

        async def save_user_token(self, **kwargs):
            self.saved_tokens.append(kwargs)

    class FakeAPI:
        def __init__(self):
            self.records = []
            self.replies = []

        async def user_info(self, access_token):
            return {"open_id": "ou_lark_authorized"}

        async def resolve_wiki_bitable_app_token(self, access_token, wiki_token):
            return f"app:{wiki_token}"

        async def user_bitable_fields(self, access_token, *, app_token, table_id):
            if table_id == worker.LARK_DOCUMENT_TABLE_ID:
                return [{"field_name": "文本"}, {"field_name": "测试3"}]
            return [{"field_name": "任务描述"}, {"field_name": "任务执行人"}]

        async def create_user_bitable_record(
            self, access_token, *, app_token, table_id, fields
        ):
            record_id = f"rec_{len(self.records) + 1}"
            self.records.append((table_id, fields))
            return {"record_id": record_id}

        async def reply(self, message_id, text):
            self.replies.append((message_id, text))
            return "om_reply"

    class Relay:
        def __init__(self):
            self.state = FakeState()
            self.api = FakeAPI()

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    pending = {
        "platform": "lark",
        "source_platform": "feishu",
        "requester_open_id": "ou_external_projection",
        "pending_items_json": json.dumps(
            [
                {
                    "platform": "lark",
                    "document_mode": "lark",
                    "source_platform": "feishu",
                    "source_message_id": "om_lark",
                    "requester_open_id": "ou_external_projection",
                    "input_text": "Lark内容",
                },
                {
                    "platform": "lark",
                    "document_mode": "feishu",
                    "source_platform": "feishu",
                    "source_message_id": "om_feishu",
                    "requester_open_id": "ou_external_projection",
                    "input_text": "飞书内容",
                },
            ]
        ),
    }

    result = asyncio.run(
        worker.BitableGroupWorkflow(relay).complete_oauth(
            pending, {"access_token": "lark-token", "expires_in": 7200}
        )
    )

    assert result["processed_count"] == 2
    assert result["failed_count"] == 0
    assert [table_id for table_id, fields in relay.api.records] == [
        worker.LARK_DOCUMENT_TABLE_ID,
        worker.BITABLE_WORKFLOW_TABLE_ID,
    ]
    assert [message_id for message_id, text in relay.api.replies] == [
        "om_lark",
        "om_feishu",
    ]
    assert len(relay.state.saved_tokens) == 1


def test_cross_platform_oauth_uses_lark_identity_for_bitable_assignee():
    worker = _load_worker_module()

    class FakeAPI:
        async def user_info(self, access_token):
            assert access_token == "lark-user-token"
            return {"open_id": "ou_lark_user", "union_id": "on_lark_user"}

        async def resolve_wiki_bitable_app_token(self, access_token, wiki_token):
            return "app_token"

        async def user_bitable_fields(self, access_token, *, app_token, table_id):
            return [{"field_name": "任务描述"}, {"field_name": "任务执行人"}]

        async def create_user_bitable_record(self, access_token, *, app_token, table_id, fields):
            self.fields = fields
            return {"record_id": "rec_lark"}

    class Relay:
        def __init__(self):
            self.api = FakeAPI()

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    workflow = worker.BitableGroupWorkflow(relay)
    result = asyncio.run(
        workflow._write_row(
            platform="lark",
            source_platform="feishu",
            requester_open_id="ou_feishu_external_projection",
            text="测试任务",
            access_token="lark-user-token",
        )
    )

    assert result["record_id"] == "rec_lark"
    assert relay.api.fields["任务执行人"] == [{"id": "ou_lark_user"}]


def test_lark_document_writes_text_and_user_to_lark_target_fields():
    worker = _load_worker_module()

    class FakeAPI:
        async def user_info(self, access_token):
            return {"open_id": "ou_lark_user"}

        async def resolve_wiki_bitable_app_token(self, access_token, wiki_token):
            self.wiki_token = wiki_token
            return "lark_app_token"

        async def user_bitable_fields(self, access_token, *, app_token, table_id):
            self.table_id = table_id
            return [{"field_name": "文本"}, {"field_name": "测试3"}]

        async def create_user_bitable_record(self, access_token, *, app_token, table_id, fields):
            self.fields = fields
            return {"record_id": "rec_lark_document"}

    class Relay:
        env = SimpleNamespace()

        def __init__(self):
            self.api = FakeAPI()

        def api_for_conversation(self, key):
            return self.api

    relay = Relay()
    result = asyncio.run(
        worker.BitableGroupWorkflow(relay)._write_row(
            platform="lark",
            source_platform="feishu",
            document_mode="lark",
            requester_open_id="ou_external_projection",
            text="lark 文档内容",
            access_token="lark-user-token",
        )
    )

    assert result["record_id"] == "rec_lark_document"
    assert relay.api.wiki_token == worker.LARK_DOCUMENT_WIKI_TOKEN
    assert relay.api.table_id == worker.LARK_DOCUMENT_TABLE_ID
    assert relay.api.fields == {"文本": "lark 文档内容", "测试3": [{"id": "ou_lark_user"}]}


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
    assert "https://bot.boooe.com/mcp" in input_text
    assert "record_result" in input_text
    assert "The user's original request is never confirmation" in input_text
    assert "确认创建/确认修改/确认删除/确认取消" in input_text
    assert "never reject or withhold a successfully rendered image" in input_text


def test_non_target_groups_keep_the_agent_relay_workflow_available():
    worker = _load_worker_module()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, SimpleNamespace())

    assert isinstance(relay.agent_workflow, worker.AgentRelayWorkflow)


def test_agent_input_tells_agent_how_to_use_attached_image():
    worker = _load_worker_module()
    input_text = worker._conversation_input(
        request_id="req_1",
        conversation_key="feishu:app:chat:1",
        text="将群头像改成这个",
        continuation=True,
        image_keys=["img_v3_rocket"],
    )

    assert "set_avatar_from_stored=true" in input_text
    assert "get_user_images" in input_text


def test_cloudflare_mcp_returns_user_images_as_mcp_image_content():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "conversation_key": "feishu:app:chat:1",
                "source_message_id": "om_source",
                "image_keys": ["img_v1_user"],
            }

    class FakeFeishu:
        async def download_image(self, message_id, image_key):
            assert (message_id, image_key) == ("om_source", "img_v1_user")
            return b"\x89PNG\r\n\x1a\nuser-image"

    relay = worker.CloudflareRelay(SimpleNamespace(), None, FakeState())
    relay.feishu = FakeFeishu()
    result = asyncio.run(
        relay.call_tool(
            "get_user_images",
            {
                "request_id": "req_1",
                "conversation_key": "feishu:app:chat:1",
            },
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"]["images"][0]["mime_type"] == "image/png"
    assert result["content"][1]["type"] == "image"
    assert result["content"][1]["mimeType"] == "image/png"


def test_cloudflare_mcp_sends_agent_image_as_a_message_reply():
    worker = _load_worker_module()
    image = b"\x89PNG\r\n\x1a\nagent-image"

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "conversation_key": "feishu:app:chat:1",
                "source_message_id": "om_source",
            }

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        async def upload_message_image(self, data):
            assert data == image
            return "img_v1_outbound"

        async def reply_image(self, message_id, image_key):
            assert (message_id, image_key) == ("om_source", "img_v1_outbound")
            return "om_image_reply"

    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()
    result = asyncio.run(
        relay.call_tool(
            "send_image",
            {
                "request_id": "req_1",
                "conversation_key": "feishu:app:chat:1",
                "image_base64": base64.b64encode(image).decode("ascii"),
            },
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"]["message_id"] == "om_image_reply"
    assert state.saved == ("om_image_reply", "feishu:app:chat:1")


def test_record_result_forwards_structured_agent_image_before_text_result():
    worker = _load_worker_module()
    image = b"\x89PNG\r\n\x1a\nagent-image"

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "conversation_key": "feishu:app:chat:1",
                "source_message_id": "om_source",
            }

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        async def upload_message_image(self, data):
            assert data == image
            return "img_v1_outbound"

        async def reply_image(self, message_id, image_key):
            assert (message_id, image_key) == ("om_source", "img_v1_outbound")
            return "om_image_reply"

    queue = FakeQueue()
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(AGENT_QUEUE=queue), None, state)
    relay.feishu = FakeFeishu()
    result = asyncio.run(
        relay.call_tool(
            "record_result",
            {
                "request_id": "req_1",
                "conversation_key": "feishu:app:chat:1",
                "status": "done",
                "title": "预览已发送",
                "markdown": "请确认创建。",
                "images": [
                    {"image_base64": base64.b64encode(image).decode("ascii")}
                ],
            },
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"]["images_sent"][0]["message_id"] == "om_image_reply"
    assert state.updated[0] == "req_1"
    assert state.updated[1]["status"] == "done"
    assert queue.messages == [{"kind": "deliver_result", "request_id": "req_1"}]


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


def test_ask_user_queues_a_question_without_closing_the_run():
    worker = _load_worker_module()

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    class FakeState:
        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "conversation_key": "feishu:app:chat:x",
            }

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

    queue = FakeQueue()
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(AGENT_QUEUE=queue), None, state)

    result = asyncio.run(
        relay.call_tool(
            "ask_user",
            {
                "request_id": "req_1",
                "conversation_key": "feishu:app:chat:x",
                "question": "请选择日历",
                "choices": ["工作", "个人"],
            },
        )
    )

    assert result["isError"] is False
    assert state.updated == (
        "req_1",
        {
            "status": "needs_user",
            "progress_message": "请选择日历\n可选项：\n- 工作\n- 个人",
        },
    )
    assert queue.messages == [{"kind": "deliver_question", "request_id": "req_1"}]


def test_question_delivery_updates_placeholder_and_keeps_run_resumable():
    worker = _load_worker_module()

    class FakeState:
        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "needs_user",
                "progress_message": "请确认创建日程？",
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.updates = []

        async def update(self, message_id, text):
            self.updates.append((message_id, text))

    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_question("req_1"))

    assert relay.feishu.updates == [("om_placeholder", "请确认创建日程？")]
    assert state.saved == ("om_placeholder", "feishu:app:chat:x")


def test_question_delivery_falls_back_to_a_reply_and_rebinds_placeholder():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self):
            self.updated = []

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "needs_user",
                "progress_message": "请确认创建日程？",
                "conversation_key": "feishu:app:chat:x",
            }

        async def update_run(self, request_id, **fields):
            self.updated.append((request_id, fields))

        async def save_reply(self, outbound_id, conversation_key):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []

        async def update(self, message_id, text):
            raise RuntimeError("Feishu update temporarily failed")

        async def reply(self, message_id, text):
            self.replies.append((message_id, text))
            return "om_question"

    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_question("req_1"))

    assert relay.feishu.replies == [("om_source", "请确认创建日程？")]
    assert state.updated == [("req_1", {"placeholder_message_id": "om_question"})]
    assert state.saved == ("om_question", "feishu:app:chat:x")


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


def test_feishu_add_group_members_uses_open_id_chat_member_api():
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
                    "pending_approval_id_list": ["ou_pending"],
                },
            }

    api = FakeFeishu()
    result = asyncio.run(
        api.add_group_members("oc_existing", ["ou_a", "ou_pending", "ou_a"])
    )

    assert result == {
        "chat_id": "oc_existing",
        "requested_member_open_ids": ["ou_a", "ou_pending"],
        "invalid_id_list": [],
        "not_existed_id_list": [],
        "pending_approval_id_list": ["ou_pending"],
        "added_member_open_ids": ["ou_a"],
    }
    assert api.call == (
        "POST",
        "/open-apis/im/v1/chats/oc_existing/members",
        {
            "params": {"member_id_type": "open_id", "succeed_type": 2},
            "json": {"id_list": ["ou_a", "ou_pending"]},
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
        "get_group_info",
        "get_group_status",
        "get_stored_image",
        "get_user_images",
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
            "update_group",
            "send_image",
        }
    )


def test_cloudflare_feishu_update_chat_sends_supported_fields():
    worker = _load_worker_module()

    class FakeFeishu(worker.FeishuAPI):
        def __init__(self):
            super().__init__(SimpleNamespace())
            self.call = None

        async def _request(self, method, path, **kwargs):
            self.call = (method, path, kwargs)
            return {"code": 0, "data": {}}

    api = FakeFeishu()
    asyncio.run(
        api.update_chat(
            "oc_group",
            {
                "name": "文帅_测试_0",
                "description": "测试群",
                "add_member_permission": "only_owner",
                "share_card_permission": "not_allowed",
            },
        )
    )
    assert api.call == (
        "PUT",
        "/open-apis/im/v1/chats/oc_group",
        {
            "params": {"user_id_type": "open_id"},
            "json": {
                "name": "文帅_测试_0",
                "description": "测试群",
                "add_member_permission": "only_owner",
                "share_card_permission": "not_allowed",
            },
        },
    )


def test_cloudflare_mcp_dispatches_update_group():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def requester(self, conversation_key):
            return {"group_chat_id": "oc_persisted"}

    class FakeFeishu:
        def __init__(self):
            self.call = None

        async def update_chat(self, chat_id, changes):
            self.call = (chat_id, changes)

    state = FakeState()
    feishu = FakeFeishu()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = feishu
    result = asyncio.run(
        relay.call_tool(
            "update_group",
            {
                "conversation_key": "feishu:app:chat:1",
                "name": "文帅_测试_0",
                "add_member_permission": "only_owner",
                "share_card_permission": "not_allowed",
            },
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"] == {
        "success": True,
        "chat_id": "oc_persisted",
        "updated_fields": [
            "add_member_permission",
            "name",
            "share_card_permission",
        ],
    }
    assert feishu.call == (
        "oc_persisted",
        {
            "name": "文帅_测试_0",
            "add_member_permission": "only_owner",
            "share_card_permission": "not_allowed",
        },
    )


def test_cloudflare_mcp_updates_group_avatar_from_latest_image_without_r2():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def requester(self, conversation_key):
            return {"group_chat_id": "oc_persisted"}

        async def latest_image_run(self, conversation_key):
            return {
                "source_message_id": "om_image",
                "image_keys": ["img_v3_rocket"],
            }

    class FakeFeishu:
        async def download_image(self, message_id, image_key):
            assert (message_id, image_key) == ("om_image", "img_v3_rocket")
            return b"image-bytes"

        async def upload_avatar_image(self, data):
            assert data == b"image-bytes"
            return "img_avatar"

        async def update_chat(self, chat_id, changes):
            self.call = (chat_id, changes)

    state = FakeState()
    feishu = FakeFeishu()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = feishu
    result = asyncio.run(
        relay.call_tool(
            "update_group",
            {
                "conversation_key": "feishu:app:chat:1",
                "set_avatar_from_stored": True,
            },
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"]["updated_fields"] == ["avatar_image_key"]
    assert feishu.call == ("oc_persisted", {"avatar_image_key": "img_avatar"})


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
                "member_open_ids": ["ou_member", ""],
            },
        )
    )

    assert "create_group" in names
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "success": True,
        "conversation_key": "feishu:app:chat:1",
        "chat_id": "oc_group",
        "member_open_ids": ["ou_member"],
    }
    assert feishu.call == ("项目群", ["ou_member"])
    assert state.bound == ("feishu:app:chat:1", "oc_group")


def test_cloudflare_mcp_exposes_and_dispatches_add_group_members():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def requester(self, conversation_key):
            assert conversation_key == "feishu:app:chat:1"
            return {"group_chat_id": "oc_existing"}

    class FakeFeishu:
        async def add_group_members(self, chat_id, member_open_ids):
            assert chat_id == "oc_existing"
            assert member_open_ids == ["ou_target"]
            return {
                "chat_id": chat_id,
                "requested_member_open_ids": member_open_ids,
                "invalid_id_list": [],
                "not_existed_id_list": [],
                "pending_approval_id_list": [],
                "added_member_open_ids": member_open_ids,
            }

    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()
    names = {tool["name"] for tool in relay.tool_definitions()}
    result = asyncio.run(
        relay.call_tool(
            "add_group_members",
            {
                "conversation_key": "feishu:app:chat:1",
                "member_open_ids": ["ou_target"],
            },
        )
    )

    assert "add_group_members" in names
    assert result["isError"] is False
    assert result["structuredContent"]["success"] is True
    assert result["structuredContent"]["chat_id"] == "oc_existing"
    assert result["structuredContent"]["added_member_open_ids"] == ["ou_target"]


def test_cloudflare_feishu_oauth_authorize_persists_state(monkeypatch):
    worker = _load_worker_module()

    class FakeState:
        async def ensure_feishu_oauth_schema(self):
            pass

        async def save_feishu_oauth_state(self, state, redirect_uri, expires_at):
            self.saved = (state, redirect_uri, expires_at)

    env = SimpleNamespace(
        FEISHU_APP_ID="cli_test",
        FEISHU_API_BASE="https://open.feishu.cn",
        FEISHU_OAUTH_SCOPE="bitable:app",
        FEISHU_OAUTH_STATE_TTL_SECONDS="600",
    )
    state = FakeState()
    relay = worker.CloudflareRelay(env, None, state)
    monkeypatch.setattr(
        worker,
        "_text_response",
        lambda body, status=200, headers=None: {
            "body": body,
            "status": status,
            "headers": headers or {},
        },
    )

    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/feishu/oauth/authorize",
    )
    result = asyncio.run(relay.feishu_oauth(request, "/feishu/oauth/authorize"))

    assert result["status"] == 302
    assert result["headers"]["Location"].startswith(
        "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
    )
    assert "app_id=cli_test" in result["headers"]["Location"]
    assert state.saved[1] == "https://bot.boooe.com/feishu/oauth/callback"


def test_cloudflare_routes_lark_conversations_and_oauth_to_lark(monkeypatch):
    worker = _load_worker_module()

    class FakeState:
        async def ensure_feishu_oauth_schema(self):
            pass

        async def save_feishu_oauth_state(self, state, redirect_uri, expires_at):
            self.saved = (state, redirect_uri, expires_at)

    env = SimpleNamespace(
        FEISHU_APP_ID="cli_feishu",
        FEISHU_APP_SECRET="feishu-secret",
        LARK_APP_ID="cli_lark",
        LARK_APP_SECRET="lark-secret",
        LARK_OAUTH_SCOPE="bitable:app",
        LARK_OAUTH_STATE_TTL_SECONDS="600",
    )
    state = FakeState()
    relay = worker.CloudflareRelay(env, None, state)

    assert relay.lark is not None
    assert relay.api_for_conversation("lark:cli_lark:oc_chat:1") is relay.lark
    assert relay.api_for_conversation("feishu:cli_feishu:oc_chat:1") is relay.feishu

    monkeypatch.setattr(
        worker,
        "_text_response",
        lambda body, status=200, headers=None: {
            "body": body,
            "status": status,
            "headers": headers or {},
        },
    )
    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/lark/oauth/authorize",
    )
    result = asyncio.run(relay.feishu_oauth(request, "/lark/oauth/authorize", "lark"))

    assert result["status"] == 302
    assert result["headers"]["Location"].startswith(
        "https://accounts.larksuite.com/open-apis/authen/v1/authorize?"
    )
    assert "app_id=cli_lark" in result["headers"]["Location"]
    assert state.saved[1] == "https://bot.boooe.com/lark/oauth/callback"


def test_cloudflare_feishu_oauth_callback_exchanges_code(monkeypatch):
    worker = _load_worker_module()

    class FakeState:
        async def ensure_feishu_oauth_schema(self):
            pass

        async def consume_feishu_oauth_state(self, state):
            assert state == "feishu_state_1"
            return {
                "state": state,
                "redirect_uri": "https://example.com/callback",
                "expires_at": worker._now() + 300,
            }

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "code": 0,
                "access_token": "u-xxx",
                "refresh_token": "r-xxx",
                "scope": "bitable:app",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            self.call = (url, kwargs)
            return FakeResponse()

    client = FakeClient()
    env = SimpleNamespace(
        FEISHU_APP_ID="cli_test",
        FEISHU_APP_SECRET="secret",
        FEISHU_API_BASE="https://open.feishu.cn",
    )
    relay = worker.CloudflareRelay(env, None, FakeState())
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda timeout: client)
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: {
            "payload": payload,
            "status": status,
            "headers": headers or {},
        },
    )

    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/feishu/oauth/callback?code=code-1&state=feishu_state_1",
    )
    result = asyncio.run(relay.feishu_oauth(request, "/feishu/oauth/callback"))

    assert result["status"] == 200
    assert result["payload"]["success"] is True
    assert result["payload"]["token"]["access_token"] == "u-xxx"
    assert client.call[0] == "https://accounts.feishu.cn/oauth/v3/token"
    assert client.call[1]["data"]["client_secret"] == "secret"
    assert client.call[1]["data"]["redirect_uri"] == "https://example.com/callback"
