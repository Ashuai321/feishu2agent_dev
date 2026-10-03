"""DEV-only 小 T relay and Bitable tools for the shared 小 C/小 T MCP.

小 T keeps separate Feishu credentials and Agent trigger routing, while its
tools are exposed by the existing DEV MCP endpoint and share the Worker,
Queue, D1 binding, OAuth app, and MCP connector with 小 C.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from worker_app import (
    FEISHU_AUTH_BASE_URL,
    PLACEHOLDER,
    XIAOT_MCP_NAME,
    XIAOT_MCP_PATH,
    CloudflareRelay,
    D1State,
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
    "search_person_candidates",
    "get_record",
    "prepare_mutation",
    "confirm_mutation",
}
XIAOT_READ_TOOL_NAMES = {
    "list_tables",
    "get_table_fields",
    "search_records",
    "search_person_candidates",
    "get_record",
}
XIAOT_DELETE_VERIFY_DELAYS = (0.0, 0.25, 0.5, 1.0, 2.0)
XIAOT_PERSON_NAME_ALIAS_GROUPS = (
    ("William", "李谦", "李威廉", "威廉"),
    ("元博 王", "yuanbo", "王元博", "元博"),
)
XIAOT_DATE_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
XIAOT_PERSON_SEARCH_MAX_RECORDS = 5000
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


def _normalize_bitable_date_value(value: Any) -> Any:
    """Convert date field input to a Unix millisecond timestamp."""
    def milliseconds(timestamp: float) -> int:
        if not math.isfinite(timestamp):
            raise ValueError("日期字段必须使用有效的毫秒时间戳")
        if abs(timestamp) < 100_000_000_000:
            timestamp *= 1000
        try:
            datetime.fromtimestamp(timestamp / 1000, tz=timezone(timedelta(0)))
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("日期字段时间戳超出支持范围") from exc
        return int(timestamp)

    if value is None or value == "":
        return value
    if isinstance(value, bool):
        raise ValueError("日期字段必须使用毫秒时间戳或 ISO 日期/时间")
    if isinstance(value, (int, float)):
        return milliseconds(float(value))
    if not isinstance(value, str):
        raise ValueError("日期字段必须使用毫秒时间戳或 ISO 日期/时间")

    raw = value.strip()
    if not raw:
        return value
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", raw):
        return milliseconds(float(raw))
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.fromisoformat(raw).replace(tzinfo=XIAOT_DATE_TIMEZONE)
        else:
            iso_value = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=XIAOT_DATE_TIMEZONE)
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(
            "日期字段必须是有效的毫秒时间戳或 ISO 日期/时间，例如 2026-10-01"
        ) from exc


def _normalize_person_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _person_name_search_terms(query: str) -> list[str]:
    normalized = _normalize_person_name(query)
    if not normalized:
        raise ValueError("search query must contain a person name")
    terms = {normalized}
    for group in XIAOT_PERSON_NAME_ALIAS_GROUPS:
        aliases = {_normalize_person_name(item) for item in group}
        if any(alias in normalized or normalized in alias for alias in aliases):
            terms.update(aliases)
    return sorted((term for term in terms if term), key=len, reverse=True)


def _person_name_has_exact_term(name: str, terms: list[str]) -> bool:
    """Match a full name or a standalone alias token, never a name fragment."""
    for term in terms:
        start = 0
        while (index := name.find(term, start)) >= 0:
            end = index + len(term)
            left_is_name_char = index > 0 and (name[index - 1].isalnum() or name[index - 1] == "_")
            right_is_name_char = end < len(name) and (name[end].isalnum() or name[end] == "_")
            if not left_is_name_char and not right_is_name_char:
                return True
            start = index + 1
    return False


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
    return Response(
        body,
        status=status,
        headers=headers,
    )


def _oauth_launcher_page(auth_url: str, origin: str, state: str) -> Response:
    nonce = secrets.token_urlsafe(18)
    channel_name = f"xiaot-oauth-{state}"
    config = _json(
        {
            "auth_url": auth_url,
            "origin": origin,
            "state": state,
            "channel_name": channel_name,
        }
    )
    config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小T账号授权</title>
<style>
body{{
  font:16px system-ui,sans-serif;
  max-width:36rem;
  margin:12vh auto;
  padding:0 1.25rem;
  color:#1f2937;
}}
button{{
  font:inherit;
  padding:.7rem 1.1rem;
  border:0;
  border-radius:.5rem;
  background:#2563eb;
  color:white;
  cursor:pointer;
}}
#status{{line-height:1.6;color:#475569}}
</style>
<h1>小T账号授权</h1>
<p>点击下方按钮打开安全授权窗口。授权成功后，授权窗口会自动关闭，原请求会继续处理。</p>
<button id="open-auth" type="button">打开授权窗口</button>
<p id="status" role="status">请确认浏览器允许弹出授权窗口。</p>
<script nonce="{nonce}">
const config={config};
let authPopup=null;
const status=document.getElementById("status");
document.getElementById("open-auth").addEventListener("click",()=>{{
  authPopup=window.open(config.auth_url,"xiaot_oauth_popup","popup=yes,width=600,height=760,resizable=yes,scrollbars=yes");
  if(!authPopup){{status.textContent="授权窗口被浏览器拦截了，请允许此网站弹出窗口后重试。";return;}}
  status.textContent="请在弹出的窗口中完成授权；完成后本页会自动更新。";
  authPopup.focus();
}});
window.addEventListener("message",event=>{{
  if(event.origin!==config.origin || event.source!==authPopup)return;
  handleResult(event.data);
}});
function handleResult(result){{
  if (!result || result.type!=="xiaot-user-oauth-result" || result.state!==config.state) return;
  status.textContent = result.success
    ? "授权成功，刚才的请求正在继续处理。可以返回飞书。"
    : "授权未完成，请返回飞书查看提示并重试。";
  if(result.success)window.close();
}}
if("BroadcastChannel" in window){{
  const resultChannel=new BroadcastChannel(config.channel_name);
  resultChannel.addEventListener("message",event=>handleResult(event.data));
}}
</script></html>"""
    return _html_response(body, nonce=nonce)


def _oauth_callback_page(platform: str, state: str) -> Response:
    nonce = secrets.token_urlsafe(18)
    result = _json(
        {
            "type": "xiaot-user-oauth-result",
            "success": True,
            "platform": platform,
            "state": state,
        }
    )
    result = result.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权完成</title>
<h1>授权完成</h1>
<p>正在通知原页面并关闭授权窗口。若窗口未自动关闭，请返回飞书继续使用。</p>
<script nonce="{nonce}">
const result={result};
if(window.opener&&!window.opener.closed)window.opener.postMessage(result,window.location.origin);
if("BroadcastChannel" in window){{
  const resultChannel=new BroadcastChannel("xiaot-oauth-{state}");
  resultChannel.postMessage(result);
  setTimeout(()=>{{resultChannel.close();window.close();}},150);
}}else{{window.close();}}
</script></html>"""
    return _html_response(body, nonce=nonce)


class XiaotBitableAPIError(RuntimeError):
    """Keep Bitable error details available for safe post-write verification."""

    def __init__(self, *, status_code: int, api_code: Any, message: str) -> None:
        self.status_code = int(status_code)
        try:
            self.api_code = int(api_code) if api_code is not None else None
        except (TypeError, ValueError):
            self.api_code = None
        self.message = str(message or "unknown Bitable API error")
        super().__init__(
            f"Feishu Bitable API failed ({self.status_code}): {self.message}"
        )


class XiaotEnvironment:
    """Expose only XiaoT's bot/Agent secrets through the existing relay names."""

    _aliases = {
        "DB": "XIAOT_DB",
        "AGENT_QUEUE": "XIAOT_AGENT_QUEUE",
        "FEISHU_APP_ID": "XIAOT_FEISHU_APP_ID",
        "FEISHU_APP_SECRET": "XIAOT_FEISHU_APP_SECRET",
        "FEISHU_BOT_OPEN_ID": "XIAOT_FEISHU_BOT_OPEN_ID",
        "FEISHU_OAUTH_REDIRECT_URI": "XIAOT_FEISHU_OAUTH_REDIRECT_URI",
        "LARK_OAUTH_REDIRECT_URI": "XIAOT_LARK_OAUTH_REDIRECT_URI",
        "WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL": "XIAOT_PUBLIC_BASE_URL",
        "WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN": "XIAOT_MCP_OAUTH_LOGIN_TOKEN",
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
    """Use 小T's platform-specific apps for identity resolution and OAuth."""

    _aliases = {
        "FEISHU_APP_ID": "XIAOT_FEISHU_APP_ID",
        "FEISHU_APP_SECRET": "XIAOT_FEISHU_APP_SECRET",
        "FEISHU_OAUTH_REDIRECT_URI": "XIAOT_FEISHU_OAUTH_REDIRECT_URI",
        "LARK_OAUTH_REDIRECT_URI": "XIAOT_LARK_OAUTH_REDIRECT_URI",
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
            # The pre-routing XiaoT configuration is the existing YW app.
            # Keep its old secret names as a fallback during migration.
            if self.lark_app_key == "lark_yw":
                legacy = "XIAOT_LARK_APP_ID" if name == "LARK_APP_ID" else "XIAOT_LARK_APP_SECRET"
                value = getattr(self.raw, legacy, None)
                if value not in (None, ""):
                    return value
            return None
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
            raise XiaotBitableAPIError(
                status_code=response.status_code,
                api_code=payload.get("code"),
                message=str(message),
            )
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
        field_names: list[str] | None = None,
        platform: str = "feishu",
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"page_size": max(1, min(int(page_size), 500))}
        if page_token:
            query["page_token"] = page_token
        if filter_formula:
            if len(filter_formula) > 2000:
                raise ValueError("filter_formula is too long")
            query["filter"] = filter_formula
        if field_names:
            query["field_names"] = json.dumps(field_names, ensure_ascii=False)
        query["user_id_type"] = "open_id"
        payload = await self.request(
            "GET",
            f"{self._table_path(table_key)}/records",
            access_token=access_token,
            platform=platform,
            params=query,
        )
        return payload.get("data") or {}

    async def search_person_candidates(
        self,
        table_key: str,
        query: str,
        *,
        access_token: str,
        platform: str = "feishu",
        filter_formula: str = "",
    ) -> dict[str, Any]:
        """Find matching people already present in permission-visible Bitable rows."""
        table = self.resolve_table(table_key)
        terms = _person_name_search_terms(query)
        schema = await self.fields(table_key, access_token=access_token, platform=platform)
        person_fields = [
            str(item.get("field_name") or "")
            for item in schema
            if str(item.get("field_name") or "").strip()
            and (
                str(item.get("ui_type") or "").strip().casefold() == "user"
                or str(item.get("type") or "") == "11"
            )
        ]
        if not person_fields:
            return {
                "success": True,
                "table": table,
                "person_fields": [],
                "candidates": [],
                "scan_truncated": False,
            }

        found: dict[str, dict[str, Any]] = {}
        page_token = ""
        scanned = 0
        scan_truncated = False
        while scanned < XIAOT_PERSON_SEARCH_MAX_RECORDS:
            data = await self.records(
                table_key,
                access_token=access_token,
                platform=platform,
                page_size=500,
                page_token=page_token,
                filter_formula=filter_formula,
                field_names=person_fields,
            )
            items = data.get("items") or []
            for record in items:
                scanned += 1
                fields = record.get("fields") if isinstance(record, dict) else {}
                if not isinstance(fields, dict):
                    continue
                for field_name in person_fields:
                    raw_people = fields.get(field_name)
                    people = raw_people if isinstance(raw_people, list) else [raw_people]
                    for person in people:
                        if not isinstance(person, dict):
                            continue
                        person_id = str(person.get("id") or "").strip()
                        person_name = str(person.get("name") or "").strip()
                        normalized_name = _normalize_person_name(person_name)
                        if not person_id or not person_name or not any(term in normalized_name for term in terms):
                            continue
                        candidate = found.setdefault(
                            person_id,
                            {
                                "id": person_id,
                                "name": person_name,
                                "fields": set(),
                                "exact_match": False,
                            },
                        )
                        candidate["fields"].add(field_name)
                        candidate["exact_match"] = candidate["exact_match"] or (
                            _person_name_has_exact_term(normalized_name, terms)
                        )
                if len(found) > 1:
                    # Two distinct platform IDs are enough to prove ambiguity.
                    break
                if scanned >= XIAOT_PERSON_SEARCH_MAX_RECORDS:
                    scan_truncated = bool(data.get("has_more"))
                    break
            if len(found) > 1 or scan_truncated or not data.get("has_more"):
                break
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise RuntimeError("多维表格人员候选查询未返回有效 page_token")
            page_token = next_token

        candidates = [
            {**candidate, "fields": sorted(candidate["fields"])}
            for candidate in found.values()
        ]
        return {
            "success": True,
            "table": table,
            "person_fields": person_fields,
            "candidates": candidates,
            "scan_truncated": scan_truncated,
            "scanned_records": scanned,
        }

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
            params={"user_id_type": "open_id"},
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
            params={"user_id_type": "open_id"},
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
            params={"user_id_type": "open_id"},
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
        # Bitable documents successful deletion as code=0 with data.deleted=true
        # and the exact record_id. Do not infer success from an empty response.
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("deleted") is not True:
            raise RuntimeError("Bitable delete response did not confirm deletion")
        if str(data.get("record_id") or "") != record_id:
            raise RuntimeError("Bitable delete response record_id did not match the target")
        return data

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
    ) -> dict[str, Any]:
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
        normalized = dict(fields)
        for field_name, value in fields.items():
            try:
                field_type = int(by_name[field_name].get("type") or 0)
            except (TypeError, ValueError):
                field_type = 0
            if field_type == 5:
                normalized[field_name] = _normalize_bitable_date_value(value)
            elif field_type == 18:
                if not isinstance(value, list):
                    raise ValueError(
                        f"relation field {field_name} must be an array of record IDs"
                    )
                record_ids: list[str] = []
                for item in value:
                    record_id = (
                        str(item.get("record_id") or "").strip()
                        if isinstance(item, dict)
                        else str(item or "").strip()
                    )
                    self._validate_record_id(record_id)
                    record_ids.append(record_id)
                # Bitable relation fields accept a string array, not objects
                # such as [{"record_id": "rec..."}].
                normalized[field_name] = record_ids
        return normalized


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
                    "When a person field such as Owner or watcher should refer to the current "
                    "requester, call get_requester_info and use its OAuth-app-specific open_id "
                    "exactly as [{\"id\": \"open_id\"}]. Do not reuse a source bot event ID. "
                    "Bitable write calls use user_id_type=open_id."
                ),
                (
                    "When a user names another person for Owner, watcher, or another person "
                    "field, use search_person_candidates in the target table if the identity is "
                    "not unambiguous. It searches only person-field values from rows visible to "
                    "the current user's Bitable authorization. Only one exact full-name or "
                    "standalone alias match can be used as the candidate; partial-name matches "
                    "are suggestions only. If it returns multiple IDs, only partial matches, no "
                    "candidate, or a truncated scan, ask the user to choose or clarify before "
                    "preparing a write; if the target table has no candidate, search related "
                    "task/sub_task/project/goal tables before asking. Never guess or write a "
                    "display name as an ID. Known XiaoT aliases: William = 李谦 / 李威廉 / 威廉; "
                    "元博 王 = yuanbo / 王元博 / 元博. "
                    "Treat aliases as search variants and still resolve the actual Bitable person ID."
                ),
                (
                    "For date fields (Bitable field type 5), use an integer Unix timestamp in "
                    "milliseconds in the proposed write. The service also converts exact ISO "
                    "date/datetime values to milliseconds (date-only values use Asia/Shanghai); "
                    "never send a natural-language date to Bitable. Show dates to the user in "
                    "readable date/time form, not as raw millisecond numbers."
                ),
                (
                    "For writable single-link relation fields (Bitable field type 18), pass the "
                    "linked record IDs as a string array, e.g. [\"rec...\"]. Never pass objects "
                    "such as [{\"record_id\": \"rec...\"}]. Read the relevant table and records "
                    "first, and use only verified record IDs."
                ),
                (
                    "Feishu Bitable creates its own operation audit entries. 小T must never write "
                    "to the changelog table; treat changelog as read-only."
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
                    "For a request about the current user's own tasks or subtasks, call "
                    "search_records with mine_only=true. Do not filter Owner in filter_formula "
                    "and do not match the person's display name. The service compares the "
                    "Owner/负责人 person-field ID to the verified requester's ID while paging "
                    "through records visible to that user's authorization. Continue with the "
                    "returned page_token until has_more is false. If a search tool returns an "
                    "error, report the search failure; never describe it as zero matching tasks."
                ),
                (
                    "When a user replies affirmatively to a pending proposal, call "
                    "confirm_mutation first using the pending proposal_id. Common clear "
                    "affirmations include 行, 好/好的, ok/okay, 可以, 执行, 开始/开始执行, "
                    "没问题, 同意, and similar short positive replies. A refusal, uncertainty, "
                    "or request to change the proposal is not confirmation. Do not start a new "
                    "proposal or re-read fields first; the service rechecks the target record and "
                    "sender before writing. If proposal_id is unavailable, omit it and the service "
                    "will resolve only one unambiguous pending proposal for that same sender."
                ),
                (
                    "For ai_text, Category must be exactly 个人消息分析文本 or 群分析文本; "
                    "preserve this distinction when updating daily-analysis text."
                ),
                (
                    "Ask about ambiguity. Claim success only after tool verification, then call "
                    "record_result once to reply to the original Feishu message. The relay "
                    "mentions the user who invoked this XiaoT request in its replies and final card. "
                    "If the user cancels the request, do not perform any pending write; call "
                    "record_result with status=cancelled. If cancellation itself cannot be "
                    "completed, use status=cancel_failed and state the reason."
                ),
                (
                    f"The relay MCP is {self.relay.mcp_name()} at "
                    f"{self.relay.base_url()}{XIAOT_MCP_PATH}. "
                    "Use record_result to reply to Feishu/Lark."
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
        self.raw_xiaot_env = raw_env
        # The XiaoT Feishu bot receives/replies to the group message. Account
        # detection and OAuth use XiaoT's own Feishu/Lark apps, never 小C's.
        self.identity_relay = self._identity_relay_for_app("lark_yw")
        self.xiaot_bitable = XiaotBitableClient(
            raw_env
        )
        self.agent_workflow = XiaotAgentRelayWorkflow(self)

    def _identity_relay_for_app(self, app_key: str) -> CloudflareRelay:
        normalized = str(app_key or "").strip().lower()
        if normalized not in {"feishu", "lark_yw", "lark_w_fam"}:
            raise RuntimeError("小T 授权应用标识无效")
        # Support callers that construct a relay without its Worker env (for
        # example, small unit fixtures); deployed instances always set it.
        if not hasattr(self, "raw_xiaot_env") and normalized in {"feishu", "lark_yw"}:
            existing = getattr(self, "identity_relay", None)
            if existing is not None:
                return existing
        return CloudflareRelay(
            XiaotLarkOAuthEnvironment(self.raw_xiaot_env, normalized),
            self.ctx,
            self.state,
        )

    @staticmethod
    def _oauth_app_key(platform: str, configured_key: Any) -> str:
        app_key = str(configured_key or "").strip().lower()
        if platform == "feishu":
            return "feishu"
        if platform == "lark":
            # Existing pending states predate app_key and were created by YW.
            return app_key if app_key in {"lark_yw", "lark_w_fam"} else "lark_yw"
        raise RuntimeError("小T 授权平台标识无效")

    def _tenant_app_routes(self) -> dict[str, str]:
        env = getattr(self, "raw_xiaot_env", None)
        if env is None:
            env = getattr(getattr(self, "identity_relay", None), "env", None)
        raw_routes = str(_env(env, "XIAOT_TENANT_APP_MAP", "") or "").strip()
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
        # Preserve the existing YW organization allowlist during migration.
        # These values were already used to distinguish external Lark tenants;
        # they now select the matching YW OAuth app explicitly.
        legacy_yw_tenants = str(
            _env(env, "LARK_EXTERNAL_TENANT_KEYS", "") or ""
        )
        for tenant in legacy_yw_tenants.split(","):
            tenant = tenant.strip()
            if tenant:
                routes.setdefault(tenant, "lark_yw")
        return routes

    async def _resolve_oauth_route(self, event: dict[str, Any]) -> tuple[str, str, str]:
        """Resolve the sender tenant to one explicit platform and OAuth app."""
        sender_tenant = str(event.get("sender_tenant_key") or "").strip()
        event_tenant = str(event.get("tenant_key") or "").strip()
        tenant_key = sender_tenant or event_tenant
        routes = self._tenant_app_routes()
        if tenant_key and tenant_key in routes:
            app_key = routes[tenant_key]
            return ("feishu" if app_key == "feishu" else "lark", app_key, tenant_key)

        # Once explicit organization routing is configured, an external tenant
        # must match exactly. Falling back to the YW app could authorize the
        # wrong organization.
        if routes and sender_tenant and sender_tenant != event_tenant:
            raise RuntimeError(
                "无法确认当前发起人的 Lark 组织：该 tenant_key 未配置对应授权应用；"
                "未发起授权，也未访问或修改多维表格。"
            )

        explicit_platform = str(
            event.get("tenant_brand") or event.get("platform") or event.get("brand") or ""
        ).strip().lower()
        if explicit_platform in {"feishu", "lark"}:
            app_key = "feishu" if explicit_platform == "feishu" else "lark_yw"
            return explicit_platform, app_key, tenant_key

        if sender_tenant and event_tenant and sender_tenant != event_tenant:
            return "lark", "lark_yw", sender_tenant
        if sender_tenant and event_tenant and sender_tenant == event_tenant:
            return "feishu", "feishu", sender_tenant

        platform = await self._detect_requester_platform(event)
        app_key = "feishu" if platform == "feishu" else "lark_yw"
        return platform, app_key, tenant_key

    async def _xiaot_user_token(
        self, *, platform: str, app_key: str, open_id: str
    ) -> dict[str, Any] | None:
        try:
            await self._ensure_xiaot_oauth_schema()
            row = await _db_first(
                self.state.db,
                "SELECT platform, app_key, open_id, access_token, refresh_token, "
                "expires_at, updated_at "
                "FROM xiaot_bitable_user_tokens WHERE app_key = ? AND open_id = ?",
                app_key,
                open_id,
            )
        except Exception:
            # Legacy Feishu/YW tokens remain usable if the new scoped table is
            # unavailable during a rolling schema upgrade. W Fam never falls
            # back to an unscoped token.
            row = None
        if row:
            return row
        # Existing DEV Feishu and YW grants remain valid through the legacy
        # token table. W Fam is always isolated by its own app_key.
        if app_key in {"feishu", "lark_yw"}:
            return await self.state.user_token(platform, open_id)
        return None

    async def _save_xiaot_user_token(
        self,
        *,
        platform: str,
        app_key: str,
        open_id: str,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> None:
        await self._ensure_xiaot_oauth_schema()
        await _db_run(
            self.state.db,
            "INSERT INTO xiaot_bitable_user_tokens "
            "(app_key, platform, open_id, access_token, refresh_token, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(app_key, open_id) DO UPDATE SET "
            "platform=excluded.platform, access_token=excluded.access_token, "
            "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
            "updated_at=excluded.updated_at",
            app_key,
            platform,
            open_id,
            access_token,
            refresh_token,
            expires_at,
            int(time.time()),
        )
        if app_key in {"feishu", "lark_yw"}:
            await self.state.save_user_token(
                platform=platform,
                open_id=open_id,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
            )

    def base_url(self) -> str:
        return super().base_url()

    def mcp_name(self) -> str:
        return XIAOT_MCP_NAME

    def memory_scope(self) -> str:
        return XIAOT_AGENT_SCOPE

    async def _source_requester_open_id(self, run: dict[str, Any]) -> str:
        row = await _db_first(
            self.state.db,
            "SELECT sender_open_id FROM feishu_events "
            "WHERE request_id = ? AND conversation_key = ?",
            str(run.get("request_id") or ""),
            str(run.get("conversation_key") or ""),
        )
        open_id = str((row or {}).get("sender_open_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", open_id):
            raise RuntimeError("当前小T请求缺少有效的原始发起人 ID，无法安全 @ 发起人")
        return open_id

    @staticmethod
    def _mention_requester_text(open_id: str, text: str) -> str:
        return f'<at user_id="{open_id}">发起人</at> {str(text or "").strip()}'

    @staticmethod
    def _result_card(run: dict[str, Any], open_id: str) -> dict[str, Any]:
        status = str(run.get("status") or "done").strip().lower()
        card_heading, template = {
            "done": ("任务成功", "turquoise"),
            "failed": ("任务失败", "red"),
            "blocked": ("任务被阻塞", "red"),
            "cancelled": ("取消任务成功", "turquoise"),
            "cancel_failed": ("取消任务失败", "red"),
        }.get(status, ("任务失败", "red"))
        title = str(run.get("title") or "").strip()
        markdown = str(run.get("markdown") or "").strip()
        details = "\n\n".join(part for part in (title, markdown) if part)
        if not details:
            details = {
                "done": "任务已完成。",
                "cancelled": "任务已取消，没有执行后续操作。",
                "cancel_failed": "取消任务未能完成。",
            }.get(status, "任务未能完成。")
        return {
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": card_heading},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<at id={open_id}></at>\n\n{details}",
                    },
                }
            ],
        }

    async def deliver_result(self, request_id: str) -> None:
        """Deliver XiaoT terminal results as a requester-mentioning card."""
        run = await self.state.get_run(request_id)
        if not run or int(run.get("delivered") or 0):
            return
        requester_open_id = await self._source_requester_open_id(run)
        api = self.api_for_conversation(str(run.get("conversation_key") or ""))
        outbound = await api.reply_card(
            str(run.get("source_message_id") or ""),
            self._result_card(run, requester_open_id),
        )
        placeholder = str(run.get("placeholder_message_id") or "").strip()
        if placeholder:
            try:
                await api.update(placeholder, "本次小T请求已结束，请查看下方结果卡片。")
            except Exception as exc:
                print(f"小T placeholder update failed: {_safe_error(exc)}")
        await _db_run(
            self.state.db,
            "UPDATE relay_runs SET delivered = 1, updated_at = ? WHERE request_id = ?",
            int(time.time()),
            request_id,
        )
        await self.state.save_reply(outbound, str(run.get("conversation_key") or ""))

    async def deliver_question(self, request_id: str) -> None:
        """Keep XiaoT clarification replies in place and mention their requester."""
        run = await self.state.get_run(request_id)
        if not run or str(run.get("status") or "") != "needs_user":
            return
        open_id = await self._source_requester_open_id(run)
        text = str(run.get("progress_message") or "请补充必要信息。")
        text = self._mention_requester_text(open_id, text)
        api = self.api_for_conversation(str(run.get("conversation_key") or ""))
        placeholder = str(run.get("placeholder_message_id") or "").strip()
        if placeholder:
            try:
                await api.update(placeholder, text)
                return
            except Exception as exc:
                print(f"小T question update failed; sending a reply: {_safe_error(exc)}")
        outbound = await api.reply(str(run.get("source_message_id") or ""), text)
        await self.state.update_run(request_id, placeholder_message_id=str(outbound))
        await self.state.save_reply(outbound, str(run.get("conversation_key") or ""))

    @staticmethod
    def _record_owned_by(
        record: dict[str, Any], *, owner_field: str, requester_open_id: str
    ) -> bool:
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        owners = fields.get(owner_field)
        if isinstance(owners, dict):
            owners = [owners]
        if not isinstance(owners, list):
            return False
        return any(
            isinstance(owner, dict)
            and str(owner.get("id") or "").strip() == requester_open_id
            for owner in owners
        )

    @staticmethod
    def _owner_page_offset(page_token: str) -> int:
        if not page_token:
            return 0
        match = re.fullmatch(r"xiaot-owner:(\d+)", page_token)
        if not match:
            raise ValueError(
                "mine_only 查询的 page_token 必须使用上一页返回的人员筛选游标"
            )
        offset = int(match.group(1))
        if offset > 1_000_000:
            raise ValueError("mine_only 查询游标超出允许范围")
        return offset

    async def _search_records_owned_by_requester(
        self,
        *,
        table_key: str,
        access_token: str,
        platform: str,
        owner_field: str,
        requester_open_id: str,
        page_size: int,
        page_token: str,
        filter_formula: str,
    ) -> dict[str, Any]:
        """Filter person-field IDs after reading each permission-visible API page."""
        offset = self._owner_page_offset(page_token)
        matches: list[dict[str, Any]] = []
        api_page_token = ""
        has_more_matches = False

        while True:
            data = await self.xiaot_bitable.records(
                table_key,
                access_token=access_token,
                platform=platform,
                page_size=500,
                page_token=api_page_token,
                filter_formula=filter_formula,
            )
            items = data.get("items") or []
            for item in items:
                if self._record_owned_by(
                    item,
                    owner_field=owner_field,
                    requester_open_id=requester_open_id,
                ):
                    matches.append(item)
                    if len(matches) > offset + page_size:
                        has_more_matches = True
                        break
            if has_more_matches or not data.get("has_more"):
                break
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token == api_page_token:
                raise RuntimeError("多维表格分页未返回有效 page_token")
            api_page_token = next_token

        items = matches[offset : offset + page_size]
        next_page_token = f"xiaot-owner:{offset + page_size}" if has_more_matches else ""
        return {
            "items": items,
            "has_more": has_more_matches,
            "page_token": next_page_token,
        }

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
        if not run.get("placeholder_message_id"):
            try:
                requester_open_id = await self._source_requester_open_id(run)
                placeholder_id = await self.api_for_conversation(
                    str(run.get("conversation_key") or "")
                ).reply(
                    str(run.get("source_message_id") or ""),
                    self._mention_requester_text(requester_open_id, PLACEHOLDER),
                )
                await self.state.update_run(
                    request_id, placeholder_message_id=str(placeholder_id)
                )
                await self.state.save_reply(
                    str(placeholder_id), str(run.get("conversation_key") or "")
                )
            except Exception as exc:
                message = _safe_error(exc)
                await self.state.update_run(
                    request_id,
                    status="failed",
                    trigger_status=0,
                    trigger_error=message,
                    title="Agent 任务失败",
                    markdown=message,
                    completed_at=int(time.time()),
                )
                await self.deliver_result(request_id)
                return
        # Queue delivery is at-least-once. Only the worker that atomically
        # moved this run from queued may send the processing reply or trigger
        # the Agent, preventing duplicate placeholders on concurrent retries.
        await super().run_agent_job(body)

    async def _ensure_xiaot_agent_run_schema(self) -> None:
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_agent_trigger_runs (
                request_id TEXT PRIMARY KEY,
                api_trigger_id TEXT NOT NULL DEFAULT '',
                agent_trigger_run_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'accepted',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            )""",
        )

    async def _record_agent_trigger_metadata(
        self, request_id: str, trigger_url: str, response: Any
    ) -> None:
        """Store only the XiaoT API run identifier needed for status polling."""
        await self._ensure_xiaot_agent_run_schema()
        payload: dict[str, Any] = {}
        try:
            decoded = response.json()
            if isinstance(decoded, dict):
                payload = decoded
        except Exception:
            pass
        agent_run_id = str(payload.get("agent_trigger_run_id") or "").strip()
        if not re.fullmatch(r"apirun_[A-Za-z0-9_-]{1,200}", agent_run_id):
            agent_run_id = ""
        parsed = urlparse(trigger_url)
        trigger_match = re.search(
            r"/v1/workspace_agents/([^/]+)/trigger/?$", parsed.path
        )
        api_trigger_id = trigger_match.group(1) if trigger_match else ""
        if not re.fullmatch(r"agtch_[A-Za-z0-9_-]{1,200}", api_trigger_id):
            api_trigger_id = ""
        await _db_run(
            self.state.db,
            """INSERT INTO xiaot_agent_trigger_runs
               (request_id, api_trigger_id, agent_trigger_run_id, status,
                error_code, error_message, updated_at)
               VALUES (?, ?, ?, 'accepted', '', '', ?)
               ON CONFLICT(request_id) DO UPDATE SET
                 api_trigger_id=excluded.api_trigger_id,
                 agent_trigger_run_id=excluded.agent_trigger_run_id,
                 status='accepted', error_code='', error_message='',
                 updated_at=excluded.updated_at""",
            request_id,
            api_trigger_id,
            agent_run_id,
            int(time.time()),
        )

    @staticmethod
    def _agent_run_status_url(trigger_url: str, api_trigger_id: str, run_id: str) -> str:
        parsed = urlparse(trigger_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("小 T Agent trigger URL must use HTTPS")
        if not re.fullmatch(r"agtch_[A-Za-z0-9_-]{1,200}", api_trigger_id):
            raise ValueError("小 T Agent trigger ID is missing or invalid")
        if not re.fullmatch(r"apirun_[A-Za-z0-9_-]{1,200}", run_id):
            raise ValueError("小 T Agent run ID is missing or invalid")
        return parsed._replace(
            path=f"/v1/workspace_agents/{api_trigger_id}/runs/{run_id}",
            params="",
            query="",
            fragment="",
        ).geturl()

    async def _poll_xiaot_agent_run(self, record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(record.get("request_id") or "")
        run_id = str(record.get("agent_trigger_run_id") or "")
        api_trigger_id = str(record.get("api_trigger_id") or "")
        result: dict[str, Any] = {
            "agent_trigger_run_id": run_id or None,
            "status": str(record.get("status") or "accepted"),
            "error_code": str(record.get("error_code") or "") or None,
            "error_message": str(record.get("error_message") or "") or None,
            "updated_at": record.get("updated_at"),
        }
        if not run_id:
            result["error_code"] = result["error_code"] or "run_id_not_returned"
            return result
        if result["status"] in {"completed", "failed"}:
            return result
        trigger_url = _env(self.env, "WORKSPACE_AGENT_RELAY_TRIGGER_URL")
        access_token = _env(self.env, "WORKSPACE_AGENT_RELAY_AGENT_TOKEN")
        if not access_token:
            result["error_code"] = "agent_access_token_missing"
            return result
        try:
            status_url = self._agent_run_status_url(
                trigger_url, api_trigger_id, run_id
            )
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    status_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": f"{self.mcp_name()}/3.0",
                    },
                )
            payload: dict[str, Any] = {}
            try:
                decoded = response.json()
                if isinstance(decoded, dict):
                    payload = decoded
            except Exception:
                pass
            if response.status_code < 200 or response.status_code >= 300:
                error = payload.get("error")
                error_code = (
                    str(error.get("code") or "")
                    if isinstance(error, dict)
                    else ""
                ) or f"status_api_http_{response.status_code}"
                error_message = (
                    str(error.get("message") or "")
                    if isinstance(error, dict)
                    else ""
                )
                result["error_code"] = error_code
                result["error_message"] = _safe_error(
                    error_message, access_token
                )[:500] or None
                return result
            status = str(payload.get("status") or "").strip()
            if status not in {
                "queued",
                "in_progress",
                "suspended",
                "completed",
                "failed",
            }:
                result["error_code"] = "unexpected_status_response"
                return result
            error = payload.get("error")
            error_code = (
                str(error.get("code") or "") if isinstance(error, dict) else ""
            )
            error_message = (
                str(error.get("message") or "") if isinstance(error, dict) else ""
            )
            error_message = _safe_error(error_message, access_token)[:500]
            updated_at = int(time.time())
            await _db_run(
                self.state.db,
                """UPDATE xiaot_agent_trigger_runs
                   SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                   WHERE request_id = ?""",
                status,
                error_code,
                error_message,
                updated_at,
                request_id,
            )
            result.update(
                {
                    "status": status,
                    "error_code": error_code or None,
                    "error_message": error_message or None,
                    "updated_at": updated_at,
                }
            )
            return result
        except Exception as exc:
            result["error_code"] = "status_api_request_failed"
            result["error_message"] = _safe_error(exc, access_token)[:500]
            return result

    async def _xiaot_run_context(self, conversation_key: str, limit: int) -> dict[str, Any]:
        rows = await self.state.recent_runs(conversation_key, limit)
        await self._ensure_xiaot_agent_run_schema()
        for index, row in enumerate(rows):
            metadata = await _db_first(
                self.state.db,
                "SELECT request_id, api_trigger_id, agent_trigger_run_id, status, "
                "error_code, error_message, updated_at "
                "FROM xiaot_agent_trigger_runs WHERE request_id = ?",
                str(row.get("request_id") or ""),
            )
            if not metadata:
                row["agent_execution"] = {"status": "not_recorded"}
                continue
            created_at = int(row.get("created_at") or 0)
            is_settled = time.time() - created_at >= 15
            should_poll = (
                index == 0
                and is_settled
                and str(metadata.get("status") or "")
                not in {"completed", "failed"}
            )
            if should_poll:
                row["agent_execution"] = await self._poll_xiaot_agent_run(metadata)
            else:
                row["agent_execution"] = {
                    "agent_trigger_run_id": metadata.get("agent_trigger_run_id") or None,
                    "status": metadata.get("status") or "accepted",
                    "error_code": metadata.get("error_code") or None,
                    "error_message": metadata.get("error_message") or None,
                    "updated_at": metadata.get("updated_at"),
                }
        return {
            "success": True,
            "conversation_key": conversation_key,
            "runs": rows,
        }

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
        # Upgrade the first XiaoT DEV schema in place; old states are left
        # unusable for callbacks because they lack a matching platform id.
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
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_identity_links (
                platform TEXT NOT NULL,
                source_union_id TEXT NOT NULL,
                account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (platform, source_union_id)
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_source_identity_links (
                platform TEXT NOT NULL,
                source_open_id TEXT NOT NULL,
                account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (platform, source_open_id)
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_identity_links_by_app (
                app_key TEXT NOT NULL,
                source_union_id TEXT NOT NULL,
                account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (app_key, source_union_id)
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_source_identity_links_by_app (
                app_key TEXT NOT NULL,
                source_open_id TEXT NOT NULL,
                account_open_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (app_key, source_open_id)
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_bitable_user_tokens (
                app_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                open_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (app_key, open_id)
            )""",
        )
        await _db_run(
            self.state.db,
            """CREATE TABLE IF NOT EXISTS xiaot_run_requesters (
                request_id TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                open_id TEXT NOT NULL,
                app_key TEXT NOT NULL DEFAULT 'feishu',
                tenant_key TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            )""",
        )
        for column, definition in (
            ("app_key", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("tenant_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            with suppress(Exception):
                await _db_run(
                    self.state.db,
                    f"ALTER TABLE xiaot_run_requesters ADD COLUMN {column} {definition}",
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

    async def _detect_requester_platform(self, event: dict[str, Any]) -> str:
        """Classify the sender before starting OAuth; never guess Feishu on failure."""
        explicit = str(
            event.get("tenant_brand") or event.get("platform") or event.get("brand") or ""
        ).strip().lower()
        if explicit in {"feishu", "lark"}:
            return explicit

        sender_tenant = str(event.get("sender_tenant_key") or "").strip()
        event_tenant = str(event.get("tenant_key") or "").strip()
        configured_lark_tenants = {
            item.strip()
            for item in _env(self.identity_relay.env, "LARK_EXTERNAL_TENANT_KEYS", "").split(",")
            if item.strip()
        }
        if sender_tenant and sender_tenant in configured_lark_tenants:
            return "lark"
        if sender_tenant and event_tenant and sender_tenant != event_tenant:
            return "lark"

        # Tenant fields can be normalized for external groups, so use the
        # platform contact APIs as a second signal. Only a unique match is
        # enough to choose which bot's OAuth flow to start.
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
                    user = await api.resolve_user_id(
                        identifier, user_id_type=identifier_type
                    )
                except Exception:
                    continue
                if user:
                    resolved.append(candidate)
                    break
        if len(resolved) == 1:
            return resolved[0]
        if len(resolved) > 1:
            reason = "Feishu 和 Lark 都能解析该发起人，结果不唯一"
        else:
            reason = "事件租户信息不足，Feishu/Lark 用户目录也无法解析该发起人"
        raise RuntimeError(
            f"无法判断当前发起人的账号类型：{reason}；未发起授权，也未访问或修改多维表格。"
        )

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
        if source_open_id:
            linked_by_source = await _db_first(
                self.state.db,
                "SELECT account_open_id FROM xiaot_bitable_source_identity_links_by_app "
                "WHERE app_key = ? AND source_open_id = ?",
                app_key,
                source_open_id,
            )
            if not linked_by_source and app_key in {"feishu", "lark_yw"}:
                linked_by_source = await _db_first(
                    self.state.db,
                    "SELECT account_open_id FROM xiaot_bitable_source_identity_links "
                    "WHERE platform = ? AND source_open_id = ?",
                    platform,
                    source_open_id,
                )
            linked_open_id = str((linked_by_source or {}).get("account_open_id") or "").strip()
            if linked_open_id:
                return {
                    "platform": platform,
                    "app_key": app_key,
                    "tenant_key": tenant_key,
                    "open_id": linked_open_id,
                }
        if platform == "lark" and source_union_id:
            linked = await _db_first(
                self.state.db,
                "SELECT account_open_id FROM xiaot_bitable_identity_links_by_app "
                "WHERE app_key = ? AND source_union_id = ?",
                app_key,
                source_union_id,
            )
            if not linked and app_key in {"feishu", "lark_yw"}:
                linked = await _db_first(
                    self.state.db,
                    "SELECT account_open_id FROM xiaot_bitable_identity_links "
                    "WHERE platform = ? AND source_union_id = ?",
                    platform,
                    source_union_id,
                )
            linked_open_id = str((linked or {}).get("account_open_id") or "").strip()
            if linked_open_id:
                return {
                    "platform": platform,
                    "app_key": app_key,
                    "tenant_key": tenant_key,
                    "open_id": linked_open_id,
                }
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
                return {
                    "platform": platform,
                    "app_key": app_key,
                    "tenant_key": tenant_key,
                    "open_id": open_id,
                }
        if platform == "lark":
            # Some Lark users arrive through a Feishu external-group event and
            # cannot be resolved through the Lark app's tenant contact API.
            # The platform has already been selected by detect_user_platform;
            # require Lark OAuth next, then bind the returned account to this
            # exact source open_id through the one-time OAuth state.  Do not
            # require a cross-app union_id to exist in the webhook event.
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
        app_key = self._oauth_app_key(platform, account_identity.get("app_key"))
        tenant_key = str(account_identity.get("tenant_key") or "").strip()
        identity_relay = self._identity_relay_for_app(app_key)
        open_id = str(account_identity.get("open_id") or "").strip()
        source_open_id = str(event.get("open_id") or "").strip()
        source_chat_id = str(event.get("chat_id") or "").strip()
        source_union_id = str(event.get("union_id") or "").strip()
        source_user_id = str(event.get("user_id") or "").strip()
        source_platform = str(account_identity.get("source_platform") or "feishu").strip()
        account_open_id = (
            "" if str(account_identity.get("identity_pending") or "") == "true" else open_id
        )
        callback_key = f"{platform.upper()}_OAUTH_REDIRECT_URI"
        redirect_uri = str(
            _env(
                identity_relay.env,
                # The selected OAuth app owns its callback URL configuration.
                callback_key,
                identity_relay.base_url() + f"/{platform}/oauth/callback",
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
            "(state, open_id, platform, account_open_id, source_union_id, source_open_id, "
            "source_user_id, source_platform, app_key, tenant_key, request_id, conversation_key, "
            "source_message_id, redirect_uri, event_json, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            state,
            open_id,
            platform,
            account_open_id,
            source_union_id,
            source_open_id,
            source_user_id,
            source_platform,
            app_key,
            tenant_key,
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
                            f"请使用你的 {platform.title()} 账号授权；点击后在页面中打开授权窗口，"
                            "授权成功后窗口会自动关闭并继续刚才的请求。"
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
        if not source_chat_id or not source_open_id:
            raise RuntimeError("无法安全发送仅本人可见的授权卡片：缺少群组或发起人标识")
        outbound = await self.api_for_conversation(conversation_key).send_ephemeral_card(
            chat_id=source_chat_id,
            open_id=source_open_id,
            card=card,
        )
        await _db_run(
            self.state.db,
            "UPDATE xiaot_bitable_oauth_states SET authorization_message_id = ? "
            "WHERE state = ? AND consumed_at IS NULL",
            str(outbound),
            state,
        )
        await self.state.save_reply(str(outbound), conversation_key)

    async def _replace_authorization_card_with_success(
        self, pending: dict[str, Any]
    ) -> None:
        """Replace XiaoT's private authorization card with a private success card."""
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
            success_card = {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "plain_text", "content": "授权成功！"},
                    }
                ],
            }
            # Ephemeral cards do not use the shared-card PATCH flow. Send the
            # replacement first, then remove the old card to avoid losing the
            # success confirmation if sending the replacement fails.
            await api.send_ephemeral_card(
                chat_id=chat_id,
                open_id=open_id,
                card=success_card,
            )
            old_message_id = str(pending.get("authorization_message_id") or "").strip()
            if old_message_id:
                await api._request(
                    "POST",
                    "/open-apis/ephemeral/v1/delete",
                    json={"message_id": old_message_id},
                )
        except Exception as exc:
            # OAuth and the user's request have already succeeded. A transient
            # card-rendering error must not undo their authorization.
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
            "SELECT * FROM xiaot_bitable_oauth_states "
            "WHERE state = ? AND platform = ? AND consumed_at IS NULL",
            state,
            platform,
        )
        if (
            not pending
            or int(pending.get("expires_at") or 0) < int(time.time())
            or not str(pending.get("redirect_uri") or "").strip()
        ):
            return _response(
                {"success": False, "error": "invalid_or_expired_state"}, 400
            )
        app_key = self._oauth_app_key(platform, pending.get("app_key"))
        identity_relay = self._identity_relay_for_app(app_key)
        auth_base = (
            "https://accounts.larksuite.com" if platform == "lark" else FEISHU_AUTH_BASE_URL
        )
        auth_url = f"{auth_base}/open-apis/authen/v1/authorize?" + urlencode(
            {
                "app_id": _env(identity_relay.env, f"{platform.upper()}_APP_ID"),
                "redirect_uri": str(pending["redirect_uri"]),
                "scope": identity_relay.platform_oauth_scope(platform),
                "state": state,
            }
        )
        parsed_base = urlparse(identity_relay.base_url())
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        return _oauth_launcher_page(auth_url, origin, state)

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
            or not (
                pending.get("account_open_id")
                or pending.get("source_open_id")
                or pending.get("source_union_id")
                or pending.get("source_user_id")
            )
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
                        "client_id": _env(identity_relay.env, f"{normalized.upper()}_APP_ID"),
                        "client_secret": _env(
                            identity_relay.env, f"{normalized.upper()}_APP_SECRET"
                        ),
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
            user_data = await identity_relay.api_for_conversation(
                f"{normalized}:oauth"
            ).user_info(access_token)
            identity = str(user_data.get("open_id") or "").strip()
            expected_open_id = str(pending.get("account_open_id") or "").strip()
            source_union_id = str(pending.get("source_union_id") or "").strip()
            source_open_id = str(pending.get("source_open_id") or "").strip()
            source_user_id = str(pending.get("source_user_id") or "").strip()
            source_platform = str(pending.get("source_platform") or normalized).strip().lower()
            is_pending_identity = expected_open_id.startswith(("pending_union:", "pending_source:"))
            if expected_open_id and not is_pending_identity:
                if identity != expected_open_id:
                    return _response(
                        {
                            "success": False,
                            "error": "requester_mismatch",
                            "message": f"授权的 {normalized.title()} 账号与发起 @小T 的人员不一致，未保存授权。",
                        },
                        403,
                    )
            else:
                requester_ids = {
                    value
                    for value in (source_open_id, source_union_id, source_user_id)
                    if value
                }
                authorized_ids = {
                    str(user_data.get(key) or "").strip()
                    for key in ("open_id", "union_id", "user_id")
                    if str(user_data.get(key) or "").strip()
                }
                same_platform_match = bool(requester_ids.intersection(authorized_ids))
                # Feishu webhook IDs and Lark OAuth IDs can live in different
                # namespaces. In that detected cross-platform case, the
                # one-time state created from the @ message is the binding;
                # same-platform authorization must still match an event ID.
                if not same_platform_match and source_platform == normalized:
                    return _response(
                        {
                            "success": False,
                            "error": "requester_mismatch",
                            "message": f"授权的 {normalized.title()} 账号与发起 @小T 的人员不一致，未保存授权。",
                        },
                        403,
                    )
                if source_union_id and str(user_data.get("union_id") or "").strip() == source_union_id:
                    await _db_run(
                        self.state.db,
                        "INSERT INTO xiaot_bitable_identity_links_by_app "
                        "(app_key, source_union_id, account_open_id, created_at) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(app_key, source_union_id) "
                        "DO UPDATE SET account_open_id = excluded.account_open_id, "
                        "created_at = excluded.created_at",
                        app_key,
                        source_union_id,
                        identity,
                        int(time.time()),
                    )
                    if app_key in {"feishu", "lark_yw"}:
                        await _db_run(
                            self.state.db,
                            "INSERT INTO xiaot_bitable_identity_links "
                            "(platform, source_union_id, account_open_id, created_at) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(platform, source_union_id) "
                            "DO UPDATE SET account_open_id = excluded.account_open_id, "
                            "created_at = excluded.created_at",
                            normalized,
                            source_union_id,
                            identity,
                            int(time.time()),
                        )
                if source_open_id:
                    await _db_run(
                        self.state.db,
                        "INSERT INTO xiaot_bitable_source_identity_links_by_app "
                        "(app_key, source_open_id, account_open_id, created_at) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(app_key, source_open_id) "
                        "DO UPDATE SET account_open_id = excluded.account_open_id, "
                        "created_at = excluded.created_at",
                        app_key,
                        source_open_id,
                        identity,
                        int(time.time()),
                    )
                    if app_key in {"feishu", "lark_yw"}:
                        await _db_run(
                            self.state.db,
                            "INSERT INTO xiaot_bitable_source_identity_links "
                            "(platform, source_open_id, account_open_id, created_at) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(platform, source_open_id) "
                            "DO UPDATE SET account_open_id = excluded.account_open_id, "
                            "created_at = excluded.created_at",
                            normalized,
                            source_open_id,
                            identity,
                            int(time.time()),
                        )
            await self._save_xiaot_user_token(
                platform=normalized,
                app_key=app_key,
                open_id=identity,
                access_token=access_token,
                refresh_token=str(token_data.get("refresh_token") or ""),
                expires_at=int(time.time()) + int(token_data.get("expires_in") or 7200),
            )
            await self._replace_authorization_card_with_success(pending)
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
                    "(request_id, conversation_key, platform, open_id, app_key, "
                    "tenant_key, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
                    "conversation_key=excluded.conversation_key, platform=excluded.platform, "
                    "open_id=excluded.open_id, app_key=excluded.app_key, "
                    "tenant_key=excluded.tenant_key",
                    request_id,
                    conversation_key,
                    normalized,
                    identity,
                    app_key,
                    str(pending.get("tenant_key") or ""),
                    int(time.time()),
                )
                await self.agent_workflow.handle_event(
                    platform=normalized,
                    conversation_key=conversation_key,
                    event=event,
                    request_id=request_id,
                )
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
        for item in definitions:
            if item.get("name") == "get_requester_info":
                item["description"] = (
                    "Return the current 小T requester's platform and open_id in the matching "
                    "Feishu/Lark OAuth app. Use this exact open_id in person fields as "
                    "[{\"id\": \"open_id\"}]; do not use the bot-event open_id."
                )
            elif item.get("name") == "record_result":
                item["description"] = (
                    "结束当前小T请求并回复 Feishu。status 使用 done/failed/blocked；"
                    "用户取消请求且未执行后续操作时使用 cancelled；取消未能完成时使用 "
                    "cancel_failed。DEV relay 会返回带有本次发起人 @提及的状态消息卡片。"
                )
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
                    "查询‘我的任务/子任务’时设置 mine_only=true；服务端按当前发起人的人员字段 ID 精确匹配，"
                    "不要在 filter_formula 中按 Owner 人名或用户 ID 筛选。"
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
                            "mine_only": {"type": "boolean", "default": False},
                        },
                    },
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "search_person_candidates",
                    "description": (
                        "只读搜索目标表中当前用户有权限读取到的人员字段候选。"
                        "仅在 Owner、关注人、负责人等人员身份不确定时调用；返回候选的 Bitable ID 和姓名，"
                        "候选带 exact_match；只有唯一完整姓名/独立别名匹配可用于提案，片段匹配只作提示。"
                        "若多 ID、只有片段匹配、无候选或扫描被截断，先询问用户，不得猜测或写入。"
                    ),
                    "inputSchema": {
                        "type": "object",
                        "required": [
                            "request_id",
                            "conversation_key",
                            "table_key",
                            "query",
                        ],
                        "properties": {
                            "request_id": string,
                            "conversation_key": string,
                            "table_key": string,
                            "query": string,
                            "filter_formula": string,
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
                        "仅在同一发起人对待确认提案作出明确肯定回复后执行单次新增/更新/删除。"
                        "行、好、ok、可以、执行、开始、没问题等肯定回复均可；否定、含糊或改动提案的回复会被拦截。"
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
        status = str(args.get("status") or "").strip().lower()
        if name == "record_result" and status in {
            "cancelled",
            "canceled",
            "cancel_failed",
        }:
            run = await self._require_xiaot_run(args)
            request_id = str(run["request_id"])
            conversation_key = str(args.get("conversation_key") or "")
            if run.get("completed_at"):
                return self._tool_result(
                    {
                        "success": True,
                        "request_id": request_id,
                        "status": run.get("status"),
                        "already_recorded": True,
                    }
                )
            image_results: list[dict[str, Any]] = []
            try:
                for image_args in self._result_image_args(args):
                    image_results.append(
                        await self._send_agent_image(
                            request_id, conversation_key, image_args
                        )
                    )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "image_send_failed",
                            "message": _safe_error(exc),
                        },
                        "result_not_recorded": True,
                        "images_sent": image_results,
                    },
                    True,
                )
            normalized_status = "cancelled" if status == "canceled" else status
            await self.state.update_run(
                request_id,
                status=normalized_status,
                title=str(args.get("title") or ""),
                markdown=str(args.get("markdown") or ""),
                completed_at=int(time.time()),
            )
            await self._enqueue({"kind": "deliver_result", "request_id": request_id})
            return self._tool_result(
                {
                    "success": True,
                    "request_id": request_id,
                    "status": normalized_status,
                    "images_sent": image_results,
                }
            )
        if name in _RELAY_TOOLS:
            if name == "get_run_context":
                conversation_key = str(args.get("conversation_key") or "")
                if not conversation_key.startswith("xiaot:"):
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "wrong_agent_context",
                                "message": "小 T run context requires a 小 T Feishu conversation.",
                            },
                        },
                        True,
                    )
                try:
                    limit = max(1, min(int(args.get("limit", 5)), 20))
                except (TypeError, ValueError):
                    limit = 5
                return self._tool_result(
                    await self._xiaot_run_context(conversation_key, limit)
                )
            if name == "get_requester_info":
                conversation_key = str(args.get("conversation_key") or "")
                if not conversation_key.startswith("xiaot:"):
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "wrong_agent_context",
                                "message": "小 T requester info requires a 小 T conversation.",
                            },
                        },
                        True,
                    )
                current = await _db_first(
                    self.state.db,
                    "SELECT request_id FROM relay_runs WHERE conversation_key = ? "
                    "AND status IN ('queued','dispatching','triggered','running','needs_user') "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    conversation_key,
                )
                if not current or not current.get("request_id"):
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "requester_not_found",
                                "message": "no active 小 T request exists for this conversation",
                            },
                        },
                        True,
                    )
                # `get_requester_info` is the identity source used to fill Bitable
                # person fields. Do not fall back to the webhook event's sender ID:
                # that ID belongs to the bot event namespace, not necessarily the
                # Feishu/Lark OAuth app namespace required by Bitable.
                await self._ensure_xiaot_oauth_schema()
                identity = await _db_first(
                    self.state.db,
                    "SELECT platform, open_id FROM xiaot_run_requesters "
                    "WHERE request_id = ? AND conversation_key = ?",
                    str(current["request_id"]),
                    conversation_key,
                )
                if not identity or not identity.get("open_id"):
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "requester_not_found",
                                "message": (
                                    "no verified OAuth requester for this 小 T request; "
                                    "do not use the bot-event sender ID"
                                ),
                            },
                        },
                        True,
                    )
                return self._tool_result(
                    {
                        "success": True,
                        "conversation_key": conversation_key,
                        "requester": {
                            "platform": str(identity.get("platform") or "feishu"),
                            "open_id": identity["open_id"],
                            "user_id_type": "open_id",
                        },
                    }
                )
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
                normalized_table_key = table_key.lower().replace("-", "_").replace(" ", "_")
                mine_only = args.get("mine_only") is True
                is_task_table = normalized_table_key in {
                    "task",
                    "sub_task",
                    "subtask",
                }
                if mine_only and not is_task_table:
                    raise ValueError("mine_only 仅支持 task 和 sub_task 表")
                schema = None
                if mine_only or (is_task_table and not include_finished):
                    schema = await self.xiaot_bitable.fields(
                        table_key,
                        access_token=access_token,
                        platform=(requester or {}).get("platform", "feishu"),
                    )
                if mine_only:
                    owner_field = next(
                        (
                            str(item.get("field_name") or "")
                            for item in schema or []
                            if str(item.get("field_name") or "").strip().casefold()
                            in {"owner", "负责人"}
                            and (
                                str(item.get("ui_type") or "").strip().casefold() == "user"
                                or item.get("type") == 11
                            )
                        ),
                        "",
                    )
                    if not owner_field:
                        raise RuntimeError(
                            f"{table_key} 表中没有可用于精确匹配当前发起人的 Owner/负责人人员字段"
                        )
                    if not requester or not requester.get("open_id"):
                        raise RuntimeError("当前请求没有已验证的发起人 ID，无法查询本人任务")
                if is_task_table and not include_finished:
                    status_field = next(
                        (
                            str(item.get("field_name") or "")
                            for item in schema or []
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
                requested_page_size = max(1, min(int(args.get("page_size") or 100), 500))
                if mine_only:
                    data = await self._search_records_owned_by_requester(
                        table_key=table_key,
                        access_token=access_token,
                        platform=(requester or {}).get("platform", "feishu"),
                        owner_field=owner_field,
                        requester_open_id=str((requester or {}).get("open_id") or ""),
                        page_size=requested_page_size,
                        page_token=str(args.get("page_token") or ""),
                        filter_formula=filter_formula,
                    )
                else:
                    data = await self.xiaot_bitable.records(
                        table_key,
                        access_token=access_token,
                        platform=(requester or {}).get("platform", "feishu"),
                        page_size=requested_page_size,
                        page_token=str(args.get("page_token") or ""),
                        filter_formula=filter_formula,
                    )
                return result(
                    {
                        "success": True,
                        "table_key": args.get("table_key"),
                        "mine_only": mine_only,
                        "items": data.get("items") or [],
                        "has_more": bool(data.get("has_more")),
                        "page_token": data.get("page_token") or "",
                    }
                )
            if name == "search_person_candidates":
                return result(
                    await self.xiaot_bitable.search_person_candidates(
                        str(args.get("table_key") or ""),
                        str(args.get("query") or ""),
                        access_token=access_token,
                        platform=(requester or {}).get("platform", "feishu"),
                        filter_formula=str(args.get("filter_formula") or ""),
                    )
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
            or previous_sender.get("app_key") != current_sender.get("app_key")
        ):
            raise ValueError(
                "stale request_id belongs to a different sender; refusing to cross user contexts"
            )
        return current

    async def _user_access_token_for_run(
        self, request_id: str, conversation_key: str
    ) -> str:
        identity = await self._requester_for_run(request_id, conversation_key)
        cached = await self._xiaot_user_token(
            platform=identity["platform"],
            app_key=self._oauth_app_key(identity["platform"], identity.get("app_key")),
            open_id=identity["open_id"],
        )
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
        if str(table.get("name") or "").strip().casefold() == "changelog":
            raise ValueError("小T不会写入 Changelog；操作记录由飞书系统自动生成")
        fields = args.get("fields") if isinstance(args.get("fields"), dict) else {}
        record_id = str(args.get("record_id") or "").strip()
        before: dict[str, Any] = {}
        if operation == "create":
            fields = await self.xiaot_bitable.validate_writable_fields(
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
                fields = await self.xiaot_bitable.validate_writable_fields(
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
            "audit_note": "操作记录由飞书系统自动生成；小T不会写入 Changelog。",
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
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if actual_value == expected_value:
                continue
            if isinstance(expected_value, list) and all(
                isinstance(item, str) and re.fullmatch(r"rec[A-Za-z0-9_-]{1,80}", item)
                for item in expected_value
            ):
                actual_ids: list[str] = []
                if not isinstance(actual_value, list):
                    return False
                for item in actual_value:
                    if isinstance(item, str):
                        actual_ids.append(item)
                    elif isinstance(item, dict) and item.get("record_id"):
                        actual_ids.append(str(item["record_id"]))
                    else:
                        return False
                if actual_ids == expected_value:
                    continue
            return False
        return True

    @staticmethod
    def _is_missing_bitable_record(exc: Exception) -> bool:
        if isinstance(exc, XiaotBitableAPIError) and exc.api_code == 1254043:
            return True
        return re.search(
            r"\bRecordIdNotFound\b|\b1254043\b|record[_ ]id does not exist|record not found|记录不存在",
            str(exc),
            re.IGNORECASE,
        ) is not None

    @staticmethod
    def _is_bitable_data_not_ready(exc: Exception) -> bool:
        if isinstance(exc, XiaotBitableAPIError) and exc.api_code == 1254607:
            return True
        return re.search(r"\bData not ready\b|1254607", str(exc), re.IGNORECASE) is not None

    async def _read_record_after_data_not_ready(
        self,
        table_key: str,
        record_id: str,
        *,
        access_token: str,
        platform: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for delay in XIAOT_DELETE_VERIFY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.xiaot_bitable.record(
                    table_key,
                    record_id,
                    access_token=access_token,
                    platform=platform,
                )
            except Exception as exc:
                if not self._is_bitable_data_not_ready(exc):
                    raise
                last_error = exc
        raise RuntimeError(
            "Bitable is still processing a previous write; could not safely recheck the target record"
        ) from last_error

    async def _delete_record_and_verify(
        self,
        table_key: str,
        record_id: str,
        *,
        expected_fields: dict[str, Any],
        access_token: str,
        platform: str,
    ) -> dict[str, Any]:
        """Trust an exact success response; poll the target ID only if ambiguous."""
        write_error: Exception | None = None
        try:
            result = await self.xiaot_bitable.delete_record(
                table_key,
                record_id,
                access_token=access_token,
                platform=platform,
            )
            if (
                isinstance(result, dict)
                and result.get("deleted") is True
                and str(result.get("record_id") or "") == record_id
            ):
                # The DELETE response itself confirms the exact target row;
                # avoid a second read that can be slow or temporarily stale.
                return result
            write_error = RuntimeError(
                "Bitable delete response did not confirm deletion of the target record"
            )
        except Exception as exc:
            # A write error does not prove that the remote mutation was not applied.
            write_error = exc

        last_record: dict[str, Any] | None = None
        last_read_error: Exception | None = None
        may_retry_delete = bool(
            write_error and self._is_bitable_data_not_ready(write_error)
        )
        retried_delete = False
        for delay in XIAOT_DELETE_VERIFY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                current = await self.xiaot_bitable.record(
                    table_key,
                    record_id,
                    access_token=access_token,
                    platform=platform,
                )
            except Exception as exc:
                if self._is_missing_bitable_record(exc):
                    return {"record_id": record_id, "deleted": True}
                last_record = None
                last_read_error = exc
                continue
            if not isinstance(current, dict) or str(current.get("record_id") or "") != record_id:
                last_record = None
                last_read_error = RuntimeError(
                    "Bitable read-back did not identify the requested record"
                )
                continue
            last_record = current
            last_read_error = None
            if (
                may_retry_delete
                and not retried_delete
                and current.get("fields") == expected_fields
            ):
                # Feishu documents Data not ready (1254607) as retryable. Only
                # retry after a direct record read proves the exact target is
                # still present and unchanged; never scan the table or delete
                # a concurrently modified row.
                retried_delete = True
                try:
                    retry_result = await self.xiaot_bitable.delete_record(
                        table_key,
                        record_id,
                        access_token=access_token,
                        platform=platform,
                    )
                    if (
                        isinstance(retry_result, dict)
                        and retry_result.get("deleted") is True
                        and str(retry_result.get("record_id") or "") == record_id
                    ):
                        return retry_result
                    write_error = RuntimeError(
                        "Bitable delete retry response did not confirm the target record"
                    )
                except Exception as exc:
                    write_error = exc
                may_retry_delete = False

        if last_record is not None:
            if last_record.get("fields") != expected_fields:
                detail = "record still exists but changed after confirmation; deletion was not retried"
            else:
                detail = "record still exists after delete and repeated read-back verification"
            if write_error:
                detail += f"; delete API error: {_safe_error(write_error)}"
            raise RuntimeError(detail) from write_error

        detail = "delete was sent, but its outcome could not be verified from Bitable read-back"
        if write_error:
            detail += f"; delete API error: {_safe_error(write_error)}"
        if last_read_error:
            detail += f"; read-back error: {_safe_error(last_read_error)}"
        raise RuntimeError(detail) from (write_error or last_read_error)

    async def _requester_for_run(
        self, request_id: str, conversation_key: str
    ) -> dict[str, str]:
        """Resolve the platform-scoped, verified requester for this exact run."""
        await self._ensure_xiaot_oauth_schema()
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
        row = await _db_first(
            self.state.db,
            "SELECT sender_open_id FROM feishu_events "
            "WHERE request_id = ? AND conversation_key = ?",
            request_id,
            conversation_key,
        )
        if not row or not row.get("sender_open_id"):
            raise ValueError("the current 小 T run has no verified Feishu requester")
        return {
            "platform": "feishu",
            "app_key": "feishu",
            "tenant_key": "",
            "open_id": str(row["sender_open_id"]),
        }

    @staticmethod
    def _is_explicit_confirmation(value: str, operation: str) -> bool:
        raw_text = str(value or "").strip().casefold()
        text = re.sub(r"[\s，。！,.!；;：:、]+", "", raw_text)
        if not text or len(text) > 100:
            return False
        if operation not in {"create", "update", "delete"}:
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
            "不需要",
            "不必",
            "不想",
            "不能",
            "不确定",
            "不同意",
            "不批准",
            "不认可",
            "未确认",
            "暂不",
            "先不",
            "先别",
            "拒绝",
            "取消",
            "撤销",
            "不要",
            "别执行",
            "别开始",
            "别做",
            "算了",
            "停止",
            "先等等",
            "再想想",
        )
        english_reply = re.sub(r"\bno\s+problem\b", "", raw_text)
        if any(token in text for token in refusals) or re.search(
            r"(?<![a-z])(?:no|nope|nah|not|don't|dont|do not|can't|cannot|cancel|reject|stop)(?![a-z])",
            english_reply,
        ):
            return False
        operation_words = {
            "create": ("确认新增", "确认创建", "确认添加", "新增", "创建", "添加"),
            "update": ("确认更新", "确认修改", "更新", "修改"),
            "delete": ("确认删除", "删除"),
        }[operation]
        other_operation_words = {
            "create": ("删除", "更新", "修改"),
            "update": ("新增", "创建", "添加", "删除"),
            "delete": ("新增", "创建", "添加", "更新", "修改"),
        }
        if any(word in text for word in other_operation_words[operation]):
            return False
        positive = (
            "没有问题",
            "没问题",
            "noproblem",
            "没事",
            "没意见",
            "没有意见",
            "无异议",
            "确认执行",
            "开始执行",
            "可以执行",
            "同意执行",
            "立即执行",
            "马上执行",
            "确认",
            "执行",
            "开始",
            "可以",
            "好的",
            "接受",
            "行",
            "好",
            "嗯",
            "恩",
            "是",
            "同意",
            "赞成",
            "支持",
            "批准",
            "通过",
            "确定",
            "照办",
            "照做",
            "继续",
            "成",
            "妥",
            "ok",
            "okay",
            "yes",
            "yep",
            "sure",
            "surething",
            "allgood",
            "yesplease",
            "goahead",
            "proceed",
            "soundsgood",
            *operation_words,
        )
        alternatives = "|".join(
            re.escape(word) for word in sorted(set(positive), key=len, reverse=True)
        )
        fillers = "嗯|恩|那就|那|就|我觉得|我认为|我同意|我确认|我|这就|当然|马上|立即|麻烦|请"
        particles = "的|啊|呀|吧|呢|哈|啦|嘞|哒|了|！|。"
        return re.fullmatch(
            rf"(?:(?:{fillers})*(?:{alternatives})(?:{particles})?)+", text
        ) is not None

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
            if operation == "delete":
                latest = await self._read_record_after_data_not_ready(
                    table_key,
                    record_id,
                    access_token=access_token,
                    platform=identity["platform"],
                )
            else:
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
                record = await self._delete_record_and_verify(
                    table_key,
                    record_id,
                    expected_fields=before.get("fields")
                    if isinstance(before.get("fields"), dict)
                    else {},
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
            raise
        return {
            "success": True,
            "verified": True,
            "operation": operation,
            "table_key": table_key,
            "record_id": record_id or record.get("record_id"),
            "record": record if operation != "delete" else None,
            "deleted": operation == "delete",
        }

    async def handle_xiaot_event(self, request: Any) -> Response:
        if str(request.method or "").upper() != "POST":
            return _response({"error": "method_not_allowed"}, 405, {"allow": "POST"})
        body = await self._body_json(request)
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        # 小T's Feishu event callback intentionally does not require a
        # Verification Token. URL verification is completed by echoing the
        # challenge, while event deliveries are acknowledged and queued.
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
            "(request_id, conversation_key, platform, open_id, app_key, "
            "tenant_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
            "conversation_key=excluded.conversation_key, platform=excluded.platform, "
            "open_id=excluded.open_id, app_key=excluded.app_key, "
            "tenant_key=excluded.tenant_key",
            request_id,
            conversation_key,
            identity["platform"],
            identity["open_id"],
            identity["app_key"],
            identity.get("tenant_key") or "",
            int(time.time()),
        )
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


class XiaotOnlyCloudflareRelay(XiaotCloudflareRelay):
    """Isolated DEV MCP surface for 小 T; the shared /mcp remains untouched."""

    _allowed_tools = XIAOT_TOOL_NAMES | _RELAY_TOOLS

    def mcp_name(self) -> str:
        return XIAOT_MCP_NAME

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in self._allowed_tools:
            return self._tool_result(
                {
                    "success": False,
                    "error": {
                        "code": "action_not_enabled_for_agent",
                        "message": "This action is not enabled for the isolated 小 T MCP.",
                    },
                },
                True,
            )
        if name == "server_info":
            return self._tool_result(
                {
                    "success": True,
                    "app_name": XIAOT_MCP_NAME,
                    "version": "3.0.0",
                    "public_base_url": self.base_url(),
                    "mcp_path": XIAOT_MCP_PATH,
                    "storage": "D1",
                    "queue": "XIAOT_AGENT_QUEUE",
                    "table_count": len(XIAOT_TABLES),
                }
            )
        return await super().call_tool(name, args)


class CombinedCloudflareRelay(CloudflareRelay):
    """Expose 小 C and 小 T actions through the existing DEV MCP endpoint."""

    def __init__(self, env: Any, ctx: Any, db_state: D1State) -> None:
        super().__init__(env, ctx, db_state)
        xiaot_db = getattr(env, "XIAOT_DB", None)
        xiaot_state = D1State(xiaot_db) if xiaot_db is not None else db_state
        self.xiaot = XiaotCloudflareRelay(XiaotEnvironment(env), ctx, xiaot_state)

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
