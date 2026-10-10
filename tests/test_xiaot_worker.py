from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]


def _load_xiaot_module():
    workers = types.ModuleType("workers")

    class Response:
        pass

    class WorkerEntrypoint:
        pass

    workers.Response = Response
    workers.WorkerEntrypoint = WorkerEntrypoint
    sys.modules.setdefault("workers", workers)
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        httpx = types.ModuleType("httpx")
        sys.modules["httpx"] = httpx

    app_spec = importlib.util.spec_from_file_location(
        "worker_app", ROOT / "cloudflare_worker/src/worker_app.py"
    )
    assert app_spec and app_spec.loader
    app = importlib.util.module_from_spec(app_spec)
    sys.modules["worker_app"] = app
    app_spec.loader.exec_module(app)

    spec = importlib.util.spec_from_file_location(
        "xiaot_worker", ROOT / "cloudflare_worker/src/xiaot_worker.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["xiaot_worker"] = module
    spec.loader.exec_module(module)
    return module


def test_xiaot_and_xiaoc_agent_workflows_include_explicit_quoted_images(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]

    async def active_rules_for_agent(_db, _agent):
        return []

    async def active_shared_confirmation_policy(_db):
        return {}

    config_module = types.ModuleType("agent_config_mcp")
    config_module.active_rules_for_agent = active_rules_for_agent
    config_module.active_shared_confirmation_policy = active_shared_confirmation_policy
    config_module.managed_rules_prompt = lambda *_args: ""
    monkeypatch.setitem(sys.modules, "agent_config_mcp", config_module)

    body = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_request",
                "chat_id": "oc_group",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_bot 识别这张图"}),
                "mentions": [{"key": "@_user_bot", "id": {"open_id": "ou_bot"}}],
                "parent_id": "om_quoted_image",
            },
        },
    }
    event = app._normalize_event(body, "ou_bot")
    assert event is not None

    class FakeAPI:
        async def get_message(self, message_id):
            assert message_id == "om_quoted_image"
            return {
                "message_id": message_id,
                "chat_id": "oc_group",
                "msg_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "title": "票据",
                            "content": [[{"tag": "img", "image_key": "img_ticket"}]],
                        }
                    }
                ),
            }

    class FakeState:
        db = object()

        def __init__(self):
            self.run = None

        async def previous_run_exists(self, _conversation_key):
            return False

        async def user_memories(self, *_args):
            return []

        async def create_run(self, **kwargs):
            self.run = kwargs

    for agent_kind in ("xiaoc", "xiaot"):
        state = FakeState()

        class FakeRelay:
            env = SimpleNamespace(FEISHU_APP_ID="app", WORKSPACE_AGENT_RELAY_MCP_NAME="relay-dev")

            def __init__(self, state):
                self.state = state

            def api_for_conversation(self, _conversation_key):
                return FakeAPI()

            def memory_scope(self):
                return "xiaoc"

            def mcp_name(self):
                return "relay-dev"

            def base_url(self):
                return "https://bot.example.test"

            async def _enqueue(self, _body):
                return None

        relay = FakeRelay(state)
        if agent_kind == "xiaoc":
            workflow = app.AgentRelayWorkflow(relay)
        else:
            workflow = worker.XiaotAgentRelayWorkflow(relay)
        asyncio.run(
            workflow.handle_event(
                platform="feishu",
                conversation_key=f"{agent_kind}:app:oc_group:conversation",
                event=event,
                request_id=f"{agent_kind}_request",
            )
        )

        assert state.run is not None
        assert state.run["image_refs"] == [
            {"message_id": "om_quoted_image", "file_key": "img_ticket"}
        ]
        assert "票据" in state.run["input_markdown"]
        assert "get_user_images" in state.run["input_markdown"]


def test_xiaot_and_xiaoc_continuations_inherit_images_from_the_replied_agent_run(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]

    async def active_rules_for_agent(_db, _agent):
        return []

    async def active_shared_confirmation_policy(_db):
        return {}

    config_module = types.ModuleType("agent_config_mcp")
    config_module.active_rules_for_agent = active_rules_for_agent
    config_module.active_shared_confirmation_policy = active_shared_confirmation_policy
    config_module.managed_rules_prompt = lambda *_args: ""
    monkeypatch.setitem(sys.modules, "agent_config_mcp", config_module)

    event = {
        "message_id": "om_followup",
        "chat_id": "oc_group",
        "open_id": "ou_user",
        "parent_id": "om_bot_question",
        "quoted_message_id": "om_bot_question",
        "mentioned_bot": True,
        "text": "准备日历事件提案",
        "image_keys": [],
        "image_refs": [],
    }

    class FakeAPI:
        async def get_message(self, message_id):
            assert message_id == "om_bot_question"
            return {
                "message_id": message_id,
                "chat_id": "oc_group",
                "msg_type": "text",
                "content": json.dumps({"text": "我已收到这张图片。你希望我做哪一项？"}),
            }

    for agent_kind in ("xiaoc", "xiaot"):
        conversation_key = f"{agent_kind}:app:oc_group:conversation"
        image_refs = [
            {"message_id": "om_original_image", "file_key": "img_ticket"}
        ]

        class FakeState:
            db = object()

            def __init__(self):
                self.run = None

            async def previous_run_exists(self, _conversation_key):
                return True

            async def reply_context(self, message_id):
                assert message_id == "om_bot_question"
                return {
                    "conversation_key": conversation_key,
                    "request_id": "prior_request",
                }

            async def get_run(self, request_id):
                assert request_id == "prior_request"
                return {
                    "conversation_key": conversation_key,
                    "image_refs": image_refs,
                }

            async def user_memories(self, *_args):
                return []

            async def create_run(self, **kwargs):
                self.run = kwargs

        state = FakeState()

        class FakeRelay:
            env = SimpleNamespace(
                FEISHU_APP_ID="app",
                WORKSPACE_AGENT_RELAY_MCP_NAME="relay-dev",
            )

            def __init__(self):
                self.state = state

            def api_for_conversation(self, _conversation_key):
                return FakeAPI()

            def memory_scope(self):
                return "xiaoc"

            def mcp_name(self):
                return "relay-dev"

            def base_url(self):
                return "https://bot.example.test"

            async def _enqueue(self, _body):
                return None

        relay = FakeRelay()
        workflow = (
            app.AgentRelayWorkflow(relay)
            if agent_kind == "xiaoc"
            else worker.XiaotAgentRelayWorkflow(relay)
        )
        asyncio.run(
            workflow.handle_event(
                platform="feishu",
                conversation_key=conversation_key,
                event=dict(event),
                request_id=f"{agent_kind}_followup",
            )
        )

        assert state.run is not None
        assert state.run["image_refs"] == image_refs
        assert state.run["image_keys"] == ["img_ticket"]
        assert "get_user_images" in state.run["input_markdown"]
        assert "continues the exact Agent response" in state.run["input_markdown"]


def test_replied_run_image_inheritance_rejects_a_different_conversation():
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]

    class FakeState:
        async def reply_context(self, _message_id):
            return {
                "conversation_key": "xiaot:app:another-chat:thread",
                "request_id": "foreign_request",
            }

        async def get_run(self, _request_id):
            raise AssertionError("must not load a run from another conversation")

    event = {
        "parent_id": "om_foreign_reply",
        "image_refs": [],
        "image_keys": [],
    }
    inherited = asyncio.run(
        app._inherit_replied_run_images(
            FakeState(), event, "xiaot:app:current-chat:thread"
        )
    )

    assert inherited is event
    assert inherited["image_refs"] == []


def test_table_allowlist_contains_only_the_seven_user_supplied_tables():
    worker = _load_xiaot_module()

    assert {item["table_id"] for item in worker.XIAOT_TABLES.values()} == {
        "tblHq7aqhe195HnD",
        "tblGljHRF28Jb3W3",
        "tblWvG10yZtqpp2A",
        "tblRSAd7V63Zp2ya",
        "tblyhk92pJBej7Vs",
        "tbl5GmADqShvAx6I",
        "tbl24pWCd60PP7GJ",
    }
    assert worker.XiaotBitableClient.resolve_table("sub-task")["table_id"] == "tblRSAd7V63Zp2ya"


def test_unknown_tables_and_malformed_record_ids_are_rejected():
    worker = _load_xiaot_module()

    try:
        worker.XiaotBitableClient.resolve_table("calendar")
    except ValueError as exc:
        assert "table_key" in str(exc)
    else:
        raise AssertionError("unknown table was accepted")

    try:
        worker.XiaotBitableClient._validate_record_id("rec123/../../other-table")
    except ValueError as exc:
        assert "record ID" in str(exc)
    else:
        raise AssertionError("malformed record id was accepted")


def test_confirmed_dev_table_extensions_do_not_replace_or_expand_the_default_allowlist():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())
    extension = {
        "blocked_task": {
            "table_id": "tblBlockedTask123",
            "name": "blocked_task",
            "description": "任务被阻塞时登记的任务表。",
        }
    }

    try:
        client.resolve_agent_table("blocked_task")
    except ValueError:
        pass
    else:
        raise AssertionError("an unconfirmed extension must not be usable")

    client.additional_tables = client.validate_additional_tables(extension)
    assert client.resolve_agent_table("blocked_task")["table_id"] == "tblBlockedTask123"
    assert client.resolve_agent_table("task")["table_id"] == "tblWvG10yZtqpp2A"
    assert len(asyncio.run(client.list_tables())) == 8

    for invalid in (
        {"task": {"table_id": "tblReplacement123", "name": "task"}},
        {"other": {"table_id": "tblWvG10yZtqpp2A", "name": "duplicate id"}},
        {"bad key!": {"table_id": "tblBlockedTask123", "name": "bad"}},
    ):
        try:
            client.validate_additional_tables(invalid)
        except ValueError:
            continue
        raise AssertionError(f"invalid extension was accepted: {invalid}")


def test_xiaot_loads_table_extensions_from_its_active_managed_rule(monkeypatch):
    worker = _load_xiaot_module()
    config = types.ModuleType("agent_config_mcp")
    expected = {
        "blocked_task": {
            "table_id": "tblBlockedTask123",
            "name": "blocked_task",
            "description": "Blocked tasks",
        }
    }

    async def active_rules(db, scope):
        assert db == "dev-db"
        assert scope == "xiaot"
        return [{"key": "bitable.additional_tables", "value": expected}]

    config.active_rules_for_agent = active_rules
    monkeypatch.setitem(sys.modules, "agent_config_mcp", config)
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db="dev-db")
    relay.xiaot_bitable = worker.XiaotBitableClient(object())

    loaded = asyncio.run(relay._load_additional_bitable_tables())

    assert loaded == expected
    relay.xiaot_bitable.additional_tables = loaded
    assert relay._resolve_bitable_table("blocked_task")["table_id"] == "tblBlockedTask123"


def test_discover_tables_uses_the_requesters_token_and_fixed_base():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())
    calls = []

    async def request(method, path, *, access_token, platform, params):
        calls.append((method, path, access_token, platform, params))
        return {
            "code": 0,
            "data": {
                "items": [{"table_id": "tblBlockedTask123", "name": "blocked_task"}],
                "has_more": False,
            },
        }

    client.request = request
    tables = asyncio.run(client.discover_tables(access_token="requester-token", platform="lark"))

    assert tables == [
        {
            "table_id": "tblBlockedTask123",
            "name": "blocked_task",
            "suggested_table_key": "blocked_task",
        }
    ]
    assert calls == [
        (
            "GET",
            f"/open-apis/bitable/v1/apps/{client.base_token}/tables",
            "requester-token",
            "lark",
            {"page_size": 100},
        )
    ]


def test_writes_reject_unknown_or_computed_fields():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token):
        assert access_token == "user-token"
        return [
            {"field_name": "Task Name", "type": 1},
            {"field_name": "Task ID", "type": 1005},
            {"field_name": "Deadline formula", "type": 20},
        ]

    client.fields = fields

    async def check():
        await client.validate_writable_fields(
            "task", {"Task Name": "test"}, access_token="user-token"
        )
        for invalid in (
            {"Not a field": "x"},
            {"Task ID": "T001"},
            {"Deadline formula": 123},
        ):
            try:
                await client.validate_writable_fields(
                    "task", invalid, access_token="user-token"
                )
            except ValueError:
                continue
            raise AssertionError(f"unsafe or unknown fields were accepted: {invalid}")

    asyncio.run(check())


def test_xiaot_uses_shared_exact_phrase_and_reaction_decisions():
    _load_xiaot_module()
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_xiaot_confirmation_test",
        ROOT / "cloudflare_worker/src/agent_config_mcp.py",
    )
    assert spec and spec.loader
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    policy = {
        "confirmation_phrases": config.DEFAULT_CONFIRMATION_PHRASES,
        "refusal_phrases": config.DEFAULT_REFUSAL_PHRASES,
        "confirmation_emojis": config.DEFAULT_CONFIRMATION_EMOJIS,
        "refusal_emojis": config.DEFAULT_REFUSAL_EMOJIS,
    }

    assert config.shared_confirmation_decision("确认创建", policy) == "confirmation"
    assert config.shared_confirmation_decision("好的", policy) == "confirmation"
    assert config.shared_confirmation_decision("确认删除", policy) == "confirmation"
    assert config.shared_confirmation_decision("不确认创建", policy) == "refusal"
    assert config.shared_confirmation_decision("不要", policy) == "refusal"
    assert config.shared_confirmation_decision("可以，然后删除另一个", policy) is None
    assert config.shared_confirmation_decision("好吧，修改成别的内容", policy) is None
    assert config.shared_confirmation_decision("", policy, reaction_emoji="YES") == "confirmation"
    assert config.shared_confirmation_decision("", policy, reaction_emoji="NO") == "refusal"


def test_xiaot_still_requires_feishu_lark_user_authorization_before_agent_work():
    worker = _load_xiaot_module()

    class FakeState:
        db = object()

        async def reply_conversation(self, _parent_id):
            return None

        async def ensure_schema(self):
            return None

        async def claim_event(self, **_fields):
            return True

        async def save_requester(self, *_fields):
            return None

        async def release_event_claim(self, _message_id):
            return None

    class FakeAgentWorkflow:
        def __init__(self):
            self.calls = []

        async def handle_event(self, **kwargs):
            self.calls.append(kwargs)

    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.env = SimpleNamespace(
        FEISHU_APP_ID="cli_dev",
        FEISHU_BOT_OPEN_ID="ou_bot",
    )
    relay.state = FakeState()
    relay.agent_workflow = FakeAgentWorkflow()
    authorization_requests = []

    async def db_run(*_args):
        return None

    async def ensure_oauth_schema():
        return None

    async def resolve_identity(_event):
        return {
            "platform": "feishu",
            "app_key": "feishu",
            "open_id": "ou_requester",
            "identity_pending": False,
        }

    async def no_user_token(**_kwargs):
        return None

    async def request_authorization(**kwargs):
        authorization_requests.append(kwargs)

    worker._db_run = db_run
    relay._ensure_xiaot_oauth_schema = ensure_oauth_schema
    relay._resolve_account_identity = resolve_identity
    relay._xiaot_user_token = no_user_token
    relay._send_user_authorization = request_authorization
    body = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_requester"},
            },
            "message": {
                "message_id": "om_auth_required",
                "chat_id": "oc_dev",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text":"@_user_bot 查任务"}',
                "mentions": [{"key": "@_user_bot", "id": {"open_id": "ou_bot"}}],
            },
        },
    }

    asyncio.run(relay._process_feishu_event(body, "xiaot"))

    assert len(authorization_requests) == 1
    assert authorization_requests[0]["account_identity"]["platform"] == "feishu"
    assert relay.agent_workflow.calls == []


def test_shared_agent_config_reaction_does_not_require_bitable_oauth_identity():
    worker = _load_xiaot_module()

    class FakeState:
        db = object()

        async def ensure_schema(self):
            pass

        async def reply_conversation(self, _parent_id):
            return "xiaot:cli_dev:oc_chat:thread"

        async def claim_event(self, **_fields):
            return True

        async def save_requester(self, *_fields):
            pass

        async def close_reply_confirmation(self, _message_id):
            pass

        async def release_event_claim(self, _message_id):
            pass

    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.env = SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot", FEISHU_APP_ID="cli_dev")
    relay.state = FakeState()
    calls = []

    class FakeWorkflow:
        async def handle_event(self, **fields):
            calls.append(fields)

    relay.agent_workflow = FakeWorkflow()

    async def should_not_resolve(_event):
        raise AssertionError("shared MCP config confirmations do not need Bitable identity lookup")

    relay._resolve_account_identity = should_not_resolve

    async def deliver_echo(_metadata, _conversation_key):
        return None

    relay._deliver_reaction_echo = deliver_echo
    body = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_reactor"}},
            "message": {
                "message_id": "om_shared_config_proposal",
                "chat_id": "oc_chat",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text":"服务器已验证这是共享配置提案的确认表情"}',
                "parent_id": "om_shared_config_proposal",
                "mentions": [],
            },
        },
        "_relay_reaction": {
            "event_key": "xiaot:reaction:evt_config",
            "target_message_id": "om_shared_config_proposal",
            "request_id": "xiaot_original_request",
            "emoji_type": "OK",
            "confirmation_pending": True,
            "decision": "confirmation",
            "agent_config_proposal": True,
            "echo_text": "用户添加了 OK",
        },
    }

    asyncio.run(relay._process_feishu_event(body, "xiaot"))

    assert len(calls) == 1
    assert calls[0]["conversation_key"] == "xiaot:cli_dev:oc_chat:thread"
    assert calls[0]["event"]["open_id"] == "ou_reactor"


def test_xiaot_requester_identity_uses_xiaot_event_namespace():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db=object())
    queries = []

    async def lookup(db, sql, *params):
        queries.append((db, sql, params))
        return {"sender_open_id": "ou_xiaot_user"}

    worker._db_first = lookup
    identity = asyncio.run(
        relay._requester_for_run("xiaot_req_1", "xiaot:cli_xiaot:oc_group:abc123")
    )

    assert identity == {"open_id": "ou_xiaot_user"}
    assert "FROM feishu_events" in queries[0][1]
    assert queries[0][2] == ("xiaot_req_1", "xiaot:cli_xiaot:oc_group:abc123")


def test_reaction_can_reuse_only_the_originating_xiaot_requesters_verified_identity():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db=object())
    queries = []

    async def lookup(db, sql, *params):
        queries.append((db, sql, params))
        return {
            "source_open_id": "ou_bot_scoped_user",
            "platform": "lark",
            "app_key": "lark_yw",
            "open_id": "ou_lark_user",
            "tenant_key": "tenant_lark",
        }

    worker._db_first = lookup
    identity = asyncio.run(
        relay._saved_requester_identity_for_reaction(
            "xiaot_req_1",
            "xiaot:cli_xiaot:oc_group:thread",
            "ou_bot_scoped_user",
        )
    )

    assert identity == {
        "platform": "lark",
        "app_key": "lark_yw",
        "open_id": "ou_lark_user",
        "tenant_key": "tenant_lark",
    }
    assert "JOIN xiaot_run_requesters" in queries[0][1]
    assert queries[0][2] == ("xiaot_req_1", "xiaot:cli_xiaot:oc_group:thread")


def test_task_and_subtask_searches_exclude_finished_unless_explicitly_requested():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    current_run = {
        "request_id": "xiaot_req_current",
        "conversation_key": "xiaot:cli_xiaot:oc_group:current",
        "input_markdown": "User task:\n查当前 task",
    }

    async def require_run(request_id, conversation_key):
        if request_id == "xiaot_req_current":
            return current_run
        raise ValueError("stale request_id")

    async def latest_run(_db, _sql, conversation_key):
        assert conversation_key == current_run["conversation_key"]
        return current_run

    async def requester_for_run(_request_id, _conversation_key):
        return {"open_id": "ou_current_user"}

    class FakeState:
        db = object()

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            assert (platform, open_id) == ("xiaot", "ou_current_user")
            return {"access_token": "user-token", "expires_at": 4_000_000_000}

    class FakeBitable:
        def __init__(self):
            self.filters = []

        async def fields(self, table_key, *, access_token):
            assert table_key == "task"
            assert access_token == "user-token"
            return [{"field_name": "Status", "type": 3}]

        async def records(self, table_key, *, access_token, page_size, page_token, filter_formula):
            assert table_key == "task"
            assert access_token == "user-token"
            self.filters.append(filter_formula)
            return {"items": [], "has_more": False, "page_token": ""}

    relay.state = FakeState()
    relay.xiaot_bitable = FakeBitable()
    relay._require_run = require_run
    relay._requester_for_run = requester_for_run
    worker._db_first = latest_run

    async def scenario():
        for include_finished in (False, True):
            await relay.call_tool(
                "search_records",
                {
                    "request_id": "stale_request_id",
                    "conversation_key": current_run["conversation_key"],
                    "table_key": "task",
                    "filter_formula": 'CurrentValue.[Owner]="Bo"',
                    "include_finished": include_finished,
                },
            )

    asyncio.run(scenario())

    assert relay.xiaot_bitable.filters == [
        'AND(CurrentValue.[Status]!="Finished", CurrentValue.[Owner]="Bo")',
        'CurrentValue.[Owner]="Bo"',
    ]


def test_bitable_mutation_is_proposal_only_until_same_sender_confirms():
    worker = _load_xiaot_module()
    config_spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_xiaot_mutation_test",
        ROOT / "cloudflare_worker/src/agent_config_mcp.py",
    )
    assert config_spec and config_spec.loader
    config = importlib.util.module_from_spec(config_spec)
    config_spec.loader.exec_module(config)
    previous_config = sys.modules.get("agent_config_mcp")
    sys.modules["agent_config_mcp"] = config

    class FakeStatement:
        def __init__(self, db, sql):
            self.db = db
            self.sql = sql
            self.params = ()

        def bind(self, *params):
            self.params = params
            return self

        async def run(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            self.db.connection.commit()
            return SimpleNamespace(meta={"changes": max(cursor.rowcount, 0)})

        async def first(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column[0] for column in cursor.description]
            return dict(zip(columns, row, strict=True))

        async def all(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            return SimpleNamespace(results=rows)

    class FakeD1:
        def __init__(self):
            self.connection = sqlite3.connect(":memory:")

        def prepare(self, sql):
            return FakeStatement(self, sql)

    class FakeBitable:
        def __init__(self):
            self.creates = []
            self.updates = []

        @staticmethod
        def resolve_table(table_key):
            return worker.XiaotBitableClient.resolve_table(table_key)

        async def validate_writable_fields(self, table_key, fields, *, access_token, platform):
            assert table_key == "task"
            assert fields in (
                {"Task Name": "Xiaot test"},
                {"Status": "In Progress"},
            )
            assert access_token == "xiaot-user-token"

        async def create_record(self, table_key, fields, *, access_token, platform):
            self.creates.append((table_key, fields))
            assert access_token == "xiaot-user-token"
            record_id = "rec_audit" if table_key == "changelog" else "rec_target"
            return {"record_id": record_id, "fields": fields}

        async def update_record(self, table_key, record_id, fields, *, access_token, platform):
            self.updates.append((table_key, record_id, fields))
            assert access_token == "xiaot-user-token"
            return {"record_id": record_id, "fields": fields}

        async def record(self, table_key, record_id, *, access_token, platform):
            assert access_token == "xiaot-user-token"
            assert table_key == "task"
            assert record_id == "rec_target"
            return {
                "record_id": record_id,
                "fields": {"Status": "Waiting/Blocked"},
            }

    async def scenario():
        state = worker.D1State(FakeD1())
        await state.ensure_schema()
        await state.ensure_feishu_oauth_schema()
        await state.save_user_token(
            platform="xiaot",
            open_id="ou_original",
            access_token="xiaot-user-token",
            refresh_token="refresh-token",
            expires_at=int(worker.time.time()) + 3600,
        )
        conversation_key = "xiaot:cli_xiaot:oc_group:proposal"
        await state.claim_event(
            message_id="xiaot:om_prepare",
            request_id="xiaot_req_prepare",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_prepare",
            conversation_key=conversation_key,
            source_message_id="om_prepare",
            input_markdown="User task:\n创建一个 task",
        )
        bitable = FakeBitable()
        relay = worker.XiaotCloudflareRelay(SimpleNamespace(), None, state)
        relay.xiaot_bitable = bitable
        await relay._ensure_xiaot_oauth_schema()

        async def save_run_requester(request_id, open_id):
            await worker._db_run(
                state.db,
                "INSERT INTO xiaot_run_requesters "
                "(request_id, conversation_key, platform, open_id, app_key, tenant_key, created_at) "
                "VALUES (?, ?, 'feishu', ?, 'feishu', '', 1)",
                request_id,
                conversation_key,
                open_id,
            )

        await save_run_requester("xiaot_req_prepare", "ou_original")
        proposal = await relay._prepare_mutation(
            {
                "request_id": "xiaot_req_prepare",
                "conversation_key": conversation_key,
                "operation": "create",
                "table_key": "task",
                "fields": {"Task Name": "Xiaot test"},
            }
        )
        assert bitable.creates == []

        await state.claim_event(
            message_id="xiaot:om_wrong_user",
            request_id="xiaot_req_wrong_user",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_other",
        )
        await state.create_run(
            request_id="xiaot_req_wrong_user",
            conversation_key=conversation_key,
            source_message_id="om_wrong_user",
            input_markdown="User task:\n确认创建",
        )
        await save_run_requester("xiaot_req_wrong_user", "ou_other")
        wrong_user = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_wrong_user",
                "conversation_key": conversation_key,
                "proposal_id": proposal["proposal_id"],
            },
        )
        assert wrong_user["isError"] is True
        assert bitable.creates == []

        await state.claim_event(
            message_id="xiaot:om_confirm",
            request_id="xiaot_req_confirm",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_confirm",
            conversation_key=conversation_key,
            source_message_id="om_confirm",
            input_markdown="User task:\n确认创建",
        )
        await save_run_requester("xiaot_req_confirm", "ou_original")
        confirmed = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_confirm",
                "conversation_key": conversation_key,
                "proposal_id": proposal["proposal_id"],
            },
        )
        await state.claim_event(
            message_id="xiaot:om_prepare_update",
            request_id="xiaot_req_prepare_update",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_prepare_update",
            conversation_key=conversation_key,
            source_message_id="om_prepare_update",
            input_markdown="User task:\n把状态改为正在执行",
        )
        await save_run_requester("xiaot_req_prepare_update", "ou_original")
        update_proposal = await relay._prepare_mutation(
            {
                "request_id": "xiaot_req_prepare_update",
                "conversation_key": conversation_key,
                "operation": "update",
                "table_key": "task",
                "record_id": "rec_target",
                "fields": {"Status": "In Progress"},
            }
        )
        await state.claim_event(
            message_id="xiaot:om_confirm_update",
            request_id="xiaot_req_confirm_update",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_confirm_update",
            conversation_key=conversation_key,
            source_message_id="om_confirm_update",
            input_markdown="User task:\n确认",
        )
        await save_run_requester("xiaot_req_confirm_update", "ou_original")
        update_confirmed = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_confirm_update",
                "conversation_key": conversation_key,
            },
        )
        return bitable, wrong_user, confirmed, update_proposal, update_confirmed

    try:
        bitable, wrong_user, confirmed, update_proposal, update_confirmed = asyncio.run(scenario())
    finally:
        if previous_config is None:
            sys.modules.pop("agent_config_mcp", None)
        else:
            sys.modules["agent_config_mcp"] = previous_config

    assert wrong_user["isError"] is True
    assert confirmed["structuredContent"]["success"] is True
    assert bitable.creates[0][0] == "changelog"
    assert bitable.creates[1] == ("task", {"Task Name": "Xiaot test"})
    assert bitable.updates[0][0] == "changelog"
    assert update_proposal["requires_explicit_user_confirmation"] is True
    assert update_confirmed["structuredContent"]["success"] is True
    assert update_confirmed["structuredContent"]["record_id"] == "rec_target"
    assert ("task", "rec_target", {"Status": "In Progress"}) in bitable.updates


def test_feishu_event_callback_requires_verification_token():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.env = SimpleNamespace(FEISHU_VERIFY_TOKEN="expected-token", FEISHU_BOT_OPEN_ID="ou_bot")
    scheduled = []

    async def schedule(body, platform):
        scheduled.append((body, platform))

    relay._schedule_feishu_event = schedule
    worker._response = lambda body, status=200, headers=None: {
        "body": body,
        "status": status,
        "headers": headers or {},
    }

    class FakeRequest:
        method = "POST"

        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    async def scenario():
        challenge = await relay.handle_xiaot_event(
            FakeRequest({"header": {"token": "expected-token"}, "challenge": "abc"})
        )
        legacy_challenge = await relay.handle_xiaot_event(
            FakeRequest(
                {
                    "type": "url_verification",
                    "token": "expected-token",
                    "challenge": "legacy-abc",
                }
            )
        )
        denied = await relay.handle_xiaot_event(
            FakeRequest({"type": "url_verification", "token": "wrong", "challenge": "denied"})
        )
        relay.env = SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot")
        unconfigured = await relay.handle_xiaot_event(
            FakeRequest({"header": {"token": "anything"}, "event": {}})
        )
        return challenge, legacy_challenge, denied, unconfigured

    challenge, legacy_challenge, denied, unconfigured = asyncio.run(scenario())

    assert challenge["body"] == {"challenge": "abc"}
    assert legacy_challenge["body"] == {"challenge": "legacy-abc"}
    assert denied["status"] == 403
    assert unconfigured["status"] == 503
    assert scheduled == []


def test_xiaot_mcp_discovery_is_mounted_at_its_dedicated_route():
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    worker._response = lambda body, status=200, headers=None: {
        "body": body,
        "status": status,
        "headers": headers or {},
    }

    class FakeState:
        def __init__(self, _db):
            pass

    app.D1State = FakeState
    app._response = worker._response
    entrypoint = object.__new__(app.Default)
    entrypoint.env = SimpleNamespace(DB=object())
    entrypoint.ctx = None

    class FakeRequest:
        method = "POST"
        url = "https://bot.boooe.com/xiaot/mcp"

        async def json(self):
            return {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}

        class Headers:
            def get(self, _name):
                return ""

        headers = Headers()

    response = asyncio.run(entrypoint.fetch(FakeRequest()))

    assert response["status"] == 200
    assert response["body"]["result"] == {
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
    }


def test_xiaot_uses_its_dedicated_mcp_name_and_resource():
    worker = _load_xiaot_module()
    env = worker.XiaotEnvironment(
        SimpleNamespace(
            WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL="https://bot.boooe.com",
            WORKSPACE_AGENT_RELAY_OAUTH_SCOPES="workspace-agent-relay",
            WORKSPACE_AGENT_RELAY_MCP_NAME="workspace-agent-relay-mcp-dev",
        )
    )
    relay = worker.XiaotCloudflareRelay(env, None, SimpleNamespace(db=object()))

    assert relay.base_url() == "https://bot.boooe.com"
    assert relay.mcp_name() == "workspace-agent-relay-mcp-xiaot-dev"
    assert relay.scopes() == ["workspace-agent-relay"]
    assert relay.mcp_endpoint() == "https://bot.boooe.com/xiaot/mcp"


def test_shared_mcp_exposes_union_and_routes_actions_by_feishu_conversation():
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    relay = object.__new__(worker.CombinedCloudflareRelay)
    relay.xiaot = SimpleNamespace(
        tool_definitions=lambda: [
            {"name": "record_plan"},
            {"name": "list_tables"},
            {"name": "search_records"},
            {"name": "prepare_mutation"},
            {"name": "confirm_mutation"},
        ],
        call_tool=lambda name, args: asyncio.sleep(0, result={"agent": "xiaot", "name": name}),
    )
    original = app.CloudflareRelay.call_tool

    async def root_call(_self, name, args):
        return {"agent": "xiaoc", "name": name, "conversation_key": args.get("conversation_key")}

    app.CloudflareRelay.call_tool = root_call
    try:
        names = {item["name"] for item in relay.tool_definitions()}
        xiaot_result = asyncio.run(
            relay.call_tool("record_plan", {"conversation_key": "xiaot:app:chat:1"})
        )
        xiaoc_result = asyncio.run(
            relay.call_tool("record_plan", {"conversation_key": "feishu:app:chat:1"})
        )
        rejected = asyncio.run(
            relay.call_tool("send_image", {"conversation_key": "xiaot:app:chat:1"})
        )
        denied_bitable = asyncio.run(relay.call_tool("list_tables", {}))
    finally:
        app.CloudflareRelay.call_tool = original

    assert "send_image" in names
    assert "list_tables" in names
    assert "confirm_mutation" in names
    assert xiaot_result == {"agent": "xiaot", "name": "record_plan"}
    assert xiaoc_result["agent"] == "xiaoc"
    assert rejected["structuredContent"]["error"]["code"] == "action_not_enabled_for_agent"
    assert denied_bitable["structuredContent"]["error"]["code"] == "wrong_agent_context"


def test_shared_queue_routes_xiaot_and_xiaoc_jobs_to_their_own_relays():
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    calls = []

    class FakeState:
        def __init__(self, _db):
            pass

        async def get_run(self, request_id):
            return {
                "conversation_key": (
                    "xiaot:app:chat:abc" if request_id.startswith("xiaot") else "feishu:app:chat"
                )
            }

        async def update_run(self, *_args, **_kwargs):
            pass

    class FakeRootRelay:
        def __init__(self, env, ctx, _state):
            self.env = env
            self.ctx = ctx

        async def _process_feishu_event(self, _body, platform):
            calls.append(("xiaoc-event", platform))

        async def run_agent_job(self, body):
            calls.append(("xiaoc-agent", body["request_id"]))

        async def deliver_result(self, request_id):
            calls.append(("xiaoc-result", request_id))

        async def deliver_question(self, request_id):
            calls.append(("xiaoc-question", request_id))

    class FakeXiaotRelay(FakeRootRelay):
        async def _process_feishu_event(self, _body, platform):
            calls.append(("xiaot-event", platform))

        async def run_agent_job(self, body):
            calls.append(("xiaot-agent", body["request_id"]))

        async def deliver_result(self, request_id):
            calls.append(("xiaot-result", request_id))

        async def deliver_question(self, request_id):
            calls.append(("xiaot-question", request_id))

    class FakeMessage:
        def __init__(self, body):
            self.body = body
            self.acked = False

        def ack(self):
            self.acked = True

        def retry(self):
            raise AssertionError("test queue message should not be retried")

    app.D1State = FakeState
    app.CloudflareRelay = FakeRootRelay
    worker.XiaotCloudflareRelay = FakeXiaotRelay
    worker.XiaotEnvironment = lambda env: env
    messages = [
        FakeMessage({"kind": "feishu_event", "platform": "xiaot", "body": {}}),
        FakeMessage({"kind": "feishu_event", "platform": "feishu", "body": {}}),
        FakeMessage({"kind": "agent", "request_id": "xiaot-agent-1"}),
        FakeMessage({"kind": "deliver_question", "request_id": "xiaot-question-1"}),
        FakeMessage({"kind": "deliver_result", "request_id": "xiaoc-result-1"}),
    ]
    entrypoint = object.__new__(app.Default)
    entrypoint.env = SimpleNamespace(DB=object())
    entrypoint.ctx = None

    asyncio.run(entrypoint.queue(SimpleNamespace(messages=messages)))

    assert calls == [
        ("xiaot-event", "xiaot"),
        ("xiaoc-event", "feishu"),
        ("xiaot-agent", "xiaot-agent-1"),
        ("xiaot-question", "xiaot-question-1"),
        ("xiaoc-result", "xiaoc-result-1"),
    ]
    assert all(message.acked for message in messages)


def test_duplicate_queue_delivery_claims_xiaot_run_before_processing_reply():
    worker = _load_xiaot_module()

    class FakeStatement:
        def __init__(self, db, sql):
            self.db = db
            self.sql = sql
            self.params = ()

        def bind(self, *params):
            self.params = params
            return self

        async def run(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            self.db.connection.commit()
            return SimpleNamespace(meta={"changes": max(cursor.rowcount, 0)})

        async def first(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [column[0] for column in cursor.description]
            return dict(zip(columns, row, strict=True))

        async def all(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            columns = [column[0] for column in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            return SimpleNamespace(results=rows)

    class FakeD1:
        def __init__(self):
            self.connection = sqlite3.connect(":memory:")

        def prepare(self, sql):
            return FakeStatement(self, sql)

    async def scenario():
        state = worker.D1State(FakeD1())
        await state.ensure_schema()
        await state.create_run(
            request_id="xiaot_run_duplicate",
            conversation_key="xiaot:cli_xiaot:oc_group:dedupe",
            source_message_id="om_source",
            input_markdown="User task:\n测试",
        )
        calls = []

        async def process_once(self, body):
            calls.append(body["request_id"])
            await asyncio.sleep(0)

        original = worker.CloudflareRelay.run_agent_job
        worker.CloudflareRelay.run_agent_job = process_once
        try:
            relay = worker.XiaotCloudflareRelay(SimpleNamespace(), None, state)
            await asyncio.gather(
                relay.run_agent_job({"request_id": "xiaot_run_duplicate"}),
                relay.run_agent_job({"request_id": "xiaot_run_duplicate"}),
            )
            run = await state.get_run("xiaot_run_duplicate")
            return calls, run
        finally:
            worker.CloudflareRelay.run_agent_job = original

    calls, run = asyncio.run(scenario())

    assert calls == ["xiaot_run_duplicate"]
    assert run["status"] == "dispatching"


def test_audit_note_records_operation_target_and_status():
    worker = _load_xiaot_module()
    note = worker.XiaotCloudflareRelay._audit_note(
        operation="update",
        table_key="task",
        record_id="rec123abc",
        fields={"Status": "Finished"},
        before={"fields": {"Status": "To-do"}},
        status="已完成",
    )

    assert '"操作":"更新"' in note
    assert '"表":"task"' in note
    assert '"记录 ID":"rec123abc"' in note
    assert '"状态":"已完成"' in note


def test_xiaot_instructions_have_bitable_workflow_and_no_calendar_routes():
    content = (ROOT / "xiaot-agent-instructions.md").read_text(encoding="utf-8")

    for expected in (
        "goal",
        "project",
        "task",
        "sub_task",
        "changelog",
        "ai_text",
        "confirm_mutation",
    ):
        assert expected in content
    assert "Google Calendar" not in content
    assert "Perfect710" not in content


def test_xiaot_registers_table_discovery_as_a_read_only_tool():
    worker = _load_xiaot_module()
    relay = worker.XiaotCloudflareRelay(SimpleNamespace(), None, SimpleNamespace(db=object()))

    tools = {item["name"]: item for item in relay.tool_definitions()}
    assert "discover_tables" in tools
    assert tools["discover_tables"]["annotations"]["readOnlyHint"] is True
    assert "discover_tables" in worker.XIAOT_READ_TOOL_NAMES


def test_additional_table_rule_is_json_validated_and_uses_versioned_rollback(monkeypatch):
    worker = _load_xiaot_module()
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_bitable_extension_test",
        ROOT / "cloudflare_worker/src/agent_config_mcp.py",
    )
    assert spec and spec.loader
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    extension = {
        "blocked_task": {
            "table_id": "tblBlockedTask123",
            "name": "blocked_task",
            "description": "Blocked tasks",
        }
    }
    writes = []

    async def current(_db, sql, *params):
        if "SELECT value_json, is_active, current_version" in sql:
            return None
        if "SELECT rule_name, source_ref, impact, current_version" in sql:
            return {
                "rule_name": "XiaoT allowed Bitable tables",
                "source_ref": "XiaoT Bitable table allowlist",
                "impact": "Adds only a verified table in the same DEV base.",
                "current_version": 2,
            }
        if "SELECT value_json, is_active, rule_name, source_ref, impact" in sql:
            return {
                "value_json": json.dumps(extension, ensure_ascii=False),
                "is_active": 1,
                "rule_name": "XiaoT allowed Bitable tables",
                "source_ref": "XiaoT Bitable table allowlist",
                "impact": "Adds only a verified table in the same DEV base.",
            }
        raise AssertionError(f"unexpected query: {sql}")

    async def no_active_rules(_db, _scope):
        return []

    async def shared_policy(_db):
        return {
            "confirmation_phrases": ["确认配置"],
            "refusal_phrases": ["取消"],
            "confirmation_emojis": ["OK"],
            "refusal_emojis": ["NO"],
        }

    async def write(_db, sql, *params):
        writes.append((sql, params))

    monkeypatch.setattr(config, "_db_first", current)
    monkeypatch.setattr(config, "active_rules_for_agent", no_active_rules)
    monkeypatch.setattr(config, "active_shared_confirmation_policy", shared_policy)
    monkeypatch.setattr(config, "_db_run", write)
    mcp = object.__new__(config.AgentConfigMCP)
    mcp.state = SimpleNamespace(db=object())
    actor = {
        "platform": "feishu",
        "open_id": "ou_requester",
        "request_id": "xiaot_request",
        "conversation_key": "xiaot:app:chat:dev",
    }

    proposal = asyncio.run(
        mcp._prepare_change(
            {
                "action": "set",
                "rule_name": "XiaoT allowed Bitable tables",
                "source_ref": "XiaoT Bitable table allowlist",
                "impact": "Adds only a verified table in the same DEV base.",
                "current_value": "seven built-in tables",
                "new_value": json.dumps(extension, ensure_ascii=False),
            },
            actor,
            "xiaot",
            "bitable.additional_tables",
        )
    )
    assert proposal["proposed_value"] == extension
    assert json.loads(writes[-1][1][7]) == extension

    rollback = asyncio.run(
        mcp._prepare_rollback(
            {"target_version": 1},
            actor,
            "xiaot",
            "bitable.additional_tables",
        )
    )
    assert rollback["target_value"] == extension
    assert rollback["rule_key"] == "bitable.additional_tables"
    assert "INSERT INTO agent_config_proposals" in writes[-1][0]
