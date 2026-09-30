from __future__ import annotations

import asyncio
import importlib.util
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


def test_xiaot_dedicated_mcp_has_only_xiaot_tools_and_correct_identity(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    relay = worker.XiaotOnlyCloudflareRelay(
        SimpleNamespace(WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL="https://bot.boooe.com"),
        None,
        SimpleNamespace(),
    )

    tools = relay.tool_definitions()
    names = {tool["name"] for tool in tools}
    assert names == worker.XIAOT_TOOL_NAMES | worker._RELAY_TOOLS
    assert not names.intersection(
        {"create_group", "update_group", "search_contacts", "send_image", "get_user_images"}
    )
    search = next(tool for tool in tools if tool["name"] == "search_records")
    assert search["inputSchema"]["properties"]["include_finished"] == {
        "type": "boolean",
        "default": False,
    }
    assert search["inputSchema"]["properties"]["mine_only"] == {
        "type": "boolean",
        "default": False,
    }
    requester_info = next(tool for tool in tools if tool["name"] == "get_requester_info")
    assert "matching Feishu/Lark OAuth app" in requester_info["description"]
    people_search = next(tool for tool in tools if tool["name"] == "search_person_candidates")
    assert people_search["annotations"]["readOnlyHint"] is True

    async def no_auth(self, _request, **_kwargs):
        return None

    monkeypatch.setattr(app.CloudflareRelay, "authorize_request", no_auth)

    class Request:
        method = "POST"
        headers = {}

        async def json(self):
            return {"jsonrpc": "2.0", "id": 1, "method": "initialize"}

    app.Response.json = staticmethod(
        lambda payload, status=200, headers=None: SimpleNamespace(
            payload=payload, status=status, headers=headers or {}
        )
    )
    response = asyncio.run(
        relay.mcp(
            Request(),
            resource_path=app.XIAOT_MCP_PATH,
            resource_name=app.XIAOT_MCP_NAME,
            initialize_instructions="isolated Xiao T server",
        )
    )
    assert response.payload["result"]["serverInfo"]["name"] == app.XIAOT_MCP_NAME
    assert response.payload["result"]["instructions"] == "isolated Xiao T server"

    info = asyncio.run(relay.call_tool("server_info", {}))["structuredContent"]
    assert info["app_name"] == app.XIAOT_MCP_NAME
    assert info["mcp_path"] == app.XIAOT_MCP_PATH


def test_xiaot_mcp_oauth_metadata_binds_its_own_resource(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    relay = worker.XiaotOnlyCloudflareRelay(
        SimpleNamespace(WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL="https://bot.boooe.com"),
        None,
        SimpleNamespace(),
    )

    app.Response.json = staticmethod(
        lambda payload, status=200, headers=None: SimpleNamespace(
            payload=payload, status=status, headers=headers or {}
        )
    )

    class Request:
        method = "GET"
        headers = {}

    resource = asyncio.run(
        relay.oauth(
            Request(),
            "/.well-known/oauth-protected-resource/xiaot/mcp",
            resource_path=app.XIAOT_MCP_PATH,
            resource_name=app.XIAOT_MCP_NAME,
            oauth_prefix="/xiaot",
            issuer_path="/xiaot",
        )
    )
    assert resource.payload["resource"] == "https://bot.boooe.com/xiaot/mcp"
    assert resource.payload["authorization_servers"] == ["https://bot.boooe.com/xiaot"]
    assert resource.payload["resource_name"] == app.XIAOT_MCP_NAME

    authorization = asyncio.run(
        relay.oauth(
            Request(),
            "/.well-known/oauth-authorization-server/xiaot",
            resource_path=app.XIAOT_MCP_PATH,
            resource_name=app.XIAOT_MCP_NAME,
            oauth_prefix="/xiaot",
            issuer_path="/xiaot",
        )
    )
    assert authorization.payload["issuer"] == "https://bot.boooe.com/xiaot"
    assert authorization.payload["authorization_endpoint"] == (
        "https://bot.boooe.com/xiaot/oauth/authorize"
    )


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


def test_writes_reject_unknown_or_computed_fields():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token, platform="feishu"):
        assert access_token == "user-token"
        assert platform == "feishu"
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


def test_date_fields_are_normalized_to_millisecond_timestamps():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token, platform="feishu"):
        return [
            {"field_name": "Task Name", "type": 1},
            {"field_name": "Dead Line", "type": 5},
            {"field_name": "Owner", "type": 11, "ui_type": "User"},
        ]

    client.fields = fields

    async def scenario():
        return await client.validate_writable_fields(
            "task",
            {"Task Name": "test", "Dead Line": "2026-10-01"},
            access_token="user-token",
            platform="feishu",
        )

    normalized = asyncio.run(scenario())
    assert normalized == {
        "Task Name": "test",
        "Dead Line": 1790784000000,
    }
    assert worker._normalize_bitable_date_value(1790784000) == 1790784000000
    assert worker._normalize_bitable_date_value("2026-10-01T00:00:00Z") == 1790812800000

    try:
        asyncio.run(
            client.validate_writable_fields(
                "task",
                {"Dead Line": "明天"},
                access_token="user-token",
            )
        )
    except ValueError as exc:
        assert "ISO 日期/时间" in str(exc)
    else:
        raise AssertionError("natural-language date was accepted as a Bitable date value")


def test_relation_fields_use_string_record_id_arrays_and_verify_readback():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token, platform="feishu"):
        assert access_token == "user-token"
        assert platform == "feishu"
        return [
            {"field_name": "Parent Goal", "type": 18, "ui_type": "SingleLink"},
            {"field_name": "Parent Project", "type": 18, "ui_type": "SingleLink"},
            {"field_name": "Task Name", "type": 1},
        ]

    client.fields = fields
    normalized = asyncio.run(
        client.validate_writable_fields(
            "project",
            {
                "Parent Goal": [{"record_id": "recGoal123"}],
                "Parent Project": ["recProject456"],
                "Task Name": "Child project",
            },
            access_token="user-token",
        )
    )

    assert normalized == {
        "Parent Goal": ["recGoal123"],
        "Parent Project": ["recProject456"],
        "Task Name": "Child project",
    }
    assert worker.XiaotCloudflareRelay._fields_match(
        {
            "fields": {
                "Parent Goal": [{"record_id": "recGoal123", "text": "Goal"}],
                "Parent Project": [{"record_id": "recProject456", "text": "Project"}],
                "Task Name": "Child project",
            }
        },
        normalized,
    )

    for invalid in (
        {"Parent Goal": {"record_id": "recGoal123"}},
        {"Parent Goal": [{"record_id": "not-a-record-id"}]},
    ):
        try:
            asyncio.run(
                client.validate_writable_fields(
                    "project", invalid, access_token="user-token"
                )
            )
        except ValueError:
            continue
        raise AssertionError(f"invalid relation value was accepted: {invalid}")


def test_person_candidate_search_uses_visible_bitable_people_and_aliases():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())
    calls = []

    async def fields(_table_key, *, access_token, platform="feishu"):
        return [
            {"field_name": "Task Name", "type": 1},
            {"field_name": "Owner", "type": 11, "ui_type": "User"},
            {"field_name": "关注人", "type": 11, "ui_type": "User"},
        ]

    async def records(_table_key, **kwargs):
        calls.append(kwargs)
        assert kwargs["field_names"] == ["Owner", "关注人"]
        assert kwargs["page_size"] == 500
        if len(calls) == 1:
            return {
                "items": [
                    {
                        "fields": {
                            "Owner": [{"id": "ou_liqian", "name": "李谦"}],
                            "关注人": [{"id": "ou_other", "name": "其他人"}],
                        }
                    }
                ],
                "has_more": True,
                "page_token": "next",
            }
        return {
            "items": [
                {"fields": {"Owner": [{"id": "ou_liqian", "name": "李谦（William）"}]}}
            ],
            "has_more": False,
        }

    client.fields = fields
    client.records = records
    result = asyncio.run(
        client.search_person_candidates(
            "task",
            "William",
            access_token="user-token",
            platform="feishu",
        )
    )

    assert len(calls) == 2
    assert calls[1]["page_token"] == "next"
    assert result["scan_truncated"] is False
    assert result["candidates"] == [
        {"id": "ou_liqian", "name": "李谦", "fields": ["Owner"], "exact_match": True}
    ]


def test_person_candidate_search_marks_name_fragments_as_suggestions_only():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token, platform="feishu"):
        return [{"field_name": "Owner", "type": 11}]

    async def records(_table_key, **_kwargs):
        return {
            "items": [{"fields": {"Owner": [{"id": "ou_liqianwen", "name": "李谦文"}]}}],
            "has_more": False,
        }

    client.fields = fields
    client.records = records
    result = asyncio.run(
        client.search_person_candidates(
            "task", "李谦", access_token="user-token", platform="feishu"
        )
    )
    assert result["candidates"] == [
        {"id": "ou_liqianwen", "name": "李谦文", "fields": ["Owner"], "exact_match": False}
    ]


def test_person_candidate_search_stops_once_it_proves_identity_is_ambiguous():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(object())

    async def fields(_table_key, *, access_token, platform="feishu"):
        return [{"field_name": "Owner", "type": 11}]

    async def records(_table_key, **_kwargs):
        return {
            "items": [
                {"fields": {"Owner": [{"id": "ou_one", "name": "张伟"}]}},
                {"fields": {"Owner": [{"id": "ou_two", "name": "张伟"}]}},
            ],
            "has_more": True,
            "page_token": "unused",
        }

    client.fields = fields
    client.records = records
    result = asyncio.run(
        client.search_person_candidates(
            "task", "张伟", access_token="user-token", platform="feishu"
        )
    )
    assert {candidate["id"] for candidate in result["candidates"]} == {"ou_one", "ou_two"}
    assert result["scan_truncated"] is False
    assert result["scanned_records"] == 2


def test_bitable_person_candidate_reads_project_only_person_fields():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(SimpleNamespace())
    calls = []

    async def response(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"code": 0, "data": {"items": [], "has_more": False}}

    client.request = response
    asyncio.run(
        client.records(
            "task",
            access_token="user-token",
            page_size=500,
            field_names=["Owner", "关注人"],
            platform="feishu",
        )
    )

    assert calls[0][2]["params"] == {
        "page_size": 500,
        "field_names": '["Owner", "关注人"]',
        "user_id_type": "open_id",
    }


def test_bitable_create_and_update_explicitly_use_open_id_for_person_fields():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(SimpleNamespace())
    calls = []

    async def response(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"code": 0, "data": {"record": {"record_id": "rec_target"}}}

    client.request = response

    async def scenario():
        await client.create_record(
            "task",
            {"Owner": [{"id": "ou_oauth_user"}]},
            access_token="user-token",
            platform="feishu",
        )
        await client.update_record(
            "task",
            "rec_target",
            {"Owner": [{"id": "ou_oauth_user"}]},
            access_token="user-token",
            platform="feishu",
        )

    asyncio.run(scenario())

    assert [call[2]["params"] for call in calls] == [
        {"user_id_type": "open_id"},
        {"user_id_type": "open_id"},
    ]
    assert all(call[2]["json"]["fields"]["Owner"] == [{"id": "ou_oauth_user"}] for call in calls)


def test_get_requester_info_returns_platform_oauth_open_id(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db=object())

    async def current_run(_db, sql, *params):
        if "FROM relay_runs" in sql:
            (conversation_key,) = params
            assert conversation_key == "xiaot:cli_xiaot:oc_group:current"
            return {"request_id": "xiaot_req_current"}
        assert "FROM xiaot_run_requesters" in sql
        request_id, conversation_key = params
        assert request_id == "xiaot_req_current"
        assert conversation_key == "xiaot:cli_xiaot:oc_group:current"
        return {"platform": "lark", "open_id": "ou_lark_oauth_app_id"}

    monkeypatch.setattr(worker, "_db_first", current_run)
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    result = asyncio.run(
        relay.call_tool(
            "get_requester_info",
            {"conversation_key": "xiaot:cli_xiaot:oc_group:current"},
        )
    )

    assert result["structuredContent"]["requester"] == {
        "platform": "lark",
        "open_id": "ou_lark_oauth_app_id",
        "user_id_type": "open_id",
    }


def test_get_requester_info_never_falls_back_to_bot_event_identity(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db=object())
    sql_calls = []

    async def db_first(_db, sql, *_params):
        sql_calls.append(sql)
        if "FROM relay_runs" in sql:
            return {"request_id": "xiaot_req_current"}
        if "FROM xiaot_run_requesters" in sql:
            return None
        raise AssertionError("requester lookup must not fall back to webhook event identity")

    monkeypatch.setattr(worker, "_db_first", db_first)
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    result = asyncio.run(
        relay.call_tool(
            "get_requester_info",
            {"conversation_key": "xiaot:cli_xiaot:oc_group:current"},
        )
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "requester_not_found"
    assert len(sql_calls) == 2


def test_xiaot_rejects_all_changelog_mutations(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.xiaot_bitable = worker.XiaotBitableClient(SimpleNamespace())

    async def active_run(_args):
        return {"request_id": "xiaot_req"}

    async def requester(_request_id, _conversation_key):
        return {"platform": "feishu", "open_id": "ou_current"}

    async def user_token(_request_id, _conversation_key):
        return "user-token"

    relay._require_xiaot_run = active_run
    relay._requester_for_run = requester
    relay._user_access_token_for_run = user_token

    async def scenario():
        try:
            await relay._prepare_mutation(
                {
                    "request_id": "xiaot_req",
                    "conversation_key": "xiaot:cli_xiaot:oc_group:current",
                    "operation": "create",
                    "table_key": "changelog",
                    "fields": {"修改日志": "manual entry"},
                }
            )
        except ValueError as exc:
            return str(exc)
        raise AssertionError("XiaoT must not write to Changelog")

    assert "不会写入 Changelog" in asyncio.run(scenario())


def test_confirmation_accepts_common_affirmations_and_rejects_negations():
    worker = _load_xiaot_module()
    check = worker.XiaotCloudflareRelay._is_explicit_confirmation

    for operation in ("create", "update", "delete"):
        for reply in (
            "行",
            "好",
            "ok",
            "可以",
            "执行",
            "开始执行",
            "开始",
            "好的",
            "行的",
            "ok的",
            "没问题",
            "确认执行",
            "嗯，行",
            "我觉得可以",
            "我同意",
            "确定",
            "没事",
            "照办",
            "继续",
            "no problem",
            "sure thing",
        ):
            assert check(reply, operation), (operation, reply)

    assert check("确认创建", "create")
    assert check("确认更新", "update")
    assert check("确认删除", "delete")
    for operation, reply in (
        ("create", "不确认创建"),
        ("delete", "不要删除"),
        ("update", "不行"),
        ("update", "不好"),
        ("update", "不可以"),
        ("update", "取消"),
        ("update", "我不同意"),
        ("update", "no"),
        ("update", "nope"),
        ("update", "not now"),
        ("update", "可以吗？"),
        ("update", "我再想想"),
        ("update", "可以，但不要执行"),
        ("update", "行，不过先取消"),
        ("update", "确认删除"),
        ("create", "确认执行 T681 状态改为 Finished"),
    ):
        assert not check(reply, operation), (operation, reply)
    assert not check("可以，然后删除另一个", "update")
    assert not check("好吧，修改成别的内容", "update")


def test_xiaot_requester_identity_is_bound_to_detected_platform():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    class FakeState:
        db = object()

        async def ensure_feishu_oauth_schema(self):
            return None

    relay.state = FakeState()
    queries = []

    async def lookup(db, sql, *params):
        queries.append((db, sql, params))
        if "xiaot_run_requesters" in sql:
            return {"platform": "lark", "open_id": "ou_lark_user"}
        return None

    async def run_sql(_db, _sql, *_params):
        return None

    worker._db_first = lookup
    worker._db_run = run_sql
    identity = asyncio.run(
        relay._requester_for_run("xiaot_req_1", "xiaot:cli_xiaot:oc_group:abc123")
    )

    assert identity == {"platform": "lark", "open_id": "ou_lark_user"}
    assert "FROM xiaot_run_requesters" in queries[0][1]
    assert queries[0][2] == ("xiaot_req_1", "xiaot:cli_xiaot:oc_group:abc123")


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
        return {"platform": "feishu", "open_id": "ou_current_user"}

    class FakeState:
        db = object()

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            assert (platform, open_id) == ("feishu", "ou_current_user")
            return {"access_token": "user-token", "expires_at": 4_000_000_000}

    class FakeBitable:
        def __init__(self):
            self.filters = []

        async def fields(self, table_key, *, access_token, platform="feishu"):
            assert table_key == "task"
            assert access_token == "user-token"
            assert platform == "feishu"
            return [{"field_name": "Status", "type": 3}]

        async def records(self, table_key, *, access_token, platform="feishu", page_size, page_token, filter_formula):
            assert table_key == "task"
            assert access_token == "user-token"
            assert platform == "feishu"
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
                        "request_id": "xiaot_req_current",
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


def test_mine_only_search_matches_person_ids_across_pages_and_uses_local_cursor():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    current_run = {
        "request_id": "xiaot_req_current",
        "conversation_key": "xiaot:cli_xiaot:oc_group:current",
        "input_markdown": "User task:\n查我的任务",
    }

    async def require_run(args):
        assert args["request_id"] == current_run["request_id"]
        assert args["conversation_key"] == current_run["conversation_key"]
        return current_run

    async def requester_for_run(_request_id, _conversation_key):
        return {"platform": "feishu", "open_id": "ou_current_user"}

    class FakeState:
        db = object()

        async def ensure_feishu_oauth_schema(self):
            return None

        async def user_token(self, platform, open_id):
            assert (platform, open_id) == ("feishu", "ou_current_user")
            return {"access_token": "user-token", "expires_at": 4_000_000_000}

    class FakeBitable:
        def __init__(self):
            self.calls = []

        async def fields(self, table_key, *, access_token, platform="feishu"):
            assert table_key == "task"
            assert access_token == "user-token"
            assert platform == "feishu"
            return [
                {"field_name": "Status", "type": 3},
                {"field_name": "Owner", "type": 11, "ui_type": "User"},
            ]

        async def records(
            self,
            table_key,
            *,
            access_token,
            platform="feishu",
            page_size,
            page_token,
            filter_formula,
        ):
            assert table_key == "task"
            assert access_token == "user-token"
            assert platform == "feishu"
            assert page_size == 500
            assert filter_formula == 'CurrentValue.[Status]!="Finished"'
            self.calls.append(page_token)
            if not page_token:
                return {
                    "items": [
                        {
                            "record_id": "rec_other",
                            "fields": {
                                "Task ID": "T680",
                                "Task Name": "文帅-同名但不是本人",
                                "Status": "To-do",
                                "Owner": [{"id": "ou_other_user", "name": "文帅"}],
                            },
                        },
                        {
                            "record_id": "rec_first_owned",
                            "fields": {
                                "Task ID": "T681",
                                "Task Name": "文帅-测试",
                                "Status": "Waiting/Blocked",
                                "Owner": [{"id": "ou_current_user", "name": "文帅"}],
                            },
                        },
                    ],
                    "has_more": True,
                    "page_token": "api-page-2",
                }
            assert page_token == "api-page-2"
            return {
                "items": [
                    {
                        "record_id": "rec_second_owned",
                        "fields": {
                            "Task ID": "T700",
                            "Task Name": "本人第二项",
                            "Status": "In Progress",
                            "Owner": [{"id": "ou_current_user", "name": "其他显示名"}],
                        },
                    },
                    {
                        "record_id": "rec_same_name",
                        "fields": {
                            "Task ID": "T701",
                            "Task Name": "其他人的同名任务",
                            "Status": "To-do",
                            "Owner": [{"id": "ou_other_user", "name": "文帅"}],
                        },
                    },
                ],
                "has_more": False,
                "page_token": "",
            }

    relay.state = FakeState()
    relay.xiaot_bitable = FakeBitable()
    relay._require_xiaot_run = require_run
    relay._requester_for_run = requester_for_run

    async def scenario():
        first = await relay.call_tool(
            "search_records",
            {
                "request_id": current_run["request_id"],
                "conversation_key": current_run["conversation_key"],
                "table_key": "task",
                "mine_only": True,
                "page_size": 1,
            },
        )
        assert first["structuredContent"].get("success") is True, first
        second = await relay.call_tool(
            "search_records",
            {
                "request_id": current_run["request_id"],
                "conversation_key": current_run["conversation_key"],
                "table_key": "task",
                "mine_only": True,
                "page_size": 1,
                "page_token": first["structuredContent"]["page_token"],
            },
        )
        return first["structuredContent"], second["structuredContent"]

    first, second = asyncio.run(scenario())

    assert first["success"] is True
    assert first["mine_only"] is True
    assert [item["record_id"] for item in first["items"]] == ["rec_first_owned"]
    assert first["has_more"] is True
    assert first["page_token"] == "xiaot-owner:1"
    assert second["success"] is True
    assert [item["record_id"] for item in second["items"]] == ["rec_second_owned"]
    assert second["has_more"] is False
    assert second["page_token"] == ""
    assert relay.xiaot_bitable.calls == ["", "api-page-2", "", "api-page-2"]


def test_bitable_mutation_is_proposal_only_until_same_sender_confirms():
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

    class FakeBitable:
        def __init__(self):
            self.creates = []
            self.updates = []
            self.deletes = []
            self.row_fields = {"rec_target": {"Status": "Waiting/Blocked"}}
            self.deleted_records = set()
            self.stale_reads_remaining = {}
            self.stale_fields = {}

        @staticmethod
        def resolve_table(table_key):
            return worker.XiaotBitableClient.resolve_table(table_key)

        async def validate_writable_fields(self, table_key, fields, *, access_token, platform="feishu"):
            assert table_key == "task"
            assert fields in (
                {"Task Name": "Xiaot test"},
                {"Status": "In Progress"},
            )
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            return fields

        async def create_record(self, table_key, fields, *, access_token, platform="feishu"):
            self.creates.append((table_key, fields))
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            record_id = "rec_audit" if table_key == "changelog" else "rec_target"
            self.row_fields[record_id] = fields
            return {"record_id": record_id, "fields": fields}

        async def update_record(self, table_key, record_id, fields, *, access_token, platform="feishu"):
            self.updates.append((table_key, record_id, fields))
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            if table_key == "task":
                self.row_fields[record_id] = {**self.row_fields[record_id], **fields}
            return {"record_id": record_id, "fields": fields}

        async def delete_record(self, table_key, record_id, *, access_token, platform="feishu"):
            self.deletes.append((table_key, record_id))
            assert table_key == "task"
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            if record_id in self.deleted_records:
                return {"deleted": True, "record_id": record_id}
            self.stale_fields[record_id] = self.row_fields.pop(record_id)
            self.deleted_records.add(record_id)
            self.stale_reads_remaining[record_id] = 2
            # Simulate Feishu applying the delete but returning a late 400 error.
            raise worker.XiaotBitableAPIError(
                status_code=400,
                api_code=None,
                message="Data not ready",
            )

        async def record(self, table_key, record_id, *, access_token, platform="feishu"):
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            assert table_key == "task"
            if record_id in self.deleted_records:
                remaining = self.stale_reads_remaining[record_id]
                if remaining:
                    self.stale_reads_remaining[record_id] = remaining - 1
                    return {"record_id": record_id, "fields": self.stale_fields[record_id]}
                raise worker.XiaotBitableAPIError(
                    status_code=200,
                    api_code=1254043,
                    message="RecordIdNotFound",
                )
            return {
                "record_id": record_id,
                "fields": self.row_fields[record_id],
            }

    async def scenario():
        state = worker.D1State(FakeD1())
        await state.ensure_schema()
        await state.ensure_feishu_oauth_schema()
        await state.save_user_token(
            platform="feishu",
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

        async def bind_requester(request_id, open_id):
            await worker._db_run(
                state.db,
                "INSERT INTO xiaot_run_requesters "
                "(request_id, conversation_key, platform, open_id, created_at) VALUES (?, ?, ?, ?, ?)",
                request_id,
                conversation_key,
                "feishu",
                open_id,
                int(worker.time.time()),
            )

        await bind_requester("xiaot_req_prepare", "ou_original")
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
        await bind_requester("xiaot_req_wrong_user", "ou_other")
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
            input_markdown="User task:\n行",
        )
        await bind_requester("xiaot_req_confirm", "ou_original")
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
        await bind_requester("xiaot_req_prepare_update", "ou_original")
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
            message_id="xiaot:om_refuse_update",
            request_id="xiaot_req_refuse_update",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_refuse_update",
            conversation_key=conversation_key,
            source_message_id="om_refuse_update",
            input_markdown="User task:\n可以，但不要执行",
        )
        await bind_requester("xiaot_req_refuse_update", "ou_original")
        refused_update = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_refuse_update",
                "conversation_key": conversation_key,
                "proposal_id": update_proposal["proposal_id"],
            },
        )
        assert refused_update["isError"] is True

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
            input_markdown="User task:\n确认执行",
        )
        await bind_requester("xiaot_req_confirm_update", "ou_original")
        update_confirmed = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_confirm_update",
                "conversation_key": conversation_key,
            },
        )

        await state.claim_event(
            message_id="xiaot:om_prepare_delete",
            request_id="xiaot_req_prepare_delete",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_prepare_delete",
            conversation_key=conversation_key,
            source_message_id="om_prepare_delete",
            input_markdown="User task:\n删除这个任务",
        )
        await bind_requester("xiaot_req_prepare_delete", "ou_original")
        delete_proposal = await relay._prepare_mutation(
            {
                "request_id": "xiaot_req_prepare_delete",
                "conversation_key": conversation_key,
                "operation": "delete",
                "table_key": "task",
                "record_id": "rec_target",
            }
        )
        await state.claim_event(
            message_id="xiaot:om_confirm_delete",
            request_id="xiaot_req_confirm_delete",
            conversation_key=conversation_key,
            chat_id="oc_group",
            open_id="ou_original",
        )
        await state.create_run(
            request_id="xiaot_req_confirm_delete",
            conversation_key=conversation_key,
            source_message_id="om_confirm_delete",
            input_markdown="User task:\n确认",
        )
        await bind_requester("xiaot_req_confirm_delete", "ou_original")
        delete_confirmed = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_confirm_delete",
                "conversation_key": conversation_key,
                "proposal_id": delete_proposal["proposal_id"],
            },
        )
        return (
            bitable,
            wrong_user,
            confirmed,
            update_proposal,
            refused_update,
            update_confirmed,
            delete_confirmed,
        )

    (
        bitable,
        wrong_user,
        confirmed,
        update_proposal,
        refused_update,
        update_confirmed,
        delete_confirmed,
    ) = asyncio.run(scenario())

    assert wrong_user["isError"] is True
    assert refused_update["isError"] is True
    assert confirmed["structuredContent"]["success"] is True
    assert bitable.creates == [("task", {"Task Name": "Xiaot test"})]
    assert update_proposal["requires_explicit_user_confirmation"] is True
    assert "飞书系统自动生成" in update_proposal["audit_note"]
    assert update_confirmed["structuredContent"]["success"] is True
    assert update_confirmed["structuredContent"]["record_id"] == "rec_target"
    assert bitable.updates == [("task", "rec_target", {"Status": "In Progress"})]
    assert delete_confirmed["structuredContent"]["success"] is True
    assert delete_confirmed["structuredContent"]["deleted"] is True
    assert bitable.deletes == [("task", "rec_target"), ("task", "rec_target")]
    assert not any(call[0] == "changelog" for call in bitable.creates)
    assert not any(call[0] == "changelog" for call in bitable.updates)
    assert "rec_target" not in bitable.row_fields


def test_delete_that_returns_error_but_still_exists_is_not_success_after_one_retry(monkeypatch):
    worker = _load_xiaot_module()
    monkeypatch.setattr(worker, "XIAOT_DELETE_VERIFY_DELAYS", (0.0, 0.0, 0.0))

    class FakeBitable:
        def __init__(self):
            self.delete_calls = 0

        async def delete_record(self, *_args, **_kwargs):
            self.delete_calls += 1
            raise worker.XiaotBitableAPIError(
                status_code=400,
                api_code=None,
                message="Data not ready",
            )

        async def record(self, *_args, **_kwargs):
            return {"record_id": "rec_target", "fields": {"Status": "In Progress"}}

    bitable = FakeBitable()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.xiaot_bitable = bitable

    async def scenario():
        try:
            await relay._delete_record_and_verify(
                "task",
                "rec_target",
                expected_fields={"Status": "In Progress"},
                access_token="xiaot-user-token",
                platform="feishu",
            )
        except RuntimeError as exc:
            return str(exc)
        raise AssertionError("a still-present record must not be reported as deleted")

    message = asyncio.run(scenario())

    assert "record still exists" in message
    assert "Data not ready" in message
    assert bitable.delete_calls == 2


def test_delete_success_response_confirms_exact_row_without_readback():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeBitable:
        async def delete_record(self, *_args, **_kwargs):
            return {"deleted": True, "record_id": "rec_target"}

        async def record(self, *_args, **_kwargs):
            raise AssertionError("a successful DELETE response must not trigger a read-back")

    relay.xiaot_bitable = FakeBitable()
    result = asyncio.run(
        relay._delete_record_and_verify(
            "task",
            "rec_target",
            expected_fields={"Status": "In Progress"},
            access_token="xiaot-user-token",
            platform="feishu",
        )
    )

    assert result == {"deleted": True, "record_id": "rec_target"}


def test_delete_retries_data_not_ready_only_after_target_is_read_unchanged(monkeypatch):
    worker = _load_xiaot_module()
    monkeypatch.setattr(worker, "XIAOT_DELETE_VERIFY_DELAYS", (0.0,))
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeBitable:
        def __init__(self):
            self.delete_calls = 0
            self.read_calls = 0

        async def delete_record(self, *_args, **_kwargs):
            self.delete_calls += 1
            if self.delete_calls == 1:
                raise worker.XiaotBitableAPIError(
                    status_code=400,
                    api_code=1254607,
                    message="Data not ready, please try again later",
                )
            return {"deleted": True, "record_id": "rec_target"}

        async def record(self, *_args, **_kwargs):
            self.read_calls += 1
            return {"record_id": "rec_target", "fields": {"Status": "To-do"}}

    bitable = FakeBitable()
    relay.xiaot_bitable = bitable
    result = asyncio.run(
        relay._delete_record_and_verify(
            "task",
            "rec_target",
            expected_fields={"Status": "To-do"},
            access_token="user-token",
            platform="feishu",
        )
    )

    assert result == {"deleted": True, "record_id": "rec_target"}
    assert bitable.read_calls == 1
    assert bitable.delete_calls == 2


def test_bitable_delete_requires_documented_success_response_fields():
    worker = _load_xiaot_module()
    client = worker.XiaotBitableClient(SimpleNamespace())
    calls = []

    async def successful_response(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "code": 0,
            "msg": "success",
            "data": {"deleted": True, "record_id": "rec_target"},
        }

    client.request = successful_response
    result = asyncio.run(
        client.delete_record(
            "task",
            "rec_target",
            access_token="xiaot-user-token",
            platform="feishu",
        )
    )

    assert result == {"deleted": True, "record_id": "rec_target"}
    assert calls[0][0] == "DELETE"
    assert calls[0][1].endswith("/records/rec_target")


def test_feishu_event_callback_does_not_require_verification_token():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.env = SimpleNamespace(FEISHU_BOT_OPEN_ID="ou_bot")
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
            FakeRequest({"header": {"token": "unexpected-token"}, "challenge": "abc"})
        )
        legacy_challenge = await relay.handle_xiaot_event(
            FakeRequest(
                {
                    "type": "url_verification",
                    "token": "unexpected-token",
                    "challenge": "legacy-abc",
                }
            )
        )
        event = await relay.handle_xiaot_event(
            FakeRequest({"header": {"event_type": "im.message.receive_v1"}, "event": {}})
        )
        return challenge, legacy_challenge, event

    challenge, legacy_challenge, event = asyncio.run(scenario())

    assert challenge["body"] == {"challenge": "abc"}
    assert legacy_challenge["body"] == {"challenge": "legacy-abc"}
    assert event["status"] == 200
    assert scheduled == [
        ({"header": {"event_type": "im.message.receive_v1"}, "event": {}}, "xiaot")
    ]


def test_xiaot_environment_uses_dedicated_dev_resources_and_urls():
    worker = _load_xiaot_module()
    env = worker.XiaotEnvironment(
        SimpleNamespace(
            DB="shared-db",
            AGENT_QUEUE="shared-queue",
            FEISHU_OAUTH_REDIRECT_URI="https://mcp.0abt.com/feishu/oauth/callback",
            LARK_OAUTH_REDIRECT_URI="https://mcp.0abt.com/lark/oauth/callback",
            WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL="https://mcp.0abt.com",
            XIAOT_DB="xiaot-dev-db",
            XIAOT_AGENT_QUEUE="xiaot-dev-queue",
            XIAOT_FEISHU_OAUTH_REDIRECT_URI=(
                "https://bot.boooe.com/feishu/oauth/callback"
            ),
            XIAOT_LARK_OAUTH_REDIRECT_URI="https://bot.boooe.com/lark/oauth/callback",
            XIAOT_PUBLIC_BASE_URL="https://bot.boooe.com",
        )
    )

    assert env.DB == "xiaot-dev-db"
    assert env.AGENT_QUEUE == "xiaot-dev-queue"
    assert env.FEISHU_OAUTH_REDIRECT_URI == (
        "https://bot.boooe.com/feishu/oauth/callback"
    )
    assert env.LARK_OAUTH_REDIRECT_URI == "https://bot.boooe.com/lark/oauth/callback"
    assert env.WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL == "https://bot.boooe.com"

    oauth_env = worker.XiaotLarkOAuthEnvironment(env.raw)
    assert oauth_env.FEISHU_OAUTH_REDIRECT_URI == "https://bot.boooe.com/feishu/oauth/callback"
    assert oauth_env.LARK_OAUTH_REDIRECT_URI == "https://bot.boooe.com/lark/oauth/callback"
    assert oauth_env.WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL == "https://bot.boooe.com"


def test_combined_relay_keeps_xiaot_state_separate_from_shared_routes():
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    env = SimpleNamespace(DB="shared-db", XIAOT_DB="xiaot-dev-db")

    relay = worker.CombinedCloudflareRelay(env, None, app.D1State("shared-db"))

    assert relay.state.db == "shared-db"
    assert relay.xiaot.state.db == "xiaot-dev-db"


def test_xiaot_mcp_route_is_mounted_with_dedicated_identity(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    worker._response = lambda body, status=200, headers=None: {
        "body": body,
        "status": status,
        "headers": headers or {},
    }

    created_databases = []

    class FakeState:
        def __init__(self, db):
            created_databases.append(db)

    app.D1State = FakeState
    app._response = worker._response
    app.CloudflareRelay.authorize_request = lambda *_args, **_kwargs: asyncio.sleep(0, result=None)
    entrypoint = object.__new__(app.Default)
    shared_db = object()
    xiaot_db = object()
    entrypoint.env = SimpleNamespace(DB=shared_db, XIAOT_DB=xiaot_db)
    entrypoint.ctx = None

    class FakeRequest:
        method = "POST"
        url = "https://bot.boooe.com/xiaot/mcp"

        class Headers:
            def get(self, _name):
                return ""

        headers = Headers()

        async def json(self):
            return {"jsonrpc": "2.0", "id": 1, "method": "initialize"}

    response = asyncio.run(entrypoint.fetch(FakeRequest()))

    assert response["status"] == 200
    assert created_databases == [xiaot_db]
    assert response["body"]["result"]["serverInfo"]["name"] == app.XIAOT_MCP_NAME
    assert "workspace-agent-relay-mcp-xiaot-dev" in response["body"]["result"]["instructions"]


def test_xiaot_uses_a_dedicated_mcp_name_and_resource():
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
    assert relay.base_url() + "/xiaot/mcp" == "https://bot.boooe.com/xiaot/mcp"


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

    class FakeCombinedRelay(FakeRootRelay):
        def __init__(self, env, ctx, state):
            super().__init__(env, ctx, state)
            self.xiaot = FakeXiaotRelay(env, ctx, state)

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
    worker.CombinedCloudflareRelay = FakeCombinedRelay
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
        await state.claim_event(
            message_id="xiaot:om_source",
            request_id="xiaot_run_duplicate",
            conversation_key="xiaot:cli_xiaot:oc_group:dedupe",
            chat_id="oc_group",
            open_id="ou_requester",
        )
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

        class FakeFeishuAPI:
            async def reply(self, message_id, text):
                assert message_id == "om_source"
                assert '<at user_id="ou_requester">发起人</at>' in text
                return "om_placeholder"

        original = worker.CloudflareRelay.run_agent_job
        worker.CloudflareRelay.run_agent_job = process_once
        try:
            relay = worker.XiaotCloudflareRelay(SimpleNamespace(), None, state)
            relay.api_for_conversation = lambda _key: FakeFeishuAPI()
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


def test_xiaot_trigger_metadata_records_only_the_workspace_agent_run_id(monkeypatch):
    worker = _load_xiaot_module()
    captured = []

    async def capture_sql(_db, sql, *params):
        captured.append((sql, params))

    class FakeResponse:
        def json(self):
            return {
                "conversation_url": "https://chatgpt.com/c/private-conversation",
                "agent_trigger_run_id": "apirun_123abc",
            }

    monkeypatch.setattr(worker, "_db_run", capture_sql)
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.state = SimpleNamespace(db=object())

    asyncio.run(
        relay._record_agent_trigger_metadata(
            "xiaot_req_1",
            "https://api.chatgpt.com/v1/workspace_agents/agtch_123abc/trigger",
            FakeResponse(),
        )
    )

    insert = next(
        item for item in captured if "INSERT INTO xiaot_agent_trigger_runs" in item[0]
    )
    assert insert[1][:3] == ("xiaot_req_1", "agtch_123abc", "apirun_123abc")
    assert all("private-conversation" not in str(item) for item in captured)


def test_xiaot_run_context_polls_workspace_agent_and_redacts_token(monkeypatch):
    worker = _load_xiaot_module()
    db_updates = []
    requested = []
    record = {
        "request_id": "xiaot_req_failed",
        "api_trigger_id": "agtch_123abc",
        "agent_trigger_run_id": "apirun_123abc",
        "status": "accepted",
        "error_code": "",
        "error_message": "",
        "updated_at": 100,
    }

    async def capture_sql(_db, sql, *params):
        db_updates.append((sql, params))

    async def find_record(_db, sql, *params):
        assert "xiaot_agent_trigger_runs" in sql
        assert params == ("xiaot_req_failed",)
        return dict(record)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "failed",
                "error": {
                    "code": "run_failed",
                    "message": "agent failed while handling token-value",
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            requested.append((url, headers))
            return FakeResponse()

    class FakeState:
        db = object()

        async def recent_runs(self, conversation_key, limit):
            assert conversation_key == "xiaot:app:group:123"
            assert limit == 5
            return [
                {
                    "request_id": "xiaot_req_failed",
                    "status": "triggered",
                    "created_at": 100,
                }
            ]

    monkeypatch.setattr(worker, "_db_run", capture_sql)
    monkeypatch.setattr(worker, "_db_first", find_record)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda timeout: FakeClient())
    relay = object.__new__(worker.XiaotCloudflareRelay)
    relay.env = worker.XiaotEnvironment(
        SimpleNamespace(
            XIAOT_AGENT_TRIGGER_URL=(
                "https://api.chatgpt.com/v1/workspace_agents/agtch_123abc/trigger"
            ),
            XIAOT_AGENT_ACCESS_TOKEN="token-value",
        )
    )
    relay.state = FakeState()

    result = asyncio.run(relay._xiaot_run_context("xiaot:app:group:123", 5))

    execution = result["runs"][0]["agent_execution"]
    assert execution["status"] == "failed"
    assert execution["error_code"] == "run_failed"
    assert execution["error_message"] == "agent failed while handling [REDACTED]"
    assert requested[0][0] == (
        "https://api.chatgpt.com/v1/workspace_agents/agtch_123abc/runs/apirun_123abc"
    )
    assert requested[0][1]["Authorization"] == "Bearer token-value"
    assert any("UPDATE xiaot_agent_trigger_runs" in sql for sql, _ in db_updates)


def test_xiaot_agent_run_status_url_rejects_non_https_trigger():
    worker = _load_xiaot_module()

    try:
        worker.XiaotCloudflareRelay._agent_run_status_url(
            "http://api.chatgpt.com/v1/workspace_agents/agtch_123/trigger",
            "agtch_123",
            "apirun_123",
        )
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-HTTPS status URL was accepted")


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
    assert "仅可读取" in content
    assert "get_requester_info" in content
    assert "search_person_candidates" in content
    assert "毫秒时间戳" in content
    assert "William" in content and "元博 王" in content


def test_xiaot_result_cards_mention_source_user_for_every_terminal_status():
    worker = _load_xiaot_module()
    expected = {
        "done": "任务成功",
        "failed": "任务失败",
        "blocked": "任务被阻塞",
        "cancelled": "取消任务成功",
        "cancel_failed": "取消任务失败",
    }
    for status, heading in expected.items():
        card = worker.XiaotCloudflareRelay._result_card(
            {"status": status, "title": "测试结果", "markdown": "已核实"},
            "ou_requester",
        )
        assert card["header"]["title"]["content"] == heading
        assert "<at id=ou_requester></at>" in card["elements"][0]["text"]["content"]


def test_xiaot_deliver_result_sends_mention_card_and_marks_run_delivered(monkeypatch):
    worker = _load_xiaot_module()
    run = {
        "request_id": "xiaot_result_1",
        "conversation_key": "xiaot:app:chat:thread",
        "source_message_id": "om_source",
        "placeholder_message_id": "om_placeholder",
        "status": "cancelled",
        "title": "用户取消",
        "markdown": "未执行写入。",
        "delivered": 0,
    }

    class State:
        db = object()

        def __init__(self):
            self.saved = None

        async def get_run(self, _request_id):
            return run

        async def save_reply(self, outbound, conversation_key):
            self.saved = (outbound, conversation_key)

    class API:
        def __init__(self):
            self.card = None
            self.updated = []

        async def reply_card(self, message_id, card):
            self.card = (message_id, card)
            return "om_result_card"

        async def update(self, message_id, text):
            self.updated.append((message_id, text))

    state = State()
    api = API()
    relay = worker.XiaotOnlyCloudflareRelay(SimpleNamespace(), None, state)

    async def requester(_run):
        return "ou_requester"

    async def db_run(_db, sql, *_args):
        assert "SET delivered = 1" in sql

    relay._source_requester_open_id = requester
    relay.api_for_conversation = lambda _key: api
    monkeypatch.setattr(worker, "_db_run", db_run)
    asyncio.run(relay.deliver_result("xiaot_result_1"))

    assert api.card[0] == "om_source"
    assert "<at id=ou_requester></at>" in api.card[1]["elements"][0]["text"]["content"]
    assert api.updated == [("om_placeholder", "本次小T请求已结束，请查看下方结果卡片。")]
    assert state.saved == ("om_result_card", "xiaot:app:chat:thread")


def test_xiaot_cancel_result_records_cancelled_and_queues_terminal_card(monkeypatch):
    worker = _load_xiaot_module()
    relay = worker.XiaotOnlyCloudflareRelay(SimpleNamespace(), None, SimpleNamespace())
    updates = []
    queued = []

    async def require_run(_args):
        return {"request_id": "xiaot_cancel_1", "completed_at": None}

    async def update_run(request_id, **fields):
        updates.append((request_id, fields))

    async def enqueue(body):
        queued.append(body)

    relay._require_xiaot_run = require_run
    relay.state.update_run = update_run
    relay._enqueue = enqueue
    monkeypatch.setattr(relay, "_result_image_args", lambda _args: [])

    result = asyncio.run(
        relay.call_tool(
            "record_result",
            {
                "request_id": "xiaot_cancel_1",
                "conversation_key": "xiaot:app:chat:thread",
                "status": "canceled",
                "title": "用户取消",
                "markdown": "未执行写入。",
            },
        )
    )

    assert result["structuredContent"]["status"] == "cancelled"
    assert updates[0][1]["status"] == "cancelled"
    assert queued == [{"kind": "deliver_result", "request_id": "xiaot_cancel_1"}]


def test_lark_requester_is_resolved_in_lark_user_namespace():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeAPI:
        async def resolve_user_id(self, identifier, *, user_id_type):
            assert (identifier, user_id_type) == ("ou_external", "open_id")
            return {"open_id": "ou_lark_canonical"}

    class FakeIdentityRelay:
        lark = FakeAPI()
        feishu = object()
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")

    class FakeState:
        db = object()

    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    async def no_link(_db, _sql, *_params):
        return None
    worker._db_first = no_link
    result = asyncio.run(
        relay._resolve_account_identity({"open_id": "ou_external"})
    )

    assert result == {"platform": "lark", "open_id": "ou_lark_canonical"}


def test_feishu_requester_is_resolved_in_oauth_app_namespace():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeAPI:
        async def resolve_user_id(self, identifier, *, user_id_type):
            assert (identifier, user_id_type) == ("on_tenant_stable", "union_id")
            return {"open_id": "ou_feishu_oauth_app_user"}

    class FakeIdentityRelay:
        lark = None
        feishu = FakeAPI()
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")

    class FakeState:
        db = object()

    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    async def no_link(_db, _sql, *_params):
        return None
    worker._db_first = no_link
    result = asyncio.run(
        relay._resolve_account_identity(
            {"open_id": "ou_xiaot_event_app_user", "union_id": "on_tenant_stable"}
        )
    )

    assert result == {"platform": "feishu", "open_id": "ou_feishu_oauth_app_user"}


def test_xiaot_platform_detection_prefers_explicit_lark_brand_without_lookup():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeIdentityRelay:
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")
        feishu = None
        lark = None

    relay.identity_relay = FakeIdentityRelay()
    assert asyncio.run(
        relay._detect_requester_platform({"open_id": "ou_lark", "platform": "lark"})
    ) == "lark"


def test_xiaot_platform_detection_does_not_guess_feishu_when_unresolved():
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class UnresolvableAPI:
        async def resolve_user_id(self, *_args, **_kwargs):
            raise RuntimeError("user is outside this app's directory")

    class FakeIdentityRelay:
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")
        feishu = UnresolvableAPI()
        lark = UnresolvableAPI()

    relay.identity_relay = FakeIdentityRelay()
    try:
        asyncio.run(relay._detect_requester_platform({"open_id": "ou_external"}))
    except RuntimeError as exc:
        assert "无法判断当前发起人的账号类型" in str(exc)
    else:
        raise AssertionError("unknown platform must not silently fall back to Feishu")


def test_unresolvable_lark_requester_gets_oauth_identity_without_union_id(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeAPI:
        async def resolve_user_id(self, _identifier, *, user_id_type):
            raise RuntimeError(f"Lark contact lookup unavailable for {user_id_type}")

    class FakeIdentityRelay:
        lark = FakeAPI()
        feishu = None
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")

    class FakeState:
        db = object()

    async def no_link(_db, _sql, *_params):
        return None

    monkeypatch.setattr(worker, "_db_first", no_link)
    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    result = asyncio.run(
        relay._resolve_account_identity(
            {
                "open_id": "ou_external_event",
                "tenant_key": "tenant_feishu",
                "sender_tenant_key": "tenant_lark",
            }
        )
    )

    assert result == {
        "platform": "lark",
        "open_id": "pending_source:ou_external_event",
        "identity_pending": "true",
        "source_open_id": "ou_external_event",
        "source_union_id": "",
        "source_user_id": "",
        "source_platform": "feishu",
    }


def test_lark_requester_reuses_oauth_identity_link(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class FakeAPI:
        async def resolve_user_id(self, *_args, **_kwargs):
            raise AssertionError("linked user should not need another contact lookup")

    class FakeIdentityRelay:
        lark = FakeAPI()
        feishu = None
        env = SimpleNamespace(LARK_EXTERNAL_TENANT_KEYS="")

    class FakeState:
        db = object()

    async def linked_identity(_db, sql, *params):
        if "xiaot_bitable_source_identity_links" in sql:
            assert params == ("lark", "ou_external_event")
            return {"account_open_id": "ou_lark_oauth_app"}
        raise AssertionError("the linked source identity should be used before union_id")

    monkeypatch.setattr(worker, "_db_first", linked_identity)
    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    result = asyncio.run(
        relay._resolve_account_identity(
            {
                "open_id": "ou_external_event",
                "union_id": "on_requester_stable",
                "tenant_key": "tenant_feishu",
                "sender_tenant_key": "tenant_lark",
            }
        )
    )

    assert result == {"platform": "lark", "open_id": "ou_lark_oauth_app"}


def test_lark_authorization_uses_lark_app_and_saves_original_request(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    statements = []
    replies = []

    class FakeIdentityRelay:
        env = worker.XiaotLarkOAuthEnvironment(
            SimpleNamespace(
                LARK_APP_ID="cli_shared_lark",
                LARK_APP_SECRET="shared-lark-secret",
                XIAOT_LARK_APP_ID="cli_lark_dev",
                XIAOT_LARK_APP_SECRET="xiaot-lark-secret",
                LARK_OAUTH_REDIRECT_URI="https://bot.boooe.com/lark/oauth/callback",
            )
        )

        def platform_oauth_scope(self, platform):
            assert platform == "lark"
            return "bitable:app"

        def base_url(self):
            return "https://bot.boooe.com"

    class FakeState:
        db = object()

        async def save_reply(self, *_args):
            return None

    class FakeAPI:
        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            replies.append((chat_id, open_id, card))
            return "om_auth_reply"

    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    relay.api_for_conversation = lambda _key: FakeAPI()
    relay.base_url = lambda: "https://bot.boooe.com"
    original_db_run = worker._db_run

    async def capture_sql(_db, sql, *params):
        statements.append((sql, params))
        return None

    monkeypatch.setattr(worker, "_db_run", capture_sql)
    event = {
        "message_id": "om_original",
        "chat_id": "oc_requester_group",
        "open_id": "ou_external_event",
        "union_id": "on_requester_stable",
        "text": "查询我的 task",
    }
    asyncio.run(
        relay._send_user_authorization(
            event=event,
            conversation_key="xiaot:cli_xiaot:oc_group:abc",
            request_id="xiaot_req_1",
            account_identity={
                "platform": "lark",
                "open_id": "ou_lark_canonical",
                "identity_pending": "true",
            },
        )
    )
    monkeypatch.setattr(worker, "_db_run", original_db_run)

    sql, params = next(item for item in statements if "INSERT INTO xiaot_bitable_oauth_states" in item[0])
    assert params[2:8] == (
        "lark",
        "",
        "on_requester_stable",
        "ou_external_event",
        "",
        "feishu",
    )
    assert params[8:11] == (
        "xiaot_req_1",
        "xiaot:cli_xiaot:oc_group:abc",
        "om_original",
    )
    assert params[11] == "https://bot.boooe.com/lark/oauth/callback"
    assert '"text":"查询我的 task"' in params[12]
    _, card_id_params = next(
        item for item in statements if "SET authorization_message_id = ?" in item[0]
    )
    assert card_id_params[0] == "om_auth_reply"
    assert card_id_params[1].startswith("xiaot_lark_")
    assert replies[0][0:2] == ("oc_requester_group", "ou_external_event")
    card = replies[0][2]
    launch_url = card["elements"][1]["actions"][0]["url"]
    assert launch_url.startswith("https://bot.boooe.com/xiaot/user-oauth/start?")
    assert "platform=lark" in launch_url
    assert "accounts.larksuite.com" not in launch_url
    assert "飞书文档" not in str(card) and "Lark 文档" not in str(card)


def test_xiaot_oauth_start_page_opens_popup_and_validates_pending_state(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    pending = {
        "platform": "lark",
        "redirect_uri": "https://bot.boooe.com/lark/oauth/callback",
        "expires_at": int(worker.time.time()) + 600,
        "consumed_at": None,
    }

    class CapturedResponse:
        def __init__(self, body, *, status=200, headers=None):
            self.body = body
            self.status = status
            self.headers = headers or {}

    class FakeIdentityRelay:
        env = worker.XiaotLarkOAuthEnvironment(
            SimpleNamespace(XIAOT_LARK_APP_ID="cli_xiaot_lark")
        )

        def base_url(self):
            return "https://bot.boooe.com"

        def platform_oauth_scope(self, platform):
            assert platform == "lark"
            return "bitable:app"

    class FakeState:
        db = object()

    async def lookup(_db, sql, *params):
        assert "consumed_at IS NULL" in sql
        assert params == ("xiaot_lark_nonce", "lark")
        return pending

    monkeypatch.setattr(worker, "_db_first", lookup)
    monkeypatch.setattr(worker, "Response", CapturedResponse)
    relay.identity_relay = FakeIdentityRelay()
    relay.state = FakeState()
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/xiaot/user-oauth/start?platform=lark&state=xiaot_lark_nonce",
    )

    response = asyncio.run(relay.handle_user_oauth_start(request))

    assert response.status == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "window.open(config.auth_url" in response.body
    assert "window.opener.postMessage" not in response.body
    assert "xiaot_oauth_popup" in response.body
    assert "accounts.larksuite.com/open-apis/authen/v1/authorize" in response.body
    assert "cli_xiaot_lark" in response.body
    assert "new BroadcastChannel(config.channel_name)" in response.body
    assert "window.close()" in response.body


def test_xiaot_user_oauth_start_route_uses_xiaot_relay(monkeypatch):
    worker = _load_xiaot_module()
    app = sys.modules["worker_app"]
    entry = object.__new__(app.Default)
    entry.env = SimpleNamespace(DB=object())
    entry.ctx = None
    calls = []

    class FakeRelay:
        async def handle_user_oauth_start(self, request):
            calls.append(request.url)
            return "xiaot-start-page"

    monkeypatch.setattr(app, "D1State", lambda _db: object())
    monkeypatch.setattr(worker, "XiaotOnlyCloudflareRelay", lambda *_args: FakeRelay())
    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/xiaot/user-oauth/start?platform=feishu&state=xiaot_feishu_nonce",
    )

    result = asyncio.run(entry.fetch(request))

    assert result == "xiaot-start-page"
    assert calls == [request.url]


def test_combined_oauth_routes_only_matching_xiaot_state_to_xiaot(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.CombinedCloudflareRelay)
    calls = []

    class FakeXiaot:
        async def handle_user_oauth_callback(self, request, platform):
            calls.append((request.url, platform))
            return "xiaot-callback"

    relay.xiaot = FakeXiaot()

    async def base_oauth(_self, request, path, platform="feishu"):
        calls.append((request.url, path, platform))
        return "shared-callback"

    monkeypatch.setattr(worker.CloudflareRelay, "feishu_oauth", base_oauth)
    lark_request = SimpleNamespace(
        url="https://bot.boooe.com/lark/oauth/callback?state=xiaot_lark_nonce"
    )
    feishu_request = SimpleNamespace(
        url="https://bot.boooe.com/feishu/oauth/callback?state=relay_state"
    )

    assert asyncio.run(
        relay.feishu_oauth(lark_request, "/lark/oauth/callback", "lark")
    ) == "xiaot-callback"
    assert asyncio.run(
        relay.feishu_oauth(feishu_request, "/feishu/oauth/callback", "feishu")
    ) == "shared-callback"
    assert calls == [
        (lark_request.url, "lark"),
        (feishu_request.url, "/feishu/oauth/callback", "feishu"),
    ]


def test_lark_oauth_callback_verifies_account_and_resumes_original_request(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)

    class CapturedResponse:
        def __init__(self, body, *, status=200, headers=None):
            self.body = body
            self.status = status
            self.headers = headers or {}

    pending = {
        "platform": "lark",
        "account_open_id": "",
        "source_union_id": "",
        "source_open_id": "ou_external_event",
        "source_user_id": "",
        "source_platform": "feishu",
        "redirect_uri": "https://bot.boooe.com/lark/oauth/callback",
        "authorization_message_id": "om_private_auth_card",
        "expires_at": int(worker.time.time()) + 600,
        "event_json": (
            '{"message_id":"om_original","chat_id":"oc_requester_group",'
            '"open_id":"ou_external_event","text":"查询 task"}'
        ),
        "request_id": "xiaot_original_request",
        "conversation_key": "xiaot:cli_xiaot:oc_group:thread",
    }
    exchange_calls = []
    saved_tokens = []
    resumed = []
    private_card_calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"access_token": "lark-user-token", "refresh_token": "refresh", "expires_in": 3600}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            exchange_calls.append((url, kwargs))
            return FakeResponse()

    class FakeAPI:
        async def user_info(self, access_token):
            assert access_token == "lark-user-token"
            return {"open_id": "ou_lark_verified"}

    class FakeIdentityRelay:
        env = worker.XiaotLarkOAuthEnvironment(
            SimpleNamespace(
                LARK_APP_ID="cli_shared_lark",
                LARK_APP_SECRET="shared-lark-secret",
                XIAOT_LARK_APP_ID="cli_lark_app",
                XIAOT_LARK_APP_SECRET="xiaot-lark-secret",
            )
        )

        def api_for_conversation(self, conversation_key):
            assert conversation_key == "lark:oauth"
            return FakeAPI()

    class FakeFeishuMessageAPI:
        async def send_ephemeral_card(self, *, chat_id, open_id, card):
            private_card_calls.append(("send", chat_id, open_id, card))
            return "om_private_success_card"

        async def _request(self, method, path, **kwargs):
            private_card_calls.append(("delete", method, path, kwargs))
            return {"code": 0}

    class FakeState:
        db = object()

        async def get_run(self, request_id):
            assert request_id == "xiaot_original_request"
            return None

        async def save_user_token(self, **kwargs):
            saved_tokens.append(kwargs)

    class FakeWorkflow:
        async def handle_event(self, **kwargs):
            resumed.append(kwargs)

    async def lookup(_db, sql, *params):
        assert "xiaot_bitable_oauth_states" in sql
        assert params == ("xiaot_lark_nonce",)
        return pending

    async def consume(_db, sql, *params):
        assert "consumed_at" in sql
        assert params[1:3] == ("xiaot_lark_nonce", "lark")
        return [{"state": "xiaot_lark_nonce"}]

    statements = []

    async def capture_sql(_db, sql, *params):
        statements.append((sql, params))
        return None

    monkeypatch.setattr(worker, "_db_first", lookup)
    monkeypatch.setattr(worker, "_db_all", consume)
    monkeypatch.setattr(worker, "_db_run", capture_sql)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda timeout: FakeClient())
    monkeypatch.setattr(worker, "Response", CapturedResponse)
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: {
            "payload": payload,
            "status": status,
            "headers": headers or {},
        },
    )
    relay.state = FakeState()
    relay.identity_relay = FakeIdentityRelay()
    relay.api_for_conversation = lambda conversation_key: FakeFeishuMessageAPI()
    relay.agent_workflow = FakeWorkflow()
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/lark/oauth/callback?code=auth-code&state=xiaot_lark_nonce",
    )

    result = asyncio.run(relay.handle_user_oauth_callback(request, "lark"))

    assert result.status == 200
    assert result.headers["content-type"] == "text/html; charset=utf-8"
    assert "window.opener.postMessage(result,window.location.origin)" in result.body
    assert '"state":"xiaot_lark_nonce"' in result.body
    assert 'new BroadcastChannel("xiaot-oauth-xiaot_lark_nonce")' in result.body
    assert "window.close()" in result.body
    assert '"success":true' in result.body
    assert exchange_calls[0][0] == "https://accounts.larksuite.com/oauth/v3/token"
    assert exchange_calls[0][1]["data"]["client_id"] == "cli_lark_app"
    assert exchange_calls[0][1]["data"]["client_secret"] == "xiaot-lark-secret"
    assert exchange_calls[0][1]["data"]["redirect_uri"] == pending["redirect_uri"]
    assert saved_tokens[0]["platform"] == "lark"
    assert saved_tokens[0]["open_id"] == "ou_lark_verified"
    assert private_card_calls[0] == (
        "send",
        "oc_requester_group",
        "ou_external_event",
        {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "plain_text", "content": "授权成功！"},
                }
            ],
        },
    )
    assert private_card_calls[1] == (
        "delete",
        "POST",
        "/open-apis/ephemeral/v1/delete",
        {"json": {"message_id": "om_private_auth_card"}},
    )
    identity_link = next(
        item for item in statements
        if "INSERT INTO xiaot_bitable_source_identity_links" in item[0]
    )
    assert identity_link[1][:3] == ("lark", "ou_external_event", "ou_lark_verified")
    assert resumed == [
        {
            "platform": "lark",
            "conversation_key": pending["conversation_key"],
            "event": {
                "message_id": "om_original",
                "chat_id": "oc_requester_group",
                "open_id": "ou_external_event",
                "text": "查询 task",
            },
            "request_id": pending["request_id"],
        }
    ]


def test_lark_oauth_callback_rejects_different_union_id(monkeypatch):
    worker = _load_xiaot_module()
    relay = object.__new__(worker.XiaotCloudflareRelay)
    pending = {
        "platform": "lark",
        "account_open_id": "",
        "source_union_id": "on_requester_stable",
        "source_open_id": "ou_external_event",
        "source_user_id": "",
        "source_platform": "lark",
        "redirect_uri": "https://bot.boooe.com/lark/oauth/callback",
        "expires_at": int(worker.time.time()) + 600,
        "event_json": '{"message_id":"om_original","text":"查询我的 task"}',
        "request_id": "xiaot_original_request",
        "conversation_key": "xiaot:cli_xiaot:oc_group:thread",
    }
    saved_tokens = []
    resumed = []
    statements = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"access_token": "lark-user-token", "expires_in": 3600}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return FakeResponse()

    class FakeAPI:
        async def user_info(self, _access_token):
            return {"open_id": "ou_another_user", "union_id": "on_another_user"}

    class FakeIdentityRelay:
        env = worker.XiaotLarkOAuthEnvironment(
            SimpleNamespace(XIAOT_LARK_APP_ID="cli_lark_app", XIAOT_LARK_APP_SECRET="secret")
        )

        def api_for_conversation(self, _conversation_key):
            return FakeAPI()

    class FakeState:
        db = object()

        async def save_user_token(self, **kwargs):
            saved_tokens.append(kwargs)

    class FakeWorkflow:
        async def handle_event(self, **kwargs):
            resumed.append(kwargs)

    async def lookup(_db, _sql, *_params):
        return pending

    async def consume(_db, _sql, *_params):
        return [{"state": "xiaot_lark_nonce"}]

    async def capture_sql(_db, sql, *params):
        statements.append((sql, params))
        return None

    monkeypatch.setattr(worker, "_db_first", lookup)
    monkeypatch.setattr(worker, "_db_all", consume)
    monkeypatch.setattr(worker, "_db_run", capture_sql)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda timeout: FakeClient())
    monkeypatch.setattr(
        worker,
        "_response",
        lambda payload, status=200, headers=None: {
            "payload": payload,
            "status": status,
            "headers": headers or {},
        },
    )
    relay.state = FakeState()
    relay.identity_relay = FakeIdentityRelay()
    relay.agent_workflow = FakeWorkflow()
    relay._ensure_xiaot_oauth_schema = lambda: asyncio.sleep(0)
    request = SimpleNamespace(
        method="GET",
        url="https://bot.boooe.com/lark/oauth/callback?code=auth-code&state=xiaot_lark_nonce",
    )

    result = asyncio.run(relay.handle_user_oauth_callback(request, "lark"))

    assert result["status"] == 403
    assert result["payload"]["error"] == "requester_mismatch"
    assert saved_tokens == []
    assert resumed == []
    assert not any("xiaot_bitable_identity_links" in sql for sql, _ in statements)


def test_xiaot_oauth_environment_only_uses_xiaot_platform_credentials():
    worker = _load_xiaot_module()
    env = worker.XiaotLarkOAuthEnvironment(
        SimpleNamespace(
            FEISHU_APP_ID="cli_feishu_shared",
            FEISHU_APP_SECRET="shared-feishu-secret",
            LARK_APP_ID="cli_lark_shared",
            LARK_APP_SECRET="shared-secret",
            XIAOT_FEISHU_APP_ID="cli_feishu_xiaot",
            XIAOT_FEISHU_APP_SECRET="xiaot-feishu-secret",
            XIAOT_LARK_APP_ID="cli_lark_xiaot",
            XIAOT_LARK_APP_SECRET="xiaot-secret",
        )
    )

    assert env.FEISHU_APP_ID == "cli_feishu_xiaot"
    assert env.FEISHU_APP_SECRET == "xiaot-feishu-secret"
    assert env.LARK_APP_ID == "cli_lark_xiaot"
    assert env.LARK_APP_SECRET == "xiaot-secret"

    unconfigured = worker.XiaotLarkOAuthEnvironment(
        SimpleNamespace(
            FEISHU_APP_ID="cli_feishu_shared",
            FEISHU_APP_SECRET="shared-feishu-secret",
            LARK_APP_ID="cli_lark_shared",
            LARK_APP_SECRET="shared-secret",
        )
    )
    assert unconfigured.FEISHU_APP_ID is None
    assert unconfigured.FEISHU_APP_SECRET is None
    assert unconfigured.LARK_APP_ID is None
    assert unconfigured.LARK_APP_SECRET is None


def test_lark_bitable_api_calls_use_lark_api_host(monkeypatch):
    worker = _load_xiaot_module()
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"code": 0, "data": {"items": []}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse()

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda timeout: FakeClient())
    client = worker.XiaotBitableClient(SimpleNamespace())
    asyncio.run(
        client.request(
            "GET",
            "/open-apis/bitable/v1/apps/base/tables/task/records",
            access_token="current-lark-user-token",
            platform="lark",
        )
    )

    assert calls[0][1].startswith("https://open.larksuite.com/")
    assert calls[0][2]["headers"]["Authorization"] == "Bearer current-lark-user-token"
