# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from workers import Response, WorkerEntrypoint

PUBLIC_BASE_URL = "https://bot.boooe.com"
MCP_PATH = "/mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_NAME = "workspace-agent-relay-mcp-prd"
PLACEHOLDER = "正在处理，Agent 完成后会回复到这条消息。"
MAX_CLIENTS = 50


SCHEMA = """
CREATE TABLE IF NOT EXISTS feishu_events (
    message_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    conversation_key TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    sender_open_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_runs (
    request_id TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    placeholder_message_id TEXT,
    input_markdown TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    title TEXT,
    markdown TEXT,
    steps_json TEXT NOT NULL DEFAULT '[]',
    image_keys_json TEXT NOT NULL DEFAULT '[]',
    progress_message TEXT,
    trigger_status INTEGER,
    trigger_error TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_relay_runs_conversation
    ON relay_runs(conversation_key, created_at DESC);
CREATE TABLE IF NOT EXISTS requesters (
    conversation_key TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    source_chat_id TEXT NOT NULL DEFAULT '',
    group_chat_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_conversations (
    outbound_message_id TEXT PRIMARY KEY,
    conversation_key TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS avatar_objects (
    conversation_key TEXT PRIMARY KEY,
    object_key TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    access_token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


def _native(value: Any) -> Any:
    """Convert a Pyodide JsProxy into ordinary Python data."""
    converter = getattr(value, "to_py", None)
    if converter is not None:
        try:
            return converter()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(v) for v in value]
    return value


def _queue_payload(value: Any) -> dict[str, Any] | None:
    """Normalize a Queue message body across Python Workers runtimes.

    Depending on the Workers runtime version, a JSON body sent by ``queue.send``
    can arrive as a normal mapping, a JsProxy, or an encoded JSON string/bytes.
    Treating the latter as a non-dict silently acknowledges the message and
    leaves its D1 run permanently queued, so decode all supported forms here.
    """
    value = _native(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    value = _native(value)
    return value if isinstance(value, dict) else None


def _env(env: Any, name: str, default: str = "") -> str:
    value = getattr(env, name, default)
    return str(_native(value) or default).strip()


async def _db_run(db: Any, sql: str, *params: Any) -> Any:
    statement = db.prepare(sql)
    if params:
        statement = statement.bind(*params)
    return await statement.run()


async def _db_first(db: Any, sql: str, *params: Any) -> dict[str, Any] | None:
    statement = db.prepare(sql)
    if params:
        statement = statement.bind(*params)
    value = await statement.first()
    if value is None:
        return None
    converted = _native(value)
    if isinstance(converted, dict):
        return converted
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): _native(getattr(value, str(key))) for key in keys()}
    return None


async def _db_all(db: Any, sql: str, *params: Any) -> list[dict[str, Any]]:
    statement = db.prepare(sql)
    if params:
        statement = statement.bind(*params)
    result = await statement.all()
    rows = _native(getattr(result, "results", result))
    return rows if isinstance(rows, list) else []


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> int:
    return int(time.time())


def _response(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    merged = {"cache-control": "no-store"}
    if headers:
        merged.update(headers)
    return Response.json(payload, status=status, headers=merged)


def _text_response(body: str, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    merged = {"cache-control": "no-store"}
    if headers:
        merged.update(headers)
    return Response(body, status=status, headers=merged)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _scope_set(value: str) -> set[str]:
    return {item for item in value.split() if item}


def _allowed_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    return bool(
        (parsed.scheme == "https" and parsed.netloc)
        or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
    )


def _safe_error(value: Any, secret: str = "") -> str:
    text = str(value or "").strip()
    return text.replace(secret, "[REDACTED]") if secret else text


def _is_non_card_update_error(value: Any) -> bool:
    """Return whether Feishu rejected an update because the message is text.

    Feishu's message update endpoint only accepts card messages.  Older relay
    runs may still have a normal text placeholder, so a completed Agent result
    must fall back to a new reply when this error is returned instead of
    retrying the same impossible PATCH forever.
    """
    return "this message is not a card" in str(value or "").lower()


class D1State:
    def __init__(self, db: Any) -> None:
        self.db = db
        self._schema_ready = False

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        # Migrations are committed to the repository for repeatable deploys;
        # this fallback also makes a newly created database self-healing when a
        # dashboard deployment did not run migrations yet.
        for statement in SCHEMA.split(";"):
            statement = statement.strip()
            if statement:
                await _db_run(self.db, statement)
        self._schema_ready = True

    async def claim_event(
        self,
        *,
        message_id: str,
        request_id: str,
        conversation_key: str,
        chat_id: str,
        open_id: str,
    ) -> bool:
        result = await _db_run(
            self.db,
            """INSERT OR IGNORE INTO feishu_events
               (message_id, request_id, conversation_key, source_chat_id,
                sender_open_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            message_id,
            request_id,
            conversation_key,
            chat_id,
            open_id,
            _now(),
        )
        meta = _native(getattr(result, "meta", {}))
        return bool(isinstance(meta, dict) and meta.get("changes", 0))

    async def requester(self, conversation_key: str) -> dict[str, Any] | None:
        return await _db_first(
            self.db,
            "SELECT conversation_key, open_id, name, source_chat_id, group_chat_id, created_at "
            "FROM requesters WHERE conversation_key = ?",
            conversation_key,
        )

    async def save_requester(
        self,
        conversation_key: str,
        open_id: str,
        name: str,
        chat_id: str,
    ) -> None:
        await _db_run(
            self.db,
            """INSERT INTO requesters
               (conversation_key, open_id, name, source_chat_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_key) DO UPDATE SET
                 open_id=excluded.open_id, name=excluded.name,
                 source_chat_id=excluded.source_chat_id, updated_at=excluded.updated_at""",
            conversation_key,
            open_id,
            name,
            chat_id,
            _now(),
            _now(),
        )

    async def bind_group(self, conversation_key: str, chat_id: str) -> None:
        await _db_run(
            self.db,
            "UPDATE requesters SET group_chat_id = ?, updated_at = ? WHERE conversation_key = ?",
            chat_id,
            _now(),
            conversation_key,
        )

    async def reply_conversation(self, outbound_id: str) -> str | None:
        row = await _db_first(
            self.db,
            "SELECT conversation_key FROM reply_conversations WHERE outbound_message_id = ?",
            outbound_id,
        )
        return str(row["conversation_key"]) if row else None

    async def save_reply(self, outbound_id: str, conversation_key: str) -> None:
        await _db_run(
            self.db,
            "INSERT OR IGNORE INTO reply_conversations "
            "(outbound_message_id, conversation_key, created_at) VALUES (?, ?, ?)",
            outbound_id,
            conversation_key,
            _now(),
        )

    async def create_run(
        self,
        *,
        request_id: str,
        conversation_key: str,
        source_message_id: str,
        input_markdown: str,
        image_keys: list[str] | None = None,
    ) -> None:
        await _db_run(
            self.db,
            """INSERT INTO relay_runs
               (request_id, conversation_key, source_message_id, input_markdown,
                image_keys_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
            request_id,
            conversation_key,
            source_message_id,
            input_markdown,
            _json(image_keys or []),
            _now(),
            _now(),
        )

    async def get_run(self, request_id: str) -> dict[str, Any] | None:
        row = await _db_first(self.db, "SELECT * FROM relay_runs WHERE request_id = ?", request_id)
        if row and isinstance(row.get("steps_json"), str):
            try:
                row["steps"] = json.loads(row["steps_json"])
            except json.JSONDecodeError:
                row["steps"] = []
        if row and isinstance(row.get("image_keys_json"), str):
            try:
                image_keys = json.loads(row["image_keys_json"])
                row["image_keys"] = image_keys if isinstance(image_keys, list) else []
            except json.JSONDecodeError:
                row["image_keys"] = []
        return row

    async def update_run(self, request_id: str, **fields: Any) -> None:
        allowed = {
            "placeholder_message_id",
            "status",
            "title",
            "markdown",
            "steps_json",
            "progress_message",
            "trigger_status",
            "trigger_error",
            "delivered",
            "completed_at",
        }
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await _db_run(
            self.db,
            f"UPDATE relay_runs SET {assignments}, updated_at = ? WHERE request_id = ?",
            *fields.values(),
            _now(),
            request_id,
        )

    async def recent_runs(self, conversation_key: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = await _db_all(
            self.db,
            "SELECT request_id, conversation_key, status, title, markdown, progress_message, "
            "created_at, updated_at, completed_at FROM relay_runs "
            "WHERE conversation_key = ? ORDER BY created_at DESC LIMIT ?",
            conversation_key,
            max(1, min(int(limit), 20)),
        )
        return rows

    async def previous_run_exists(self, conversation_key: str) -> bool:
        row = await _db_first(
            self.db,
            "SELECT request_id FROM relay_runs WHERE conversation_key = ? LIMIT 1",
            conversation_key,
        )
        return row is not None

    async def save_avatar(self, conversation_key: str, object_key: str, size: int) -> None:
        await _db_run(
            self.db,
            "INSERT INTO avatar_objects(conversation_key, object_key, size, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_key) DO UPDATE SET "
            "object_key=excluded.object_key, size=excluded.size, updated_at=excluded.updated_at",
            conversation_key,
            object_key,
            size,
            _now(),
        )

    async def avatar(self, conversation_key: str) -> dict[str, Any] | None:
        return await _db_first(
            self.db,
            "SELECT object_key, size, updated_at FROM avatar_objects WHERE conversation_key = ?",
            conversation_key,
        )

    async def oauth_client(self, client_id: str) -> dict[str, Any] | None:
        return await _db_first(
            self.db, "SELECT * FROM oauth_clients WHERE client_id = ?", client_id
        )


class FeishuAPI:
    def __init__(self, env: Any) -> None:
        self.env = env
        self.base = _env(env, "FEISHU_API_BASE", "https://open.feishu.cn")
        self._token: str = ""
        self._token_expires = 0

    async def _tenant_token(self) -> str:
        if self._token and self._token_expires > _now() + 60:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": _env(self.env, "FEISHU_APP_ID"),
                    "app_secret": _env(self.env, "FEISHU_APP_SECRET"),
                },
            )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("Feishu token response did not contain tenant_access_token")
        self._token = token
        self._token_expires = _now() + int(payload.get("expire", 7200))
        return token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {await self._tenant_token()}"
        headers.setdefault("Content-Type", "application/json")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method, f"{self.base}{path}", headers=headers, **kwargs
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            message = payload.get("msg") or payload.get("message") or response.text
            raise RuntimeError(f"Feishu API {path} failed ({response.status_code}): {message}")
        return payload

    async def bot_open_id(self) -> str:
        payload = await self._request("GET", "/open-apis/bot/v3/info")
        value = payload.get("bot", {}).get("open_id")
        if not value:
            raise RuntimeError("Feishu bot identity returned no open_id")
        return str(value)

    async def reply(self, message_id: str, text: str) -> str:
        payload = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            params={"user_id_type": "open_id"},
            json={"msg_type": "text", "content": _json({"text": text})},
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu reply response returned no message_id")
        return str(outbound)

    @staticmethod
    def _card_content(text: str) -> str:
        # A card is required because Feishu's message PATCH endpoint cannot
        # edit ordinary text messages.  lark_md keeps the Agent's markdown
        # readable while allowing the same card to be updated in place.
        return _json(
            {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": text},
                    }
                ],
            }
        )

    async def reply_card(self, message_id: str, text: str) -> str:
        payload = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            params={"user_id_type": "open_id"},
            json={"msg_type": "interactive", "content": self._card_content(text)},
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu card reply response returned no message_id")
        return str(outbound)

    async def update(self, message_id: str, text: str) -> None:
        await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            params={"user_id_type": "open_id"},
            json={"msg_type": "text", "content": _json({"text": text})},
        )

    async def update_card(self, message_id: str, text: str) -> None:
        await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            params={"user_id_type": "open_id"},
            json={"msg_type": "interactive", "content": self._card_content(text)},
        )

    async def create_private_group(self, open_id: str, name: str | None = None) -> str:
        payload = await self._request(
            "POST",
            "/open-apis/im/v1/chats",
            params={"user_id_type": "open_id"},
            json={
                "name": name or "机器人与TA的私聊",
                "chat_mode": "group",
                "user_id_list": [open_id],
            },
        )
        chat_id = payload.get("data", {}).get("chat_id")
        if not chat_id:
            raise RuntimeError("Feishu create chat response returned no chat_id")
        return str(chat_id)

    async def download_image(self, message_id: str, file_key: str) -> bytes:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": "image"},
                headers={"Authorization": f"Bearer {await self._tenant_token()}"},
            )
        response.raise_for_status()
        return response.content


def _normalize_event(body: dict[str, Any], bot_open_id: str) -> dict[str, Any] | None:
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not message_id or not chat_id:
        return None
    if message.get("chat_type") != "group" or message.get("message_type") not in {"text", "image"}:
        return None
    if str(sender.get("sender_type") or "").lower() in {"app", "bot"}:
        return None
    content = message.get("content") or "{}"
    try:
        parsed_content = json.loads(content) if isinstance(content, str) else content
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed_content, dict):
        return None
    text = parsed_content.get("text", "")
    image_keys = []
    if message.get("message_type") == "image":
        image_key = parsed_content.get("image_key")
        if isinstance(image_key, str) and image_key:
            image_keys.append(image_key)
        text = "[用户发送了一张图片，已保存为必要文件]"
    if not isinstance(text, str):
        return None
    mentions = message.get("mentions") or []
    mentioned_bot = False
    for mention in mentions:
        mention_id = mention.get("id") if isinstance(mention, dict) else {}
        if isinstance(mention_id, dict) and mention_id.get("open_id") == bot_open_id:
            mentioned_bot = True
            key = mention.get("key") or ""
            if key:
                text = re.sub(re.escape(str(key)), "", text)
    text = text.strip()
    if not mentioned_bot or (not text and not image_keys):
        return None
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "open_id": str(sender_id.get("open_id") or ""),
        "name": str(sender.get("sender_id", {}).get("open_id") or ""),
        "parent_id": str(message.get("parent_id") or message.get("root_id") or ""),
        "text": text,
        "image_keys": image_keys,
        "tenant_key": str(header.get("tenant_key") or ""),
    }


def _request_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"feishu_{stamp}_{secrets.token_hex(6)}"


def _conversation_input(
    *,
    request_id: str,
    conversation_key: str,
    text: str,
    continuation: bool,
    working_directory: str = "",
) -> str:
    # The trigger API accepts ``input`` as a string. The Agent's protocol
    # parser expects the established text envelope: protocol header,
    # completion contract, and then the untrusted user task. A JSON string is
    # displayed as an ordinary chat prompt and does not enter Relay mode.
    turn_mode = "continuation" if continuation else "initial"
    header = [
        f"request_id: {request_id}",
        f"conversation_key: {conversation_key}",
        f"relay_mcp: {MCP_NAME}",
        "protocol: local-agent-shell/v1",
        f"turn_mode: {turn_mode}",
    ]
    if working_directory:
        header.append(f"working_directory: {working_directory.strip()}")
    if continuation:
        body = [
            "Same relay protocol as before: record_plan → record_progress(step_updates) → record_result, using the request_id above.",
            "Keep record_plan user-visible. If the relay tool is unavailable, still call record_progress/record_result so the operator is informed.",
            "",
            "User task:",
            text.strip(),
        ]
    else:
        body = [
            "Completion contract:",
            "The local operator CANNOT see your ChatGPT-side plan, tool calls, or reasoning. This relay is their only view of your work.",
            "This trigger starts ONE turn (one request_id scope). If the user corrects your direction mid-turn, revise the plan and do not use record_result to signal a plan change.",
            "After reading the user task, call update_conversation_title once for a new conversation, then record_plan with a user-visible step plan.",
            "After completing several steps, call record_progress with step_updates.",
            "Call record_result exactly once when this turn is truly over: status=done when delivered, status=failed on an execution error, status=blocked only for an external hard blocker.",
            "Do not only answer in the ChatGPT conversation.",
            "",
            "User task:",
            text.strip(),
        ]
    return "\n".join([*header, "", *body])


class CloudflareRelay:
    def __init__(self, env: Any, ctx: Any, db_state: D1State) -> None:
        self.env = env
        self.ctx = ctx
        self.state = db_state
        self.feishu = FeishuAPI(env)

    def base_url(self) -> str:
        return _env(self.env, "WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL", PUBLIC_BASE_URL).rstrip("/")

    def scopes(self) -> list[str]:
        return (
            _env(self.env, "WORKSPACE_AGENT_RELAY_OAUTH_SCOPES", "workspace-agent-relay").split()
        ) or ["workspace-agent-relay"]

    def auth_mode(self) -> str:
        configured = _env(self.env, "WORKSPACE_AGENT_RELAY_AUTH_MODE")
        if configured:
            return configured.lower()
        return "oauth" if _env(self.env, "WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN") else "none"

    async def authorize_request(self, request: Any) -> Response | None:
        if self.auth_mode() == "none":
            return None
        auth = str(request.headers.get("authorization") or "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        valid = False
        if self.auth_mode() == "shared_token":
            valid = bool(token) and hmac.compare_digest(
                token, _env(self.env, "WORKSPACE_AGENT_RELAY_AUTH_TOKEN")
            )
        elif token:
            row = await _db_first(
                self.state.db, "SELECT * FROM oauth_tokens WHERE access_token = ?", token
            )
            valid = bool(
                row
                and int(row.get("expires_at", 0)) >= _now()
                and row.get("resource") == self.base_url() + MCP_PATH
            )
        if valid:
            return None
        metadata = f"{self.base_url()}/.well-known/oauth-protected-resource/mcp"
        return _response(
            {"error": "unauthorized", "error_description": "MCP authorization required"},
            401,
            {"WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{metadata}"'},
        )

    async def oauth(self, request: Any, path: str) -> Response:
        base = self.base_url()
        if path in {
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
        }:
            return _response(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/oauth/authorize",
                    "token_endpoint": f"{base}/oauth/token",
                    "registration_endpoint": f"{base}/oauth/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": self.scopes(),
                }
            )
        if path in {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            return _response(
                {
                    "resource": base + MCP_PATH,
                    "authorization_servers": [base],
                    "scopes_supported": self.scopes(),
                    "bearer_methods_supported": ["header"],
                    "resource_name": MCP_NAME,
                }
            )
        if path == "/oauth/register" and request.method == "POST":
            payload = await self._body_json(request)
            redirect_uris = payload.get("redirect_uris")
            if (
                not isinstance(redirect_uris, list)
                or not redirect_uris
                or not all(isinstance(uri, str) and _allowed_redirect(uri) for uri in redirect_uris)
            ):
                return _response({"error": "invalid_client_metadata"}, 400)
            count = await _db_first(self.state.db, "SELECT COUNT(*) AS count FROM oauth_clients")
            if count and int(count.get("count", 0)) >= MAX_CLIENTS:
                return _response({"error": "too_many_clients"}, 429)
            client_id = "mcp_client_" + secrets.token_urlsafe(24)
            now = _now()
            await _db_run(
                self.state.db,
                "INSERT INTO oauth_clients(client_id, client_name, redirect_uris_json, created_at) VALUES (?, ?, ?, ?)",
                client_id,
                str(payload.get("client_name") or "ChatGPT"),
                _json(redirect_uris),
                now,
            )
            return _response(
                {
                    "client_id": client_id,
                    "client_name": str(payload.get("client_name") or "ChatGPT"),
                    "redirect_uris": redirect_uris,
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "client_id_issued_at": now,
                },
                201,
            )
        if path == "/oauth/authorize":
            params = (
                await self._body_params(request)
                if request.method == "POST"
                else self._query_params(request)
            )
            if request.method == "GET":
                hidden = "".join(
                    f'<input type="hidden" name="{key}" value="{str(value).replace(chr(34), "&quot;")}">'
                    for key, value in params.items()
                )
                html = (
                    "<!doctype html><meta charset=utf-8><title>Authorize</title>"
                    "<main style='font-family:system-ui;max-width:480px;margin:48px auto'>"
                    "<h1>Authorize MCP</h1><form method='post' action='/oauth/authorize'>"
                    f"{hidden}<label>Token <input name='login_token' type='password' autofocus></label> "
                    "<button>Authorize</button></form></main>"
                )
                return _text_response(html, headers={"content-type": "text/html; charset=utf-8"})
            if not hmac.compare_digest(
                str(params.get("login_token") or ""),
                _env(self.env, "WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN"),
            ):
                return _text_response("Invalid login token", 401)
            client = await self.state.oauth_client(str(params.get("client_id") or ""))
            redirect_uri = str(params.get("redirect_uri") or "")
            if not client:
                return _text_response("Unknown client", 400)
            try:
                redirects = json.loads(str(client.get("redirect_uris_json") or "[]"))
            except json.JSONDecodeError:
                redirects = []
            if redirect_uri not in redirects or params.get("response_type") != "code":
                return _text_response("Invalid authorization request", 400)
            if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
                return _text_response("PKCE S256 is required", 400)
            scope = str(params.get("scope") or " ".join(self.scopes()))
            if not _scope_set(scope).issubset(set(self.scopes())):
                return _text_response("Unsupported scope", 400)
            code = "mcp_code_" + secrets.token_urlsafe(32)
            await _db_run(
                self.state.db,
                "INSERT INTO oauth_codes(code, client_id, redirect_uri, code_challenge, scope, resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                code,
                str(params["client_id"]),
                redirect_uri,
                str(params["code_challenge"]),
                scope,
                base + MCP_PATH,
                _now() + 300,
            )
            query = {"code": code}
            if params.get("state"):
                query["state"] = str(params["state"])
            separator = "&" if "?" in redirect_uri else "?"
            location = redirect_uri + separator + urlencode(query)
            return _text_response("", 302, {"Location": location})
        if path == "/oauth/token" and request.method == "POST":
            params = await self._body_params(request)
            code = str(params.get("code") or "")
            row = await _db_first(self.state.db, "SELECT * FROM oauth_codes WHERE code = ?", code)
            if not row:
                return _response({"error": "invalid_grant"}, 400)
            await _db_run(self.state.db, "DELETE FROM oauth_codes WHERE code = ?", code)
            if int(row.get("expires_at", 0)) < _now() or params.get("client_id") != row.get(
                "client_id"
            ):
                return _response({"error": "invalid_grant"}, 400)
            if params.get("redirect_uri") != row.get("redirect_uri"):
                return _response({"error": "invalid_grant"}, 400)
            if not hmac.compare_digest(
                _pkce_s256(str(params.get("code_verifier") or "")), str(row.get("code_challenge"))
            ):
                return _response({"error": "invalid_grant"}, 400)
            token = "mcp_at_" + secrets.token_urlsafe(40)
            expires_in = max(
                int(_env(self.env, "WORKSPACE_AGENT_RELAY_OAUTH_TOKEN_TTL_SECONDS", "86400")), 60
            )
            await _db_run(
                self.state.db,
                "INSERT INTO oauth_tokens(access_token, client_id, scope, resource, expires_at) VALUES (?, ?, ?, ?, ?)",
                token,
                str(row["client_id"]),
                str(row["scope"]),
                str(row["resource"]),
                _now() + expires_in,
            )
            return _response(
                {
                    "access_token": token,
                    "token_type": "Bearer",
                    "expires_in": expires_in,
                    "scope": row["scope"],
                }
            )
        return _response({"error": "not_found"}, 404)

    async def mcp(self, request: Any) -> Response:
        unauthorized = await self.authorize_request(request)
        if unauthorized is not None:
            return unauthorized
        if request.method != "POST":
            return _response({"error": "MCP endpoint requires POST"}, 405, {"Allow": "POST"})
        body = await self._body_json(request)
        if not body:
            return _response({"error": "invalid JSON-RPC body"}, 400)
        if body.get("method", "").startswith("notifications/"):
            return _text_response("", 202)
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": MCP_NAME, "version": "3.0.0"},
                    "instructions": (
                        "Use record_plan, record_progress and record_result for every relay turn. "
                        "The relay name is workspace-agent-relay-mcp-prd."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_definitions()}
            elif method == "tools/call":
                result = await self.call_tool(
                    str(params.get("name") or ""), params.get("arguments") or {}
                )
            else:
                return _response(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    },
                    200,
                )
            return _response({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            return _response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": _safe_error(exc)},
                },
                200,
            )

    def tool_definitions(self) -> list[dict[str, Any]]:
        string = {"type": "string"}
        return [
            {
                "name": "server_info",
                "description": "Return Cloudflare Worker relay information.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "record_plan",
                "description": "Record the current turn plan.",
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "steps"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "steps": {"type": "array"},
                    },
                },
            },
            {
                "name": "record_progress",
                "description": "Record progress and update plan steps.",
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "message"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "message": string,
                        "step_updates": {"type": "array"},
                    },
                },
            },
            {
                "name": "record_result",
                "description": "Record the final result exactly once.",
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "status", "title", "markdown"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "status": {"type": "string", "enum": ["done", "failed", "blocked"]},
                        "title": string,
                        "markdown": string,
                    },
                },
            },
            {
                "name": "update_conversation_title",
                "description": "Update the relay conversation title.",
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "title"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "title": string,
                    },
                },
            },
            {
                "name": "ask_user",
                "description": "Pause the current turn with a question for the operator.",
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "question"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "question": string,
                        "choices": {"type": "array"},
                        "context": string,
                    },
                },
            },
            {
                "name": "get_run_context",
                "description": "Read recent runs for a relay conversation.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string, "limit": {"type": "integer"}},
                },
            },
            {
                "name": "get_requester_info",
                "description": "Return the Feishu user who mentioned the bot.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string},
                },
            },
            {
                "name": "create_private_group",
                "description": "Create a private Feishu group containing only the requester and bot.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string, "chat_name": string},
                },
            },
            {
                "name": "get_group_status",
                "description": "Read the created group mapping for a conversation.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string},
                },
            },
            {
                "name": "get_stored_image",
                "description": "Read whether an avatar is stored in R2.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string},
                },
            },
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "server_info":
            return self._tool_result(
                {
                    "success": True,
                    "app_name": MCP_NAME,
                    "version": "3.0.0",
                    "public_base_url": self.base_url(),
                    "storage": "D1",
                    "queue": "AGENT_QUEUE",
                    "r2": "AVATARS",
                }
            )
        request_id = str(args.get("request_id") or "")
        conversation_key = str(args.get("conversation_key") or "")
        if name == "record_plan":
            await self._require_run(request_id, conversation_key)
            steps = args.get("steps") if isinstance(args.get("steps"), list) else []
            await self.state.update_run(request_id, status="running", steps_json=_json(steps))
            return self._tool_result(
                {
                    "success": True,
                    "request_id": request_id,
                    "conversation_key": conversation_key,
                    "steps": steps,
                    "status": "running",
                }
            )
        if name == "record_progress":
            run = await self._require_run(request_id, conversation_key)
            steps = run.get("steps", [])
            for update in args.get("step_updates") or []:
                if not isinstance(update, dict):
                    continue
                for step in steps:
                    if step.get("id") == update.get("id"):
                        step.update(
                            {key: update[key] for key in ("status", "note") if key in update}
                        )
            await self.state.update_run(
                request_id,
                status="running",
                steps_json=_json(steps),
                progress_message=str(args.get("message") or ""),
            )
            return self._tool_result(
                {
                    "success": True,
                    "request_id": request_id,
                    "steps": steps,
                    "message": args.get("message", ""),
                }
            )
        if name == "record_result":
            run = await self._require_run(request_id, conversation_key)
            if run.get("completed_at"):
                return self._tool_result(
                    {
                        "success": True,
                        "request_id": request_id,
                        "status": run.get("status"),
                        "already_recorded": True,
                    }
                )
            status = str(args.get("status") or "failed")
            if status not in {"done", "failed", "blocked"}:
                status = "failed"
            await self.state.update_run(
                request_id,
                status=status,
                title=str(args.get("title") or ""),
                markdown=str(args.get("markdown") or ""),
                completed_at=_now(),
            )
            await self._enqueue({"kind": "deliver_result", "request_id": request_id})
            return self._tool_result({"success": True, "request_id": request_id, "status": status})
        if name == "update_conversation_title":
            await self._require_run(request_id, conversation_key)
            await self.state.update_run(request_id, title=str(args.get("title") or ""))
            return self._tool_result({"success": True, "title": args.get("title", "")})
        if name == "ask_user":
            await self._require_run(request_id, conversation_key)
            question = str(args.get("question") or "")
            await self.state.update_run(request_id, status="needs_user", progress_message=question)
            return self._tool_result(
                {
                    "success": True,
                    "status": "needs_user",
                    "question": question,
                    "choices": args.get("choices") or [],
                }
            )
        if name == "get_run_context":
            rows = await self.state.recent_runs(conversation_key, int(args.get("limit", 5)))
            return self._tool_result(
                {"success": True, "conversation_key": conversation_key, "runs": rows}
            )
        if name == "get_requester_info":
            row = await self.state.requester(conversation_key)
            return self._tool_result(
                {"success": bool(row), "conversation_key": conversation_key, "requester": row}
                if row
                else {
                    "success": False,
                    "error": {
                        "code": "requester_not_found",
                        "message": "no requester registered for this conversation",
                    },
                }
            )
        if name == "create_private_group":
            row = await self.state.requester(conversation_key)
            if not row or not row.get("open_id"):
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "requester_not_found",
                            "message": "requester open_id unavailable",
                        },
                    },
                    True,
                )
            try:
                chat_id = await self.feishu.create_private_group(
                    str(row["open_id"]), args.get("chat_name")
                )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "create_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            await self.state.bind_group(conversation_key, chat_id)
            return self._tool_result(
                {
                    "success": True,
                    "conversation_key": conversation_key,
                    "chat_id": chat_id,
                    "user_open_id": row["open_id"],
                }
            )
        if name == "get_group_status":
            row = await self.state.requester(conversation_key)
            return self._tool_result(
                {
                    "success": True,
                    "conversation_key": conversation_key,
                    "created": bool(row and row.get("group_chat_id")),
                    "chat_id": row.get("group_chat_id") if row else None,
                }
            )
        if name == "get_stored_image":
            row = await self.state.avatar(conversation_key)
            return self._tool_result(
                {"success": True, "has_avatar": bool(row), "size": row.get("size") if row else None}
            )
        return self._tool_result(
            {
                "success": False,
                "error": {"code": "unknown_tool", "message": f"unknown tool {name}"},
            },
            True,
        )

    @staticmethod
    def _tool_result(value: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": _json(value)}],
            "structuredContent": value,
            "isError": is_error,
        }

    async def _require_run(self, request_id: str, conversation_key: str) -> dict[str, Any]:
        if not request_id or not conversation_key:
            raise ValueError("request_id and conversation_key are required")
        run = await self.state.get_run(request_id)
        if not run or str(run.get("conversation_key")) != conversation_key:
            raise ValueError("request_id does not belong to conversation_key")
        return run

    async def _body_json(self, request: Any) -> dict[str, Any]:
        try:
            value = await request.json()
        except Exception:
            try:
                value = json.loads(await request.text())
            except Exception:
                value = {}
        return value if isinstance(value, dict) else {}

    async def _body_params(self, request: Any) -> dict[str, str]:
        content_type = str(request.headers.get("content-type") or "")
        if "application/json" in content_type:
            return {key: str(value) for key, value in (await self._body_json(request)).items()}
        raw = await request.text()
        return {key: values[0] for key, values in parse_qs(raw).items() if values}

    def _query_params(self, request: Any) -> dict[str, str]:
        query = parse_qs(urlparse(request.url).query)
        return {key: values[0] for key, values in query.items() if values}

    async def _enqueue(self, body: dict[str, Any]) -> None:
        queue = getattr(self.env, "AGENT_QUEUE", None)
        if queue is not None:
            await queue.send(body)
            return
        # Local/dev fallback; production should always configure AGENT_QUEUE.
        if body.get("kind") == "deliver_result":
            try:
                await self.deliver_result(str(body.get("request_id") or ""))
            except Exception as exc:
                print(f"Local result delivery failed: {_safe_error(exc)}")
        else:
            await self.run_agent_job(body)

    async def _store_run_images(self, run: dict[str, Any]) -> None:
        """Persist only explicitly attached Feishu images in the optional R2 bucket."""
        image_keys = run.get("image_keys") if isinstance(run.get("image_keys"), list) else []
        bucket = getattr(self.env, "AVATARS", None)
        if not image_keys or bucket is None:
            return
        for image_key in image_keys[:3]:
            if not isinstance(image_key, str) or not image_key:
                continue
            data = await self.feishu.download_image(str(run["source_message_id"]), image_key)
            object_key = f"{run['conversation_key']}/{run['source_message_id']}/{image_key}"
            await bucket.put(object_key, data)
            await self.state.save_avatar(str(run["conversation_key"]), object_key, len(data))

    async def run_agent_job(self, body: dict[str, Any]) -> None:
        request_id = str(body.get("request_id") or "")
        run = await self.state.get_run(request_id)
        if not run:
            return
        # A Queue retry after a transient Feishu delivery failure must not
        # trigger the Workspace Agent a second time.
        if run.get("status") == "failed" and run.get("completed_at"):
            await self.deliver_result(request_id)
            return
        try:
            await self._store_run_images(run)
            placeholder_id = run.get("placeholder_message_id")
            if not placeholder_id:
                placeholder_id = await self.feishu.reply_card(
                    str(run["source_message_id"]), PLACEHOLDER
                )
                await self.state.update_run(request_id, placeholder_message_id=placeholder_id)
                # Bind the editable card immediately.  A user can quote the
                # in-progress card before the Agent finishes and still stay
                # in the same relay conversation.
                await self.state.save_reply(str(placeholder_id), str(run["conversation_key"]))
            trigger_url = _env(self.env, "WORKSPACE_AGENT_RELAY_TRIGGER_URL")
            access_token = _env(self.env, "WORKSPACE_AGENT_RELAY_AGENT_TOKEN")
            if not trigger_url or not access_token:
                raise RuntimeError("Workspace Agent trigger URL or token is not configured")
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    trigger_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(body.get("idempotency_key") or request_id),
                        "User-Agent": f"{MCP_NAME}/3.0",
                    },
                    json={
                        "conversation_key": run["conversation_key"],
                        "input": run["input_markdown"],
                    },
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(
                    f"Workspace Agent trigger failed HTTP {response.status_code}: {_safe_error(response.text, access_token)}"
                )
            await self.state.update_run(
                request_id, status="triggered", trigger_status=response.status_code
            )
        except Exception as exc:
            message = _safe_error(exc, _env(self.env, "WORKSPACE_AGENT_RELAY_AGENT_TOKEN"))
            await self.state.update_run(
                request_id,
                status="failed",
                trigger_status=0,
                trigger_error=message,
                title="Agent 任务失败",
                markdown=message,
                completed_at=_now(),
            )
            await self.deliver_result(request_id)

    async def deliver_result(self, request_id: str) -> None:
        run = await self.state.get_run(request_id)
        if not run or int(run.get("delivered") or 0):
            return
        status = str(run.get("status") or "done")
        title = str(run.get("title") or "").strip()
        markdown = str(run.get("markdown") or "").strip()
        body = "\n".join(item for item in (title, markdown) if item).strip()
        if status == "failed":
            text = f"Agent 任务失败：{body or '未提供失败原因'}"
        elif status == "blocked":
            text = f"Agent 任务被阻塞：{body or '未提供原因'}"
        else:
            text = body or "Agent 已完成，但未返回内容。"
        try:
            placeholder = str(run.get("placeholder_message_id") or "")
            if placeholder:
                try:
                    await self.feishu.update_card(placeholder, text)
                    outbound = placeholder
                except Exception as exc:
                    if not _is_non_card_update_error(exc):
                        raise
                    # A plain-text Feishu message cannot be edited through the
                    # update API.  Preserve the visible placeholder and post
                    # the Agent result as a normal reply instead.
                    print(
                        "Feishu placeholder is not a card; falling back to a new reply"
                    )
                    outbound = await self.feishu.reply(
                        str(run["source_message_id"]), text
                    )
            else:
                outbound = await self.feishu.reply(str(run["source_message_id"]), text)
            await _db_run(
                self.state.db,
                "UPDATE relay_runs SET delivered = 1, updated_at = ? WHERE request_id = ?",
                _now(),
                request_id,
            )
            await self.state.save_reply(outbound, str(run["conversation_key"]))
        except Exception as exc:
            await self.state.update_run(request_id, trigger_error=_safe_error(exc))
            raise

    async def handle_feishu(self, request: Any) -> Response:
        body = await self._body_json(request)
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        verify = _env(self.env, "FEISHU_VERIFY_TOKEN")
        if verify and str(header.get("token") or "") != verify:
            return _response({"code": 1}, 403)
        if body.get("challenge"):
            return _response({"challenge": body["challenge"]})
        if str(header.get("event_type") or "") != "im.message.receive_v1":
            return _response({"code": 0})
        bot_id = _env(self.env, "FEISHU_BOT_OPEN_ID")
        if not bot_id:
            # Do not make a Feishu API call in the webhook request.  A cold
            # Worker must acknowledge within Feishu's timeout; resolve the
            # value once with /open-apis/bot/v3/info and store it as a Secret.
            print("FEISHU_BOT_OPEN_ID is not configured; event ignored")
            return _response({"code": 0})
        try:
            event = _normalize_event(body, bot_id)
            if event is None or not event["open_id"]:
                return _response({"code": 0})
            parent = event["parent_id"]
            conversation_key = await self.state.reply_conversation(parent) if parent else None
            if not conversation_key:
                conversation_key = f"feishu:{_env(self.env, 'FEISHU_APP_ID')}:{event['chat_id']}:{secrets.token_hex(6)}"
            request_id = _request_id()
            if not await self.state.claim_event(
                message_id=event["message_id"],
                request_id=request_id,
                conversation_key=conversation_key,
                chat_id=event["chat_id"],
                open_id=event["open_id"],
            ):
                return _response({"code": 0})
            await self.state.save_requester(
                conversation_key, event["open_id"], event["name"], event["chat_id"]
            )
            continuation = await self.state.previous_run_exists(conversation_key)
            input_text = _conversation_input(
                request_id=request_id,
                conversation_key=conversation_key,
                text=event["text"],
                continuation=continuation,
            )
            await self.state.create_run(
                request_id=request_id,
                conversation_key=conversation_key,
                source_message_id=event["message_id"],
                input_markdown=input_text,
                image_keys=event.get("image_keys") or [],
            )
            await self._enqueue(
                {
                    "kind": "agent",
                    "request_id": request_id,
                    "idempotency_key": f"{_env(self.env, 'FEISHU_APP_ID')}:{event['message_id']}",
                }
            )
        except Exception as exc:
            # Always acknowledge after validation to avoid an endless Feishu retry
            # storm; the real error is retained in Worker logs.
            print(f"Feishu event processing failed: {_safe_error(exc)}")
        return _response({"code": 0})


class Default(WorkerEntrypoint):
    async def fetch(self, request: Any) -> Response:
        url = urlparse(request.url)
        state = D1State(self.env.DB)
        relay = CloudflareRelay(self.env, self.ctx, state)
        path = url.path
        if path == "/health" and request.method == "GET":
            return _response(
                {
                    "ok": True,
                    "service": "feishu2agents-python-worker",
                    "public_base_url": relay.base_url(),
                    "python_worker": True,
                    "python_origin": False,
                }
            )
        if path in {"/", "/api/health"} and request.method == "GET":
            return _response(
                {
                    "ok": True,
                    "service": MCP_NAME,
                    "endpoints": ["/feishu/events", "/mcp", "/oauth/token"],
                }
            )
        if path in {"/feishu/events", "/feishu/event"}:
            if request.method == "POST":
                return await relay.handle_feishu(request)
            return _response(
                {
                    "error": "method_not_allowed",
                    "message": "Feishu webhook endpoint accepts POST requests only",
                },
                status=405,
                headers={"allow": "POST"},
            )
        if path.startswith("/.well-known/") or path.startswith("/oauth/"):
            return await relay.oauth(request, path)
        if path == MCP_PATH:
            return await relay.mcp(request)
        return _response({"error": "not_found"}, 404)

    async def queue(self, batch: Any, env: Any = None, ctx: Any = None) -> None:
        # The deployed Python Workers runtime invokes Queue handlers with
        # (self, batch, env, ctx).  Some runtime versions leave the explicit
        # env/ctx arguments as None while still exposing them on the
        # WorkerEntrypoint instance, so support both forms.
        runtime_env = env if env is not None else self.env
        runtime_ctx = ctx if ctx is not None else self.ctx
        if runtime_env is None:
            raise RuntimeError("Queue consumer did not receive a Worker environment")
        state = D1State(runtime_env.DB)
        relay = CloudflareRelay(runtime_env, runtime_ctx, state)
        for message in batch.messages:
            request_id = ""
            try:
                body = _queue_payload(message.body)
                if body is None:
                    print("Queue message ignored: body is not a JSON object")
                    message.ack()
                    continue
                request_id = str(body.get("request_id") or "")
                if body.get("kind") == "deliver_result":
                    await relay.deliver_result(request_id)
                else:
                    await relay.run_agent_job(body)
                message.ack()
            except Exception as exc:
                error = _safe_error(
                    exc, _env(runtime_env, "WORKSPACE_AGENT_RELAY_AGENT_TOKEN")
                )
                # Keep the run inspectable if an exception escapes the job
                # handler itself.  run_agent_job already records its own
                # failures; this covers queue/runtime errors around it.
                if request_id:
                    try:
                        await state.update_run(
                            request_id,
                            trigger_status=0,
                            trigger_error=f"queue consumer: {error}",
                        )
                    except Exception as db_exc:
                        print(f"Queue diagnostic write failed: {_safe_error(db_exc)}")
                print(f"Queue job failed: {error}")
                message.retry()
