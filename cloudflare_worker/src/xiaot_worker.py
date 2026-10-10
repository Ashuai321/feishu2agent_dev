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
    FEISHU_AUTH_BASE_URL,
    CloudflareRelay,
    D1State,
    _db_all,
    _db_first,
    _db_run,
    _env,
    _event_task_text,
    _inherit_replied_run_images,
    _json,
    _load_explicit_quoted_context,
    _normalize_event,
    _request_id,
    _response,
    _safe_error,
)
from workers import Response


def _html_response(body: str, status: int = 200, *, nonce: str = "") -> Response:
    headers = {
        "cache-control": "no-store",
        "content-type": "text/html; charset=utf-8",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
    }
    if nonce:
        headers["content-security-policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
    return Response(body, status=status, headers=headers)


def _oauth_launcher_page(auth_url: str, origin: str, state: str) -> Response:
    nonce = secrets.token_urlsafe(18)
    channel_name = f"xiaot-oauth-{state}"
    config = _json(
        {"auth_url": auth_url, "origin": origin, "state": state, "channel_name": channel_name}
    )
    config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小T账号授权</title>
<style>body{{font:16px system-ui,sans-serif;max-width:36rem;margin:12vh auto;padding:0 1.25rem;color:#1f2937}}button{{font:inherit;padding:.7rem 1.1rem;border:0;border-radius:.5rem;background:#2563eb;color:white;cursor:pointer}}#status{{line-height:1.6;color:#475569}}</style>
<h1>小T账号授权</h1><p>点击下方按钮打开安全授权窗口。授权成功后，授权窗口会自动关闭，原请求会继续处理。</p>
<button id="open-auth" type="button">打开授权窗口</button><p id="status" role="status">请确认浏览器允许弹出授权窗口。</p>
<script nonce="{nonce}">
const config={config}; let authPopup=null; const status=document.getElementById("status");
document.getElementById("open-auth").addEventListener("click",()=>{{
  authPopup=window.open(config.auth_url,"xiaot_oauth_popup","popup=yes,width=600,height=760,resizable=yes,scrollbars=yes");
  if(!authPopup){{status.textContent="授权窗口被浏览器拦截了，请允许此网站弹出窗口后重试。";return;}}
  status.textContent="请在弹出的窗口中完成授权；完成后本页会自动更新。"; authPopup.focus();
}});
window.addEventListener("message",event=>{{if(event.origin===config.origin&&event.source===authPopup)handleResult(event.data);}});
function handleResult(result){{
  if(!result||result.type!=="xiaot-user-oauth-result"||result.state!==config.state)return;
  status.textContent=result.success?"授权成功，刚才的请求正在继续处理。可以返回飞书。":"授权未完成，请返回飞书查看提示并重试。";
  if(result.success)window.close();
}}
if("BroadcastChannel" in window){{const channel=new BroadcastChannel(config.channel_name);channel.addEventListener("message",event=>handleResult(event.data));}}
</script></html>"""
    return _html_response(body, nonce=nonce)


def _oauth_callback_page(platform: str, state: str) -> Response:
    nonce = secrets.token_urlsafe(18)
    result = _json(
        {"type": "xiaot-user-oauth-result", "success": True, "platform": platform, "state": state}
    )
    result = result.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权完成</title><h1>授权完成</h1><p>正在通知原页面并关闭授权窗口。若窗口未自动关闭，请返回飞书继续使用。</p>
<script nonce="{nonce}">
const result={result};
if(window.opener&&!window.opener.closed)window.opener.postMessage(result,window.location.origin);
if("BroadcastChannel" in window){{const channel=new BroadcastChannel("xiaot-oauth-{state}");channel.postMessage(result);setTimeout(()=>{{channel.close();window.close();}},150);}}
else{{window.close();}}
</script></html>"""
    return _html_response(body, nonce=nonce)

XIAOT_AGENT_SCOPE = "xiaot-bitable-agent-dev"
XIAOT_TOOL_NAMES = {
    "list_tables",
    "discover_tables",
    "get_table_fields",
    "search_records",
    "get_record",
    "prepare_mutation",
    "confirm_mutation",
}
XIAOT_READ_TOOL_NAMES = {
    "list_tables",
    "discover_tables",
    "get_table_fields",
    "search_records",
    "get_record",
}
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
    """Expose 小 T secrets through the existing relay's Feishu env names."""

    _aliases = {
        "FEISHU_APP_ID": "XIAOT_FEISHU_APP_ID",
        "FEISHU_APP_SECRET": "XIAOT_FEISHU_APP_SECRET",
        "FEISHU_VERIFY_TOKEN": "XIAOT_FEISHU_VERIFY_TOKEN",
        "FEISHU_BOT_OPEN_ID": "XIAOT_FEISHU_BOT_OPEN_ID",
        "FEISHU_OAUTH_REDIRECT_URI": "XIAOT_FEISHU_OAUTH_REDIRECT_URI",
        "FEISHU_OAUTH_SCOPE": "XIAOT_FEISHU_OAUTH_SCOPE",
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
    """Expose only 小T's selected DEV OAuth app to platform API clients."""

    _aliases = {
        "FEISHU_APP_ID": "XIAOT_FEISHU_APP_ID",
        "FEISHU_APP_SECRET": "XIAOT_FEISHU_APP_SECRET",
        "FEISHU_OAUTH_REDIRECT_URI": "XIAOT_FEISHU_OAUTH_REDIRECT_URI",
        "FEISHU_OAUTH_SCOPE": "XIAOT_FEISHU_OAUTH_SCOPE",
        "LARK_OAUTH_REDIRECT_URI": "XIAOT_LARK_OAUTH_REDIRECT_URI",
        "LARK_OAUTH_SCOPE": "XIAOT_LARK_OAUTH_SCOPE",
        "WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL": "XIAOT_PUBLIC_BASE_URL",
    }
    _lark_app_aliases = {
        "lark_yw": ("XIAOT_LARK_YW_APP_ID", "XIAOT_LARK_YW_APP_SECRET"),
        "lark_w_fam": ("XIAOT_LARK_W_FAM_APP_ID", "XIAOT_LARK_W_FAM_APP_SECRET"),
    }

    def __init__(self, raw: Any, lark_app_key: str = "lark_yw") -> None:
        self.raw = raw
        self.lark_app_key = str(lark_app_key or "lark_yw").strip().lower()

    def __getattr__(self, name: str) -> Any:
        if name in {"LARK_APP_ID", "LARK_APP_SECRET"}:
            aliases = self._lark_app_aliases.get(self.lark_app_key)
            if aliases:
                index = 0 if name == "LARK_APP_ID" else 1
                value = getattr(self.raw, aliases[index], None)
                if value not in (None, ""):
                    return value
            if self.lark_app_key == "lark_yw":
                legacy = "XIAOT_LARK_APP_ID" if name == "LARK_APP_ID" else "XIAOT_LARK_APP_SECRET"
                value = getattr(self.raw, legacy, None)
                if value not in (None, ""):
                    return value
            return None
        alias = self._aliases.get(name)
        if alias:
            return getattr(self.raw, alias, None)
        return getattr(self.raw, name, None)


class XiaotBitableClient:
    """Bitable API client constrained to the user-approved base and tables."""

    base_token = "CNd6bKd5ZaRWkssv5rUccCmBn0e"

    def __init__(self, env: Any) -> None:
        self.env = env
        self.additional_tables: dict[str, dict[str, str]] = {}

    @staticmethod
    def validate_additional_tables(value: Any) -> dict[str, dict[str, str]]:
        """Validate the XiaoT-only, versioned extension without overriding built-ins."""
        if not isinstance(value, dict):
            raise ValueError("bitable.additional_tables must be an object keyed by table_key")
        if len(value) > 30:
            raise ValueError("bitable.additional_tables supports at most 30 extra tables")
        built_in_ids = {table["table_id"] for table in XIAOT_TABLES.values()}
        built_in_aliases = {"subtask", "space", "aitext"}
        result: dict[str, dict[str, str]] = {}
        for raw_key, raw_table in value.items():
            key = str(raw_key or "").strip().lower().replace("-", "_").replace(" ", "_")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ValueError(f"invalid additional table key: {raw_key}")
            if key in XIAOT_TABLES or key in built_in_aliases or key in result:
                raise ValueError(f"additional table key cannot replace a built-in or duplicate: {key}")
            if not isinstance(raw_table, dict):
                raise ValueError(f"additional table {key} must be an object")
            table_id = str(raw_table.get("table_id") or "").strip()
            if not re.fullmatch(r"tbl[A-Za-z0-9_-]{4,80}", table_id):
                raise ValueError(f"additional table {key} requires a valid tbl table_id")
            if table_id in built_in_ids or any(
                table_id == item["table_id"] for item in result.values()
            ):
                raise ValueError(f"additional table {key} cannot duplicate another table_id")
            name = str(raw_table.get("name") or "").strip()
            description = str(raw_table.get("description") or "").strip()
            if not name or len(name) > 120:
                raise ValueError(f"additional table {key} requires a 1-120 character name")
            if len(description) > 300:
                raise ValueError(f"additional table {key} description must be at most 300 characters")
            result[key] = {
                "table_id": table_id,
                "name": name,
                "description": description,
            }
        return result

    @staticmethod
    def resolve_table(
        table_key: str,
        additional_tables: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, str]:
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
        tables = {**XIAOT_TABLES, **(additional_tables or {})}
        if normalized not in tables:
            raise ValueError(
                "table_key must be one of the seven built-in tables or an active, confirmed "
                "bitable.additional_tables entry"
            )
        return tables[normalized]

    def resolve_agent_table(self, table_key: str) -> dict[str, str]:
        return self.resolve_table(table_key, self.additional_tables)

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
            raise RuntimeError(f"{normalized.title()} Bitable API failed ({response.status_code}): {message}")
        return payload

    def _table_path(self, table_key: str) -> str:
        table = self.resolve_agent_table(table_key)
        return (
            f"/open-apis/bitable/v1/apps/{XiaotBitableClient.base_token}/tables/{table['table_id']}"
        )

    async def list_tables(self) -> list[dict[str, str]]:
        # Expose the seven built-ins plus only explicitly confirmed DEV extensions.
        tables = {**XIAOT_TABLES, **self.additional_tables}
        return [
            {
                "table_key": key,
                "table_id": value["table_id"],
                "name": value["name"],
                "description": value["description"],
            }
            for key, value in tables.items()
        ]

    async def discover_tables(
        self, *, access_token: str, platform: str = "feishu"
    ) -> list[dict[str, str]]:
        """List table metadata in the fixed base using the current user's OAuth token."""
        discovered: list[dict[str, str]] = []
        page_token = ""
        for _ in range(20):
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = await self.request(
                "GET",
                f"/open-apis/bitable/v1/apps/{self.base_token}/tables",
                access_token=access_token,
                platform=platform,
                params=params,
            )
            data = payload.get("data") or {}
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                table_id = str(item.get("table_id") or "").strip()
                name = str(item.get("name") or item.get("table_name") or "").strip()
                if not table_id or not name:
                    continue
                discovered.append(
                    {
                        "table_id": table_id,
                        "name": name,
                        "suggested_table_key": re.sub(
                            r"[^a-z0-9_]+", "_", name.lower()
                        ).strip("_")[:64],
                    }
                )
            if not data.get("has_more"):
                return discovered
            page_token = str(data.get("page_token") or "").strip()
            if not page_token:
                raise RuntimeError("Bitable table discovery returned has_more without page_token")
        raise RuntimeError("Bitable table discovery reached the safe pagination limit")

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
        self, table_key: str, record_id: str, fields: dict[str, Any], *, access_token: str, platform: str = "feishu"
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
        self, table_key: str, fields: dict[str, Any], *, access_token: str, platform: str = "feishu"
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
        event = await _load_explicit_quoted_context(
            self.relay.api_for_conversation(conversation_key), event
        )
        if continuation:
            event = await _inherit_replied_run_images(
                self.relay.state, event, conversation_key
            )
        turn_mode = "continuation" if continuation else "initial"
        managed_rule_context = ""
        try:
            from agent_config_mcp import (
                active_rules_for_agent,
                active_shared_confirmation_policy,
                managed_rules_prompt,
            )

            managed_rules = await active_rules_for_agent(self.relay.state.db, "xiaot")
            confirmation_policy = await active_shared_confirmation_policy(self.relay.state.db)
            managed_rule_context = managed_rules_prompt(
                "xiaot", managed_rules, confirmation_policy
            )
        except Exception as exc:
            managed_rule_context = (
                "DEV managed runtime rule store or shared confirmation policy status: unavailable. "
                "For any request to inspect, change, confirm, or roll back a managed DEV runtime "
                "rule or the shared confirmation/refusal policy, do not infer current stored state "
                "or success from chat history or source defaults, do not claim the rule was checked "
                "or changed, and do not execute the mutation. Explain that the DEV rule service "
                "could not be loaded and no change was made. Ordinary read-only tasks may continue; "
                "confirmation-required actions must also stop until the shared policy is available."
            )
            print(f"DEV managed Agent rules could not be loaded for XiaoT: {_safe_error(exc)}")
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
                    "Use only 小 T Bitable tools, the seven built-in tables, and exact additional "
                    "tables listed by the active confirmed DEV runtime rule "
                    "bitable.additional_tables. A managed override is authoritative for its own "
                    "rule; do not claim a table was added until the config MCP confirms it and "
                    "list_tables shows it. For a request to add a table, discover its exact ID "
                    "inside the configured base with discover_tables using the current user's "
                    "authorization, then propose the complete mapping through "
                    "workspace-agent-relay-general-functions-dev and wait for shared confirmation. "
                    "Keep the existing seven entries and base unchanged. Read field schemas and "
                    "target records before proposing changes. Never invent field names, record IDs, "
                    "relation IDs, dates, or people."
                ),
                (
                    "Reads are read-only. Stage each create/update/delete with prepare_mutation. "
                    "Show the table, target, old/new values, and fields through ask_user with "
                    "confirmation_required=true. Call "
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
                    "When a user replies with a clear confirmation or uses a server-validated "
                    "configured confirmation reaction on the exact pending proposal, call "
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
                    "record_result once to reply to the original Feishu message. For a confirmed "
                    "write that succeeds or explicitly fails, prefix the record_result title with "
                    "[confirmed-action-result-card] so the shared relay sends a result card, "
                    "mentions the requester, and quotes the current confirmation message. "
                    "For a configured confirmation reaction, the relay enforces this card format."
                ),
                (
                    f"The relay MCP is {self.relay.mcp_name()} at "
                    f"{self.relay.base_url()}/mcp. Use record_result to reply to Feishu."
                ),
                managed_rule_context,
                "",
                "User task:",
                _event_task_text(event, continuation=continuation),
            ]
        )
        if event.get("image_refs"):
            prompt += (
                "\n\nThis request includes one or more user images, either attached now, explicitly "
                "quoted, or preserved from the exact Agent run whose reply is being continued. "
                "Call workspace-agent-relay-mcp.get_user_images with the current request_id "
                "and conversation_key before trying to inspect or use them. Do not claim to "
                "have seen an image unless that tool returns an image attachment."
            )
        await self.relay.state.create_run(
            request_id=request_id,
            conversation_key=conversation_key,
            source_message_id=str(event["message_id"]),
            input_markdown=prompt,
            image_keys=event.get("image_keys") or [],
            image_refs=event.get("image_refs") or [],
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
        self.raw_xiaot_env = env.raw if isinstance(env, XiaotEnvironment) else env
        self.identity_relay = self._identity_relay_for_app("lark_yw")
        self.xiaot_bitable = XiaotBitableClient(
            self.raw_xiaot_env
        )
        self.agent_workflow = XiaotAgentRelayWorkflow(self)

    async def _load_additional_bitable_tables(self) -> dict[str, dict[str, str]]:
        """Load only XiaoT's active versioned DEV table extension."""
        try:
            from agent_config_mcp import active_rules_for_agent

            rules = await active_rules_for_agent(self.state.db, "xiaot")
            value = next(
                (
                    item.get("value")
                    for item in rules
                    if item.get("key") == "bitable.additional_tables"
                ),
                {},
            )
            return self.xiaot_bitable.validate_additional_tables(value)
        except Exception as exc:
            # The built-in table behavior remains available if the managed
            # rule store cannot be read. An unavailable extension fails closed.
            print(f"小T DEV managed Bitable table extension unavailable: {_safe_error(exc)}")
            return {}

    def _resolve_bitable_table(self, table_key: str) -> dict[str, str]:
        resolver = getattr(self.xiaot_bitable, "resolve_agent_table", None)
        if callable(resolver):
            return resolver(table_key)
        return self.xiaot_bitable.resolve_table(table_key)

    def _identity_relay_for_app(self, app_key: str) -> CloudflareRelay:
        normalized = str(app_key or "").strip().lower()
        if normalized not in {"feishu", "lark_yw", "lark_w_fam"}:
            raise RuntimeError("小T 授权应用标识无效")
        return CloudflareRelay(
            XiaotLarkOAuthEnvironment(self.raw_xiaot_env, normalized),
            self.ctx,
            self.state,
        )

    async def _saved_requester_identity_for_reaction(
        self,
        request_id: str,
        conversation_key: str,
        actor_open_id: str,
    ) -> dict[str, str] | None:
        """Reuse only a previously verified XiaoT identity for this exact actor/run."""
        request = str(request_id or "").strip()
        conversation = str(conversation_key or "").strip()
        actor = str(actor_open_id or "").strip()
        if (
            not request.startswith("xiaot_")
            or not conversation.startswith("xiaot:")
            or not actor
        ):
            return None
        try:
            row = await _db_first(
                self.state.db,
                "SELECT e.sender_open_id AS source_open_id, q.platform, q.app_key, "
                "q.open_id, q.tenant_key FROM feishu_events e "
                "JOIN xiaot_run_requesters q ON q.request_id = e.request_id "
                "AND q.conversation_key = e.conversation_key "
                "WHERE e.request_id = ? AND e.conversation_key = ? LIMIT 1",
                request,
                conversation,
            )
        except Exception:
            # Older DEV databases may not have a requester mapping for a run.
            return None
        if not row or str(row.get("source_open_id") or "").strip() != actor:
            return None
        platform = str(row.get("platform") or "").strip().lower()
        app_key = str(row.get("app_key") or "").strip().lower()
        open_id = str(row.get("open_id") or "").strip()
        if (
            platform not in {"feishu", "lark"}
            or app_key not in {"feishu", "lark_yw", "lark_w_fam"}
            or not open_id
            or open_id.startswith("pending_source:")
        ):
            return None
        return {
            "platform": platform,
            "app_key": app_key,
            "open_id": open_id,
            "tenant_key": str(row.get("tenant_key") or ""),
        }

    @staticmethod
    def _oauth_app_key(platform: str, configured_key: Any) -> str:
        app_key = str(configured_key or "").strip().lower()
        if platform == "feishu":
            return "feishu"
        if platform == "lark":
            return app_key if app_key in {"lark_yw", "lark_w_fam"} else "lark_yw"
        raise RuntimeError("小T 授权平台标识无效")

    def _tenant_app_routes(self) -> dict[str, str]:
        raw_routes = str(_env(self.raw_xiaot_env, "XIAOT_TENANT_APP_MAP", "") or "").strip()
        routes: dict[str, str] = {}
        if raw_routes:
            try:
                parsed = json.loads(raw_routes)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("XIAOT_TENANT_APP_MAP is not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("XIAOT_TENANT_APP_MAP must be a JSON object")
            for tenant_key, app_key in parsed.items():
                tenant = str(tenant_key or "").strip()
                app = str(app_key or "").strip().lower()
                if not tenant or app not in {"feishu", "lark_yw", "lark_w_fam"}:
                    raise RuntimeError("XIAOT_TENANT_APP_MAP contains an invalid tenant/app route")
                routes[tenant] = app
        for tenant in str(_env(self.raw_xiaot_env, "LARK_EXTERNAL_TENANT_KEYS", "") or "").split(","):
            if tenant.strip():
                routes.setdefault(tenant.strip(), "lark_yw")
        return routes

    async def _resolve_oauth_route(self, event: dict[str, Any]) -> tuple[str, str, str]:
        sender_tenant = str(event.get("sender_tenant_key") or "").strip()
        event_tenant = str(event.get("tenant_key") or "").strip()
        tenant_key = sender_tenant or event_tenant
        routes = self._tenant_app_routes()
        if tenant_key and tenant_key in routes:
            app_key = routes[tenant_key]
            return ("feishu" if app_key == "feishu" else "lark", app_key, tenant_key)
        if routes and sender_tenant and sender_tenant != event_tenant:
            raise RuntimeError(
                "无法确认当前发起人的 Lark 组织：该 tenant_key 未配置对应授权应用；"
                "未发起授权，也未访问或修改多维表格。"
            )
        explicit = str(
            event.get("tenant_brand") or event.get("platform") or event.get("brand") or ""
        ).strip().lower()
        if explicit in {"feishu", "lark"}:
            return explicit, "feishu" if explicit == "feishu" else "lark_yw", tenant_key
        if sender_tenant and event_tenant:
            if sender_tenant != event_tenant:
                return "lark", "lark_yw", sender_tenant
            return "feishu", "feishu", sender_tenant
        platform = await self._detect_requester_platform(event)
        return platform, "feishu" if platform == "feishu" else "lark_yw", tenant_key

    async def _detect_requester_platform(self, event: dict[str, Any]) -> str:
        explicit = str(
            event.get("tenant_brand") or event.get("platform") or event.get("brand") or ""
        ).strip().lower()
        if explicit in {"feishu", "lark"}:
            return explicit
        sender_tenant = str(event.get("sender_tenant_key") or "").strip()
        event_tenant = str(event.get("tenant_key") or "").strip()
        configured = {
            item.strip()
            for item in str(_env(self.raw_xiaot_env, "LARK_EXTERNAL_TENANT_KEYS", "") or "").split(",")
            if item.strip()
        }
        if sender_tenant and sender_tenant in configured:
            return "lark"
        if sender_tenant and event_tenant and sender_tenant != event_tenant:
            return "lark"
        identifiers = (
            (str(event.get("open_id") or "").strip(), "open_id"),
            (str(event.get("union_id") or "").strip(), "union_id"),
            (str(event.get("user_id") or "").strip(), "user_id"),
        )
        resolved: list[str] = []
        for candidate in ("feishu", "lark"):
            api = getattr(self.identity_relay, candidate, None)
            if api is None:
                continue
            for identifier, identifier_type in identifiers:
                if not identifier:
                    continue
                try:
                    if await api.resolve_user_id(identifier, user_id_type=identifier_type):
                        resolved.append(candidate)
                        break
                except Exception:
                    continue
        if len(resolved) == 1:
            return resolved[0]
        reason = "Feishu 和 Lark 都能解析该发起人，结果不唯一" if resolved else (
            "事件租户信息不足，Feishu/Lark 用户目录也无法解析该发起人"
        )
        raise RuntimeError(f"无法判断当前发起人的账号类型：{reason}；未发起授权，也未访问或修改多维表格。")

    async def _resolve_account_identity(self, event: dict[str, Any]) -> dict[str, str]:
        source_open_id = str(event.get("open_id") or "").strip()
        if not source_open_id:
            raise ValueError("小T event does not contain a requester open_id")
        platform, app_key, tenant_key = await self._resolve_oauth_route(event)
        identity_relay = self._identity_relay_for_app(app_key)
        api = identity_relay.lark if platform == "lark" else identity_relay.feishu
        if api is None:
            raise RuntimeError(f"{platform.title()} authorization is not configured")
        source_union_id = str(event.get("union_id") or "").strip()
        linked = await _db_first(
            self.state.db,
            "SELECT account_open_id FROM xiaot_bitable_source_identity_links_by_app "
            "WHERE app_key = ? AND source_open_id = ?",
            app_key,
            source_open_id,
        )
        if not linked and app_key in {"feishu", "lark_yw"}:
            linked = await _db_first(
                self.state.db,
                "SELECT account_open_id FROM xiaot_bitable_source_identity_links "
                "WHERE platform = ? AND source_open_id = ?",
                platform,
                source_open_id,
            )
        linked_id = str((linked or {}).get("account_open_id") or "").strip()
        if linked_id:
            return {"platform": platform, "app_key": app_key, "tenant_key": tenant_key, "open_id": linked_id}
        if platform == "lark" and source_union_id:
            linked = await _db_first(
                self.state.db,
                "SELECT account_open_id FROM xiaot_bitable_identity_links_by_app "
                "WHERE app_key = ? AND source_union_id = ?",
                app_key,
                source_union_id,
            )
            linked_id = str((linked or {}).get("account_open_id") or "").strip()
            if linked_id:
                return {"platform": platform, "app_key": app_key, "tenant_key": tenant_key, "open_id": linked_id}
        for identifier, identifier_type in (
            (source_union_id, "union_id"),
            (str(event.get("user_id") or "").strip(), "user_id"),
            (source_open_id, "open_id"),
        ):
            if not identifier:
                continue
            try:
                resolved = await api.resolve_user_id(identifier, user_id_type=identifier_type)
            except Exception:
                continue
            open_id = str(resolved.get("open_id") or "").strip()
            if open_id:
                return {"platform": platform, "app_key": app_key, "tenant_key": tenant_key, "open_id": open_id}
        if platform == "lark":
            return {
                "platform": platform,
                "app_key": app_key,
                "tenant_key": tenant_key,
                "open_id": f"pending_source:{source_open_id}",
                "identity_pending": "true",
                "source_open_id": source_open_id,
                "source_union_id": source_union_id,
                "source_user_id": str(event.get("user_id") or "").strip(),
                "source_platform": "feishu",
            }
        raise RuntimeError(
            "无法安全确认当前发起人的账号；事件未提供可用于本人 OAuth 校验的稳定用户标识，"
            "未创建 Agent 任务，也未使用其他用户权限。"
        )

    async def _xiaot_user_token(self, *, platform: str, app_key: str, open_id: str) -> dict[str, Any] | None:
        await self._ensure_xiaot_oauth_schema()
        row = await _db_first(
            self.state.db,
            "SELECT platform, app_key, open_id, access_token, refresh_token, expires_at, updated_at "
            "FROM xiaot_bitable_user_tokens WHERE app_key = ? AND open_id = ?",
            app_key,
            open_id,
        )
        if row:
            return row
        if app_key in {"feishu", "lark_yw"}:
            legacy = await self.state.user_token(platform, open_id)
            if not legacy and platform == "feishu":
                legacy = await self.state.user_token("xiaot", open_id)
            return legacy
        return None

    async def _save_xiaot_user_token(
        self, *, platform: str, app_key: str, open_id: str, access_token: str,
        refresh_token: str, expires_at: int,
    ) -> None:
        await self._ensure_xiaot_oauth_schema()
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_bitable_user_tokens "
            "(app_key, platform, open_id, access_token, refresh_token, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(app_key, open_id) DO UPDATE SET "
            "platform=excluded.platform, access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            app_key, platform, open_id, access_token, refresh_token, expires_at, int(time.time()),
        )
        if app_key in {"feishu", "lark_yw"}:
            await self.state.save_user_token(
                platform=platform, open_id=open_id, access_token=access_token,
                refresh_token=refresh_token, expires_at=expires_at,
            )

    def base_url(self) -> str:
        return super().base_url()

    def mcp_name(self) -> str:
        return super().mcp_name()

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
                source_union_id TEXT NOT NULL DEFAULT '',
                source_open_id TEXT NOT NULL DEFAULT '',
                source_user_id TEXT NOT NULL DEFAULT '',
                source_platform TEXT NOT NULL DEFAULT 'feishu',
                app_key TEXT NOT NULL DEFAULT 'feishu',
                tenant_key TEXT NOT NULL DEFAULT '',
                request_id TEXT NOT NULL DEFAULT '',
                conversation_key TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                event_json TEXT NOT NULL DEFAULT '{}',
                authorization_message_id TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                created_at INTEGER NOT NULL
            )""",
        )
        for column, definition in (
            ("platform", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("account_open_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_union_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_open_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_user_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_platform", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("app_key", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("tenant_key", "TEXT NOT NULL DEFAULT ''"),
            ("request_id", "TEXT NOT NULL DEFAULT ''"),
            ("event_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("authorization_message_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            with suppress(Exception):
                await _db_run(
                    self.state.db,
                    f"ALTER TABLE xiaot_bitable_oauth_states ADD COLUMN {column} {definition}",
                )
        for statement in (
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_identity_links (
                platform TEXT NOT NULL, source_union_id TEXT NOT NULL, account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, PRIMARY KEY (platform, source_union_id))""",
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_source_identity_links (
                platform TEXT NOT NULL, source_open_id TEXT NOT NULL, account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, PRIMARY KEY (platform, source_open_id))""",
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_identity_links_by_app (
                app_key TEXT NOT NULL, source_union_id TEXT NOT NULL, account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, PRIMARY KEY (app_key, source_union_id))""",
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_source_identity_links_by_app (
                app_key TEXT NOT NULL, source_open_id TEXT NOT NULL, account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, PRIMARY KEY (app_key, source_open_id))""",
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_user_tokens (
                app_key TEXT NOT NULL, platform TEXT NOT NULL, open_id TEXT NOT NULL,
                access_token TEXT NOT NULL, refresh_token TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (app_key, open_id))""",
            """CREATE TABLE IF NOT EXISTS xiaot_run_requesters (
                request_id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL, platform TEXT NOT NULL,
                open_id TEXT NOT NULL, app_key TEXT NOT NULL DEFAULT 'feishu',
                tenant_key TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)""",
        ):
            await _db_run(self.state.db, statement)
        with suppress(Exception):
            await _db_run(
                self.state.db,
                "ALTER TABLE xiaot_run_requesters ADD COLUMN app_key TEXT NOT NULL DEFAULT 'feishu'",
            )
        with suppress(Exception):
            await _db_run(
                self.state.db,
                "ALTER TABLE xiaot_run_requesters ADD COLUMN tenant_key TEXT NOT NULL DEFAULT ''",
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
        platform = str(account_identity.get("platform") or "feishu").strip().lower()
        app_key = self._oauth_app_key(platform, account_identity.get("app_key"))
        identity_relay = self._identity_relay_for_app(app_key)
        open_id = str(account_identity.get("open_id") or "").strip()
        account_open_id = "" if account_identity.get("identity_pending") == "true" else open_id
        callback_key = f"{platform.upper()}_OAUTH_REDIRECT_URI"
        redirect_uri = str(
            _env(identity_relay.env, callback_key, identity_relay.base_url() + f"/{platform}/oauth/callback")
            or ""
        ).strip()
        if not open_id or not redirect_uri:
            raise RuntimeError(f"小T 的 {platform.title()} 用户授权回调地址尚未配置")
        state = f"xiaot_{platform}_" + secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 600
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_bitable_oauth_states "
            "(state, open_id, platform, account_open_id, source_union_id, source_open_id, "
            "source_user_id, source_platform, app_key, tenant_key, request_id, conversation_key, "
            "source_message_id, redirect_uri, event_json, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state,
            open_id,
            platform,
            account_open_id,
            str(event.get("union_id") or ""),
            str(event.get("open_id") or ""),
            str(event.get("user_id") or ""),
            str(account_identity.get("source_platform") or "feishu"),
            app_key,
            str(account_identity.get("tenant_key") or ""),
            request_id,
            conversation_key,
            str(event.get("message_id") or ""),
            redirect_uri,
            _json(event),
            expires_at,
            int(time.time()),
        )
        launch_url = identity_relay.base_url() + "/xiaot/user-oauth/start?" + urlencode(
            {"platform": platform, "state": state}
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
                            f"请使用你的 {platform.title()} 账号授权；点击后打开授权窗口，"
                            "授权成功后窗口会自动关闭，原请求将继续处理。"
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
                            "url": launch_url,
                        }
                    ],
                },
            ],
        }
        chat_id = str(event.get("chat_id") or "").strip()
        source_open_id = str(event.get("open_id") or "").strip()
        if not chat_id or not source_open_id:
            raise RuntimeError("无法安全发送仅本人可见的授权卡片：缺少群组或发起人标识")
        api = self.api_for_conversation(conversation_key)
        outbound = await api.send_ephemeral_card(
            chat_id=chat_id,
            open_id=source_open_id,
            card=card,
        )
        await _db_run(
            self.state.db,
            "UPDATE xiaot_bitable_oauth_states SET authorization_message_id = ? WHERE state = ?",
            str(outbound),
            state,
        )
        await self.state.save_reply(
            str(outbound),
            conversation_key,
            message_text="授权请求：点击“授权并继续”完成个人 Feishu/Lark 账号授权。",
        )

    async def _replace_authorization_card_with_success(self, pending: dict[str, Any]) -> None:
        try:
            event = json.loads(str(pending.get("event_json") or "{}"))
            if not isinstance(event, dict):
                raise RuntimeError("授权状态缺少原始事件")
            chat_id = str(event.get("chat_id") or "").strip()
            open_id = str(event.get("open_id") or "").strip()
            conversation_key = str(pending.get("conversation_key") or "").strip()
            if not chat_id or not open_id or not conversation_key:
                raise RuntimeError("授权状态缺少私有卡片接收人信息")
            api = self.api_for_conversation(conversation_key)
            await api.send_ephemeral_card(
                chat_id=chat_id,
                open_id=open_id,
                card={
                    "config": {"wide_screen_mode": True},
                    "elements": [
                        {"tag": "div", "text": {"tag": "plain_text", "content": "授权成功！"}}
                    ],
                },
            )
            old_message_id = str(pending.get("authorization_message_id") or "").strip()
            if old_message_id:
                await api._request("POST", "/open-apis/ephemeral/v1/delete", json={"message_id": old_message_id})
        except Exception as exc:
            print(f"小T授权成功卡片更新失败: {_safe_error(exc)}")

    async def handle_user_oauth_start(self, request: Any) -> Response:
        if str(request.method or "").upper() != "GET":
            return _response({"error": "method_not_allowed"}, 405, {"allow": "GET"})
        params = parse_qs(urlparse(request.url).query)
        platform = str((params.get("platform") or [""])[0]).strip().lower()
        state = str((params.get("state") or [""])[0]).strip()
        if platform not in {"feishu", "lark"} or not state.startswith(f"xiaot_{platform}_"):
            return _response({"success": False, "error": "invalid_authorization_request"}, 400)
        await self._ensure_xiaot_oauth_schema()
        pending = await _db_first(
            self.state.db,
            "SELECT * FROM xiaot_bitable_oauth_states WHERE state = ? AND platform = ? AND consumed_at IS NULL",
            state,
            platform,
        )
        if not pending or int(pending.get("expires_at") or 0) < int(time.time()):
            return _response({"success": False, "error": "invalid_or_expired_state"}, 400)
        app_key = self._oauth_app_key(platform, pending.get("app_key"))
        identity_relay = self._identity_relay_for_app(app_key)
        auth_base = "https://accounts.larksuite.com" if platform == "lark" else FEISHU_AUTH_BASE_URL
        auth_url = f"{auth_base}/open-apis/authen/v1/authorize?" + urlencode(
            {
                "app_id": _env(identity_relay.env, f"{platform.upper()}_APP_ID"),
                "redirect_uri": str(pending.get("redirect_uri") or ""),
                "scope": identity_relay.platform_oauth_scope(platform),
                "state": state,
            }
        )
        parsed_base = urlparse(identity_relay.base_url())
        return _oauth_launcher_page(auth_url, f"{parsed_base.scheme}://{parsed_base.netloc}", state)

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
            or not (pending.get("account_open_id") or pending.get("source_open_id") or pending.get("source_union_id") or pending.get("source_user_id"))
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
        app_key = self._oauth_app_key(normalized, pending.get("app_key"))
        identity_relay = self._identity_relay_for_app(app_key)
        auth_base = "https://accounts.larksuite.com" if normalized == "lark" else FEISHU_AUTH_BASE_URL
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{auth_base}/oauth/v3/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": _env(identity_relay.env, f"{normalized.upper()}_APP_ID"),
                        "client_secret": _env(identity_relay.env, f"{normalized.upper()}_APP_SECRET"),
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
            user_data = await identity_relay.api_for_conversation(f"{normalized}:oauth").user_info(access_token)
            identity = str(user_data.get("open_id") or "").strip()
            expected_open_id = str(pending.get("account_open_id") or "").strip()
            source_ids = {
                str(pending.get("source_open_id") or "").strip(),
                str(pending.get("source_union_id") or "").strip(),
                str(pending.get("source_user_id") or "").strip(),
            } - {""}
            authorized_ids = {
                str(user_data.get(key) or "").strip() for key in ("open_id", "union_id", "user_id")
            } - {""}
            if expected_open_id and not expected_open_id.startswith(("pending_union:", "pending_source:")):
                if identity != expected_open_id:
                    return _response({"success": False, "error": "requester_mismatch", "message": f"授权的 {normalized.title()} 账号与发起 @小T 的人员不一致，未保存授权。"}, 403)
            elif not (source_ids & authorized_ids) and str(pending.get("source_platform") or normalized) == normalized:
                return _response({"success": False, "error": "requester_mismatch", "message": f"授权的 {normalized.title()} 账号与发起 @小T 的人员不一致，未保存授权。"}, 403)
            now = int(time.time())
            source_union_id = str(pending.get("source_union_id") or "").strip()
            source_open_id = str(pending.get("source_open_id") or "").strip()
            if source_union_id and str(user_data.get("union_id") or "").strip() == source_union_id:
                await _db_run(self.state.db,
                    "INSERT INTO xiaot_bitable_identity_links_by_app (app_key, source_union_id, account_open_id, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(app_key, source_union_id) DO UPDATE SET account_open_id=excluded.account_open_id, created_at=excluded.created_at",
                    app_key, source_union_id, identity, now)
                if app_key in {"feishu", "lark_yw"}:
                    await _db_run(self.state.db,
                        "INSERT INTO xiaot_bitable_identity_links (platform, source_union_id, account_open_id, created_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(platform, source_union_id) DO UPDATE SET account_open_id=excluded.account_open_id, created_at=excluded.created_at",
                        normalized, source_union_id, identity, now)
            if source_open_id:
                await _db_run(self.state.db,
                    "INSERT INTO xiaot_bitable_source_identity_links_by_app (app_key, source_open_id, account_open_id, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(app_key, source_open_id) DO UPDATE SET account_open_id=excluded.account_open_id, created_at=excluded.created_at",
                    app_key, source_open_id, identity, now)
                if app_key in {"feishu", "lark_yw"}:
                    await _db_run(self.state.db,
                        "INSERT INTO xiaot_bitable_source_identity_links (platform, source_open_id, account_open_id, created_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(platform, source_open_id) DO UPDATE SET account_open_id=excluded.account_open_id, created_at=excluded.created_at",
                        normalized, source_open_id, identity, now)
            await self._save_xiaot_user_token(
                platform=normalized, app_key=app_key, open_id=identity, access_token=access_token,
                refresh_token=str(token_data.get("refresh_token") or ""),
                expires_at=now + int(token_data.get("expires_in") or 7200),
            )
            await self._replace_authorization_card_with_success(pending)
            # Completing OAuth (including notifying the popup) must not depend on
            # whether replaying the original Agent request succeeds. The user
            # token is already stored at this point, so return the close-window
            # page even if the queued request is malformed or its replay fails.
            try:
                event = json.loads(str(pending.get("event_json") or "{}"))
                request_id = str(pending.get("request_id") or "")
                conversation_key = str(pending.get("conversation_key") or "")
                if not request_id or not conversation_key or not isinstance(event, dict):
                    raise RuntimeError("授权状态缺少待处理的原始请求")
                if not await self.state.get_run(request_id):
                    await _db_run(self.state.db,
                        "INSERT INTO xiaot_run_requesters (request_id, conversation_key, platform, open_id, app_key, tenant_key, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET conversation_key=excluded.conversation_key, "
                        "platform=excluded.platform, open_id=excluded.open_id, app_key=excluded.app_key, tenant_key=excluded.tenant_key",
                        request_id, conversation_key, normalized, identity, app_key,
                        str(pending.get("tenant_key") or ""), now)
                    await self.agent_workflow.handle_event(
                        platform=normalized, conversation_key=conversation_key, event=event, request_id=request_id
                    )
            except Exception as exc:
                print(f"小T 授权已完成，但恢复原请求失败: {_safe_error(exc)}")
            return _oauth_callback_page(normalized, state)
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
                    "description": "列出小 T 当前允许访问的七张内置表及已确认的 DEV 扩展表。",
                    "inputSchema": {
                        "type": "object",
                        "required": ["request_id", "conversation_key"],
                        "properties": {"request_id": string, "conversation_key": string},
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "discover_tables",
                    "description": (
                        "使用当前发起人的个人飞书/Lark 授权，在小 T 固定的多维表格 Base 中读取表名和 table_id；"
                        "只用于核实用户要求纳入管理的确切表，不会改变允许表清单。"
                    ),
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
            if name in XIAOT_TOOL_NAMES:
                self.xiaot_bitable.additional_tables = await self._load_additional_bitable_tables()
            if name in XIAOT_READ_TOOL_NAMES:
                run = await self._require_xiaot_run(args)
                args = {**args, "request_id": str(run["request_id"])}
            elif name in {"prepare_mutation", "confirm_mutation"}:
                run = await self._require_xiaot_run(args)
                args = {**args, "request_id": str(run["request_id"])}
            access_token = ""
            account_platform = "feishu"
            if name in XIAOT_READ_TOOL_NAMES - {"list_tables"} or name in {
                "prepare_mutation",
                "confirm_mutation",
            }:
                identity = await self._requester_for_run(
                    str(args.get("request_id") or ""),
                    str(args.get("conversation_key") or ""),
                )
                account_platform = identity["platform"]
                access_token = await self._user_access_token_for_run(
                    str(args.get("request_id") or ""),
                    str(args.get("conversation_key") or ""),
                )
            if name == "list_tables":
                return result({"success": True, "tables": await self.xiaot_bitable.list_tables()})
            if name == "discover_tables":
                return result(
                    {
                        "success": True,
                        "base_token": self.xiaot_bitable.base_token,
                        "tables": await self.xiaot_bitable.discover_tables(
                            access_token=access_token,
                            platform=account_platform,
                        ),
                        "note": "Metadata only; discovering a table does not authorize it for normal tools.",
                    }
                )
            if name == "get_table_fields":
                table = self._resolve_bitable_table(str(args.get("table_key") or ""))
                return result(
                    {
                        "success": True,
                        "table": table,
                        "fields": await self.xiaot_bitable.fields(
                            str(args["table_key"]), access_token=access_token, platform=account_platform
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
                        table_key, access_token=access_token, platform=account_platform
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
                    page_size=int(args.get("page_size") or 100),
                    page_token=str(args.get("page_token") or ""),
                    filter_formula=filter_formula,
                    platform=account_platform,
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
                    platform=account_platform,
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
        if any(previous_sender.get(key) != current_sender.get(key) for key in ("platform", "app_key", "open_id")):
            raise ValueError(
                "stale request_id belongs to a different platform account; refusing to cross user contexts"
            )
        return current

    async def _user_access_token_for_run(
        self, request_id: str, conversation_key: str
    ) -> str:
        identity = await self._requester_for_run(request_id, conversation_key)
        cached = await self._xiaot_user_token(
            platform=identity["platform"],
            app_key=identity["app_key"],
            open_id=identity["open_id"],
        )
        if not cached:
            raise RuntimeError(
                f"当前 @小T 的 {identity['platform'].title()} 用户尚未完成个人多维表格授权；请先授权后重新发送请求"
            )
        if int(cached.get("expires_at") or 0) <= int(time.time()) + 30:
            raise RuntimeError(
                f"当前 @小T 的 {identity['platform'].title()} 个人授权已过期；请重新授权后再继续，未使用机器人应用权限代替"
            )
        return str(cached.get("access_token") or "")

    async def _ensure_proposal_schema(self) -> None:
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

    async def _prepare_mutation(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        await self._require_xiaot_run(args)
        identity = await self._requester_for_run(request_id, conversation_key)
        platform = identity["platform"]
        from agent_config_mcp import active_shared_confirmation_policy

        confirmation_policy = await active_shared_confirmation_policy(self.state.db)
        access_token = await self._user_access_token_for_run(request_id, conversation_key)
        operation = str(args.get("operation") or "").strip().lower()
        if operation not in {"create", "update", "delete"}:
            raise ValueError("operation must be create, update, or delete")
        table_key = str(args.get("table_key") or "").strip()
        table = self._resolve_bitable_table(table_key)
        fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
        record_id = str(args.get("record_id") or "").strip()
        before: dict[str, Any] = {}
        if operation == "create":
            await self.xiaot_bitable.validate_writable_fields(
                table_key, fields, access_token=access_token, platform=platform
            )
        else:
            if not record_id:
                raise ValueError(f"{operation} proposals require record_id")
            before = await self.xiaot_bitable.record(
                table_key, record_id, access_token=access_token, platform=platform
            )
            if operation == "update" and not fields:
                raise ValueError("update proposals require fields")
            if operation == "update":
                await self.xiaot_bitable.validate_writable_fields(
                    table_key, fields, access_token=access_token, platform=platform
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
            platform,
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
            "confirmation_phrases": confirmation_policy["confirmation_phrases"],
            "refusal_phrases": confirmation_policy["refusal_phrases"],
            "confirmation_emojis": confirmation_policy["confirmation_emojis"],
            "refusal_emojis": confirmation_policy["refusal_emojis"],
            "expires_in_seconds": 900,
        }

    @staticmethod
    def _current_user_task(run: dict[str, Any]) -> str:
        value = str(run.get("input_markdown") or "")
        return value.rsplit("User task:\n", 1)[-1].strip()

    async def _requester_for_run(
        self, request_id: str, conversation_key: str
    ) -> dict[str, str]:
        """Resolve the app-scoped OAuth identity saved for the verified XiaoT request."""
        row = await _db_first(
            self.state.db,
            "SELECT platform, open_id, app_key, tenant_key FROM xiaot_run_requesters "
            "WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        if row and row.get("open_id"):
            return {
                "platform": str(row.get("platform") or "feishu"),
                "open_id": str(row["open_id"]),
                "app_key": self._oauth_app_key(
                    str(row.get("platform") or "feishu"), row.get("app_key")
                ),
                "tenant_key": str(row.get("tenant_key") or ""),
            }
        # Requests created before platform-aware routing used the bot event ID.
        legacy = await _db_first(
            self.state.db,
            "SELECT sender_open_id FROM feishu_events WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        if not legacy or not legacy.get("sender_open_id"):
            raise ValueError("the current 小 T run has no verified requester")
        return {
            "platform": "feishu",
            "open_id": str(legacy["sender_open_id"]),
            "app_key": "feishu",
            "tenant_key": "",
        }

    async def _confirmation_decision(
        self, value: str, *, request_id: str, conversation_key: str
    ) -> tuple[str | None, dict[str, Any]]:
        from agent_config_mcp import (
            active_shared_confirmation_policy,
            shared_confirmation_decision,
        )

        policy = await active_shared_confirmation_policy(self.state.db)
        event = await _db_first(
            self.state.db,
            "SELECT reaction_emoji FROM feishu_events "
            "WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        decision = shared_confirmation_decision(
            value,
            policy,
            reaction_emoji=(event or {}).get("reaction_emoji"),
        )
        if not value.strip() or len(value.strip()) > 100:
            return None, policy
        return decision, policy

    async def _confirm_mutation(self, args: dict[str, Any]) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        run = await self._require_xiaot_run(args)
        request_id = str(run["request_id"])
        identity = await self._requester_for_run(request_id, conversation_key)
        platform = identity["platform"]
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
                platform,
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
        ) != identity.get("open_id") or proposal.get("requester_platform", "feishu") != platform:
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
        decision, confirmation_policy = await self._confirmation_decision(
            user_task, request_id=request_id, conversation_key=conversation_key
        )
        if decision == "refusal":
            cancelled = await _db_all(
                self.state.db,
                "UPDATE xiaot_bitable_proposals SET status = 'cancelled' "
                "WHERE proposal_id = ? AND status = 'pending' "
                "AND conversation_key = ? AND requester_platform = ? "
                "AND requester_open_id = ? RETURNING proposal_id",
                proposal_id,
                conversation_key,
                platform,
                str(identity["open_id"]),
            )
            if not cancelled:
                raise ValueError("proposal is no longer pending and could not be cancelled")
            return {
                "success": True,
                "write_performed": False,
                "cancelled": True,
                "refused": True,
                "proposal_id": proposal_id,
                "note": "A shared refusal phrase cancelled the proposal; no Bitable write was performed.",
            }
        if decision != "confirmation":
            raise ValueError(
                "the current sender reply is not an exact shared confirmation phrase; accepted phrases: "
                + _json(confirmation_policy["confirmation_phrases"])
            )
        table_key = str(proposal["table_key"])
        record_id = str(proposal.get("record_id") or "")
        before = json.loads(str(proposal.get("before_json") or "{}"))
        fields = json.loads(str(proposal.get("fields_json") or "{}"))
        if operation in {"update", "delete"}:
            latest = await self.xiaot_bitable.record(
                table_key, record_id, access_token=access_token, platform=platform
            )
            if latest != before:
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
                platform=platform,
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
                record = await self.xiaot_bitable.create_record(
                    table_key, fields, access_token=access_token, platform=platform
                )
            elif operation == "update":
                record = await self.xiaot_bitable.update_record(
                    table_key, record_id, fields, access_token=access_token, platform=platform
                )
            else:
                record = await self.xiaot_bitable.delete_record(
                    table_key, record_id, access_token=access_token, platform=platform
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
                    platform=platform,
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
                platform=platform,
            )
        except Exception:
            audit_warning = "目标操作已完成，但日志结果更新失败；请核查 changelog 中的执行中记录。"
        return {
            "success": True,
            "verified": operation != "delete",
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
        platform: str,
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
            platform=platform,
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
        if str(header.get("event_type") or "") not in {
            "im.message.receive_v1",
            "im.message.reaction.created_v1",
        }:
            return _response({"code": 0})
        if not _env(self.env, "FEISHU_BOT_OPEN_ID"):
            return _response({"code": 0})
        await self._schedule_feishu_event(body, "xiaot")
        return _response({"code": 0})

    async def _process_feishu_event(self, body: dict[str, Any], platform: str = "xiaot") -> None:
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        is_reaction = str(header.get("event_type") or "") == "im.message.reaction.created_v1"
        if is_reaction:
            body = await self._prepare_reaction_message(
                body, "xiaot", xiaot_only=True
            )
            if not body:
                return
        reaction_metadata = (
            body.get("_relay_reaction")
            if isinstance(body.get("_relay_reaction"), dict)
            else None
        )
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
        event_key = (
            str(reaction_metadata.get("event_key") or "")
            if reaction_metadata
            else f"xiaot:{event['message_id']}"
        )
        if not await self.state.claim_event(
            message_id=event_key,
            request_id=request_id,
            conversation_key=conversation_key,
            chat_id=event["chat_id"],
            open_id=event["open_id"],
            reaction_emoji=(
                str(reaction_metadata.get("emoji_type") or "")
                if reaction_metadata
                else ""
            ),
        ):
            return
        handed_to_agent = False
        try:
            await self.state.save_requester(
                conversation_key,
                event["open_id"],
                event.get("name") or "",
                event["chat_id"],
                "xiaot",
            )
            if not reaction_metadata:
                # A quoted reply may target the user's original message, not
                # a bot reply. Bind that inbound message to this Agent
                # conversation before any authorization or workflow branch
                # can return, so quoting it later resumes the same session.
                await self.state.bind_message_conversation(
                    str(event["message_id"]), conversation_key
                )
            if reaction_metadata:
                await self._deliver_reaction_echo(reaction_metadata, conversation_key)
                if bool(reaction_metadata.get("confirmation_pending")):
                    await self.state.close_reply_confirmation(
                        str(reaction_metadata.get("target_message_id") or "")
                    )
                if not bool(reaction_metadata.get("confirmation_pending")):
                    return
            elif event.get("parent_id"):
                await self.state.close_reply_confirmation(str(event["parent_id"]))
            if reaction_metadata and bool(
                reaction_metadata.get("agent_config_proposal")
            ):
                # A pending DEV rule/policy proposal is owned by the relay MCP,
                # not by a user's Bitable account. Its exact-message reaction
                # was validated above; do not block that confirmation on
                # unrelated Feishu/Lark Bitable identity or OAuth lookup.
                await self.agent_workflow.handle_event(
                    platform="feishu",
                    conversation_key=conversation_key,
                    event=event,
                    request_id=request_id,
                )
                handed_to_agent = True
                return
            await self._ensure_xiaot_oauth_schema()
            try:
                identity = None
                if reaction_metadata and bool(
                    reaction_metadata.get("confirmation_pending")
                ):
                    identity = await self._saved_requester_identity_for_reaction(
                        str(reaction_metadata.get("request_id") or ""),
                        conversation_key,
                        str(event.get("open_id") or ""),
                    )
                if identity is None:
                    identity = await self._resolve_account_identity(event)
            except Exception as exc:
                await self.api_for_conversation(conversation_key).reply(
                    str(event["message_id"]),
                    f"无法确认当前发起人的 Feishu/Lark 账号：{_safe_error(exc)}。未访问或修改多维表格。",
                )
                return
            await _db_run(
                self.state.db,
                "INSERT INTO xiaot_run_requesters "
                "(request_id, conversation_key, platform, open_id, app_key, tenant_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
                "conversation_key=excluded.conversation_key, platform=excluded.platform, "
                "open_id=excluded.open_id, app_key=excluded.app_key, tenant_key=excluded.tenant_key",
                request_id,
                conversation_key,
                identity["platform"],
                identity["open_id"],
                identity["app_key"],
                identity.get("tenant_key") or "",
                int(time.time()),
            )
            user_token = None
            if not identity.get("identity_pending"):
                user_token = await self._xiaot_user_token(
                    platform=identity["platform"],
                    app_key=identity["app_key"],
                    open_id=identity["open_id"],
                )
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
            handed_to_agent = True
        except Exception:
            # If a pre-agent step fails, retaining the claim makes Feishu's
            # Queue retry look like a duplicate and silently drop the event.
            # The XiaoT Bitable authorization check above remains mandatory.
            if not handed_to_agent:
                with suppress(Exception):
                    await self.state.release_event_claim(event_key)
            raise


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
