"""DEV-only 小 T relay and Bitable tools for the shared 小 C/小 T MCP.

小 T keeps separate Feishu credentials and Agent trigger routing, while its
tools are exposed by the existing DEV MCP endpoint and share the Worker,
Queue, D1 binding, OAuth app, and MCP connector with 小 C.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote, urlencode, urlparse, parse_qs

import httpx
from worker_app import (
    CloudflareRelay,
    D1State,
    FEISHU_AUTH_BASE_URL,
    _db_all,
    _db_first,
    _db_run,
    _env,
    _json,
    _normalize_event,
    _request_id,
    _response,
    _safe_error,
)
from workers import Response

XIAOT_AGENT_SCOPE = "xiaot-bitable-agent-dev"
XIAOT_TOOL_NAMES = {
    "list_tables",
    "get_table_fields",
    "search_records",
    "get_record",
    "prepare_mutation",
    "confirm_mutation",
}
XIAOT_READ_TOOL_NAMES = {"list_tables", "get_table_fields", "search_records", "get_record"}
XIAOT_TABLES: dict[str, dict[str, str]] = {
    "goal": {
        "table_id": "tblHq7aqhe195HnD",
        "name": "goal",
        "description": "高层目标或分类，用于组织相关项目；偏战略或归类，不直接对应具体执行。",
    },
    "project": {
        "table_id": "tblGljHRF28Jb3W3",
        "name": "project",
        "description": "Goal/Category 的进一步拆分，代表一个相对完整的事项或阶段性目标。",
    },
    "task": {
        "table_id": "tblWvG10yZtqpp2A",
        "name": "task",
        "description": "明确的执行单元，定义谁在什么时间做什么事。",
    },
    "sub_task": {
        "table_id": "tblRSAd7V63Zp2ya",
        "name": "sub task",
        "description": "Task 的细分步骤或 checklist，用于拆解执行细节。",
    },
    "space_list": {
        "table_id": "tblyhk92pJBej7Vs",
        "name": "space list",
        "description": "多维表格空间清单。",
    },
    "changelog": {
        "table_id": "tbl5GmADqShvAx6I",
        "name": "changelog",
        "description": "记录哪个用户何时更改了哪个 goal、project、task 或 subtask。",
    },
    "ai_text": {
        "table_id": "tbl24pWCd60PP7GJ",
        "name": "ai text",
        "description": "每日任务分析文本；分类为个人消息分析文本或群分析文本。",
    },
}

_RELAY_TOOLS = {
    "server_info",
    "record_plan",
    "record_progress",
    "record_result",
    "update_conversation_title",
    "ask_user",
    "get_run_context",
    "get_requester_info",
}


class XiaotEnvironment:
    """Expose only XiaoT's bot/Agent secrets through the existing relay names."""

    _aliases = {
        "FEISHU_APP_ID": "XIAOT_FEISHU_APP_ID",
        "FEISHU_APP_SECRET": "XIAOT_FEISHU_APP_SECRET",
        "FEISHU_VERIFY_TOKEN": "XIAOT_FEISHU_VERIFY_TOKEN",
        "FEISHU_BOT_OPEN_ID": "XIAOT_FEISHU_BOT_OPEN_ID",
        "WORKSPACE_AGENT_RELAY_TRIGGER_URL": "XIAOT_AGENT_TRIGGER_URL",
        "WORKSPACE_AGENT_RELAY_AGENT_TOKEN": "XIAOT_AGENT_ACCESS_TOKEN",
    }

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.request_origin = ""

    def __getattr__(self, name: str) -> Any:
        alias = self._aliases.get(name)
        if alias:
            value = getattr(self.raw, alias, None)
            if value not in (None, ""):
                return value
        return getattr(self.raw, name, None)


class XiaotLarkOAuthEnvironment:
    """Use 小T's Lark app for XiaoT identity resolution and OAuth only."""

    _aliases = {
        "LARK_APP_ID": "XIAOT_LARK_APP_ID",
        "LARK_APP_SECRET": "XIAOT_LARK_APP_SECRET",
    }

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def __getattr__(self, name: str) -> Any:
        alias = self._aliases.get(name)
        if alias:
            # Never fall back to a different agent's Lark app if XiaoT's
            # dedicated credentials are missing.
            return getattr(self.raw, alias, None)
        return getattr(self.raw, name, None)


class XiaotBitableClient:
    """Bitable API client constrained to the user-approved base and tables."""

    base_token = "CNd6bKd5ZaRWkssv5rUccCmBn0e"

    def __init__(self, env: Any) -> None:
        self.env = env

    @staticmethod
    def resolve_table(table_key: str) -> dict[str, str]:
        normalized = str(table_key or "").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "subtask": "sub_task",
            "sub_task": "sub_task",
            "space": "space_list",
            "space_list": "space_list",
            "ai_text": "ai_text",
            "aitext": "ai_text",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in XIAOT_TABLES:
            raise ValueError(
                "table_key must be one of goal, project, task, sub_task, "
                "space_list, changelog, ai_text"
            )
        return XIAOT_TABLES[normalized]

    async def request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        platform: str = "feishu",
        **kwargs: Any,
    ) -> dict[str, Any]:
        token = str(access_token or "").strip()
        if not token:
            raise RuntimeError(
                "缺少当前飞书发起人的个人授权；请先完成小 T 多维表格授权，再重试"
            )
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Content-Type", "application/json")
        normalized = str(platform or "feishu").strip().lower()
        if normalized not in {"feishu", "lark"}:
            raise ValueError("platform must be feishu or lark")
        base = _env(
            self.env,
            f"{normalized.upper()}_API_BASE",
            "https://open.larksuite.com" if normalized == "lark" else "https://open.feishu.cn",
        )
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.request(
                method,
                f"{str(base).rstrip('/')}{path}",
                headers=headers,
                **kwargs,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            message = payload.get("msg") or payload.get("message") or response.text
            raise RuntimeError(f"Feishu Bitable API failed ({response.status_code}): {message}")
        return payload

    @staticmethod
    def _table_path(table_key: str) -> str:
        table = XiaotBitableClient.resolve_table(table_key)
        return (
            f"/open-apis/bitable/v1/apps/{XiaotBitableClient.base_token}/tables/{table['table_id']}"
        )

    async def list_tables(self) -> list[dict[str, str]]:
        # Return only the allowlisted tables; never expose unrelated tables in the base.
        return [
            {
                "table_key": key,
                "table_id": value["table_id"],
                "name": value["name"],
                "description": value["description"],
            }
            for key, value in XIAOT_TABLES.items()
        ]

    async def fields(
        self, table_key: str, *, access_token: str, platform: str = "feishu"
    ) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"{self._table_path(table_key)}/fields",
            access_token=access_token,
            platform=platform,
            params={"page_size": 100},
        )
        data = payload.get("data") or {}
        return [item for item in data.get("items", []) if isinstance(item, dict)]

    async def records(
        self,
        table_key: str,
        *,
        access_token: str,
        page_size: int = 100,
        page_token: str = "",
        filter_formula: str = "",
        platform: str = "feishu",
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"page_size": max(1, min(int(page_size), 500))}
        if page_token:
            query["page_token"] = page_token
        if filter_formula:
            if len(filter_formula) > 2000:
                raise ValueError("filter_formula is too long")
            query["filter"] = filter_formula
        payload = await self.request(
            "GET",
            f"{self._table_path(table_key)}/records",
            access_token=access_token,
            platform=platform,
            params=query,
        )
        return payload.get("data") or {}

    async def record(
        self, table_key: str, record_id: str, *, access_token: str, platform: str = "feishu"
    ) -> dict[str, Any]:
        value = str(record_id or "").strip()
        self._validate_record_id(value)
        payload = await self.request(
            "GET",
            f"{self._table_path(table_key)}/records/{quote(value, safe='')}",
            access_token=access_token,
            platform=platform,
        )
        record = (payload.get("data") or {}).get("record")
        if not isinstance(record, dict):
            raise RuntimeError("Bitable response did not contain the requested record")
        return record

    async def create_record(
        self, table_key: str, fields: dict[str, Any], *, access_token: str, platform: str = "feishu"
    ) -> dict[str, Any]:
        payload = await self.request(
            "POST",
            f"{self._table_path(table_key)}/records",
            access_token=access_token,
            platform=platform,
            json={"fields": fields},
        )
        record = (payload.get("data") or {}).get("record")
        if not isinstance(record, dict) or not record.get("record_id"):
            raise RuntimeError("Bitable create response did not contain record_id")
        return record

    async def update_record(
        self,
        table_key: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        access_token: str,
        platform: str = "feishu",
    ) -> dict[str, Any]:
        self._validate_record_id(record_id)
        payload = await self.request(
            "PUT",
            f"{self._table_path(table_key)}/records/{quote(record_id, safe='')}",
            access_token=access_token,
            platform=platform,
            json={"fields": fields},
        )
        record = (payload.get("data") or {}).get("record")
        if not isinstance(record, dict) or not record.get("record_id"):
            raise RuntimeError("Bitable update response did not contain record_id")
        return record

    async def delete_record(
        self, table_key: str, record_id: str, *, access_token: str, platform: str = "feishu"
    ) -> dict[str, Any]:
        self._validate_record_id(record_id)
        payload = await self.request(
            "DELETE",
            f"{self._table_path(table_key)}/records/{quote(record_id, safe='')}",
            access_token=access_token,
            platform=platform,
        )
        return payload.get("data") or {"record_id": record_id, "deleted": True}

    @staticmethod
    def _validate_record_id(record_id: str) -> None:
        if not re.fullmatch(r"rec[A-Za-z0-9_-]{1,80}", str(record_id or "")):
            raise ValueError("record_id must be a valid Feishu Bitable record ID")

    async def validate_writable_fields(
        self,
        table_key: str,
        fields: dict[str, Any],
        *,
        access_token: str,
        platform: str = "feishu",
    ) -> None:
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a non-empty object")
        if len(_json(fields).encode("utf-8")) > 20_000:
            raise ValueError("fields payload exceeds the 20 KB limit")
        schema = await self.fields(table_key, access_token=access_token, platform=platform)
        by_name = {str(item.get("field_name") or ""): item for item in schema}
        unknown = sorted(set(fields) - set(by_name))
        if unknown:
            raise ValueError(f"unknown fields for {table_key}: {', '.join(unknown)}")
        read_only = [
            key for key in fields if int(by_name[key].get("type") or 0) in {20, 1005, 3001}
        ]
        if read_only:
            raise ValueError(f"computed or system fields cannot be written: {', '.join(read_only)}")


class XiaotAgentRelayWorkflow:
    """Small T-specific agent prompt; deliberately excludes calendar policies."""

    def __init__(self, relay: XiaotCloudflareRelay) -> None:
        self.relay = relay

    async def handle_event(
        self,
        *,
        platform: str,
        conversation_key: str,
        event: dict[str, Any],
        request_id: str,
    ) -> None:
        continuation = await self.relay.state.previous_run_exists(conversation_key)
        turn_mode = "continuation" if continuation else "initial"
        prompt = "\n".join(
            [
                f"request_id: {request_id}",
                f"conversation_key: {conversation_key}",
                f"relay_mcp: {self.relay.mcp_name()}",
                "protocol: local-agent-shell/v1",
                f"turn_mode: {turn_mode}",
                "",
                "You are 小 T, a Feishu group assistant for the authorized Bitable base.",
                (
                "For a new conversation, call update_conversation_title once, then "
                    "record_plan. Keep the plan and progress visible through relay tools."
                ),
                (
                    "Use only 小 T Bitable tools and the seven allowlisted tables. Read field "
                    "schemas and target records before proposing changes. Never invent field "
                    "names, record IDs, relation IDs, dates, or people."
                ),
                (
                    "Reads are read-only. Stage each create/update/delete with prepare_mutation. "
                    "Show the table, target, old/new values, and fields through ask_user. Call "
                    "confirm_mutation only after clear confirmation of that exact proposal. "
                    "Refusal, ambiguity, changed fields, or a new target cancels it; never write "
                    "on the initial request alone."
                ),
                (
                    "Use changelog to preserve who changed what and when. If a reliable audit "
                    "entry cannot be created, do not perform the requested mutation."
                ),
                (
                    "Goal/Project status meanings: Daily = routine/no concrete content; Later = "
                    "details confirmed but deferred; Waiting/Blocked = blocked by a dependency "
                    "or issue; Ongoing = lower-priority progress; "
                    "Important = active high priority; "
                    "Finished = complete."
                ),
                (
                    "Task/SubTask status meanings: Todo = not started; Later = details confirmed "
                    "but deferred; Waiting/Blocked = cannot proceed due to a dependency, person, "
                    "or missing information; Reviewing = execution done and awaiting review; "
                    "Finished = completed and confirmed."
                ),
                (
                    "Read status options from the target table before writing and use exact labels. "
                    "Goal/Project Ongoing maps to Normal; Task/Sub Task Todo maps to To-do and "
                    "In Progress means actively being worked on. Ask if the user requested a "
                    "status that is not an available option."
                ),
                (
                    "For search_records on task or sub_task, exclude Status=Finished by default. "
                    "Set include_finished=true only when the user explicitly asks to include "
                    "Finished records."
                ),
                (
                    "When a user replies with a clear confirmation to a pending proposal, call "
                    "confirm_mutation first using the pending proposal_id. Do not start a new "
                    "proposal or re-read fields first; the service rechecks the target record and "
                    "sender before writing. If proposal_id is unavailable, omit it and the service "
                    "will resolve only one unambiguous pending proposal for that same sender."
                ),
                (
                    "For ai_text, Category must be exactly 个人消息分析文本 or 群分析文本; "
                    "preserve this distinction when updating daily-analysis text."
                ),
                (
                    "Every confirmed write also creates a changelog audit entry with the Feishu "
                    "requester, action, target, and time. "
                    "Include this side effect in the proposal. "
                    "Ask about ambiguity. Claim success only after tool verification, then call "
                    "record_result once to reply to the original Feishu message."
                ),
                (
                    f"The relay MCP is {self.relay.mcp_name()} at "
                    f"{self.relay.base_url()}/mcp. Use record_result to reply to Feishu."
                ),
                "",
                "User task:",
                str(event.get("text") or "").strip(),
            ]
        )
        await self.relay.state.create_run(
            request_id=request_id,
            conversation_key=conversation_key,
            source_message_id=str(event["message_id"]),
            input_markdown=prompt,
            image_keys=event.get("image_keys") or [],
        )
        await self.relay._enqueue(
            {
                "kind": "agent",
                "request_id": request_id,
                "idempotency_key": f"{_env(self.relay.env, 'FEISHU_APP_ID')}:{event['message_id']}",
            }
        )


class XiaotCloudflareRelay(CloudflareRelay):
    """DEV-only 小 T Feishu relay and Bitable tool implementation."""

    def __init__(self, env: Any, ctx: Any, db_state: D1State) -> None:
        super().__init__(env, ctx, db_state)
        raw_env = env.raw if isinstance(env, XiaotEnvironment) else env
        # The XiaoT bot receives/replies to the Feishu group message. Lark
        # identity resolution and OAuth use XiaoT's own Lark app, while the
        # existing Feishu user-OAuth app stays unchanged.
        self.identity_relay = CloudflareRelay(
            XiaotLarkOAuthEnvironment(raw_env), ctx, db_state
        )
        self.xiaot_bitable = XiaotBitableClient(
            raw_env
        )
        self.agent_workflow = XiaotAgentRelayWorkflow(self)

    def base_url(self) -> str:
        return super().base_url()

    def mcp_name(self) -> str:
        return str(
            _env(self.env, "WORKSPACE_AGENT_RELAY_MCP_NAME", "workspace-agent-relay-mcp-dev")
            or "workspace-agent-relay-mcp-dev"
        )

    def memory_scope(self) -> str:
        return XIAOT_AGENT_SCOPE

    async def run_agent_job(self, body: dict[str, Any]) -> None:
        request_id = str(body.get("request_id") or "")
        run = await self.state.get_run(request_id)
        if not run:
            return
        status = str(run.get("status") or "")
        if status == "failed" and run.get("completed_at"):
            await super().run_agent_job(body)
            return
        if status != "queued":
            return
        now = int(time.time())
        claimed = await _db_all(
            self.state.db,
            "UPDATE relay_runs SET status = 'dispatching', updated_at = ? "
            "WHERE request_id = ? AND status = 'queued' RETURNING request_id",
            now,
            request_id,
        )
        if not claimed:
            return
        # Queue delivery is at-least-once. Only the worker that atomically
        # moved this run from queued may send the processing reply or trigger
        # the Agent, preventing duplicate placeholders on concurrent retries.
        await super().run_agent_job(body)

    async def _ensure_xiaot_oauth_schema(self) -> None:
        await self.state.ensure_feishu_oauth_schema()
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_oauth_states (
                state TEXT PRIMARY KEY,
                open_id TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'feishu',
                account_open_id TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                conversation_key TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                event_json TEXT NOT NULL DEFAULT '{}',
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                created_at INTEGER NOT NULL
            )""",
        )
        # Upgrade the first XiaoT DEV schema in place; old states are left
        # unusable for callbacks because they lack a matching platform id.
        for column, definition in (
            ("platform", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("account_open_id", "TEXT NOT NULL DEFAULT ''"),
            ("request_id", "TEXT NOT NULL DEFAULT ''"),
            ("event_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            with suppress(Exception):
                await _db_run(
                    self.state.db,
                    f"ALTER TABLE xiaot_bitable_oauth_states ADD COLUMN {column} {definition}",
                )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_run_requesters (
                request_id TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_proposals (
                proposal_id TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                requester_open_id TEXT NOT NULL,
                requester_platform TEXT NOT NULL DEFAULT 'feishu',
                operation TEXT NOT NULL,
                table_key TEXT NOT NULL,
                record_id TEXT NOT NULL DEFAULT '',
                fields_json TEXT NOT NULL DEFAULT '{}',
                before_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )""",
        )
        with suppress(Exception):
            await _db_run(
                self.state.db,
                "ALTER TABLE xiaot_bitable_proposals ADD COLUMN requester_platform TEXT NOT NULL DEFAULT 'feishu'",
            )

    async def _resolve_account_identity(self, event: dict[str, Any]) -> dict[str, str]:
        source_open_id = str(event.get("open_id") or "").strip()
        if not source_open_id:
            raise ValueError("小T event does not contain a requester open_id")
        platform = await self.identity_relay.detect_user_platform(event, "feishu")
        api = self.identity_relay.lark if platform == "lark" else self.identity_relay.feishu
        if api is None:
            raise RuntimeError(f"{platform.title()} authorization is not configured")
        # open_id is scoped to the app that produced the webhook.  XiaoT's
        # event app and the existing personal-OAuth app can therefore have
        # different open_ids for the same human.  Resolve into the OAuth app's
        # namespace (prefer the tenant-stable union_id) before using it as a
        # token key; never cache an event app id under an unrelated OAuth app.
        identifiers = (
            (str(event.get("union_id") or "").strip(), "union_id"),
            (str(event.get("user_id") or "").strip(), "user_id"),
            (source_open_id, "open_id"),
        )
        for identifier, identifier_type in identifiers:
            if not identifier:
                continue
            try:
                resolved = await api.resolve_user_id(
                    identifier, user_id_type=identifier_type
                )
            except Exception:
                continue
            open_id = str(resolved.get("open_id") or "").strip()
            if open_id:
                return {"platform": platform, "open_id": open_id}
        raise RuntimeError(
            "无法安全确认当前发起人的 Lark 账号；未创建 Agent 任务，也未使用其他用户权限。"
        )

    async def _send_user_authorization(
        self,
        *,
        event: dict[str, Any],
        conversation_key: str,
        request_id: str,
        account_identity: dict[str, str],
    ) -> None:
        await self._ensure_xiaot_oauth_schema()
        platform = str(account_identity.get("platform") or "feishu")
        open_id = str(account_identity.get("open_id") or "").strip()
        callback_key = f"{platform.upper()}_OAUTH_REDIRECT_URI"
        redirect_uri = str(
            _env(
                self.identity_relay.env,
                callback_key,
                self.identity_relay.base_url() + f"/{platform}/oauth/callback",
            )
            or ""
        ).strip()
        if not open_id or not redirect_uri:
            raise RuntimeError(f"小T 的 {platform.title()} 用户授权回调地址尚未配置")
        state = f"xiaot_{platform}_" + secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 600
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_bitable_oauth_states "
            "(state, open_id, platform, account_open_id, request_id, conversation_key, "
            "source_message_id, redirect_uri, event_json, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state,
            open_id,
            platform,
            open_id,
            request_id,
            conversation_key,
            str(event.get("message_id") or ""),
            redirect_uri,
            _json(event),
            expires_at,
            int(time.time()),
        )
        scope = self.identity_relay.platform_oauth_scope(platform)
        auth_base = (
            "https://accounts.larksuite.com" if platform == "lark" else FEISHU_AUTH_BASE_URL
        )
        auth_url = f"{auth_base}/open-apis/authen/v1/authorize?" + urlencode(
            {
                "app_id": _env(self.identity_relay.env, f"{platform.upper()}_APP_ID"),
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
            }
        )
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "小 T 需要使用当前发起人的账号权限访问多维表格。"
                            f"请使用你的 {platform.title()} 账号授权；授权后会自动继续刚才的请求。"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "授权并继续"},
                            "type": "primary",
                            "url": auth_url,
                        }
                    ],
                },
            ],
        }
        outbound = await self.api_for_conversation(conversation_key).reply_card(
            str(event["message_id"]), card
        )
        await self.state.save_reply(str(outbound), conversation_key)

    async def handle_user_oauth_callback(self, request: Any, platform: str) -> Response:
        if str(request.method or "").upper() != "GET":
            return _response({"error": "method_not_allowed"}, 405, {"allow": "GET"})
        normalized = str(platform or "").strip().lower()
        if normalized not in {"feishu", "lark"}:
            return _response({"success": False, "error": "unsupported_platform"}, 400)
        params = parse_qs(urlparse(request.url).query)
        code = str((params.get("code") or [""])[0]).strip()
        state = str((params.get("state") or [""])[0]).strip()
        if not code or not state:
            return _response({"success": False, "error": "missing_code_or_state"}, 400)
        await self._ensure_xiaot_oauth_schema()
        pending = await _db_first(
            self.state.db,
            "SELECT * FROM xiaot_bitable_oauth_states WHERE state = ? AND consumed_at IS NULL",
            state,
        )
        if (
            not pending
            or str(pending.get("platform") or "") != normalized
            or not pending.get("account_open_id")
            or int(pending.get("expires_at") or 0) < int(time.time())
        ):
            return _response({"success": False, "error": "invalid_or_expired_state"}, 400)
        consumed = await _db_all(
            self.state.db,
            "UPDATE xiaot_bitable_oauth_states SET consumed_at = ? "
            "WHERE state = ? AND platform = ? AND consumed_at IS NULL RETURNING state",
            int(time.time()),
            state,
            normalized,
        )
        if not consumed:
            return _response({"success": False, "error": "state_already_used"}, 409)
        redirect_uri = str(pending.get("redirect_uri") or "")
        auth_base = (
            "https://accounts.larksuite.com"
            if normalized == "lark"
            else FEISHU_AUTH_BASE_URL
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{auth_base}/oauth/v3/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": _env(self.identity_relay.env, f"{normalized.upper()}_APP_ID"),
                        "client_secret": _env(self.identity_relay.env, f"{normalized.upper()}_APP_SECRET"),
                        "redirect_uri": redirect_uri,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
            token_payload = response.json()
            token_data = token_payload.get("data") or token_payload
            if response.status_code >= 400 or not token_data.get("access_token"):
                return _response(
                    {
                        "success": False,
                        "error": "token_exchange_failed",
                        "message": str(
                            token_payload.get("msg")
                            or token_payload.get("error_description")
                            or f"{normalized.title()} 没有返回 user_access_token"
                        ),
                    },
                    502,
                )
            access_token = str(token_data["access_token"])
            user_data = await self.identity_relay.api_for_conversation(
                f"{normalized}:oauth"
            ).user_info(access_token)
            identity = str(user_data.get("open_id") or "")
            if identity != str(pending.get("account_open_id") or ""):
                return _response(
                    {
                        "success": False,
                        "error": "requester_mismatch",
                        "message": f"授权的 {normalized.title()} 账号与发起 @小T 的人员不一致，未保存授权。",
                    },
                    403,
                )
            await self.state.save_user_token(
                platform=normalized,
                open_id=identity,
                access_token=access_token,
                refresh_token=str(token_data.get("refresh_token") or ""),
                expires_at=int(time.time()) + int(token_data.get("expires_in") or 7200),
            )
            event = json.loads(str(pending.get("event_json") or "{}"))
            request_id = str(pending.get("request_id") or "")
            conversation_key = str(pending.get("conversation_key") or "")
            if not request_id or not conversation_key or not isinstance(event, dict):
                raise RuntimeError("授权状态缺少待处理的原始请求")
            existing_run = await self.state.get_run(request_id)
            if not existing_run:
                await _db_run(
                    self.state.db,
                    "INSERT INTO xiaot_run_requesters "
                    "(request_id, conversation_key, platform, open_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
                    "conversation_key=excluded.conversation_key, platform=excluded.platform, "
                    "open_id=excluded.open_id",
                    request_id,
                    conversation_key,
                    normalized,
                    identity,
                    int(time.time()),
                )
                await self.agent_workflow.handle_event(
                    platform=normalized,
                    conversation_key=conversation_key,
                    event=event,
                    request_id=request_id,
                )
            return _response(
                {
                    "success": True,
                    "message": "授权完成，刚才的请求已自动继续处理。",
                }
            )
        except Exception as exc:
            return _response(
                {
                    "success": False,
                    "error": "oauth_callback_failed",
                    "message": _safe_error(exc),
                },
                502,
            )

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions = [
            item for item in super().tool_definitions() if item.get("name") in _RELAY_TOOLS
        ]
        string = {"type": "string"}
        definitions.extend(
            [
                {
                    "name": "list_tables",
                    "description": "列出小 T 被允许访问的七张多维表格及用途。",
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key"],
                        "properties": {"request_id": string, "conversation_key": string},
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "get_table_fields",
                    "description": "读取指定允许表的字段、类型和选项。写入前必须先读取。",
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key", "table_key"],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "table_key": string,
                        },
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                "name": "search_records",
                "description": (
                    "查询允许表中的记录，支持 Feishu Bitable filter formula 和分页。"
                    "task/sub_task 默认排除 Status=Finished；仅用户明确要求时设置 include_finished=true。"
                ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key", "table_key"],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "table_key": string,
                            "page_size": {"type": "integer"},
                            "page_token": string,
                            "filter_formula": string,
                            "include_finished": {"type": "boolean", "default": False},
                        },
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "get_record",
                    "description": "按记录 ID 读取允许表中的精确记录。",
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key", "table_key", "record_id"],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "table_key": string,
                            "record_id": string,
                        },
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "prepare_mutation",
                    "description": (
                        "建立待确认的新增/更新/删除提案；该工具不写入表格。"
                        "必须先把返回的完整提案发给用户。"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key", "operation", "table_key"],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "operation": {"type": "string", "enum": ["create", "update", "delete"]},
                            "table_key": string,
                            "record_id": string,
                            "fields": {"type": "object"},
                        },
                    },
                },
                {
                    "name": "confirm_mutation",
                    "description": (
                        "仅在用户明确确认紧邻的完整提案后执行对应的单次新增/更新/删除。"
                        "拒绝或模糊回复会被服务端拦截。"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key"],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "proposal_id": string,
                        },
                    },
                },
            ]
        )
        return definitions

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in _RELAY_TOOLS:
            if name in {
                "record_plan",
                "record_progress",
                "record_result",
                "update_conversation_title",
                "ask_user",
            }:
                try:
                    run = await self._require_xiaot_run(args)
                    args = {**args, "request_id": str(run["request_id"])}
                except (ValueError, RuntimeError, KeyError) as exc:
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "xiaot_run_context_invalid",
                                "message": _safe_error(exc),
                            },
                        },
                        True,
                    )
            return await super().call_tool(name, args)
        result = self._tool_result
        try:
            if name in XIAOT_READ_TOOL_NAMES:
                run = await self._require_xiaot_run(args)
                args = {**args, "request_id": str(run["request_id"])}
            elif name in {"prepare_mutation", "confirm_mutation"}:
                run = await self._require_xiaot_run(args)
                args = {**args, "request_id": str(run["request_id"])}
            access_token = ""
            requester: dict[str, str] | None = None
            if name in XIAOT_READ_TOOL_NAMES - {"list_tables"} or name in {
                "prepare_mutation",
                "confirm_mutation",
            }:
                requester = await self._requester_for_run(
                    str(args.get("request_id") or ""),
                    str(args.get("conversation_key") or ""),
                )
                access_token = await self._user_access_token_for_run(
                    str(args.get("request_id") or ""),
                    str(args.get("conversation_key") or ""),
                )
            if name == "list_tables":
                return result({"success": True, "tables": await self.xiaot_bitable.list_tables()})
            if name == "get_table_fields":
                table = self.xiaot_bitable.resolve_table(str(args.get("table_key") or ""))
                return result(
                    {
                        "success": True,
                        "table": table,
                        "fields": await self.xiaot_bitable.fields(
                            str(args["table_key"]),
                            access_token=access_token,
                            platform=(requester or {}).get("platform", "feishu"),
                        ),
                    }
                )
            if name == "search_records":
                table_key = str(args.get("table_key") or "")
                filter_formula = str(args.get("filter_formula") or "")
                include_finished = args.get("include_finished") is True
                if table_key.lower().replace("-", "_").replace(" ", "_") in {
                    "task",
                    "sub_task",
                    "subtask",
                } and not include_finished:
                    schema = await self.xiaot_bitable.fields(
                        table_key,
                        access_token=access_token,
                        platform=(requester or {}).get("platform", "feishu"),
                    )
                    status_field = next(
                        (
                            str(item.get("field_name") or "")
                            for item in schema
                            if str(item.get("field_name") or "").strip().casefold()
                            in {"status", "状态"}
                        ),
                        "",
                    )
                    if not status_field:
                        raise RuntimeError(
                            f"{table_key} 表中没有可用于排除 Finished 的 Status/状态字段"
                        )
                    escaped_field = status_field.replace("\\", "\\\\").replace("]", "\\]")
                    finished_filter = f'CurrentValue.[{escaped_field}]!="Finished"'
                    filter_formula = (
                        f"AND({finished_filter}, {filter_formula})"
                        if filter_formula
                        else finished_filter
                    )
                data = await self.xiaot_bitable.records(
                    table_key,
                    access_token=access_token,
                    platform=(requester or {}).get("platform", "feishu"),
                    page_size=int(args.get("page_size") or 100),
                    page_token=str(args.get("page_token") or ""),
                    filter_formula=filter_formula,
                )
                return result(
                    {
                        "success": True,
                        "table_key": args.get("table_key"),
                        "items": data.get("items") or [],
                        "has_more": bool(data.get("has_more")),
                        "page_token": data.get("page_token") or "",
                    }
                )
            if name == "get_record":
                record = await self.xiaot_bitable.record(
                    str(args.get("table_key") or ""),
                    str(args.get("record_id") or ""),
                    access_token=access_token,
                    platform=(requester or {}).get("platform", "feishu"),
                )
                return result({"success": True, "record": record})
            if name == "prepare_mutation":
                return result(await self._prepare_mutation(args))
            if name == "confirm_mutation":
                return result(await self._confirm_mutation(args))
        except (ValueError, RuntimeError, KeyError) as exc:
            return result(
                {
                    "success": False,
                    "error": {"code": "bitable_operation_failed", "message": _safe_error(exc)},
                },
                True,
            )
        except Exception as exc:
            return result(
                {
                    "success": False,
                    "error": {"code": "bitable_api_error", "message": _safe_error(exc)},
                },
                True,
            )
        return result(
            {
                "success": False,
                "error": {"code": "unknown_tool", "message": f"unknown tool {name}"},
            },
            True,
        )

    async def _require_xiaot_run(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        if not conversation_key.startswith("xiaot:"):
            raise ValueError("小 T Bitable tools require a 小 T Feishu conversation")
        if not request_id:
            raise ValueError("request_id and conversation_key are required for 小 T tools")

        # Feishu confirmation replies start a new relay run in the same Agent
        # conversation. The Agent can still pass the previous run's valid ID;
        # accepting it would inspect the old user text instead of the current
        # confirmation and can make the update appear to fail. Bind Bitable
        # actions to the newest live run, and only repair a stale ID when the
        # previous and current runs belong to the same verified sender.
        current = await _db_first(
            self.state.db,
            "SELECT r.* FROM relay_runs r "
            "WHERE r.conversation_key = ? AND r.status IN ('queued','dispatching','triggered','running','needs_user') "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1",
            conversation_key,
        )
        if not current:
            raise ValueError("no active 小 T request exists for this conversation")
        current_id = str(current.get("request_id") or "")
        if request_id == current_id:
            return await self._require_run(current_id, conversation_key)

        previous = await self._require_run(request_id, conversation_key)
        previous_sender = await self._requester_for_run(request_id, conversation_key)
        current_sender = await self._requester_for_run(current_id, conversation_key)
        if (
            previous_sender.get("platform") != current_sender.get("platform")
            or previous_sender.get("open_id") != current_sender.get("open_id")
        ):
            raise ValueError(
                "stale request_id belongs to a different sender; refusing to cross user contexts"
            )
        return current

    async def _user_access_token_for_run(
        self, request_id: str, conversation_key: str
    ) -> str:
        identity = await self._requester_for_run(request_id, conversation_key)
        await self.state.ensure_feishu_oauth_schema()
        cached = await self.state.user_token(identity["platform"], identity["open_id"])
        if not cached:
            raise RuntimeError(
                f"当前 @小T 的 {identity['platform'].title()} 用户尚未完成个人多维表格授权；请先授权后重试"
            )
        if int(cached.get("expires_at") or 0) <= int(time.time()) + 30:
            raise RuntimeError(
                f"当前 @小T 的 {identity['platform'].title()} 个人授权已过期；请重新授权后再继续，未使用机器人应用权限代替"
            )
        return str(cached.get("access_token") or "")

    async def _ensure_proposal_schema(self) -> None:
        await self._ensure_xiaot_oauth_schema()

    async def _prepare_mutation(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        await self._require_xiaot_run(args)
        identity = await self._requester_for_run(request_id, conversation_key)
        access_token = await self._user_access_token_for_run(request_id, conversation_key)
        operation = str(args.get("operation") or "").strip().lower()
        if operation not in {"create", "update", "delete"}:
            raise ValueError("operation must be create, update, or delete")
        table_key = str(args.get("table_key") or "").strip()
        table = self.xiaot_bitable.resolve_table(table_key)
        fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
        record_id = str(args.get("record_id") or "").strip()
        before: dict[str, Any] = {}
        if operation == "create":
            await self.xiaot_bitable.validate_writable_fields(
                table_key,
                fields,
                access_token=access_token,
                platform=identity["platform"],
            )
        else:
            if not record_id:
                raise ValueError(f"{operation} proposals require record_id")
            before = await self.xiaot_bitable.record(
                table_key,
                record_id,
                access_token=access_token,
                platform=identity["platform"],
            )
            if operation == "update" and not fields:
                raise ValueError("update proposals require fields")
            if operation == "update":
                await self.xiaot_bitable.validate_writable_fields(
                    table_key,
                    fields,
                    access_token=access_token,
                    platform=identity["platform"],
                )
            if operation == "delete":
                fields = {}
        await self._ensure_proposal_schema()
        proposal_id = "xbt_" + secrets.token_urlsafe(18)
        now = int(time.time())
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_bitable_proposals "
            "(proposal_id, conversation_key, requester_open_id, requester_platform, operation, table_key, "
            "record_id, fields_json, before_json, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            proposal_id,
            conversation_key,
            str(identity["open_id"]),
            str(identity["platform"]),
            operation,
            table_key,
            record_id,
            _json(fields),
            _json(before),
            now,
            now + 900,
        )
        return {
            "success": True,
            "write_performed": False,
            "proposal_id": proposal_id,
            "operation": operation,
            "table": table,
            "record_id": record_id or None,
            "before": before or None,
            "proposed_fields": fields or None,
            "audit_entry": {
                "table_key": "changelog",
                "action": operation,
                "requester_open_id": str(identity["open_id"]),
                "note": "执行前创建‘执行中’记录，成功后更新结果；目标写入失败则标记失败。",
            },
            "requires_explicit_user_confirmation": True,
            "expires_in_seconds": 900,
        }

    @staticmethod
    def _current_user_task(run: dict[str, Any]) -> str:
        value = str(run.get("input_markdown") or "")
        return value.rsplit("User task:\n", 1)[-1].strip()

    @staticmethod
    def _fields_match(record: dict[str, Any], expected: dict[str, Any]) -> bool:
        actual = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        return all(actual.get(key) == value for key, value in expected.items())

    async def _requester_for_run(
        self, request_id: str, conversation_key: str
    ) -> dict[str, str]:
        """Resolve the platform-scoped, verified requester for this exact run."""
        await self._ensure_xiaot_oauth_schema()
        row = await _db_first(
            self.state.db,
            "SELECT platform, open_id FROM xiaot_run_requesters "
            "WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        if row and row.get("open_id"):
            return {
                "platform": str(row.get("platform") or "feishu"),
                "open_id": str(row["open_id"]),
            }
        row = await _db_first(
            self.state.db,
            "SELECT sender_open_id FROM feishu_events "
            "WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        if not row or not row.get("sender_open_id"):
            raise ValueError("the current 小 T run has no verified Feishu requester")
        return {"platform": "feishu", "open_id": str(row["sender_open_id"])}

    @staticmethod
    def _is_explicit_confirmation(value: str, operation: str) -> bool:
        text = re.sub(r"[\s，。！？,.!?；;：:]+", "", str(value or "")).casefold()
        if not text or len(text) > 100:
            return False
        refusals = (
            "不执行",
            "不开始",
            "不接受",
            "不确认",
            "不行",
            "不好的",
            "不好",
            "不可以",
            "不新增",
            "不创建",
            "不更新",
            "不修改",
            "不删除",
            "拒绝",
            "取消",
            "不要",
            "no",
            "nope",
        )
        if any(token in text for token in refusals):
            return False
        operation_words = {
            "create": ("确认新增", "确认创建", "确认添加"),
            "update": ("确认更新", "确认修改"),
            "delete": ("确认删除",),
        }[operation]
        positive = (
            "确认",
            "执行",
            "开始",
            "接受",
            "行",
            "行的",
            "好",
            "好的",
            "可以",
            "yes",
            "ok",
            "同意",
        )
        return text in positive or any(text == item for item in operation_words)

    async def _confirm_mutation(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        run = await self._require_xiaot_run(args)
        request_id = str(run["request_id"])
        identity = await self._requester_for_run(request_id, conversation_key)
        access_token = await self._user_access_token_for_run(request_id, conversation_key)
        await self._ensure_proposal_schema()
        proposal_id = str(args.get("proposal_id") or "").strip()
        if proposal_id:
            proposal = await _db_first(
                self.state.db,
                "SELECT * FROM xiaot_bitable_proposals WHERE proposal_id = ?",
                proposal_id,
            )
        else:
            candidates = await _db_all(
                self.state.db,
                "SELECT * FROM xiaot_bitable_proposals WHERE conversation_key = ? "
                "AND requester_platform = ? AND requester_open_id = ? "
                "AND status = 'pending' AND expires_at >= ? "
                "ORDER BY created_at DESC LIMIT 2",
                conversation_key,
                str(identity["platform"]),
                str(identity["open_id"]),
                int(time.time()),
            )
            if len(candidates) > 1:
                raise ValueError(
                    "multiple pending proposals exist; provide the proposal_id for the one being confirmed"
                )
            proposal = candidates[0] if candidates else None
            proposal_id = str((proposal or {}).get("proposal_id") or "")
        if not proposal or proposal.get("status") != "pending":
            raise ValueError("proposal was not found, is no longer pending, or was already used")
        if proposal.get("conversation_key") != conversation_key or proposal.get(
            "requester_open_id"
        ) != identity.get("open_id") or proposal.get("requester_platform", "feishu") != identity.get(
            "platform"
        ):
            raise ValueError("proposal belongs to a different conversation or requester")
        if int(proposal.get("expires_at") or 0) < int(time.time()):
            await _db_run(
                self.state.db,
                "UPDATE xiaot_bitable_proposals SET status = 'expired' WHERE proposal_id = ?",
                proposal_id,
            )
            raise ValueError("proposal expired; prepare and show a fresh proposal")
        operation = str(proposal.get("operation") or "")
        user_task = self._current_user_task(run)
        if not self._is_explicit_confirmation(user_task, operation):
            raise ValueError(
                "the current sender reply is not an explicit confirmation of this proposal"
            )
        table_key = str(proposal["table_key"])
        record_id = str(proposal.get("record_id") or "")
        before = json.loads(str(proposal.get("before_json") or "{}"))
        fields = json.loads(str(proposal.get("fields_json") or "{}"))
        if operation in {"update", "delete"}:
            latest = await self.xiaot_bitable.record(
                table_key,
                record_id,
                access_token=access_token,
                platform=identity["platform"],
            )
            if latest.get("fields") != before.get("fields"):
                await _db_run(
                    self.state.db,
                    "UPDATE xiaot_bitable_proposals SET status = 'stale' WHERE proposal_id = ?",
                    proposal_id,
                )
                raise ValueError(
                    "target record changed after the proposal; re-read and present a new proposal"
                )
        claimed = await _db_all(
            self.state.db,
            "UPDATE xiaot_bitable_proposals SET status = 'executing' "
            "WHERE proposal_id = ? AND status = 'pending' RETURNING proposal_id",
            proposal_id,
        )
        if not claimed:
            raise ValueError("proposal was already claimed by another request")
        try:
            audit = await self._create_audit_entry(
                operation=operation,
                table_key=table_key,
                record_id=record_id,
                fields=fields,
                requester_open_id=str(identity["open_id"]),
                access_token=access_token,
                requester_platform=identity["platform"],
            )
        except Exception:
            await _db_run(
                self.state.db,
                "UPDATE xiaot_bitable_proposals SET status = 'audit_failed' WHERE proposal_id = ?",
                proposal_id,
            )
            raise
        try:
            if operation == "create":
                created = await self.xiaot_bitable.create_record(
                    table_key,
                    fields,
                    access_token=access_token,
                    platform=identity["platform"],
                )
                record = await self.xiaot_bitable.record(
                    table_key,
                    str(created["record_id"]),
                    access_token=access_token,
                    platform=identity["platform"],
                )
                if not self._fields_match(record, fields):
                    raise RuntimeError("created record read-back did not match the proposal")
            elif operation == "update":
                for attempt in range(2):
                    write_error: Exception | None = None
                    try:
                        await self.xiaot_bitable.update_record(
                            table_key,
                            record_id,
                            fields,
                            access_token=access_token,
                            platform=identity["platform"],
                        )
                    except Exception as exc:
                        write_error = exc
                    try:
                        record = await self.xiaot_bitable.record(
                            table_key,
                            record_id,
                            access_token=access_token,
                            platform=identity["platform"],
                        )
                    except Exception as read_error:
                        raise RuntimeError(
                            "update outcome could not be verified after the write; do not retry manually until the row is checked"
                        ) from (write_error or read_error)
                    if self._fields_match(record, fields):
                        break
                    unchanged = record.get("fields") == before.get("fields")
                    if attempt == 0 and unchanged:
                        # PUT is idempotent, and the read-back proved that the
                        # first attempt did not change the target row.
                        continue
                    detail = _safe_error(write_error) if write_error else "API returned without applying the proposed fields"
                    raise RuntimeError(
                        f"update was not verified after {attempt + 1} attempt(s): {detail}"
                    ) from write_error
            else:
                record = await self.xiaot_bitable.delete_record(
                    table_key,
                    record_id,
                    access_token=access_token,
                    platform=identity["platform"],
                )
            await _db_run(
                self.state.db,
                "UPDATE xiaot_bitable_proposals SET status = 'completed' WHERE proposal_id = ?",
                proposal_id,
            )
        except Exception:
            # Do not retry automatically: a timeout can occur after a write was applied.
            await _db_run(
                self.state.db,
                "UPDATE xiaot_bitable_proposals SET status = 'outcome_unknown' "
                "WHERE proposal_id = ?",
                proposal_id,
            )
            with suppress(Exception):
                await self.xiaot_bitable.update_record(
                    "changelog",
                    str(audit["record_id"]),
                    {
                        "修改日志": self._audit_note(
                            operation=operation,
                            table_key=table_key,
                            record_id=record_id,
                            fields=fields,
                            before=before,
                            status="结果待核实；请检查对应记录",
                        )
                    },
                    access_token=access_token,
                    platform=identity["platform"],
                )
            raise
        audit_warning = ""
        try:
            await self.xiaot_bitable.update_record(
                "changelog",
                str(audit["record_id"]),
                {
                    "修改日志": self._audit_note(
                        operation=operation,
                        table_key=table_key,
                        record_id=record_id or str(record.get("record_id") or ""),
                        fields=fields,
                        before=before,
                        status="已完成",
                    )
                },
                access_token=access_token,
                platform=identity["platform"],
            )
        except Exception:
            audit_warning = "目标操作已完成，但日志结果更新失败；请核查 changelog 中的执行中记录。"
        return {
            "success": True,
            "verified": True,
            "operation": operation,
            "table_key": table_key,
            "record_id": record_id or record.get("record_id"),
            "record": record if operation != "delete" else None,
            "deleted": operation == "delete",
            "audit_record_id": str(audit["record_id"]),
            "audit_warning": audit_warning or None,
        }

    async def _create_audit_entry(
        self,
        *,
        operation: str,
        table_key: str,
        record_id: str,
        fields: dict[str, Any],
        requester_open_id: str,
        access_token: str,
        requester_platform: str = "feishu",
    ) -> dict[str, Any]:
        return await self.xiaot_bitable.create_record(
            "changelog",
            {
                "修改日志": self._audit_note(
                    operation=operation,
                    table_key=table_key,
                    record_id=record_id,
                    fields=fields,
                    before={},
                    status="执行中",
                ),
                "修改人": [{"id": requester_open_id}],
                "日期": int(time.time() * 1000),
            },
            access_token=access_token,
            platform=requester_platform,
        )

    @staticmethod
    def _audit_note(
        *,
        operation: str,
        table_key: str,
        record_id: str,
        fields: dict[str, Any],
        before: dict[str, Any],
        status: str,
    ) -> str:
        names = {"create": "新增", "update": "更新", "delete": "删除"}
        return _json(
            {
                "状态": status,
                "操作": names.get(operation, operation),
                "表": table_key,
                "记录 ID": record_id or None,
                "变更字段": fields or None,
                "变更前": (before.get("fields") if before else None),
                "时间": int(time.time() * 1000),
            }
        )

    async def handle_xiaot_event(self, request: Any) -> Response:
        if str(request.method or "").upper() != "POST":
            return _response({"error": "method_not_allowed"}, 405, {"allow": "POST"})
        body = await self._body_json(request)
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        verify = _env(self.env, "FEISHU_VERIFY_TOKEN")
        if not verify:
            return _response({"error": "verification_token_not_configured"}, 503)
        # Feishu's URL-verification POST uses the legacy envelope with a
        # top-level `token`; schema 2.0 event deliveries put it in `header`.
        # Accept both so the platform can validate the same callback URL.
        supplied_token = str(header.get("token") or body.get("token") or "")
        if not secrets.compare_digest(supplied_token, verify):
            return _response({"code": 1}, 403)
        if body.get("challenge"):
            return _response({"challenge": body["challenge"]})
        if str(header.get("event_type") or "") != "im.message.receive_v1":
            return _response({"code": 0})
        if not _env(self.env, "FEISHU_BOT_OPEN_ID"):
            return _response({"code": 0})
        await self._schedule_feishu_event(body, "xiaot")
        return _response({"code": 0})

    async def _process_feishu_event(self, body: dict[str, Any], platform: str = "xiaot") -> None:
        bot_id = _env(self.env, "FEISHU_BOT_OPEN_ID")
        event = _normalize_event(body, bot_id, allow_unmentioned_reply=True)
        if event is None or not event.get("open_id"):
            return
        parent = str(event.get("parent_id") or "")
        conversation_key = await self.state.reply_conversation(parent) if parent else None
        if not conversation_key or not str(conversation_key).startswith("xiaot:"):
            conversation_key = None
        if not event.get("mentioned_bot") and not conversation_key:
            return
        app_id = _env(self.env, "FEISHU_APP_ID")
        if not conversation_key:
            conversation_key = f"xiaot:{app_id}:{event['chat_id']}:{secrets.token_hex(6)}"
        request_id = _request_id("xiaot")
        await self.state.ensure_schema()
        await self._ensure_xiaot_oauth_schema()
        if not await self.state.claim_event(
            message_id=f"xiaot:{event['message_id']}",
            request_id=request_id,
            conversation_key=conversation_key,
            chat_id=event["chat_id"],
            open_id=event["open_id"],
        ):
            return
        await self.state.save_requester(
            conversation_key,
            event["open_id"],
            event.get("name") or "",
            event["chat_id"],
            "xiaot",
        )
        try:
            identity = await self._resolve_account_identity(event)
        except Exception as exc:
            with suppress(Exception):
                await self.api_for_conversation(conversation_key).reply(
                    str(event["message_id"]),
                    f"无法确认当前发起人的 Feishu/Lark 账号：{_safe_error(exc)}。未访问或修改多维表格。",
                )
            return
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_run_requesters "
            "(request_id, conversation_key, platform, open_id, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
            "conversation_key=excluded.conversation_key, platform=excluded.platform, "
            "open_id=excluded.open_id",
            request_id,
            conversation_key,
            identity["platform"],
            identity["open_id"],
            int(time.time()),
        )
        user_token = await self.state.user_token(identity["platform"], identity["open_id"])
        if not user_token or int(user_token.get("expires_at") or 0) <= int(time.time()) + 30:
            await self._send_user_authorization(
                event=event,
                conversation_key=conversation_key,
                request_id=request_id,
                account_identity=identity,
            )
            return
        await self.agent_workflow.handle_event(
            platform=identity["platform"],
            conversation_key=conversation_key,
            event=event,
            request_id=request_id,
        )


class CombinedCloudflareRelay(CloudflareRelay):
    """Expose 小 C and 小 T actions through the existing DEV MCP endpoint."""

    def __init__(self, env: Any, ctx: Any, db_state: D1State) -> None:
        super().__init__(env, ctx, db_state)
        self.xiaot = XiaotCloudflareRelay(XiaotEnvironment(env), ctx, db_state)

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions = super().tool_definitions()
        definitions.extend(
            item
            for item in self.xiaot.tool_definitions()
            if item.get("name") in XIAOT_TOOL_NAMES
        )
        return definitions

    async def feishu_oauth(
        self, request: Any, path: str, platform: str = "feishu"
    ) -> Response:
        if path.endswith("/oauth/callback"):
            state = str((parse_qs(urlparse(request.url).query).get("state") or [""])[0])
            if state.startswith(f"xiaot_{platform}_"):
                return await self.xiaot.handle_user_oauth_callback(request, platform)
        return await super().feishu_oauth(request, path, platform)

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = args if isinstance(args, dict) else {}
        conversation_key = str(args.get("conversation_key") or "")
        is_xiaot = conversation_key.startswith("xiaot:")
        if name in XIAOT_TOOL_NAMES:
            if not is_xiaot:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "wrong_agent_context",
                            "message": "小 T Bitable actions require the current 小 T Feishu run.",
                        },
                    },
                    True,
                )
            return await self.xiaot.call_tool(name, args)
        if is_xiaot:
            if name == "server_info":
                return await super().call_tool(name, args)
            if name in _RELAY_TOOLS:
                return await self.xiaot.call_tool(name, args)
            return self._tool_result(
                {
                    "success": False,
                    "error": {
                        "code": "action_not_enabled_for_agent",
                        "message": "This action is not enabled for the 小 T Agent.",
                    },
                },
                True,
            )
        return await super().call_tool(name, args)
