"""Minimal Feishu HTTP helpers used by the standalone test scripts.

The production Worker and the existing bot pipeline are intentionally left
unchanged.  These helpers only cover the two explicit test actions: sending a
bot message and writing one user-authorized Bitable record.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FEISHU_BASE_URL = "https://open.feishu.cn"


class FeishuScriptError(RuntimeError):
    """A safe, user-facing error from a standalone Feishu script."""


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overriding real environment vars."""
    dotenv = Path(path)
    if not dotenv.exists():
        return
    for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise FeishuScriptError(f"缺少环境变量 {name}，请写入 .env 或当前终端环境")
    return value


def _request_json(
    method: str,
    path: str,
    *,
    token: str = "",
    query: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    url = FEISHU_BASE_URL.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise FeishuScriptError(f"请求飞书失败：{exc.reason}") from exc
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuScriptError(f"飞书返回了无法解析的响应（HTTP {status}）") from exc
    if not isinstance(data, dict):
        raise FeishuScriptError(f"飞书返回格式不正确（HTTP {status}）")
    if status >= 400 or data.get("code", 0) not in (0, None):
        code = data.get("code", status)
        message = data.get("msg") or data.get("message") or "未知错误"
        raise FeishuScriptError(f"飞书 API 错误 code={code}: {message}")
    return data


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    data = _request_json(
        "POST",
        "/open-apis/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    token = str(data.get("tenant_access_token") or "")
    if not token:
        raise FeishuScriptError("飞书没有返回 tenant_access_token")
    return token


def exchange_user_access_token(
    app_id: str, app_secret: str, code: str, state: str = ""
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
    }
    if state:
        payload["state"] = state
    data = _request_json(
        "POST",
        "/open-apis/authen/v2/oauth/token",
        payload=payload,
    )
    result = data.get("data")
    if not isinstance(result, dict) or not result.get("access_token"):
        raise FeishuScriptError("飞书没有返回 user_access_token")
    return result


def send_text_mention(
    tenant_token: str,
    *,
    chat_id: str,
    user_open_id: str,
    user_name: str,
    text: str,
) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    content = f'<at user_id="{user_open_id}">{user_name}</at> {escaped}'
    data = _request_json(
        "POST",
        "/open-apis/im/v1/messages",
        token=tenant_token,
        query={"receive_id_type": "chat_id"},
        payload={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": content}, ensure_ascii=False),
        },
    )
    message_id = str((data.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise FeishuScriptError("消息已请求发送，但飞书没有返回 message_id")
    return message_id


def resolve_wiki_bitable_app_token(user_token: str, wiki_token: str) -> str:
    data = _request_json(
        "GET",
        "/open-apis/wiki/v2/spaces/get_node",
        token=user_token,
        query={"token": wiki_token},
    )
    node = (data.get("data") or {}).get("node") or {}
    if not isinstance(node, dict):
        raise FeishuScriptError("Wiki 节点响应缺少 node")
    app_token = str(node.get("obj_token") or node.get("token") or "")
    obj_type = str(node.get("obj_type") or "")
    if not app_token:
        raise FeishuScriptError("Wiki 节点没有返回多维表格 obj_token")
    if obj_type and obj_type not in {"bitable", "sheet"}:
        raise FeishuScriptError(f"Wiki 节点类型是 {obj_type}，不是多维表格")
    return app_token


def get_bitable_fields(user_token: str, app_token: str, table_id: str) -> list[dict[str, Any]]:
    data = _request_json(
        "GET",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/fields",
        token=user_token,
        query={"page_size": 100},
    )
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        raise FeishuScriptError("字段列表响应格式不正确")
    return [item for item in items if isinstance(item, dict)]


def get_bitable_records(user_token: str, app_token: str, table_id: str) -> list[dict[str, Any]]:
    data = _request_json(
        "GET",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/records",
        token=user_token,
        query={"page_size": 100},
    )
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        raise FeishuScriptError("记录列表响应格式不正确")
    return [item for item in items if isinstance(item, dict)]


def create_bitable_record(
    user_token: str,
    *,
    app_token: str,
    table_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    data = _request_json(
        "POST",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/records",
        token=user_token,
        payload={"fields": fields},
    )
    record = (data.get("data") or {}).get("record")
    if not isinstance(record, dict):
        raise FeishuScriptError("创建记录成功响应中没有 record")
    return record


def get_user_info(user_token: str) -> dict[str, Any]:
    data = _request_json("GET", "/open-apis/authen/v1/user_info", token=user_token)
    result = data.get("data")
    if not isinstance(result, dict):
        raise FeishuScriptError("当前用户信息响应格式不正确")
    return result


def parse_date_to_millis(value: str) -> int:
    raw = value.strip()
    parsed: datetime
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.combine(
                date.fromisoformat(raw), time.min, tzinfo=ZoneInfo("Asia/Shanghai")
            )
        except ValueError as exc:
            raise FeishuScriptError(
                f"日期格式不正确：{value}，请使用 YYYY-MM-DD 或 ISO datetime"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)
