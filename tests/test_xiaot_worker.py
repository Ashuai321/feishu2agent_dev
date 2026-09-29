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


def test_confirmation_requires_an_exact_positive_reply_for_the_operation():
    worker = _load_xiaot_module()
    check = worker.XiaotCloudflareRelay._is_explicit_confirmation

    assert check("确认创建", "create")
    assert check("好的", "update")
    assert check("确认删除", "delete")
    assert not check("不确认创建", "create")
    assert not check("不要删除", "delete")
    assert not check("可以，然后删除另一个", "delete")
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
            self.row_fields = {"rec_target": {"Status": "Waiting/Blocked"}}

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

        async def record(self, table_key, record_id, *, access_token, platform="feishu"):
            assert access_token == "xiaot-user-token"
            assert platform == "feishu"
            assert table_key == "task"
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
            input_markdown="User task:\n确认创建",
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
        await bind_requester("xiaot_req_confirm_update", "ou_original")
        update_confirmed = await relay.call_tool(
            "confirm_mutation",
            {
                "request_id": "xiaot_req_confirm_update",
                "conversation_key": conversation_key,
            },
        )
        return bitable, wrong_user, confirmed, update_proposal, update_confirmed

    bitable, wrong_user, confirmed, update_proposal, update_confirmed = asyncio.run(scenario())

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


def test_xiaot_mcp_route_is_mounted_with_dedicated_identity(monkeypatch):
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
    app.CloudflareRelay.authorize_request = lambda *_args, **_kwargs: asyncio.sleep(0, result=None)
    entrypoint = object.__new__(app.Default)
    entrypoint.env = SimpleNamespace(DB=object())
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
        async def reply_card(self, message_id, card):
            replies.append((message_id, card))
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
    card = replies[0][1]
    auth_url = card["elements"][1]["actions"][0]["url"]
    assert auth_url.startswith("https://accounts.larksuite.com/")
    assert "app_id=cli_lark_dev" in auth_url
    assert "飞书文档" not in str(card) and "Lark 文档" not in str(card)


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
    pending = {
        "platform": "lark",
        "account_open_id": "",
        "source_union_id": "",
        "source_open_id": "ou_external_event",
        "source_user_id": "",
        "source_platform": "feishu",
        "redirect_uri": "https://bot.boooe.com/lark/oauth/callback",
        "expires_at": int(worker.time.time()) + 600,
        "event_json": '{"message_id":"om_original","text":"查询 task"}',
        "request_id": "xiaot_original_request",
        "conversation_key": "xiaot:cli_xiaot:oc_group:thread",
    }
    exchange_calls = []
    saved_tokens = []
    resumed = []

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

    assert result["status"] == 200
    assert result["payload"]["success"] is True
    assert exchange_calls[0][0] == "https://accounts.larksuite.com/oauth/v3/token"
    assert exchange_calls[0][1]["data"]["client_id"] == "cli_lark_app"
    assert exchange_calls[0][1]["data"]["client_secret"] == "xiaot-lark-secret"
    assert exchange_calls[0][1]["data"]["redirect_uri"] == pending["redirect_uri"]
    assert saved_tokens[0]["platform"] == "lark"
    assert saved_tokens[0]["open_id"] == "ou_lark_verified"
    identity_link = next(
        item for item in statements
        if "INSERT INTO xiaot_bitable_source_identity_links" in item[0]
    )
    assert identity_link[1][:3] == ("lark", "ou_external_event", "ou_lark_verified")
    assert resumed == [
        {
            "platform": "lark",
            "conversation_key": pending["conversation_key"],
            "event": {"message_id": "om_original", "text": "查询 task"},
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
