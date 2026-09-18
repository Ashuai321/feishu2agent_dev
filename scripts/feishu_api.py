"""Minimal Feishu/Lark HTTP helpers used by the standalone test scripts.

The helpers keep platform endpoints and user tokens separate while preserving
the existing Feishu defaults.  They cover sending a bot message and writing
one user-authorized Bitable record.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FEISHU_BASE_URL = "https://open.feishu.cn"
FEISHU_OAUTH_REDIRECT_URI = "https://bot.boooe.com/feishu/oauth/callback"


@dataclass(frozen=True)
class PlatformEndpoints:
    name: str
    api_base: str
    auth_base: str
    oauth_redirect_uri: str
    app_id_env: str
    app_secret_env: str
    user_token_env: str
    user_token_file: str


def platform_endpoints(platform: str = "feishu") -> PlatformEndpoints:
    """Return the API/auth endpoints and token names for one platform."""
    name = str(platform or "feishu").strip().lower()
    if name == "feishu":
        return PlatformEndpoints(
            "feishu",
            "https://open.feishu.cn",
            "https://accounts.feishu.cn",
            FEISHU_OAUTH_REDIRECT_URI,
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_USER_ACCESS_TOKEN",
            ".feishu-user-token.json",
        )
    if name == "lark":
        return PlatformEndpoints(
            "lark",
            "https://open.larksuite.com",
            "https://accounts.larksuite.com",
            "https://bot.boooe.com/lark/oauth/callback",
            "LARK_APP_ID",
            "LARK_APP_SECRET",
            "LARK_USER_ACCESS_TOKEN",
            ".lark-user-token.json",
        )
    raise FeishuScriptError(f"不支持的平台：{platform}，请使用 feishu 或 lark")


class FeishuScriptError(RuntimeError):
    """A safe, user-facing error from a standalone Feishu script."""


def _ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context from the bundled CA certificate set."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overriding real environment vars.

    The scripts are often started from the user's home directory.  When the
    default path is used, also check the project root next to this scripts
    package so ``python /absolute/path/scripts/...`` behaves like a command run
    after ``cd`` into the project.
    """
    candidates = [Path(path)]
    if str(path) == ".env":
        candidates.append(Path(__file__).resolve().parents[1] / ".env")
    dotenv = next((candidate for candidate in candidates if candidate.exists()), None)
    if dotenv is None:
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
    base_url: str = FEISHU_BASE_URL,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
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
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
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


def get_tenant_access_token(
    app_id: str, app_secret: str, *, base_url: str = FEISHU_BASE_URL
) -> str:
    data = _request_json(
        "POST",
        "/open-apis/auth/v3/tenant_access_token/internal",
        payload={"app_id": app_id, "app_secret": app_secret},
        base_url=base_url,
    )
    token = str(data.get("tenant_access_token") or "")
    if not token:
        raise FeishuScriptError("飞书没有返回 tenant_access_token")
    return token


def exchange_user_access_token(
    app_id: str,
    app_secret: str,
    code: str,
    state: str = "",
    redirect_uri: str = FEISHU_OAUTH_REDIRECT_URI,
    auth_base: str = "https://accounts.feishu.cn",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": app_id,
        "client_secret": app_secret,
        "redirect_uri": redirect_uri,
    }
    url = auth_base.rstrip("/") + "/oauth/v3/token"
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20, context=_ssl_context()
        ) as response:
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
        message = (
            data.get("error_description")
            or data.get("msg")
            or data.get("message")
            or "未知错误"
        )
        raise FeishuScriptError(f"飞书 API 错误 code={data.get('code', status)}: {message}")
    result = data.get("data") if isinstance(data.get("data"), dict) else data
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
    base_url: str = FEISHU_BASE_URL,
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
        base_url=base_url,
    )
    message_id = str((data.get("data") or {}).get("message_id") or "")
    if not message_id:
        raise FeishuScriptError("消息已请求发送，但飞书没有返回 message_id")
    return message_id


def resolve_wiki_bitable_app_token(
    user_token: str, wiki_token: str, *, base_url: str = FEISHU_BASE_URL
) -> str:
    data = _request_json(
        "GET",
        "/open-apis/wiki/v2/spaces/get_node",
        token=user_token,
        query={"token": wiki_token},
        base_url=base_url,
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


def get_bitable_fields(
    user_token: str,
    app_token: str,
    table_id: str,
    *,
    base_url: str = FEISHU_BASE_URL,
) -> list[dict[str, Any]]:
    data = _request_json(
        "GET",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/fields",
        token=user_token,
        query={"page_size": 100},
        base_url=base_url,
    )
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        raise FeishuScriptError("字段列表响应格式不正确")
    return [item for item in items if isinstance(item, dict)]


def get_bitable_records(
    user_token: str,
    app_token: str,
    table_id: str,
    *,
    base_url: str = FEISHU_BASE_URL,
) -> list[dict[str, Any]]:
    data = _request_json(
        "GET",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/records",
        token=user_token,
        query={"page_size": 100},
        base_url=base_url,
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
    base_url: str = FEISHU_BASE_URL,
) -> dict[str, Any]:
    data = _request_json(
        "POST",
        f"/open-apis/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/tables/"
        f"{urllib.parse.quote(table_id, safe='')}/records",
        token=user_token,
        payload={"fields": fields},
        base_url=base_url,
    )
    record = (data.get("data") or {}).get("record")
    if not isinstance(record, dict):
        raise FeishuScriptError("创建记录成功响应中没有 record")
    return record


def get_user_info(
    user_token: str, *, base_url: str = FEISHU_BASE_URL
) -> dict[str, Any]:
    data = _request_json(
        "GET", "/open-apis/authen/v1/user_info", token=user_token, base_url=base_url
    )
    result = data.get("data")
    if not isinstance(result, dict):
        raise FeishuScriptError("当前用户信息响应格式不正确")
    return result


def get_bot_info(
    app_id: str,
    app_secret: str,
    *,
    base_url: str = FEISHU_BASE_URL,
) -> dict[str, Any]:
    """Return the bot identity for one platform application.

    This small diagnostic call is deliberately separate from ``get_user_info``:
    the former proves that the selected Feishu/Lark bot credentials are being
    used, while the latter proves which human user authorized the operation.
    """
    tenant_token = get_tenant_access_token(app_id, app_secret, base_url=base_url)
    data = _request_json(
        "GET", "/open-apis/bot/v3/info", token=tenant_token, base_url=base_url
    )
    bot = data.get("bot")
    if not isinstance(bot, dict) or not bot.get("open_id"):
        raise FeishuScriptError("机器人身份响应中没有 open_id")
    return bot


def list_chat_members(
    tenant_token: str,
    chat_id: str,
    *,
    base_url: str = FEISHU_BASE_URL,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """List the members visible to the platform bot in one group."""
    result: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query = {
            "member_id_type": "open_id",
            "page_size": max(1, min(page_size, 100)),
        }
        if page_token:
            query["page_token"] = page_token
        data = _request_json(
            "GET",
            f"/open-apis/im/v1/chats/{urllib.parse.quote(chat_id, safe='')}/members",
            token=tenant_token,
            query=query,
            base_url=base_url,
        )
        payload = data.get("data") or {}
        items = payload.get("items") or [] if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise FeishuScriptError("群成员响应格式不正确")
        for item in items:
            if isinstance(item, dict):
                result.append(item)
        has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
        next_token = str(payload.get("page_token") or "") if isinstance(payload, dict) else ""
        if not has_more or not next_token or next_token == page_token:
            break
        page_token = next_token
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
