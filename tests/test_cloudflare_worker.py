from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
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


def test_result_body_converts_serialized_paragraph_breaks_without_touching_paths():
    worker = _load_worker_module()

    result = worker._compose_result_body(
        "确认语句查询结果",
        r"第一段\\n\\n第二段\\n- 项目 C:\new-folder",
    )

    assert result == "确认语句查询结果\n第一段\n\n第二段\n- 项目 C:\\new-folder"


def test_reaction_event_echoes_the_marked_text_and_mentions_the_reactor():
    worker = _load_worker_module()
    body = {
        "header": {"event_id": "evt_reaction", "tenant_key": "tenant_dev"},
        "event": {
            "message_id": "om_bot_message",
            "chat_id": "oc_dev",
            "user_id": {"open_id": "ou_reactor"},
            "reaction_type": {"emoji_type": "THUMBSUP"},
        },
    }

    parts = worker._reaction_event_parts(body)
    echo = worker._reaction_quote_text("确认配置\n范围：DEV", "THUMBSUP", "ou_reactor")

    assert parts == {
        "actor_open_id": "ou_reactor",
        "target_message_id": "om_bot_message",
        "event_id": "evt_reaction",
        "emoji_type": "THUMBSUP",
        "chat_id": "oc_dev",
        "tenant_key": "tenant_dev",
    }
    assert "确认配置\n范围：DEV" in echo
    assert '<at user_id="ou_reactor">你</at>' in echo


def test_shared_confirmation_policy_has_a_human_readable_tool_view():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_for_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)

    rendered = config_mcp.AgentConfigMCP._shared_policy_text(
        {
            "version": 2,
            "confirmation_phrases": ["OK", "YES"],
            "refusal_phrases": ["NO"],
            "confirmation_emojis": ["THUMBSUP"],
            "refusal_emojis": ["CrossMark"],
            "source": "shared DEV policy",
        }
    )

    assert "\n确认语句：OK、YES\n" in rendered
    assert "\\n" not in rendered
    assert "版本：2" in rendered


def test_reaction_can_use_reaction_only_message_context_without_reply_anchor():
    worker = _load_worker_module()
    config_mcp = types.ModuleType("agent_config_mcp")

    async def ensure_schema(_db):
        return None

    async def active_policy(_db):
        return {
            "confirmation_phrases": ["确认配置"],
            "refusal_phrases": ["取消"],
            "confirmation_emojis": ["THUMBSUP"],
            "refusal_emojis": ["CrossMark"],
        }

    config_mcp.ensure_agent_config_schema = ensure_schema
    config_mcp.active_shared_confirmation_policy = active_policy
    config_mcp.shared_confirmation_decision = lambda *_args, **_kwargs: "confirmation"
    previous_module = sys.modules.get("agent_config_mcp")
    sys.modules["agent_config_mcp"] = config_mcp

    class FakeState:
        db = object()

        async def ensure_schema(self):
            return None

        async def reply_context(self, _message_id):
            return {
                "conversation_key": "feishu:cli_dev:oc_chat:run",
                "message_text": "授权卡片说明",
                "confirmation_required": 0,
                "request_id": "",
            }

        async def reply_conversation(self, _message_id):
            return None

        async def requester(self, _conversation_key):
            return {"source_chat_id": "oc_chat"}

    relay = object.__new__(worker.CloudflareRelay)
    relay.state = FakeState()
    body = {
        "header": {"event_id": "evt_ack", "tenant_key": "tenant_dev"},
        "event": {
            "message_id": "om_auth_card",
            "chat_id": "oc_chat",
            "user_id": {"open_id": "ou_reactor"},
            "reaction_type": {"emoji_type": "THUMBSUP"},
        },
    }

    try:
        prepared = asyncio.run(relay._prepare_reaction_message(body, "feishu"))
    finally:
        if previous_module is None:
            sys.modules.pop("agent_config_mcp", None)
        else:
            sys.modules["agent_config_mcp"] = previous_module

    assert prepared is not None
    assert prepared["_relay_reaction"]["confirmation_pending"] is False
    assert "授权卡片说明" in prepared["_relay_reaction"]["echo_text"]
    assert '<at user_id="ou_reactor">你</at>' in prepared["_relay_reaction"]["echo_text"]


def test_shared_proposal_text_is_readable_instead_of_serialized_json():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_proposal_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)
    mcp = object.__new__(config_mcp.AgentConfigMCP)

    rendered = mcp._proposal_reply(
        {
            "agent_scope": "shared",
            "rule_key": "shared.confirmation_policy",
            "proposal_id": "acfg_test",
            "list_type": "confirmation",
            "phrase_operation": "add",
            "before": {"confirmation_phrases": ["OK"]},
            "after": {"confirmation_phrases": ["OK", "YES"]},
            "impact": "所有 DEV Agent 共用一套确认策略。",
            "confirmation_phrases": ["OK", "YES"],
            "refusal_phrases": ["NO"],
            "confirmation_emojis": ["THUMBSUP"],
            "refusal_emojis": ["CrossMark"],
        }
    )

    assert "\n操作：新增确认语句\n" in rendered
    assert "确认语句：OK → OK、YES" in rendered
    assert "\\n" not in rendered
    assert "{\"" not in rendered


def test_prepare_shared_policy_tool_returns_readable_content_and_structured_data():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_tool_output_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)

    async def ensure_schema(_db):
        return None

    async def actor_context(_args):
        return {"agent_scope": "xiaoc", "open_id": "ou_user"}

    async def prepare(_args, _actor):
        return {
            "success": True,
            "agent_scope": "shared",
            "rule_key": "shared.confirmation_policy",
            "proposal_id": "acfg_test",
            "list_type": "confirmation",
            "phrase_operation": "add",
            "before": {"confirmation_phrases": ["OK"]},
            "after": {"confirmation_phrases": ["OK", "YES"]},
            "impact": "所有 DEV Agent 共用一套确认策略。",
            "confirmation_phrases": ["OK", "YES"],
            "refusal_phrases": ["NO"],
            "confirmation_emojis": ["THUMBSUP"],
            "refusal_emojis": ["CrossMark"],
        }

    async def deliver(_args, _proposal):
        return None

    config_mcp.ensure_agent_config_schema = ensure_schema
    mcp = object.__new__(config_mcp.AgentConfigMCP)
    mcp.state = SimpleNamespace(db=object())
    mcp._run_actor_context = actor_context
    mcp._prepare_shared_confirmation_policy_change = prepare
    mcp._deliver_prepared_proposal = deliver

    result = asyncio.run(
        mcp.call_tool(
            "prepare_shared_confirmation_policy_change",
            {"request_id": "req_1", "conversation_key": "feishu:dev:chat"},
        )
    )

    assert result["isError"] is False
    assert result["structuredContent"]["proposal_id"] == "acfg_test"
    assert "\n操作：新增确认语句\n" in result["content"][0]["text"]
    assert "\\n" not in result["content"][0]["text"]


def test_configured_reaction_is_forwarded_only_for_the_exact_pending_proposal():
    worker = _load_worker_module()
    config_mcp = types.ModuleType("agent_config_mcp")

    async def ensure_schema(_db):
        return None

    async def active_policy(_db):
        return {
            "confirmation_phrases": ["确认配置"],
            "refusal_phrases": ["取消"],
            "confirmation_emojis": ["THUMBSUP"],
            "refusal_emojis": ["CrossMark"],
        }

    config_mcp.ensure_agent_config_schema = ensure_schema
    config_mcp.active_shared_confirmation_policy = active_policy
    config_mcp.shared_confirmation_decision = lambda *_args, **_kwargs: "confirmation"
    previous_module = sys.modules.get("agent_config_mcp")
    sys.modules["agent_config_mcp"] = config_mcp
    previous_lookup = worker._db_first

    async def lookup(_db, sql, *_params):
        return {"proposal_id": "acfg_pending"} if "agent_config_proposals" in sql else None

    worker._db_first = lookup

    class FakeState:
        db = object()

        async def ensure_schema(self):
            return None

        async def reply_context(self, _message_id):
            return {
                "conversation_key": "feishu:cli_dev:oc_chat:run",
                "message_text": "待确认提案",
                "confirmation_required": 1,
                "request_id": "req_pending",
            }

        async def reply_conversation(self, _message_id):
            return "feishu:cli_dev:oc_chat:run"

        async def requester(self, _conversation_key):
            return {"source_chat_id": "oc_chat"}

    relay = object.__new__(worker.CloudflareRelay)
    relay.state = FakeState()
    body = {
        "header": {"event_id": "evt_confirm", "tenant_key": "tenant_dev"},
        "event": {
            "message_id": "om_proposal",
            "chat_id": "oc_chat",
            "user_id": {"open_id": "ou_user"},
            "reaction_type": {"emoji_type": "THUMBSUP"},
        },
    }

    try:
        prepared = asyncio.run(relay._prepare_reaction_message(body, "feishu"))
    finally:
        worker._db_first = previous_lookup
        if previous_module is None:
            sys.modules.pop("agent_config_mcp", None)
        else:
            sys.modules["agent_config_mcp"] = previous_module

    assert prepared is not None
    assert prepared["_relay_reaction"]["confirmation_pending"] is True
    assert prepared["_relay_reaction"]["decision"] == "confirmation"
    assert prepared["_relay_reaction"]["agent_config_proposal"] is True


def test_xiaot_bitable_proposal_reaction_is_bound_to_requester_and_one_pending_write():
    worker = _load_worker_module()
    config_mcp = types.ModuleType("agent_config_mcp")

    async def ensure_schema(_db):
        return None

    async def active_policy(_db):
        return {
            "confirmation_phrases": ["确认配置"],
            "refusal_phrases": ["取消"],
            "confirmation_emojis": ["YES"],
            "refusal_emojis": ["NO"],
        }

    config_mcp.ensure_agent_config_schema = ensure_schema
    config_mcp.active_shared_confirmation_policy = active_policy
    config_mcp.shared_confirmation_decision = (
        lambda _text, _policy, *, reaction_emoji="":
        "confirmation" if reaction_emoji == "YES" else None
    )
    previous_module = sys.modules.get("agent_config_mcp")
    sys.modules["agent_config_mcp"] = config_mcp
    previous_lookup = worker._db_first
    previous_all = worker._db_all
    pending_count = 1

    async def lookup(_db, _sql, *_params):
        return None

    async def all_rows(_db, sql, *params):
        if "xiaot_bitable_proposals" not in sql:
            return []
        assert params[:3] == (
            "xiaot:app:oc_chat:thread",
            "ou_requester",
            "ou_lark_account",
        )
        assert len(params) == 4 and isinstance(params[3], int)
        return [{"proposal_id": f"xbt_{index}"} for index in range(pending_count)]

    worker._db_first = lookup
    worker._db_all = all_rows

    class FakeState:
        db = object()

        async def ensure_schema(self):
            return None

        async def reply_context(self, _message_id):
            return {
                "conversation_key": "xiaot:app:oc_chat:thread",
                "message_text": "待确认的任务写入提案",
                "confirmation_required": 1,
                "request_id": "xiaot_request_1",
            }

        async def reply_conversation(self, _message_id):
            return "xiaot:app:oc_chat:thread"

        async def requester(self, _conversation_key):
            return {"source_chat_id": "oc_chat"}

    relay = object.__new__(worker.CloudflareRelay)
    relay.state = FakeState()

    async def saved_identity(request_id, conversation_key, actor_open_id):
        assert request_id == "xiaot_request_1"
        assert conversation_key == "xiaot:app:oc_chat:thread"
        assert actor_open_id == "ou_requester"
        return {"platform": "lark", "app_key": "lark_yw", "open_id": "ou_lark_account"}

    relay._saved_requester_identity_for_reaction = saved_identity

    async def prepare():
        body = {
            "header": {"event_id": "evt_confirm", "tenant_key": "tenant_dev"},
            "event": {
                "message_id": "om_xiaot_proposal",
                "chat_id": "oc_chat",
                "user_id": {"open_id": "ou_requester"},
                "reaction_type": {"emoji_type": "YES"},
            },
        }
        return await relay._prepare_reaction_message(body, "xiaot", xiaot_only=True)

    try:
        prepared = asyncio.run(prepare())
        assert prepared is not None
        assert prepared["_relay_reaction"]["confirmation_pending"] is True
        assert prepared["_relay_reaction"]["decision"] == "confirmation"
        pending_count = 2
        ambiguous = asyncio.run(prepare())
        assert ambiguous is not None
        assert ambiguous["_relay_reaction"]["confirmation_pending"] is False
    finally:
        worker._db_first = previous_lookup
        worker._db_all = previous_all
        if previous_module is None:
            sys.modules.pop("agent_config_mcp", None)
        else:
            sys.modules["agent_config_mcp"] = previous_module


def test_agent_rules_do_not_inject_legacy_per_agent_copies_of_shared_policy():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_legacy_policy_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)
    previous_all = config_mcp._db_all
    rows = [
        {"rule_key": "shared.confirmation_policy", "rule_name": "old local copy"},
        {"rule_key": "calendar.default_timezone", "rule_name": "timezone"},
    ]

    async def query(_db, sql, *params):
        assert "AND rule_key NOT IN (?, ?)" in sql
        assert params == (
            "xiaoc",
            config_mcp.SHARED_CONFIRMATION_RULE_KEY,
            "shared_confirmation_policy",
            config_mcp.MAX_ACTIVE_RULES + 1,
        )
        return [row for row in rows if row["rule_key"] not in params[1:3]]

    config_mcp._db_all = query
    try:
        active = asyncio.run(config_mcp.active_rules_for_agent(object(), "xiaoc"))
    finally:
        config_mcp._db_all = previous_all

    assert [rule["key"] for rule in active] == ["calendar.default_timezone"]


def test_legacy_confirmation_copies_bootstrap_once_to_one_shared_version():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_shared_bootstrap_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)

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
            return dict(zip((item[0] for item in cursor.description), row, strict=True))

        async def all(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            rows = [
                dict(zip((item[0] for item in cursor.description), row, strict=True))
                for row in cursor.fetchall()
            ]
            self.db.connection.commit()
            return SimpleNamespace(results=rows)

    class FakeD1:
        def __init__(self):
            self.connection = sqlite3.connect(":memory:")

        def prepare(self, sql):
            return FakeStatement(self, sql)

    async def scenario():
        db = FakeD1()
        await config_mcp.ensure_agent_config_schema(db)
        legacy = {
            "xiaoc": {
                "confirmation_phrases": ["确认配置", "[OK]"],
                "refusal_phrases": ["取消", "[No]"],
            },
            "xiaot": {
                "confirmation_phrases": ["确认配置", "YES"],
                "refusal_phrases": ["取消", "不要"],
            },
        }
        for scope, value in legacy.items():
            await config_mcp._db_run(
                db,
                "INSERT INTO agent_config_current "
                "(agent_scope, rule_key, rule_name, source_ref, impact, value_json, is_active, "
                "current_version, updated_by_platform, updated_by_open_id, updated_request_id, "
                "updated_conversation_key, last_operation, last_proposal_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 'feishu', 'ou_test', 'req_test', "
                "'chat_test', 'set', ?, 1)",
                scope,
                config_mcp.SHARED_CONFIRMATION_RULE_KEY,
                "legacy policy",
                "legacy",
                "legacy",
                json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False),
                f"legacy_{scope}",
            )

        first = await config_mcp.active_shared_confirmation_policy(db)
        second = await config_mcp.active_shared_confirmation_policy(db)
        versions = await config_mcp._db_all(
            db,
            "SELECT version FROM agent_config_versions WHERE agent_scope = 'shared' "
            "AND rule_key = ?",
            config_mcp.SHARED_CONFIRMATION_RULE_KEY,
        )
        return first, second, versions

    first, second, versions = asyncio.run(scenario())

    assert first["version"] == second["version"] == 1
    assert first["source"] == "shared DEV policy"
    assert first["confirmation_phrases"] == ["确认配置", "[OK]", "YES"]
    assert first["refusal_phrases"] == ["取消", "[No]", "不要"]
    assert first["confirmation_emojis"] == config_mcp.DEFAULT_CONFIRMATION_EMOJIS
    assert first["refusal_emojis"] == config_mcp.DEFAULT_REFUSAL_EMOJIS
    assert versions == [{"version": 1}]


def test_existing_shared_policy_merges_and_retires_legacy_agent_copies_once():
    worker = _load_worker_module()
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "agent_config_mcp_legacy_merge_test", ROOT / "cloudflare_worker/src/agent_config_mcp.py"
    )
    assert spec and spec.loader
    config_mcp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_mcp)

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
            return (
                dict(zip((item[0] for item in cursor.description), row, strict=True))
                if row is not None
                else None
            )

        async def all(self):
            cursor = self.db.connection.execute(self.sql, self.params)
            rows = [
                dict(zip((item[0] for item in cursor.description), row, strict=True))
                for row in cursor.fetchall()
            ]
            self.db.connection.commit()
            return SimpleNamespace(results=rows)

    class FakeD1:
        def __init__(self):
            self.connection = sqlite3.connect(":memory:")

        def prepare(self, sql):
            return FakeStatement(self, sql)

    async def scenario():
        db = FakeD1()
        await config_mcp.ensure_agent_config_schema(db)
        shared = {
            "confirmation_phrases": list(config_mcp.DEFAULT_CONFIRMATION_PHRASES),
            "refusal_phrases": list(config_mcp.DEFAULT_REFUSAL_PHRASES),
            "confirmation_emojis": list(config_mcp.DEFAULT_CONFIRMATION_EMOJIS),
            "refusal_emojis": list(config_mcp.DEFAULT_REFUSAL_EMOJIS),
        }
        await config_mcp._db_run(
            db,
            "INSERT INTO agent_config_current "
            "(agent_scope, rule_key, rule_name, source_ref, impact, value_json, is_active, "
            "current_version, updated_by_platform, updated_by_open_id, updated_request_id, "
            "updated_conversation_key, last_operation, last_proposal_id, updated_at) "
            "VALUES ('shared', ?, 'shared policy', 'seed', 'seed', ?, 1, 1, 'system', "
            "'seed', 'seed', 'seed', 'set', 'seed_shared_v1', 1)",
            config_mcp.SHARED_CONFIRMATION_RULE_KEY,
            json.dumps(shared, ensure_ascii=False),
        )
        for scope in ("xiaoc", "xiaot"):
            old = {
                "confirmation_phrases": [
                    *config_mcp.DEFAULT_CONFIRMATION_PHRASES,
                    f"{scope.upper()}_LEGACY_CONFIRM",
                ],
                "refusal_phrases": [
                    *config_mcp.DEFAULT_REFUSAL_PHRASES,
                    f"{scope.upper()}_LEGACY_REFUSAL",
                ],
            }
            await config_mcp._db_run(
                db,
                "INSERT INTO agent_config_current "
                "(agent_scope, rule_key, rule_name, source_ref, impact, value_json, is_active, "
                "current_version, updated_by_platform, updated_by_open_id, updated_request_id, "
                "updated_conversation_key, last_operation, last_proposal_id, updated_at) "
                "VALUES (?, 'shared_confirmation_policy', 'old local copy', 'old', 'old', ?, "
                "1, 1, 'feishu', 'old_user', 'old_request', 'old_chat', 'set', ?, 2)",
                scope,
                json.dumps(json.dumps(old, ensure_ascii=False), ensure_ascii=False),
                f"old_local_{scope}_v1",
            )

        real_db_all = config_mcp._db_all
        simulated_race = False

        async def lose_to_concurrent_merge(db_arg, sql, *params):
            nonlocal simulated_race
            if not simulated_race and sql.startswith(
                "UPDATE agent_config_current SET value_json = ?"
            ):
                simulated_race = True
                # Commit the competing request's version, then make this
                # request observe the compare-and-swap miss it would get.
                await real_db_all(db_arg, sql, *params)
                return []
            return await real_db_all(db_arg, sql, *params)

        config_mcp._db_all = lose_to_concurrent_merge
        try:
            first = await config_mcp.active_shared_confirmation_policy(db)
        finally:
            config_mcp._db_all = real_db_all
        second = await config_mcp.active_shared_confirmation_policy(db)
        legacy = await config_mcp._db_all(
            db,
            "SELECT agent_scope, is_active, current_version FROM agent_config_current "
            "WHERE rule_key = 'shared_confirmation_policy' ORDER BY agent_scope",
        )
        versions = await config_mcp._db_all(
            db,
            "SELECT version, operation FROM agent_config_versions "
            "WHERE agent_scope = 'shared' AND rule_key = ? ORDER BY version",
            config_mcp.SHARED_CONFIRMATION_RULE_KEY,
        )
        active_rows = await config_mcp.active_rules_for_agent(db, "xiaoc")
        return first, second, legacy, versions, active_rows

    first, second, legacy, versions, active_rows = asyncio.run(scenario())

    assert first["version"] == second["version"] == 2
    assert first["confirmation_phrases"] == [
        *config_mcp.DEFAULT_CONFIRMATION_PHRASES,
        "XIAOC_LEGACY_CONFIRM",
        "XIAOT_LEGACY_CONFIRM",
    ]
    assert first["refusal_phrases"] == [
        *config_mcp.DEFAULT_REFUSAL_PHRASES,
        "XIAOC_LEGACY_REFUSAL",
        "XIAOT_LEGACY_REFUSAL",
    ]
    assert len(first["confirmation_phrases"]) <= config_mcp.MAX_SHARED_PHRASES
    assert len(first["refusal_phrases"]) <= config_mcp.MAX_SHARED_PHRASES
    assert first["confirmation_emojis"] == config_mcp.DEFAULT_CONFIRMATION_EMOJIS
    assert first["refusal_emojis"] == config_mcp.DEFAULT_REFUSAL_EMOJIS
    assert legacy == [
        {"agent_scope": "xiaoc", "is_active": 0, "current_version": 2},
        {"agent_scope": "xiaot", "is_active": 0, "current_version": 2},
    ]
    assert versions == [
        {"version": 1, "operation": "set"},
        {"version": 2, "operation": "legacy_policy_merge"},
    ]
    assert active_rows == []
    assert config_mcp.AgentConfigMCP._normalize_rule_key("shared_confirmation_policy") == (
        config_mcp.SHARED_CONFIRMATION_RULE_KEY
    )


def test_ask_user_marks_a_pending_xiaot_proposal_even_if_agent_omits_flag():
    worker = _load_worker_module()
    previous_lookup = worker._db_first

    async def lookup(_db, sql, *_params):
        if "FROM agent_config_proposals" in sql:
            return None
        if "FROM xiaot_run_requesters" in sql:
            return {"open_id": "ou_requester"}
        if "FROM xiaot_bitable_proposals" in sql:
            return {"proposal_id": "xbt_pending"}
        raise AssertionError(f"unexpected lookup: {sql}")

    worker._db_first = lookup

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "conversation_key": "xiaot:app:chat:thread",
                "status": "running",
            }

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

    state = FakeState()
    relay = object.__new__(worker.CloudflareRelay)
    relay.state = state
    deliveries = []

    async def enqueue(kind, request_id):
        deliveries.append((kind, request_id))

    relay._enqueue_delivery = enqueue
    try:
        result = asyncio.run(
            relay.call_tool(
                "ask_user",
                {
                    "request_id": "xiaot_req",
                    "conversation_key": "xiaot:app:chat:thread",
                    "question": "请确认是否执行以下新增提案：新增一条任务记录。",
                },
            )
        )
    finally:
        worker._db_first = previous_lookup

    assert result["structuredContent"]["status"] == "needs_user"
    assert state.updated[1]["confirmation_required"] == 1
    assert deliveries == [("deliver_question", "xiaot_req")]


def test_confirmed_reaction_result_is_a_mentioned_card_replying_to_marked_message():
    worker = _load_worker_module()
    policy_module = types.ModuleType("agent_config_mcp")

    async def active_policy(_db):
        return {"confirmation_phrases": [], "refusal_phrases": [], "confirmation_emojis": ["YES"], "refusal_emojis": []}

    def decision(_text, _policy, *, reaction_emoji=""):
        return "confirmation" if reaction_emoji == "YES" else None

    policy_module.active_shared_confirmation_policy = active_policy
    policy_module.shared_confirmation_decision = decision
    previous_policy = sys.modules.get("agent_config_mcp")
    sys.modules["agent_config_mcp"] = policy_module
    previous_lookup = worker._db_first
    previous_run = worker._db_run

    async def lookup(_db, sql, *_params):
        if "reaction_emoji" in sql:
            return {"reaction_emoji": "YES"}
        if "sender_open_id" in sql:
            return {"sender_open_id": "ou_reactor"}
        raise AssertionError(f"unexpected lookup: {sql}")

    async def noop_run(*_args, **_kwargs):
        return None

    worker._db_first = lookup
    worker._db_run = noop_run

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_marked_proposal",
                "status": "done",
                "title": "新增任务成功",
                "markdown": "任务记录已创建。",
                "delivered": 0,
                "conversation_key": "xiaot:app:chat:thread",
            }

        async def save_reply(self, outbound_id, conversation_key, **_kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.cards = []

        async def reply_card(self, message_id, card, *, uuid=None):
            self.cards.append((message_id, card, uuid))
            return "om_confirmed_result_card"

    relay = worker.CloudflareRelay(SimpleNamespace(), None, FakeState())
    relay.feishu = FakeFeishu()
    try:
        asyncio.run(relay.deliver_result("xiaot_reaction_run"))
    finally:
        worker._db_first = previous_lookup
        worker._db_run = previous_run
        if previous_policy is None:
            sys.modules.pop("agent_config_mcp", None)
        else:
            sys.modules["agent_config_mcp"] = previous_policy

    message_id, card, _uuid = relay.feishu.cards[0]
    assert message_id == "om_marked_proposal"
    assert card["header"]["title"]["content"] == "确认操作成功"
    assert card["elements"][0]["text"]["content"] == (
        "<at id=ou_reactor></at>\n\n新增任务成功\n任务记录已创建。"
    )


def test_pre_handoff_event_failure_releases_claim_for_queue_retry():
    worker = _load_worker_module()

    class FakeState:
        async def reply_conversation(self, _parent_id):
            return None

        async def ensure_schema(self):
            pass

        async def claim_event(self, **fields):
            self.claimed = fields
            return True

        async def save_requester(self, *_fields):
            raise RuntimeError("temporary D1 failure")

        async def release_event_claim(self, message_id):
            self.released = message_id

    state = FakeState()
    relay = worker.CloudflareRelay(
        SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot", FEISHU_APP_ID="app"),
        None,
        state,
    )
    body = _event("text", {"text": "@_user_bot 查一下"})

    try:
        asyncio.run(relay._process_feishu_event(body, "feishu"))
    except RuntimeError as exc:
        assert "temporary D1 failure" in str(exc)
    else:
        raise AssertionError("the simulated transient failure should reach Queue retry")

    assert state.released == "om_123"


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


def test_unmentioned_reply_to_a_mapped_result_card_resumes_the_same_conversation():
    worker = _load_worker_module()

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    class FakeState:
        db = object()

        async def reply_conversation(self, parent_id):
            assert parent_id == "om_result_card"
            return "feishu:app:chat:existing"

        async def ensure_schema(self):
            pass

        async def claim_event(self, **fields):
            self.claimed = fields
            return True

        async def save_requester(self, *fields):
            self.requester = fields

        async def close_reply_confirmation(self, outbound_id):
            self.closed_confirmation = outbound_id

        async def previous_run_exists(self, conversation_key):
            assert conversation_key == "feishu:app:chat:existing"
            return True

        async def create_run(self, **fields):
            self.created = fields

    queue = FakeQueue()
    state = FakeState()
    env = SimpleNamespace(
        FEISHU_BOT_OPEN_ID="ou_bot",
        FEISHU_APP_ID="app",
        AGENT_QUEUE=queue,
    )
    relay = worker.CloudflareRelay(env, None, state)
    body = _event(
        "text",
        {"text": "请继续"},
        text_mention=False,
        parent_id="om_result_card",
    )

    asyncio.run(relay._process_feishu_event(body, "feishu"))

    assert state.created["conversation_key"] == "feishu:app:chat:existing"
    assert "turn_mode: continuation" in state.created["input_markdown"]
    assert queue.messages[0]["kind"] == "agent"


def test_generic_feishu_route_does_not_take_over_a_xiaot_reply():
    worker = _load_worker_module()
    worker.Response.json = staticmethod(lambda payload, **kwargs: payload)

    class FakeState:
        async def reply_conversation(self, parent_id):
            assert parent_id == "om_xiaot_card"
            return "xiaot:cli_xiaot:oc_chat:thread"

        async def create_run(self, **_fields):
            raise AssertionError("a non-owning Feishu route must not create a run")

    relay = object.__new__(worker.CloudflareRelay)
    relay.env = SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_feishu_bot", FEISHU_APP_ID="cli_feishu")
    relay.state = FakeState()
    relay.bitable_workflow = SimpleNamespace(is_target_group=lambda _chat_id: False)
    relay.agent_workflow = SimpleNamespace(
        handle_event=lambda **_fields: (_ for _ in ()).throw(
            AssertionError("the generic Agent must not receive XiaoT continuations")
        )
    )
    body = _event(
        "text",
        {"text": "确认新增"},
        text_mention=False,
        parent_id="om_xiaot_card",
    )

    asyncio.run(relay._process_feishu_event(body, "feishu"))


def test_unmentioned_reply_to_an_unmapped_message_is_ignored_outside_target_group():
    worker = _load_worker_module()

    class FakeState:
        async def reply_conversation(self, parent_id):
            return None

    relay = worker.CloudflareRelay(
        SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot", FEISHU_APP_ID="app"),
        None,
        FakeState(),
    )
    body = _event(
        "text",
        {"text": "不要开始新会话"},
        text_mention=False,
        parent_id="om_not_from_this_bot",
    )

    asyncio.run(relay._process_feishu_event(body, "feishu"))


def test_worker_keeps_parent_for_an_mentioned_reply():
    worker = _load_worker_module()

    event = worker._normalize_event(
        _event("text", {"text": "继续刚才的问题"}, text_mention=True, parent_id="om_bot_card"),
        "ou_bot",
    )

    assert event is not None
    assert event["parent_id"] == "om_bot_card"
    assert event["mentioned_bot"] is True


def test_explicit_quoted_image_is_loaded_from_its_own_message_and_chat():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event(
            "text",
            {"text": "@_user_bot 看一下这张图"},
            parent_id="om_quoted_image",
        ),
        "ou_bot",
    )
    assert event is not None

    class FakeAPI:
        async def get_message(self, message_id):
            assert message_id == "om_quoted_image"
            return {
                "message_id": "om_quoted_image",
                "chat_id": "oc_123",
                "msg_type": "image",
                "content": json.dumps({"image_key": "img_from_quoted_message"}),
            }

    hydrated = asyncio.run(worker._load_explicit_quoted_context(FakeAPI(), event))

    assert hydrated["quoted_context_status"] == "loaded"
    assert hydrated["quoted_context_has_images"] is True
    assert hydrated["image_refs"] == [
        {"message_id": "om_quoted_image", "file_key": "img_from_quoted_message"}
    ]
    assert "quoted message context" in worker._event_task_text(hydrated).lower()


def test_get_message_normalizes_documented_feishu_response_shape():
    worker = _load_worker_module()
    api = worker.FeishuAPI(SimpleNamespace())
    requests = []

    async def fake_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {
            "data": {
                "items": [
                    {
                        "message_id": "om_quoted_image",
                        "chat_id": "oc_123",
                        "msg_type": "image",
                        "body": {
                            "content": json.dumps(
                                {"image_key": "img_from_quoted_message"}
                            )
                        },
                    }
                ]
            }
        }

    api._request = fake_request
    message = asyncio.run(api.get_message("om_quoted_image"))

    assert requests == [
        ("GET", "/open-apis/im/v1/messages/om_quoted_image", {})
    ]
    assert message["message_type"] == "image"
    assert message["content"] == json.dumps(
        {"image_key": "img_from_quoted_message"}
    )
    event = worker._normalize_event(
        _event("text", {"text": "@_user_bot"}, parent_id="om_quoted_image"),
        "ou_bot",
    )
    assert event is not None
    hydrated = asyncio.run(worker._load_explicit_quoted_context(api, event))
    assert hydrated["quoted_context_status"] == "loaded"
    assert hydrated["image_refs"] == [
        {"message_id": "om_quoted_image", "file_key": "img_from_quoted_message"}
    ]


def test_mention_only_reply_loads_the_explicitly_quoted_image():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event("text", {"text": "@_user_bot"}, parent_id="om_quoted_image"),
        "ou_bot",
    )

    assert event is not None
    assert event["text"] == ""
    assert event["quoted_message_id"] == "om_quoted_image"

    class FakeAPI:
        async def get_message(self, message_id):
            assert message_id == "om_quoted_image"
            return {
                "message_id": "om_quoted_image",
                "chat_id": "oc_123",
                "msg_type": "image",
                "content": json.dumps({"image_key": "img_from_quoted_message"}),
            }

    hydrated = asyncio.run(worker._load_explicit_quoted_context(FakeAPI(), event))

    assert hydrated["quoted_context_status"] == "loaded"
    assert hydrated["image_refs"] == [
        {"message_id": "om_quoted_image", "file_key": "img_from_quoted_message"}
    ]
    assert "quoted message context" in worker._event_task_text(hydrated).lower()

    # An empty @ without a quoted target still does not create a task.
    assert (
        worker._normalize_event(_event("text", {"text": "@_user_bot"}), "ou_bot")
        is None
    )


def test_quoted_context_is_not_fetched_without_an_explicit_bot_mention():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event(
            "text",
            {"text": "继续"},
            text_mention=False,
            parent_id="om_quoted_image",
        ),
        "ou_bot",
        allow_unmentioned_reply=True,
    )
    assert event is not None

    class FakeAPI:
        async def get_message(self, _message_id):
            raise AssertionError("unmentioned replies must not fetch quote content")

    hydrated = asyncio.run(worker._load_explicit_quoted_context(FakeAPI(), event))
    assert hydrated["quoted_message_id"] == ""
    assert "quoted_context_status" not in hydrated


def test_quoted_context_rejects_a_message_from_another_chat():
    worker = _load_worker_module()
    event = worker._normalize_event(
        _event("text", {"text": "@_user_bot 识别图片"}, parent_id="om_wrong_chat"),
        "ou_bot",
    )
    assert event is not None

    class FakeAPI:
        async def get_message(self, _message_id):
            return {
                "message_id": "om_wrong_chat",
                "chat_id": "oc_other",
                "msg_type": "text",
                "content": json.dumps({"text": "不可用的内容"}),
            }

    hydrated = asyncio.run(worker._load_explicit_quoted_context(FakeAPI(), event))
    assert hydrated["quoted_context_status"] == "unavailable"
    assert "不可用的内容" not in worker._event_task_text(hydrated)


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

        async def save_reply_context(self, *_args, **_kwargs):
            return None

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

        async def save_reply_context(self, *_args, **_kwargs):
            return None

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

        async def save_reply_context(self, *_args, **_kwargs):
            return None

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

        async def save_reply_context(self, *_args, **_kwargs):
            return None

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
        "relay_mcp: workspace-agent-relay-mcp-dev\n"
        "protocol: local-agent-shell/v1\n"
        "turn_mode: initial\n"
    )
    assert "Completion contract:" in input_text
    assert "User task:\n测试" in input_text
    assert "https://bot.boooe.com/mcp" in input_text
    assert "record_result" in input_text
    assert "[calendar-result-card]" in input_text
    assert "The user's original request is never confirmation" in input_text
    assert "确认创建/确认修改/确认删除/确认取消" in input_text
    assert "a delete tool call that completes without an error is success even when its response body is empty" in input_text
    assert "Do not read or search for the event after a successful delete" in input_text
    assert "never retry an ambiguous delete" in input_text
    assert "Use the marker only for a terminal calendar write success or explicit execution failure, or for the final acknowledgement" in input_text
    assert "including a direct reply of 不" in input_text
    assert "status=done and the same title marker" in input_text
    assert "never call a calendar write tool after the refusal" in input_text
    assert "never reject or withhold a successfully rendered image" in input_text


def test_agent_input_includes_persistent_memory_before_user_task():
    worker = _load_worker_module()

    input_text = worker._conversation_input(
        request_id="req_1",
        conversation_key="feishu:app:chat:1",
        text="创建一个日程",
        continuation=True,
        persistent_memories=[
            {
                "memory_key": "calendar default",
                "memory_type": "rule",
                "memory_value": "未指定日历时使用 Bo Bozway",
            }
        ],
    )

    memory_at = input_text.index('"value":"未指定日历时使用 Bo Bozway"')
    task_at = input_text.index("User task:\n创建一个日程")
    assert memory_at < task_at
    assert "A saved memory becomes active immediately" in input_text
    assert "newest matching saved user preference supersedes older user-specific" in input_text


def test_agent_relay_loads_sender_memory_on_every_turn():
    worker = _load_worker_module()

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    class FakeState:
        db = object()

        def __init__(self):
            self.memory_lookup = None
            self.created = None

        async def previous_run_exists(self, conversation_key):
            return True

        async def user_memories(self, agent_scope, platform, requester_open_id):
            self.memory_lookup = (agent_scope, platform, requester_open_id)
            return [
                {
                    "memory_key": "calendar default",
                    "memory_type": "rule",
                    "memory_value": "未指定日历时使用 Bo Bozway",
                }
            ]

        async def create_run(self, **fields):
            self.created = fields

    queue = FakeQueue()
    state = FakeState()
    relay = worker.CloudflareRelay(
        SimpleNamespace(
            AGENT_QUEUE=queue,
            WORKSPACE_AGENT_RELAY_MCP_NAME="workspace-agent-relay-mcp-dev",
        ),
        None,
        state,
    )
    asyncio.run(
        relay.agent_workflow.handle_event(
            platform="feishu",
            conversation_key="feishu:app:chat:1",
            event={"message_id": "om_next", "open_id": "ou_current", "text": "创建日程"},
            request_id="feishu_req_next",
        )
    )

    assert state.memory_lookup == (
        "workspace-agent-relay-mcp-dev",
        "feishu",
        "ou_current",
    )
    input_text = state.created["input_markdown"]
    assert '"value":"未指定日历时使用 Bo Bozway"' in input_text
    assert input_text.index('"value":"未指定日历时使用 Bo Bozway"') < input_text.index(
        "User task:\n创建日程"
    )
    assert queue.messages[0]["request_id"] == "feishu_req_next"


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

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
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


def test_agent_image_base64_takes_precedence_over_private_attachment_url():
    worker = _load_worker_module()
    image = b"\x89PNG\r\n\x1a\nagent-image"

    data = asyncio.run(
        worker.CloudflareRelay._image_bytes_from_args(
            {
                "image_url": "https://chatgpt.com/private/attachment-that-is-not-public",
                "image_base64": base64.b64encode(image).decode("ascii"),
            }
        )
    )

    assert data == image


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

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
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


def test_record_result_card_marker_uses_the_existing_title_field():
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
                "conversation_key": "feishu:app:chat:1",
                "completed_at": None,
            }

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

    queue = FakeQueue()
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(AGENT_QUEUE=queue), None, state)
    result_tool = next(item for item in relay.tool_definitions() if item["name"] == "record_result")
    assert "reply_format" not in result_tool["inputSchema"]["properties"]
    result = asyncio.run(
        relay.call_tool(
            "record_result",
            {
                "request_id": "req_1",
                "conversation_key": "feishu:app:chat:1",
                "status": "done",
                "title": "[calendar-result-card] 创建成功",
                "markdown": "日程链接：<https://calendar.google.com/event?id=evt_1>",
            },
        )
    )

    assert result["isError"] is False
    assert state.updated[1]["title"] == "[calendar-result-card] 创建成功"
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


def test_result_delivery_quotes_source_and_retires_legacy_placeholder():
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

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []
            self.updates = []

        async def reply(self, message_id, text, *, uuid=None):
            self.replies.append((message_id, text, uuid))
            return "om_result"

        async def update(self, message_id, text):
            self.updates.append((message_id, text))

    async def noop_db_run(*args, **kwargs):
        return None

    async def requester_lookup(_db, sql, *_params):
        if "sender_open_id" in sql:
            return {"sender_open_id": "ou_requester"}
        return None

    worker._db_run = noop_db_run
    worker._db_first = requester_lookup
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.replies == [
        (
            "om_source",
            '<at user_id="ou_requester">你</at>\n\n完成\n结果正文',
            worker._result_delivery_idempotency_uuid("req_1"),
        )
    ]
    assert relay.feishu.updates == [
        ("om_placeholder", "任务已完成，结果已在引用消息中回复。")
    ]
    assert state.saved == ("om_result", "feishu:app:chat:x")


def test_result_delivery_sends_a_success_card_and_binds_replies_to_the_conversation():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "done",
                "title": "[calendar-result-card] 创建成功，日程链接：https://calendar.google.com/event?id=evt_1",
                "markdown": "创建成功，日程链接：https://calendar.google.com/event?id=evt_1",
                "delivered": 0,
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.cards = []
            self.updates = []

        async def reply_card(self, message_id, card, *, uuid=None):
            self.cards.append((message_id, card, uuid))
            return "om_result_card"

        async def update(self, message_id, text):
            self.updates.append((message_id, text))

    db_updates = []

    async def fake_db_run(db, sql, *params):
        db_updates.append((sql, params))

    async def fake_db_first(db, sql, *params):
        assert "sender_open_id" in sql
        assert params == ("req_1",)
        return {"sender_open_id": "ou_requester"}

    worker._db_run = fake_db_run
    worker._db_first = fake_db_first
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.cards[0][0] == "om_source"
    card = relay.feishu.cards[0][1]
    assert relay.feishu.cards[0][2] == worker._calendar_result_idempotency_uuid("req_1")
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "日程操作成功"
    body = card["elements"][0]["text"]["content"]
    assert body == (
        "<at id=ou_requester></at>\n\n"
        "创建成功，日程链接：https://calendar.google.com/event?id=evt_1"
    )
    assert body.count("https://calendar.google.com/event?id=evt_1") == 1
    assert state.saved == ("om_result_card", "feishu:app:chat:x")
    assert relay.feishu.updates == [("om_placeholder", "日程操作已完成，结果见下方消息卡片。")]
    assert db_updates and db_updates[0][1][-1] == "req_1"


def test_failed_calendar_result_uses_a_red_card():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "status": "failed",
                "title": "[calendar-result-card] 修改失败",
                "markdown": "日程未修改。",
                "delivered": 0,
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.cards = []

        async def reply_card(self, message_id, card, *, uuid=None):
            self.cards.append((message_id, card, uuid))
            return "om_failed_result_card"

    async def noop_db_run(*args, **kwargs):
        return None

    async def fake_db_first(*args, **kwargs):
        return {"sender_open_id": "ou_requester"}

    worker._db_run = noop_db_run
    worker._db_first = fake_db_first
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.cards[0][1]["header"]["template"] == "red"
    assert relay.feishu.cards[0][1]["header"]["title"]["content"] == "日程操作失败"
    assert relay.feishu.cards[0][2] == worker._calendar_result_idempotency_uuid("req_1")
    assert state.saved == ("om_failed_result_card", "feishu:app:chat:x")


def test_reply_card_sends_a_stable_uuid_when_provided():
    worker = _load_worker_module()
    api = worker.FeishuAPI(SimpleNamespace())
    requests = []

    async def fake_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {"data": {"message_id": "om_card"}}

    api._request = fake_request
    card = {"header": {"title": {"content": "日程操作成功"}}}

    result = asyncio.run(api.reply_card("om_source", card, uuid="stable-result-uuid"))

    assert result == "om_card"
    assert requests[0][2]["json"]["uuid"] == "stable-result-uuid"


def test_feishu_reaction_api_adds_lists_and_deletes_the_exact_reaction():
    worker = _load_worker_module()
    api = worker.FeishuAPI(SimpleNamespace())
    requests = []

    async def fake_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == "POST":
            return {"data": {"reaction_id": "reaction_123"}}
        if method == "GET":
            return {
                "data": {
                    "items": [
                        {
                            "reaction_id": "reaction_123",
                            "operator": {"operator_id": "cli_dev", "operator_type": "app"},
                            "reaction_type": {"emoji_type": "Typing"},
                        }
                    ],
                    "has_more": False,
                }
            }
        return {"data": {}}

    api._request = fake_request

    async def exercise():
        reaction_id = await api.add_reaction("om_source", "Typing")
        items = await api.list_reactions("om_source", "Typing")
        await api.delete_reaction("om_source", reaction_id)
        return reaction_id, items

    reaction_id, items = asyncio.run(exercise())

    assert reaction_id == "reaction_123"
    assert items[0]["reaction_type"]["emoji_type"] == "Typing"
    assert requests[0] == (
        "POST",
        "/open-apis/im/v1/messages/om_source/reactions",
        {"json": {"reaction_type": {"emoji_type": "Typing"}}},
    )
    assert requests[1][2]["params"] == {
        "page_size": 50,
        "reaction_type": "Typing",
        "user_id_type": "open_id",
    }
    assert requests[2][:2] == (
        "DELETE",
        "/open-apis/im/v1/messages/om_source/reactions/reaction_123",
    )


def test_processing_reaction_uses_typing_and_cleanup_targets_only_saved_reaction_id():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        def __init__(self):
            self.updated = []

        async def update_run(self, request_id, **fields):
            self.updated.append((request_id, fields))

    class FakeFeishu:
        env = SimpleNamespace()
        app_id_env = "FEISHU_APP_ID"

        def __init__(self):
            self.added = []
            self.deleted = []

        async def add_reaction(self, message_id, emoji_type):
            self.added.append((message_id, emoji_type))
            return "reaction_123"

        async def delete_reaction(self, message_id, reaction_id):
            self.deleted.append((message_id, reaction_id))

    async def claim_run(_db, sql, *params):
        assert "processing_reaction_state = 'none'" in sql
        assert params[-1] == "req_1"
        return [{"request_id": "req_1"}]

    async def no_reaction_event(*_args, **_kwargs):
        return None

    worker._db_all = claim_run
    worker._db_first = no_reaction_event
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()
    run = {
        "request_id": "req_1",
        "conversation_key": "feishu:app:chat:x",
        "source_message_id": "om_source",
        "created_at": 1,
        "processing_reaction_state": "none",
    }

    async def exercise():
        await relay._ensure_processing_reaction("req_1", run)
        active = {
            **run,
            "processing_reaction_id": "reaction_123",
            "processing_reaction_state": "active",
        }
        await relay._clear_processing_reaction("req_1", active)

    asyncio.run(exercise())

    assert relay.feishu.added == [("om_source", "Typing")]
    assert relay.feishu.deleted == [("om_source", "reaction_123")]
    assert state.updated == [
        (
            "req_1",
            {
                "processing_reaction_id": "reaction_123",
                "processing_reaction_state": "active",
            },
        ),
        ("req_1", {"processing_reaction_state": "cleared"}),
    ]


def test_delivered_result_retry_cleans_reaction_without_sending_second_reply():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, _request_id):
            return {
                "request_id": "req_1",
                "source_message_id": "om_source",
                "status": "done",
                "title": "完成",
                "markdown": "结果正文",
                "delivered": 1,
                "conversation_key": "feishu:app:chat:x",
                "processing_reaction_id": "reaction_123",
                "processing_reaction_state": "active",
            }

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

    class FakeFeishu:
        def __init__(self):
            self.deleted = []
            self.replies = []

        async def delete_reaction(self, message_id, reaction_id):
            self.deleted.append((message_id, reaction_id))

        async def reply(self, *_args, **_kwargs):
            self.replies.append((_args, _kwargs))

    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.deleted == [("om_source", "reaction_123")]
    assert relay.feishu.replies == []
    assert state.updated == ("req_1", {"processing_reaction_state": "cleared"})


def test_processing_reaction_cleanup_retries_with_backoff_delay():
    worker = _load_worker_module()

    class FakeState:
        def __init__(self, _db):
            pass

        async def get_run(self, _request_id):
            return None

        async def update_run(self, *_args, **_kwargs):
            return None

    class FakeRelay:
        def __init__(self, env, _ctx, _state):
            self.env = env

        async def deliver_result(self, _request_id):
            raise worker.ProcessingReactionCleanupPending("retry cleanup")

    class FakeMessage:
        body = {"kind": "deliver_result", "request_id": "req_1"}
        attempts = 2

        def __init__(self):
            self.retried = []

        def retry(self, **kwargs):
            self.retried.append(kwargs)

    message = FakeMessage()
    worker.D1State = FakeState
    worker.CloudflareRelay = FakeRelay
    entrypoint = object.__new__(worker.Default)
    entrypoint.env = SimpleNamespace(DB=object())
    entrypoint.ctx = None

    asyncio.run(entrypoint.queue(SimpleNamespace(messages=[message])))

    assert message.retried == [{"delaySeconds": 120}]


def test_blocked_result_uses_text_even_when_card_format_was_requested():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "status": "blocked",
                "title": "[calendar-result-card] 缺少日历权限",
                "markdown": "请重新授权。",
                "delivered": 0,
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []

        async def reply(self, message_id, text, *, uuid=None):
            self.replies.append((message_id, text, uuid))
            return "om_result_text"

    async def noop_db_run(*args, **kwargs):
        return None

    worker._db_run = noop_db_run
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.replies == [
        (
            "om_source",
            "Agent 任务被阻塞：缺少日历权限\n请重新授权。",
            worker._result_delivery_idempotency_uuid("req_1"),
        )
    ]
    assert state.saved == ("om_result_text", "feishu:app:chat:x")


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

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []

        async def update(self, message_id, text):
            raise RuntimeError("Feishu update temporarily failed")

        async def reply(self, message_id, text, *, uuid=None):
            self.replies.append((message_id, text, uuid))
            return "om_result"

    async def noop_db_run(*args, **kwargs):
        return None

    worker._db_run = noop_db_run
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_result("req_1"))

    assert relay.feishu.replies == [
        (
            "om_source",
            "完成\n结果正文",
            worker._result_delivery_idempotency_uuid("req_1"),
        )
    ]
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
                "confirmation_required": 0,
        },
    )
    assert queue.messages == [{"kind": "deliver_question", "request_id": "req_1"}]


def test_question_delivery_updates_placeholder_and_keeps_run_resumable():
    worker = _load_worker_module()

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            return {
                "request_id": request_id,
                "source_message_id": "om_source",
                "placeholder_message_id": "om_placeholder",
                "status": "needs_user",
                "progress_message": "请确认创建日程？",
                "conversation_key": "feishu:app:chat:x",
            }

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

        async def update_run(self, request_id, **fields):
            self.updated = (request_id, fields)

    class FakeFeishu:
        def __init__(self):
            self.replies = []
            self.updates = []

        async def reply(self, message_id, text, *, uuid=None):
            self.replies.append((message_id, text, uuid))
            return "om_question"

        async def update(self, message_id, text):
            self.updates.append((message_id, text))

    async def requester_lookup(_db, sql, *_params):
        if "sender_open_id" in sql:
            return {"sender_open_id": "ou_requester"}
        return None

    worker._db_first = requester_lookup
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_question("req_1"))

    assert relay.feishu.replies == [
        (
            "om_source",
            '<at user_id="ou_requester">你</at>\n\n请确认创建日程？',
            worker._question_delivery_idempotency_uuid("req_1"),
        )
    ]
    assert relay.feishu.updates == [("om_placeholder", "请查看我对原消息的引用回复。")]
    assert state.saved == ("om_question", "feishu:app:chat:x")
    assert state.updated == ("req_1", {"placeholder_message_id": "om_question"})


def test_question_delivery_falls_back_to_a_reply_and_rebinds_placeholder():
    worker = _load_worker_module()

    class FakeState:
        db = object()

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

        async def save_reply(self, outbound_id, conversation_key, **kwargs):
            self.saved = (outbound_id, conversation_key)

    class FakeFeishu:
        def __init__(self):
            self.replies = []

        async def update(self, message_id, text):
            raise RuntimeError("Feishu update temporarily failed")

        async def reply(self, message_id, text, *, uuid=None):
            self.replies.append((message_id, text, uuid))
            return "om_question"

    async def requester_lookup(_db, sql, *_params):
        if "sender_open_id" in sql:
            return {"sender_open_id": "ou_requester"}
        return None

    worker._db_first = requester_lookup
    state = FakeState()
    relay = worker.CloudflareRelay(SimpleNamespace(), None, state)
    relay.feishu = FakeFeishu()

    asyncio.run(relay.deliver_question("req_1"))

    assert relay.feishu.replies == [
        (
            "om_source",
            '<at user_id="ou_requester">你</at>\n\n请确认创建日程？',
            worker._question_delivery_idempotency_uuid("req_1"),
        )
    ]
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
        "get_user_memory",
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
            "save_user_memory",
            "forget_user_memory",
            "create_private_group",
            "create_group",
            "update_group",
            "send_image",
        }
    )


def test_cloudflare_mcp_memory_tools_are_sender_scoped_and_verified():
    worker = _load_worker_module()

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

    class FakeQueue:
        def __init__(self):
            self.messages = []

        async def send(self, body):
            self.messages.append(body)

    async def scenario():
        state = worker.D1State(FakeD1())
        await state.ensure_schema()
        await state.claim_event(
            message_id="om_memory_source",
            request_id="feishu_req_memory",
            conversation_key="feishu:app:chat:1",
            chat_id="oc_1",
            open_id="ou_requester",
        )
        await state.create_run(
            request_id="feishu_req_memory",
            conversation_key="feishu:app:chat:1",
            source_message_id="om_memory_source",
            input_markdown="test input",
        )
        queue = FakeQueue()
        relay = worker.CloudflareRelay(
            SimpleNamespace(
                AGENT_QUEUE=queue,
                WORKSPACE_AGENT_RELAY_MCP_NAME="workspace-agent-relay-mcp-dev",
            ),
            None,
            state,
        )
        args = {
            "request_id": "feishu_req_memory",
            "conversation_key": "feishu:app:chat:1",
            "memory_key": "calendar default",
            "memory_type": "rule",
            "memory_value": "未指定日历时使用 Bo Bozway",
        }
        tool_names = {item["name"] for item in relay.tool_definitions()}
        saved = await relay.call_tool("save_user_memory", args)
        requester_items = await state.user_memories(
            "workspace-agent-relay-mcp-dev", "feishu", "ou_requester"
        )
        other_user_items = await state.user_memories(
            "workspace-agent-relay-mcp-dev", "feishu", "ou_someone_else"
        )
        await state.claim_event(
            message_id="om_memory_followup",
            request_id="feishu_req_followup",
            conversation_key="feishu:app:chat:1",
            chat_id="oc_1",
            open_id="ou_requester",
        )
        await relay.agent_workflow.handle_event(
            platform="feishu",
            conversation_key="feishu:app:chat:1",
            event={
                "message_id": "om_memory_followup",
                "open_id": "ou_requester",
                "text": "创建日程",
            },
            request_id="feishu_req_followup",
        )
        followup_run = await state.get_run("feishu_req_followup")
        forgotten = await relay.call_tool(
            "forget_user_memory",
            {
                "request_id": "feishu_req_memory",
                "conversation_key": "feishu:app:chat:1",
                "memory_keys": ["calendar default"],
            },
        )
        after_forget = await state.user_memories(
            "workspace-agent-relay-mcp-dev", "feishu", "ou_requester"
        )
        return (
            tool_names,
            saved,
            requester_items,
            other_user_items,
            followup_run,
            queue.messages,
            forgotten,
            after_forget,
        )

    (
        tool_names,
        saved,
        requester_items,
        other_user_items,
        followup_run,
        queued_messages,
        forgotten,
        after_forget,
    ) = asyncio.run(scenario())

    assert {"get_user_memory", "save_user_memory", "forget_user_memory"} <= tool_names
    assert saved["isError"] is False
    assert saved["structuredContent"]["verified"] is True
    assert requester_items[0]["memory_value"] == "未指定日历时使用 Bo Bozway"
    assert other_user_items == []
    assert '"value":"未指定日历时使用 Bo Bozway"' in followup_run["input_markdown"]
    assert followup_run["input_markdown"].index('"value":"未指定日历时使用 Bo Bozway"') < (
        followup_run["input_markdown"].index("User task:\n创建日程")
    )
    assert queued_messages[0]["request_id"] == "feishu_req_followup"
    assert forgotten["isError"] is False
    assert forgotten["structuredContent"]["deleted_keys"] == ["calendar default"]
    assert forgotten["structuredContent"]["verified"] is True
    assert after_forget == []


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


def test_workspace_oauth_registration_reuses_client_without_touching_tokens(monkeypatch):
    worker = _load_worker_module()
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: {
            "payload": payload,
            "status": status,
            "headers": headers or {},
        },
    )
    db_calls = []

    async def query_all(_db, sql, *params):
        db_calls.append((sql, params))
        assert "oauth_clients" in sql
        assert "oauth_tokens" not in sql
        assert params == ("ChatGPT",)
        return [
            {
                "client_id": "mcp_client_existing_dev",
                "client_name": "ChatGPT",
                "redirect_uris_json": '["https://chatgpt.com/callback"]',
                "created_at": 123,
            }
        ]

    async def unexpected_write(_db, sql, *_params):
        raise AssertionError(f"reconnect should not write: {sql}")

    monkeypatch.setattr(worker, "_db_all", query_all)
    monkeypatch.setattr(worker, "_db_run", unexpected_write)

    class FakeState:
        db = object()

        async def ensure_relay_oauth_schema(self):
            pass

    class Request:
        method = "POST"
        headers = {"content-type": "application/json"}
        url = "https://bot.boooe.com/agent-config/oauth/register"

        async def json(self):
            return {
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chatgpt.com/callback"],
            }

    relay = worker.CloudflareRelay(SimpleNamespace(), None, FakeState())
    response = asyncio.run(relay.oauth(Request(), "/oauth/register"))

    assert response["status"] == 200
    assert response["payload"]["client_id"] == "mcp_client_existing_dev"
    assert db_calls
    assert all("oauth_tokens" not in sql for sql, _ in db_calls)


def test_unknown_workspace_oauth_client_error_explains_registration_without_token_changes(
    monkeypatch,
):
    worker = _load_worker_module()
    monkeypatch.setattr(
        worker,
        "_text_response",
        lambda body, status=200, headers=None: {
            "body": body,
            "status": status,
            "headers": headers or {},
        },
    )

    class FakeState:
        async def ensure_relay_oauth_schema(self):
            pass

        async def oauth_client(self, _client_id):
            return None

    class Request:
        method = "POST"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        url = "https://bot.boooe.com/agent-config/oauth/authorize"

        async def text(self):
            return (
                "login_token=dev-login&client_id=stale-client&"
                "redirect_uri=https%3A%2F%2Fchatgpt.com%2Fcallback&response_type=code"
            )

    relay = worker.CloudflareRelay(
        SimpleNamespace(WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN="dev-login"),
        None,
        FakeState(),
    )
    response = asyncio.run(relay.oauth(Request(), "/oauth/authorize"))

    assert response["status"] == 400
    assert "Unknown client" in response["body"]
    assert "Dynamic Client Registration first" in response["body"]
    assert "issued OAuth tokens were not changed" in response["body"]


def test_chatgpt_public_oauth_client_is_registered_only_after_dev_login(monkeypatch):
    worker = _load_worker_module()
    monkeypatch.setattr(
        worker,
        "_text_response",
        lambda body, status=200, headers=None: {
            "body": body,
            "status": status,
            "headers": headers or {},
        },
    )
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: {
            "payload": payload,
            "status": status,
            "headers": headers or {},
        },
    )
    statements = []

    async def db_run(_db, sql, *params):
        statements.append((sql, params))
        return SimpleNamespace(meta={"changes": 1})

    monkeypatch.setattr(worker, "_db_run", db_run)

    class FakeState:
        db = object()

        def __init__(self):
            self.client = None

        async def ensure_relay_oauth_schema(self):
            pass

        async def oauth_client(self, client_id):
            if self.client and self.client["client_id"] == client_id:
                return self.client
            return None

    class Request:
        method = "POST"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        url = "https://bot.boooe.com/agent-config/oauth/authorize"

        async def text(self):
            return (
                "login_token=dev-login&"
                "client_id=https%3A%2F%2Fchatgpt.com%2Foauth%2Fclient-123&"
                "redirect_uri=https%3A%2F%2Fchatgpt.com%2Fconnector_platform_oauth_redirect&"
                "response_type=code&code_challenge=challenge&"
                "code_challenge_method=S256&scope=workspace-agent-relay&"
                "resource=https%3A%2F%2Fbot.boooe.com%2Fagent-config%2Fmcp"
            )

    state = FakeState()
    relay = worker.CloudflareRelay(
        SimpleNamespace(WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN="dev-login"), None, state
    )
    original_db_run = worker._db_run

    async def capture_insert(_db, sql, *params):
        result = await db_run(_db, sql, *params)
        if "INSERT OR IGNORE INTO oauth_clients" in sql:
            state.client = {
                "client_id": params[0],
                "client_name": params[1],
                "redirect_uris_json": params[2],
            }
        return result

    monkeypatch.setattr(worker, "_db_run", capture_insert)
    response = asyncio.run(relay.oauth(Request(), "/oauth/authorize"))
    monkeypatch.setattr(worker, "_db_run", original_db_run)

    assert response["status"] == 302
    assert response["headers"]["Location"].startswith("https://chatgpt.com/")
    assert any("INSERT OR IGNORE INTO oauth_clients" in sql for sql, _ in statements)
    assert any("INSERT INTO oauth_codes" in sql for sql, _ in statements)
    assert all("oauth_tokens" not in sql for sql, _ in statements)


def test_managed_rules_prompt_makes_active_override_authoritative():
    worker = _load_worker_module()
    previous = sys.modules.get("worker_app")
    sys.modules["worker_app"] = worker
    spec = importlib.util.spec_from_file_location(
        "managed_rules_prompt_regression_test",
        ROOT / "cloudflare_worker/src/agent_config_mcp.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        prompt = module.managed_rules_prompt(
            "xiaoc",
            [
                {
                    "key": "calendar.default_timezone",
                    "value": "America/Los_Angeles",
                    "version": 4,
                }
            ],
        )
    finally:
        if previous is None:
            sys.modules.pop("worker_app", None)
        else:
            sys.modules["worker_app"] = previous

    assert "active managed override is authoritative" in prompt
    assert "source defaults apply only when no active override exists" in prompt
    assert '"America/Los_Angeles"' in prompt


def test_agent_config_oauth_prefix_uses_shared_login_and_keeps_resource(monkeypatch):
    worker = _load_worker_module()
    monkeypatch.setattr(
        worker,
        "_text_response",
        lambda body, status=200, headers=None: {
            "body": body,
            "status": status,
            "headers": headers or {},
        },
    )

    class FakeState:
        async def ensure_relay_oauth_schema(self):
            pass

        async def oauth_client(self, _client_id):
            return None

    class Request:
        method = "GET"
        headers = {}
        url = (
            "https://bot.boooe.com/agent-config/oauth/authorize?"
            "response_type=code&client_id=client&redirect_uri=https%3A%2F%2Fchatgpt.com%2Fcallback"
        )

    env = SimpleNamespace(
        WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN="shared-login",
        AGENT_CONFIG_MCP_OAUTH_LOGIN_TOKEN="legacy-agent-config-login",
        XIAOT_MCP_OAUTH_LOGIN_TOKEN="legacy-xiaot-login",
    )
    relay = worker.CloudflareRelay(env, None, FakeState())
    page = asyncio.run(relay.oauth(Request(), "/oauth/authorize"))

    assert "action='/agent-config/oauth/authorize'" in page["body"]

    class PostRequest:
        method = "POST"
        headers = {"content-type": "application/x-www-form-urlencoded"}
        url = "https://bot.boooe.com/agent-config/oauth/authorize"

        async def text(self):
            return (
                "login_token=legacy-agent-config-login&client_id=unknown&"
                "redirect_uri=https%3A%2F%2Fchatgpt.com%2Fcallback&response_type=code"
            )

    post = asyncio.run(relay.oauth(PostRequest(), "/oauth/authorize"))
    assert post["status"] == 400
    assert "Unknown client" in post["body"]


def test_agent_config_oauth_routes_to_common_authorization_server(monkeypatch):
    worker = _load_worker_module()

    class FakeState:
        pass

    class FakeRelay:
        def __init__(self, _env, _ctx, _state):
            pass

        async def oauth(self, _request, path):
            return {"delegated_path": path}

    class Request:
        method = "GET"
        url = "https://bot.boooe.com/agent-config/oauth/authorize"

    monkeypatch.setattr(worker, "D1State", lambda _db: FakeState())
    monkeypatch.setattr(worker, "CloudflareRelay", FakeRelay)
    entrypoint = worker.Default()
    entrypoint.env = SimpleNamespace(DB=object())
    entrypoint.ctx = None

    result = asyncio.run(entrypoint.fetch(Request()))
    assert result == {"delegated_path": "/oauth/authorize"}


def test_shared_oauth_login_accepts_existing_dev_mcp_tokens_without_rotation(monkeypatch):
    worker = _load_worker_module()
    env = SimpleNamespace(
        WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN="shared-login",
        AGENT_CONFIG_MCP_OAUTH_LOGIN_TOKEN="legacy-agent-config-login",
        XIAOT_MCP_OAUTH_LOGIN_TOKEN="legacy-xiaot-login",
    )

    assert worker._oauth_login_token_matches(env, "shared-login")
    assert worker._oauth_login_token_matches(env, "legacy-agent-config-login")
    assert worker._oauth_login_token_matches(env, "legacy-xiaot-login")
    assert not worker._oauth_login_token_matches(env, "wrong-login")


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
