# ruff: noqa: E501

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from workers import Response, WorkerEntrypoint

PUBLIC_BASE_URL = "https://bot.boooe.com"
FEISHU_AUTH_BASE_URL = "https://accounts.feishu.cn"
MCP_PATH = "/mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_NAME = "workspace-agent-relay-mcp-prd"
PLACEHOLDER = "正在处理，Agent 完成后会回复到这条消息。"
MAX_CLIENTS = 50
TRIGGER_RUNS_BETA = "workspace_agent_runs=v1"
BITABLE_AUTOMATION_WEBHOOK_PATH = "/bitable/automation/webhook"
BITABLE_AUTOMATION_MAX_TEXT_LENGTH = 10000
CALENDAR_CONFIRMATION_GATE = (
    "When using connected calendar tools, treat create, update, delete, cancel, recurrence, "
    "and invitation-response actions as calendar mutations. Complete read-only discovery, "
    "exact-target/timezone/conflict checks, and a complete proposal first. The user's original "
    "request is never confirmation. Before a mutation, show the exact calendar, event title/ID, "
    "local time/timezone, changed fields, and deletion or recurrence scope, then ask for an "
    "operation-specific explicit confirmation (确认创建/确认修改/确认删除/确认取消 or an "
    "English equivalent). Invoke the write tool only after the matching confirmation for the "
    "immediately preceding proposal; if anything changes, re-propose. Re-read after success. "
    "If a calendar preview or other Agent image is produced, call the relay send_image tool "
    "with an actual HTTPS URL, data URL, or base64 payload and wait for success before "
    "record_result; a ChatGPT-side attachment or Markdown image link alone is not delivered "
    "to Feishu/Lark. Preserve the requested template's visual structure, but never reject or "
    "withhold a successfully rendered image only because its pixel dimensions or aspect ratio "
    "differs from the reference; send the generated image as-is."
)


def _format_user_question(question: Any, choices: Any = None) -> str:
    """Render an Agent question as the text sent back to the Feishu thread.

    ``ask_user`` is a pause point in an Agent turn, so the question must be
    persisted in the same run that later receives the user's quoted reply.
    Keep the rendering deliberately plain text because the existing
    placeholder is a plain text message and is edited in place.
    """
    text = str(question or "").strip() or "请补充必要信息。"
    if not isinstance(choices, list):
        return text
    options = [str(choice).strip() for choice in choices if str(choice).strip()]
    if not options:
        return text
    return f"{text}\n可选项：\n" + "\n".join(f"- {choice}" for choice in options)


def _image_upload_metadata(
    data: bytes, *, filename_prefix: str = "avatar"
) -> tuple[str, str]:
    """Return a filename and MIME type matching the actual image bytes.

    Feishu validates the multipart metadata against the image data. Sending
    PNG bytes as ``avatar.jpg``/``image/jpeg`` produces a 400 parameter error.
    """
    prefix = str(filename_prefix or "avatar").strip() or "avatar"
    signatures: tuple[tuple[bytes, str, str], ...] = (
        (b"\x89PNG\r\n\x1a\n", f"{prefix}.png", "image/png"),
        (b"GIF87a", f"{prefix}.gif", "image/gif"),
        (b"GIF89a", f"{prefix}.gif", "image/gif"),
        (b"BM", f"{prefix}.bmp", "image/bmp"),
    )
    for signature, filename, content_type in signatures:
        if data.startswith(signature):
            return filename, content_type
    if data.startswith(b"\xff\xd8\xff"):
        return f"{prefix}.jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return f"{prefix}.webp", "image/webp"
    # HEIC/HEIF files use the ISO-BMFF container.  Feishu accepts these for
    # image uploads and converts them to JPEG, so do not reject them merely
    # because they do not have a JPEG/PNG magic header.
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"heic",
        b"heix",
        b"hevc",
        b"hevx",
        b"mif1",
        b"msf1",
    }:
        return f"{prefix}.heic", "image/heic"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return f"{prefix}.tiff", "image/tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return f"{prefix}.ico", "image/x-icon"
    raise RuntimeError(
        "avatar image format is unsupported or cannot be detected; "
        "send a JPEG, PNG, WEBP, GIF, TIFF, BMP, or ICO image"
    )


_GROUP_UPDATE_ENUMS: dict[str, set[str]] = {
    "add_member_permission": {"all_members", "only_owner"},
    "share_card_permission": {"allowed", "not_allowed"},
    "at_all_permission": {"all_members", "only_owner"},
    "edit_permission": {"all_members", "only_owner"},
    "join_message_visibility": {"all_members", "only_owner", "not_anyone"},
    "leave_message_visibility": {"all_members", "only_owner", "not_anyone"},
    "membership_approval": {"no_approval_required", "approval_required"},
    "chat_type": {"private", "public"},
    "group_message_type": {"chat", "thread"},
    "urgent_setting": {"all_members", "only_owner"},
    "video_conference_setting": {"all_members", "only_owner"},
    "pin_manage_setting": {"all_members", "only_owner"},
    "hide_member_count_setting": {"all_members", "only_owner"},
}


def _validate_group_updates(changes: dict[str, Any]) -> str | None:
    """Validate documented Feishu update-chat fields before sending them."""
    supported = {
        "name",
        "avatar_image_key",
        "description",
        "i18n_names",
        "add_member_permission",
        "share_card_permission",
        "at_all_permission",
        "edit_permission",
        "owner_id",
        "join_message_visibility",
        "leave_message_visibility",
        "membership_approval",
        "chat_type",
        "group_message_type",
        "urgent_setting",
        "video_conference_setting",
        "pin_manage_setting",
        "hide_member_count_setting",
    }
    unknown = sorted(set(changes) - supported)
    if unknown:
        return f"unsupported group update field(s): {', '.join(unknown)}"
    name = changes.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return "name must be a non-empty string"
    if isinstance(name, str) and len(name) > 60:
        return "name must be 60 characters or fewer"
    description = changes.get("description")
    if description is not None and not isinstance(description, str):
        return "description must be a string"
    if isinstance(description, str) and len(description) > 100:
        return "description must be 100 characters or fewer"
    i18n_names = changes.get("i18n_names")
    if i18n_names is not None and (
        not isinstance(i18n_names, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in i18n_names.items()
        )
    ):
        return "i18n_names must be an object of language codes to names"
    for field, allowed in _GROUP_UPDATE_ENUMS.items():
        value = changes.get(field)
        if value is not None and value not in allowed:
            return f"{field} must be one of: {', '.join(sorted(allowed))}"
    add_permission = changes.get("add_member_permission")
    share_permission = changes.get("share_card_permission")
    if add_permission == "only_owner" and share_permission == "allowed":
        return "share_card_permission must be not_allowed when add_member_permission is only_owner"
    if add_permission == "all_members" and share_permission == "not_allowed":
        return "share_card_permission must be allowed when add_member_permission is all_members"
    return None


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
    platform TEXT NOT NULL DEFAULT 'feishu',
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
CREATE TABLE IF NOT EXISTS feishu_oauth_states (
    state TEXT PRIMARY KEY,
    redirect_uri TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bitable_pending_requests (
    state TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    document_mode TEXT NOT NULL DEFAULT 'feishu',
    source_platform TEXT NOT NULL DEFAULT 'feishu',
    redirect_uri TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    requester_union_id TEXT NOT NULL DEFAULT '',
    requester_user_id TEXT NOT NULL DEFAULT '',
    input_text TEXT NOT NULL,
    pending_items_json TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bitable_user_tokens (
    platform TEXT NOT NULL,
    open_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (platform, open_id)
);
CREATE TABLE IF NOT EXISTS bitable_group_modes (
    source_platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (source_platform, chat_id, requester_open_id)
);
CREATE TABLE IF NOT EXISTS bitable_group_mode_prompts (
    prompt_message_id TEXT PRIMARY KEY,
    source_platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bitable_group_mode_prompts_lookup
    ON bitable_group_mode_prompts(source_platform, chat_id, requester_open_id, created_at DESC);
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


def _format_bitable_automation_text(value: Any) -> str:
    """Turn a Feishu automation payload into a bounded group-chat message.

    The HTTP action can send either a raw value selected from the AI node or a
    JSON object containing that value.  Prefer common result keys while still
    preserving arbitrary JSON so the endpoint remains useful when Feishu adds
    a new response shape.
    """

    candidate = value
    if isinstance(value, dict):
        for key in (
            "text",
            "message",
            "content",
            "result",
            "output",
            "analysis",
            "summary",
            "answer",
            "response_body",
        ):
            selected = value.get(key)
            if selected not in (None, "", [], {}):
                candidate = selected
                break

    if isinstance(candidate, str):
        text = candidate.strip()
    else:
        try:
            text = json.dumps(candidate, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(candidate)
        text = text.strip()

    if not text:
        text = "多维表格 AI 分析返回为空"
    text = text[:BITABLE_AUTOMATION_MAX_TEXT_LENGTH]
    return f"[多维表格 AI 分析]\n{text}"


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
        # Existing D1 databases created before platform routing do not have
        # this column.  ALTER is intentionally idempotent through the guarded
        # exception so old Feishu rows remain readable.
        with contextlib.suppress(Exception):
            await _db_run(
                self.db,
                "ALTER TABLE requesters ADD COLUMN platform TEXT NOT NULL DEFAULT 'feishu'",
            )
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
        try:
            return await _db_first(
                self.db,
                "SELECT conversation_key, open_id, name, source_chat_id, platform, group_chat_id, created_at "
                "FROM requesters WHERE conversation_key = ?",
                conversation_key,
            )
        except Exception:
            row = await _db_first(
                self.db,
                "SELECT conversation_key, open_id, name, source_chat_id, group_chat_id, created_at "
                "FROM requesters WHERE conversation_key = ?",
                conversation_key,
            )
            if row is not None:
                row["platform"] = "feishu"
            return row

    async def save_requester(
        self,
        conversation_key: str,
        open_id: str,
        name: str,
        chat_id: str,
        platform: str = "feishu",
    ) -> None:
        try:
            await _db_run(
                self.db,
                """INSERT INTO requesters
                   (conversation_key, open_id, name, source_chat_id, platform, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(conversation_key) DO UPDATE SET
                     open_id=excluded.open_id, name=excluded.name,
                     source_chat_id=excluded.source_chat_id, platform=excluded.platform,
                     updated_at=excluded.updated_at""",
                conversation_key,
                open_id,
                name,
                chat_id,
                platform,
                _now(),
                _now(),
            )
        except Exception:
            # A pre-platform D1 deployment may still have the original table;
            # keep the legacy Feishu path usable until the next migration.
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

    async def latest_image_run(self, conversation_key: str) -> dict[str, Any] | None:
        """Return the newest relay run that contains a Feishu image key.

        R2 is optional on the free Worker deployment.  Keeping the original
        message id and image key in D1 lets the group-avatar operation fetch
        the image directly from Feishu later, so an image request is still
        actionable when no R2 binding is present.
        """
        row = await _db_first(
            self.db,
            "SELECT request_id, source_message_id, image_keys_json, created_at "
            "FROM relay_runs WHERE conversation_key = ? "
            "AND image_keys_json != '[]' ORDER BY created_at DESC LIMIT 1",
            conversation_key,
        )
        if not row:
            return None
        try:
            image_keys = json.loads(row.get("image_keys_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            image_keys = []
        if not isinstance(image_keys, list):
            image_keys = []
        row["image_keys"] = [str(value) for value in image_keys if str(value)]
        return row if row["image_keys"] else None

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

    async def save_feishu_oauth_state(
        self, state: str, redirect_uri: str, expires_at: int
    ) -> None:
        await _db_run(
            self.db,
            "INSERT INTO feishu_oauth_states(state, redirect_uri, expires_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            state,
            redirect_uri,
            expires_at,
            _now(),
        )

    async def consume_feishu_oauth_state(self, state: str) -> dict[str, Any] | None:
        row = await _db_first(
            self.db,
            "SELECT state, redirect_uri, expires_at FROM feishu_oauth_states WHERE state = ?",
            state,
        )
        if row is not None:
            await _db_run(self.db, "DELETE FROM feishu_oauth_states WHERE state = ?", state)
        return row

    async def ensure_feishu_oauth_schema(self) -> None:
        """Create the OAuth state table when a deploy has not run migrations yet.

        Existing installations may already have the initial D1 migrations, while
        Cloudflare's GitHub build does not automatically apply a newly committed
        migration.  Keeping this idempotent guard on the OAuth path makes the
        new endpoint self-healing without changing any existing tables or data.
        """
        await _db_run(
            self.db,
            """CREATE TABLE IF NOT EXISTS feishu_oauth_states (
                state TEXT PRIMARY KEY,
                redirect_uri TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""",
        )
        await _db_run(
            self.db,
            """CREATE TABLE IF NOT EXISTS bitable_pending_requests (
                state TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                document_mode TEXT NOT NULL DEFAULT 'feishu',
                source_platform TEXT NOT NULL DEFAULT 'feishu',
                redirect_uri TEXT NOT NULL,
                conversation_key TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                source_chat_id TEXT NOT NULL,
                requester_open_id TEXT NOT NULL,
                input_text TEXT NOT NULL,
                pending_items_json TEXT NOT NULL DEFAULT '[]',
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )""",
        )
        # Keep pending OAuth records compatible with the initial deployment,
        # which only stored the authorization platform. The document target
        # is separate because a Feishu user can select the Lark document (and
        # vice versa); the OAuth platform must remain tied to the requester.
        for column, definition in (
            ("document_mode", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("source_platform", "TEXT NOT NULL DEFAULT 'feishu'"),
            ("requester_union_id", "TEXT NOT NULL DEFAULT ''"),
            ("requester_user_id", "TEXT NOT NULL DEFAULT ''"),
            ("pending_items_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            with contextlib.suppress(Exception):
                await _db_run(
                    self.db,
                    f"ALTER TABLE bitable_pending_requests ADD COLUMN {column} {definition}",
                )
        await _db_run(
            self.db,
            """CREATE TABLE IF NOT EXISTS bitable_user_tokens (
                platform TEXT NOT NULL,
                open_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (platform, open_id)
            )""",
        )
        await _db_run(
            self.db,
            """CREATE TABLE IF NOT EXISTS bitable_group_mode_prompts (
                prompt_message_id TEXT PRIMARY KEY,
                source_platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                requester_open_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )""",
        )
        await _db_run(
            self.db,
            """CREATE INDEX IF NOT EXISTS idx_bitable_group_mode_prompts_lookup
                ON bitable_group_mode_prompts(source_platform, chat_id, requester_open_id, created_at DESC)""",
        )

    async def save_bitable_pending(
        self,
        *,
        state: str,
        platform: str,
        document_mode: str = "feishu",
        source_platform: str = "feishu",
        redirect_uri: str,
        conversation_key: str,
        source_message_id: str,
        source_chat_id: str,
        requester_open_id: str,
        requester_union_id: str = "",
        requester_user_id: str = "",
        input_text: str,
        expires_at: int,
    ) -> None:
        await _db_run(
            self.db,
            """INSERT INTO bitable_pending_requests
               (state, platform, document_mode, source_platform, redirect_uri, conversation_key,
                source_message_id, source_chat_id, requester_open_id,
                requester_union_id, requester_user_id, input_text, pending_items_json,
                expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            state,
            platform,
            document_mode,
            source_platform,
            redirect_uri,
            conversation_key,
            source_message_id,
            source_chat_id,
            requester_open_id,
            requester_union_id,
            requester_user_id,
            input_text,
            "[]",
            expires_at,
            _now(),
        )

    async def bitable_pending_authorization(
        self, *, platform: str, requester_open_id: str
    ) -> dict[str, Any] | None:
        """Return an active authorization request for this platform user."""
        return await _db_first(
            self.db,
            """SELECT * FROM bitable_pending_requests
               WHERE platform = ? AND requester_open_id = ? AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            platform,
            requester_open_id,
            _now(),
        )

    async def append_bitable_pending_item(
        self, *, state: str, item: dict[str, Any]
    ) -> None:
        row = await _db_first(
            self.db,
            "SELECT pending_items_json FROM bitable_pending_requests WHERE state = ?",
            state,
        )
        if row is None:
            return
        try:
            items = json.loads(str(row.get("pending_items_json") or "[]"))
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        items.append(item)
        await _db_run(
            self.db,
            "UPDATE bitable_pending_requests SET pending_items_json = ? WHERE state = ?",
            _json(items),
            state,
        )

    async def consume_bitable_pending(self, state: str) -> dict[str, Any] | None:
        row = await _db_first(
            self.db,
            "SELECT * FROM bitable_pending_requests WHERE state = ?",
            state,
        )
        if row is not None:
            await _db_run(self.db, "DELETE FROM bitable_pending_requests WHERE state = ?", state)
        return row

    async def user_token(self, platform: str, open_id: str) -> dict[str, Any] | None:
        return await _db_first(
            self.db,
            "SELECT platform, open_id, access_token, refresh_token, expires_at, updated_at "
            "FROM bitable_user_tokens WHERE platform = ? AND open_id = ?",
            platform,
            open_id,
        )

    async def save_user_token(
        self,
        *,
        platform: str,
        open_id: str,
        access_token: str,
        refresh_token: str,
        expires_at: int,
    ) -> None:
        await _db_run(
            self.db,
            """INSERT INTO bitable_user_tokens
               (platform, open_id, access_token, refresh_token, expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, open_id) DO UPDATE SET
                 access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token,
                 expires_at=excluded.expires_at,
                 updated_at=excluded.updated_at""",
            platform,
            open_id,
            access_token,
            refresh_token,
            expires_at,
            _now(),
        )

    async def save_bitable_group_mode(
        self, *, source_platform: str, chat_id: str, requester_open_id: str, mode: str
    ) -> None:
        await _db_run(
            self.db,
            """INSERT INTO bitable_group_modes
               (source_platform, chat_id, requester_open_id, mode, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source_platform, chat_id, requester_open_id) DO UPDATE SET
                 mode=excluded.mode, updated_at=excluded.updated_at""",
            source_platform,
            chat_id,
            requester_open_id,
            mode,
            _now(),
        )

    async def bitable_group_mode(
        self, *, source_platform: str, chat_id: str, requester_open_id: str
    ) -> str | None:
        row = await _db_first(
            self.db,
            """SELECT mode FROM bitable_group_modes
               WHERE source_platform = ? AND chat_id = ? AND requester_open_id = ?""",
            source_platform,
            chat_id,
            requester_open_id,
        )
        mode = str((row or {}).get("mode") or "").strip().lower()
        return mode if mode in {"feishu", "lark"} else None

    async def save_bitable_group_mode_prompt(
        self,
        *,
        prompt_message_id: str,
        source_platform: str,
        chat_id: str,
        requester_open_id: str,
        mode: str,
    ) -> None:
        """Bind a two-turn document choice to the bot prompt message.

        The legacy per-user mode remains as a fallback for a fresh @ message,
        while this binding keeps two outstanding prompts independent when a
        user selects different document targets before replying to either.
        """
        await _db_run(
            self.db,
            """INSERT OR REPLACE INTO bitable_group_mode_prompts
               (prompt_message_id, source_platform, chat_id, requester_open_id, mode, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            prompt_message_id,
            source_platform,
            chat_id,
            requester_open_id,
            mode,
            _now(),
        )

    async def bitable_group_mode_for_prompt(
        self,
        *,
        prompt_message_id: str,
        source_platform: str,
        chat_id: str,
        requester_open_id: str,
    ) -> str | None:
        row = await _db_first(
            self.db,
            """SELECT mode FROM bitable_group_mode_prompts
               WHERE prompt_message_id = ? AND source_platform = ?
                 AND chat_id = ? AND requester_open_id = ?""",
            prompt_message_id,
            source_platform,
            chat_id,
            requester_open_id,
        )
        mode = str((row or {}).get("mode") or "").strip().lower()
        return mode if mode in {"feishu", "lark"} else None


class FeishuAPI:
    def __init__(self, env: Any, platform: str = "feishu") -> None:
        self.env = env
        self.platform = str(platform or "feishu").strip().lower() or "feishu"
        if self.platform not in {"feishu", "lark"}:
            raise ValueError("platform must be feishu or lark")
        prefix = self.platform.upper()
        self.base = _env(
            env,
            f"{prefix}_API_BASE",
            "https://open.larksuite.com" if self.platform == "lark" else "https://open.feishu.cn",
        )
        self.app_id_env = f"{prefix}_APP_ID"
        self.app_secret_env = f"{prefix}_APP_SECRET"
        self._token: str = ""
        self._token_expires = 0

    async def _tenant_token(self) -> str:
        if self._token and self._token_expires > _now() + 60:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": _env(self.env, self.app_id_env),
                    "app_secret": _env(self.env, self.app_secret_env),
                },
            )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError(
                f"{self.platform.title()} token response did not contain tenant_access_token"
            )
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
            raise RuntimeError(
                f"{self.platform.title()} API {path} failed ({response.status_code}): {message}"
            )
        return payload

    async def _user_request(
        self, access_token: str, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Call a platform API with a human user's OAuth token.

        Bot tenant tokens and user tokens are intentionally separate.  The
        Bitable workflow uses this method only after the OAuth callback has
        matched the returned user open_id to the sender who mentioned the bot.
        """
        token = str(access_token or "").strip()
        if not token:
            raise RuntimeError("user access token is empty")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
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
            raise RuntimeError(
                f"{self.platform.title()} user API {path} failed "
                f"({response.status_code}): {message}"
            )
        return payload

    async def user_info(self, access_token: str) -> dict[str, Any]:
        payload = await self._user_request(
            access_token, "GET", "/open-apis/authen/v1/user_info"
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("open_id"):
            raise RuntimeError(f"{self.platform.title()} user info returned no open_id")
        return data

    async def resolve_user_id(
        self, identifier: str, *, user_id_type: str = "open_id"
    ) -> dict[str, Any]:
        """Resolve an event sender in this platform's user namespace."""
        value = str(identifier or "").strip()
        if not value:
            return {}
        payload = await self._request(
            "GET",
            f"/open-apis/contact/v3/users/{quote(value, safe='')}",
            params={"user_id_type": user_id_type},
        )
        data = payload.get("data") or {}
        user = data.get("user") if isinstance(data, dict) else None
        return user if isinstance(user, dict) else (data if isinstance(data, dict) else {})

    async def resolve_wiki_bitable_app_token(
        self, access_token: str, wiki_token: str
    ) -> str:
        payload = await self._user_request(
            access_token,
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": wiki_token},
        )
        node = (payload.get("data") or {}).get("node") or {}
        if not isinstance(node, dict):
            raise RuntimeError("Wiki node response has no node")
        app_token = str(node.get("obj_token") or node.get("token") or "")
        obj_type = str(node.get("obj_type") or "")
        if not app_token:
            raise RuntimeError("Wiki node did not return a Bitable app_token")
        if obj_type and obj_type not in {"bitable", "sheet"}:
            raise RuntimeError(f"Wiki node type is {obj_type}, not a Bitable")
        return app_token

    async def create_user_bitable_record(
        self,
        access_token: str,
        *,
        app_token: str,
        table_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        payload = await self._user_request(
            access_token,
            "POST",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params={"user_id_type": "open_id"},
            json={"fields": fields},
        )
        record = (payload.get("data") or {}).get("record")
        if not isinstance(record, dict) or not record.get("record_id"):
            raise RuntimeError("Bitable create response did not contain record_id")
        return record

    async def user_bitable_fields(
        self,
        access_token: str,
        *,
        app_token: str,
        table_id: str,
    ) -> list[dict[str, Any]]:
        payload = await self._user_request(
            access_token,
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            params={"user_id_type": "open_id", "page_size": 100},
        )
        items = (payload.get("data") or {}).get("items") or []
        if not isinstance(items, list):
            raise RuntimeError("Bitable fields response is invalid")
        return [item for item in items if isinstance(item, dict)]

    async def bot_open_id(self) -> str:
        payload = await self._request("GET", "/open-apis/bot/v3/info")
        value = payload.get("bot", {}).get("open_id")
        if not value:
            raise RuntimeError(f"{self.platform.title()} bot identity returned no open_id")
        return str(value)

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send a bot-authored text message to a group chat."""
        target = str(chat_id or "").strip()
        if not target:
            raise ValueError("chat_id is required")
        payload = await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": target,
                "msg_type": "text",
                "content": _json({"text": str(text or "")}),
            },
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu message response returned no message_id")
        return str(outbound)

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

    async def reply_image(self, message_id: str, image_key: str) -> str:
        """Reply to a message with an uploaded image."""
        key = str(image_key or "").strip()
        if not key:
            raise ValueError("image_key is required")
        payload = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            params={"user_id_type": "open_id"},
            json={"msg_type": "image", "content": _json({"image_key": key})},
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu image reply response returned no message_id")
        return str(outbound)

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        """Reply with an interactive card.

        Feishu and Lark use the same interactive-message endpoint.  Keeping
        the OAuth URL inside an ``open_url`` button prevents the raw URL from
        being exposed as a chat link while still opening it in the platform's
        in-app web view after the user taps the button.
        """
        payload = await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            params={"user_id_type": "open_id"},
            json={"msg_type": "interactive", "content": _json(card)},
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu card reply response returned no message_id")
        return str(outbound)

    async def send_ephemeral_card(
        self, *, chat_id: str, open_id: str, card: dict[str, Any]
    ) -> str:
        """Send a group card that is visible only to one online user.

        Feishu/Lark's ephemeral-card endpoint deliberately uses the group and
        recipient IDs instead of replying to the source message.  This keeps
        OAuth links private to the person who mentioned the bot.
        """
        payload = await self._request(
            "POST",
            "/open-apis/ephemeral/v1/send",
            json={
                "chat_id": str(chat_id),
                "open_id": str(open_id),
                "msg_type": "interactive",
                "card": card,
            },
        )
        outbound = payload.get("data", {}).get("message_id")
        if not outbound:
            raise RuntimeError("Feishu ephemeral card response returned no message_id")
        return str(outbound)

    async def update(self, message_id: str, text: str) -> None:
        await self._request(
            "PUT",
            f"/open-apis/im/v1/messages/{message_id}",
            params={"user_id_type": "open_id"},
            json={"msg_type": "text", "content": _json({"text": text})},
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

    async def create_group(self, name: str, open_ids: list[str]) -> str:
        """Create a Feishu group with the explicitly supplied members.

        The app identity is automatically added by Feishu as the group bot.
        ``open_ids`` is the exact list of user members to invite; the requester
        is not implicitly added.
        """
        group_name = str(name or "").strip()
        members = [str(open_id).strip() for open_id in open_ids if str(open_id).strip()]
        if not group_name:
            raise RuntimeError("group name is required")
        if not members:
            raise RuntimeError("at least one group member open_id is required")
        payload = await self._request(
            "POST",
            "/open-apis/im/v1/chats",
            params={"user_id_type": "open_id"},
            json={
                "name": group_name,
                "chat_mode": "group",
                "user_id_list": members,
            },
        )
        chat_id = payload.get("data", {}).get("chat_id")
        if not chat_id:
            raise RuntimeError("Feishu create group response returned no chat_id")
        return str(chat_id)

    async def add_group_members(
        self, chat_id: str, member_open_ids: list[str]
    ) -> dict[str, Any]:
        """Add explicitly confirmed Open IDs to an existing Feishu group."""
        group_id = str(chat_id or "").strip()
        members: list[str] = []
        for open_id in member_open_ids:
            value = str(open_id or "").strip()
            if value and value not in members:
                members.append(value)
        if not group_id:
            raise RuntimeError("chat_id is required")
        if not members:
            raise RuntimeError("at least one member_open_id is required")
        if len(members) > 50:
            raise RuntimeError("Feishu allows at most 50 members per request")
        payload = await self._request(
            "POST",
            f"/open-apis/im/v1/chats/{group_id}/members",
            params={"member_id_type": "open_id", "succeed_type": 2},
            json={"id_list": members},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        result: dict[str, Any] = {
            "chat_id": group_id,
            "requested_member_open_ids": members,
        }
        for key in ("invalid_id_list", "not_existed_id_list", "pending_approval_id_list"):
            value = data.get(key, payload.get(key, []))
            result[key] = value if isinstance(value, list) else []
        result["added_member_open_ids"] = [
            open_id
            for open_id in members
            if open_id
            not in set(result["invalid_id_list"])
            and open_id not in set(result["not_existed_id_list"])
            and open_id not in set(result["pending_approval_id_list"])
        ]
        return result

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Read the current settings for a regular group."""
        payload = await self._request(
            "GET",
            f"/open-apis/im/v1/chats/{chat_id}",
            params={"user_id_type": "open_id"},
        )
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    async def update_chat(self, chat_id: str, changes: dict[str, Any]) -> None:
        """Update the supplied fields on an existing regular group."""
        if not changes:
            raise RuntimeError("at least one group field is required")
        body: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "avatar_image_key":
                body["avatar"] = value
            else:
                body[key] = value
        await self._request(
            "PUT",
            f"/open-apis/im/v1/chats/{chat_id}",
            params={"user_id_type": "open_id"},
            json=body,
        )

    async def _upload_image(
        self, data: bytes, *, image_type: str, filename_prefix: str
    ) -> str:
        """Upload image bytes and return the platform ``image_key``."""
        if not data:
            raise RuntimeError("cannot upload an empty image")
        if len(data) > 10 * 1024 * 1024:
            raise RuntimeError("image exceeds Feishu's 10MB limit")
        filename, content_type = _image_upload_metadata(
            data, filename_prefix=filename_prefix
        )
        token = await self._tenant_token()
        async with httpx.AsyncClient(timeout=30) as client:
            async def send(file_content_type: str) -> httpx.Response:
                return await client.post(
                    f"{self.base}/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": image_type},
                    files={"image": (filename, data, file_content_type)},
                )

            response = await send(content_type)
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            # Feishu's parser occasionally rejects an otherwise valid image
            # when a proxy rewrites the per-file MIME type.  The documented
            # Python example uses application/octet-stream, so retry only for
            # parameter/format errors while retaining the real filename.
            error_code = payload.get("code")
            if (
                response.status_code >= 400 or error_code not in (None, 0)
            ) and str(error_code) in {"234001", "234011"}:
                response = await send("application/octet-stream")
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            message = payload.get("msg") or payload.get("message") or response.text
            log_id = response.headers.get("X-Tt-Logid", "")
            suffix = f", logid={log_id}" if log_id else ""
            raise RuntimeError(
                f"Feishu API /open-apis/im/v1/images failed "
                f"({response.status_code}, code={payload.get('code', 'unknown')}{suffix}; "
                f"filename={filename}, content_type={content_type}): {message}"
            )
        image_key = str((payload.get("data") or {}).get("image_key") or "")
        if not image_key:
            raise RuntimeError("Feishu image upload returned no image_key")
        return image_key

    async def upload_avatar_image(self, data: bytes) -> str:
        """Upload image bytes as a group avatar and return its image_key."""
        return await self._upload_image(
            data, image_type="avatar", filename_prefix="avatar"
        )

    async def upload_message_image(self, data: bytes) -> str:
        """Upload image bytes for a chat message and return its image_key."""
        return await self._upload_image(
            data, image_type="message", filename_prefix="image"
        )

    async def search_contacts(self, query: str) -> list[dict[str, Any]]:
        """Resolve Feishu contacts from a mobile number or email address.

        Feishu's legacy name-search endpoint requires a user access token.  The
        Worker only has a tenant access token, so use the tenant-supported
        ``batch_get_id`` endpoint instead.  The Agent's group workflow asks for
        a mobile number or email when it needs to identify a member.
        """
        text = str(query or "").strip()
        emails = sorted(
            set(re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text))
        )
        mobiles = sorted(
            set(re.findall(r"(?<!\d)(?:\+?86[\s-]*)?(1\d{10})(?!\d)", text))
        )
        if not emails and not mobiles:
            raise RuntimeError(
                "Feishu contact lookup requires a mobile number or email; "
                "name-only search is not available with the tenant token"
            )
        payload = await self._request(
            "POST",
            "/open-apis/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            json={"emails": emails, "mobiles": mobiles},
        )
        users = payload.get("data", {}).get("user_list", [])
        if not isinstance(users, list):
            raise RuntimeError("Feishu contact search returned an invalid users list")
        candidates: list[dict[str, Any]] = []
        for user in users:
            if not isinstance(user, dict):
                continue
            open_id = str(user.get("user_id") or user.get("open_id") or "")
            if not open_id:
                continue
            candidate: dict[str, Any] = {
                "name": str(user.get("name") or ""),
                "open_id": open_id,
            }
            for field in ("email", "enterprise_email", "mobile", "department_ids", "title"):
                if user.get(field) is not None:
                    candidate[field] = user[field]
            candidates.append(candidate)
        return candidates

    async def download_image(self, message_id: str, file_key: str) -> bytes:
        """Download an image attached to a Feishu message.

        The message-resource endpoint returns a JSON error body for permission
        and resource mismatches, even though its HTTP status is 400. Do not
        use ``raise_for_status`` here: preserving Feishu's code, message, and
        log id is necessary to distinguish a missing ``im:message`` scope from
        a bad message/resource pair.
        """
        token = await self._tenant_token()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": "image"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = payload.get("msg") or payload.get("message") or response.text
            code = payload.get("code", "unknown")
            log_id = response.headers.get("X-Tt-Logid", "")
            suffix = f", logid={log_id}" if log_id else ""
            raise RuntimeError(
                "Feishu message resource download failed "
                f"({response.status_code}, code={code}{suffix}; "
                f"message_id={message_id}, file_key={file_key}, type=image): {message}"
            )
        if not response.content:
            raise RuntimeError(
                "Feishu message resource download returned an empty image "
                f"(message_id={message_id}, file_key={file_key}, type=image)"
            )
        return response.content


def _message_parts(message_type: str, content: Any) -> tuple[str, list[str]] | None:
    """Extract text and image keys from text, image, and rich-text messages.

    Feishu sends a message containing a caption plus an image as ``post``.  It
    is easy to mistake that for an unsupported message type because a pure
    image is sent as ``image``.  The two formats carry the same image key, but
    ``post`` nests blocks under a locale (usually ``zh_cn``).
    """
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if message_type == "text":
        text = parsed.get("text", "")
        return (text, []) if isinstance(text, str) else None
    if message_type == "image":
        image_key = parsed.get("image_key")
        return ("", [image_key]) if isinstance(image_key, str) and image_key else ("", [])
    if message_type != "post":
        return None

    # Rich-text content is localized: {"zh_cn": {"title": "", "content": [...]}}.
    payload: dict[str, Any] = parsed
    for value in parsed.values():
        if isinstance(value, dict) and isinstance(value.get("content"), list):
            payload = value
            break
    text_parts: list[str] = []
    image_keys: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        text_parts.append(title)
    rows = payload.get("content")
    if not isinstance(rows, list):
        return None
    for row in rows:
        elements = row if isinstance(row, list) else [row]
        for element in elements:
            if not isinstance(element, dict):
                continue
            tag = str(element.get("tag") or "")
            if tag in {"text", "a", "at"}:
                value = element.get("text") or element.get("user_name") or ""
                if isinstance(value, str):
                    text_parts.append(value)
            elif tag == "img":
                image_key = element.get("image_key")
                if isinstance(image_key, str) and image_key:
                    image_keys.append(image_key)
    return ("".join(text_parts), image_keys)


def _normalize_event(
    body: dict[str, Any], bot_open_id: str, *, allow_unmentioned_reply: bool = False
) -> dict[str, Any] | None:
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    message_id = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    if not message_id or not chat_id:
        return None
    message_type = str(message.get("message_type") or "")
    if message.get("chat_type") != "group" or message_type not in {"text", "image", "post"}:
        return None
    if str(sender.get("sender_type") or "").lower() in {"app", "bot"}:
        return None
    parts = _message_parts(message_type, message.get("content") or "{}")
    if parts is None:
        return None
    text, image_keys = parts
    if image_keys:
        text = f"{text}\n[用户发送了一张图片，已保存为必要文件]"
    parent_id = str(message.get("parent_id") or message.get("root_id") or "")
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
    # The bot must be explicitly @mentioned for every request.  When that
    # mention is attached to a reply quoting one of our messages, the handler
    # uses the parent mapping to continue the existing conversation; a fresh
    # @mention without a mapped parent starts a new conversation.
    if (not mentioned_bot and not (allow_unmentioned_reply and parent_id)) or (
        not text and not image_keys
    ):
        return None
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "open_id": str(sender_id.get("open_id") or ""),
        "union_id": str(sender_id.get("union_id") or ""),
        "user_id": str(sender_id.get("user_id") or ""),
        "name": str(sender.get("sender_id", {}).get("open_id") or ""),
        "parent_id": parent_id,
        "mentioned_bot": mentioned_bot,
        "text": text,
        "image_keys": image_keys,
        "tenant_key": str(header.get("tenant_key") or ""),
        "sender_tenant_key": str(
            sender.get("tenant_key") or sender_id.get("tenant_key") or ""
        ),
    }


def _request_id(platform: str = "feishu") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    normalized = str(platform or "feishu").strip().lower() or "feishu"
    return f"{normalized}_{stamp}_{secrets.token_hex(6)}"


def _conversation_input(
    *,
    request_id: str,
    conversation_key: str,
    text: str,
    continuation: bool,
    image_keys: list[str] | None = None,
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
            (
                "This is an interactive Feishu turn. Use the connected Feishu/Lark and calendar tools "
                "to carry out the user's request, then call the relay MCP record_result so the answer "
                "is returned to the quoted Feishu message."
            ),
            CALENDAR_CONFIRMATION_GATE,
            f"The relay MCP server is {MCP_NAME} at https://bot.boooe.com{MCP_PATH}; call record_result there before ending the turn.",
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
            CALENDAR_CONFIRMATION_GATE,
            (
                f"The relay MCP server is {MCP_NAME} at https://bot.boooe.com{MCP_PATH}. "
                "It is the required Feishu reply channel; do not only answer in the ChatGPT conversation."
            ),
            "Use get_requester_info with the conversation_key when the task depends on the person who mentioned the bot.",
            "",
            "User task:",
            text.strip(),
        ]
    if image_keys:
        body.extend(
            [
                "",
                "The Feishu message includes one or more user images. Call "
                "workspace-agent-relay-mcp.get_user_images with the current request_id and "
                "conversation_key before trying to inspect or use them. Do not claim to have "
                "seen an image unless that tool returns an image attachment.",
                "If the user asks to use an image as a group avatar, the avatar workflow can "
                "still call update_group with set_avatar_from_stored=true.",
            ]
        )
    return "\n".join([*header, "", *body])


BITABLE_WORKFLOW_GROUP_CHAT_ID = "oc_5e9132f3638772d53d92d6fc5e953abc"
BITABLE_WORKFLOW_WIKI_TOKEN = "AYNDwkmOUiZtbgkZ3BAcLchUnnb"
BITABLE_WORKFLOW_TABLE_ID = "tblVyvH3RGHqwQBC"
LARK_DOCUMENT_WIKI_TOKEN = "UmGRwFFDQiegHckOVh0j0DIrpRc"
LARK_DOCUMENT_TABLE_ID = "tblmd8DAQwM00t7B"


class BitableGroupWorkflow:
    """Handle the dedicated group-to-Bitable test flow.

    This workflow is deliberately isolated from the Workspace Agent relay. A
    message in the configured test group is handled synchronously as:

    ``source bot -> detected user platform -> matching OAuth -> user API write``.

    The sender's open_id is available from the event, but a bot event never
    contains that person's user access token. The first request therefore
    replies with a platform-specific authorization URL. The callback stores a
    short-lived pending request, verifies the OAuth identity when both sides
    share an ID namespace, and uses the one-time state binding for a
    cross-platform external sender before writing with the user's token.
    """

    def __init__(self, relay: CloudflareRelay) -> None:
        self.relay = relay

    def target_group(self) -> str:
        return _env(
            self.relay.env,
            "BITABLE_WORKFLOW_GROUP_CHAT_ID",
            BITABLE_WORKFLOW_GROUP_CHAT_ID,
        ) or BITABLE_WORKFLOW_GROUP_CHAT_ID

    def is_target_group(self, chat_id: str) -> bool:
        return str(chat_id or "").strip() == self.target_group()

    @staticmethod
    def parse_document_command(text: str) -> tuple[str, str] | None:
        """Return the selected document platform and any inline payload.

        The command can be sent as ``[飞书文档]``/``[lark文档]`` or without the
        display brackets.  An optional payload after whitespace or a colon is
        accepted so both the two-turn flow and a single-message flow work.
        """
        value = str(text or "").strip()
        aliases = (
            ("[飞书文档]", "feishu"),
            ("飞书文档", "feishu"),
            ("[lark文档]", "lark"),
            ("lark文档", "lark"),
        )
        lowered = value.casefold()
        for label, mode in aliases:
            prefix = label.casefold()
            if lowered == prefix:
                return mode, ""
            if not lowered.startswith(prefix):
                continue
            remainder = value[len(label) :]
            if remainder and remainder[0] not in " \t\r\n:：,，":
                continue
            return mode, remainder.lstrip(" \t\r\n:：,，")
        return None

    @staticmethod
    def mode_label(mode: str) -> str:
        return "飞书文档" if str(mode or "").strip().lower() == "feishu" else "lark文档"

    async def reply_mode_prompt(
        self, source_platform: str, event: dict[str, Any], mode: str
    ) -> str:
        api = self.relay.api_for_conversation(f"{source_platform}:workflow")
        return await api.reply(
            str(event["message_id"]),
            f"已选择 [{self.mode_label(mode)}]。请继续 @机器人发送要写入的内容。",
        )

    async def reply_mode_required(self, source_platform: str, event: dict[str, Any]) -> None:
        api = self.relay.api_for_conversation(f"{source_platform}:workflow")
        await api.reply(
            str(event["message_id"]),
            "此群只支持两种指令：请输入 [飞书文档] 或 [lark文档]，然后继续 @机器人发送内容。",
        )

    def target(self, mode: str) -> tuple[str, str, str, str]:
        """Return wiki token, table id, text field, and assignee field."""
        normalized = str(mode or "feishu").strip().lower()
        env = getattr(self.relay, "env", None)
        if normalized == "lark":
            return (
                _env(env, "LARK_DOCUMENT_WIKI_TOKEN", LARK_DOCUMENT_WIKI_TOKEN)
                or LARK_DOCUMENT_WIKI_TOKEN,
                _env(env, "LARK_DOCUMENT_TABLE_ID", LARK_DOCUMENT_TABLE_ID)
                or LARK_DOCUMENT_TABLE_ID,
                "文本",
                "测试3",
            )
        return (
            _env(env, "BITABLE_WORKFLOW_WIKI_TOKEN", BITABLE_WORKFLOW_WIKI_TOKEN)
            or BITABLE_WORKFLOW_WIKI_TOKEN,
            _env(env, "BITABLE_WORKFLOW_TABLE_ID", BITABLE_WORKFLOW_TABLE_ID)
            or BITABLE_WORKFLOW_TABLE_ID,
            "任务描述",
            "任务执行人",
        )

    @staticmethod
    def _auth_base(platform: str) -> str:
        return (
            "https://accounts.larksuite.com"
            if platform == "lark"
            else FEISHU_AUTH_BASE_URL
        )

    def _callback_uri(self, platform: str) -> str:
        return _env(
            self.relay.env,
            f"{platform.upper()}_OAUTH_REDIRECT_URI",
            self.relay.base_url() + f"/{platform}/oauth/callback",
        )

    def _scope(self, platform: str) -> str:
        return self.relay.platform_oauth_scope(platform)

    async def _authorization_url(
        self,
        *,
        platform: str,
        document_mode: str = "feishu",
        source_platform: str,
        conversation_key: str,
        event: dict[str, Any],
    ) -> str | None:
        callback_uri = self._callback_uri(platform)

        # One active authorization is enough for all requests from the same
        # account. Queue later requests behind that state instead of sending
        # another card. The account platform remains the lookup key; the
        # document mode is retained per queued item.
        find_pending = getattr(self.relay.state, "bitable_pending_authorization", None)
        append_item = getattr(self.relay.state, "append_bitable_pending_item", None)
        requester_open_id = str(event["open_id"])
        item = {
            "platform": platform,
            "document_mode": document_mode,
            "source_platform": source_platform,
            "conversation_key": conversation_key,
            "source_message_id": str(event["message_id"]),
            "source_chat_id": str(event["chat_id"]),
            "requester_open_id": requester_open_id,
            "requester_union_id": str(event.get("union_id") or ""),
            "requester_user_id": str(event.get("user_id") or ""),
            "input_text": str(event.get("text") or "").strip(),
        }
        if callable(find_pending) and callable(append_item):
            existing = await find_pending(
                platform=platform,
                requester_open_id=requester_open_id,
            )
            if existing and str(existing.get("state") or ""):
                await append_item(state=str(existing["state"]), item=item)
                return None

        state = f"bitable_{platform}_" + secrets.token_urlsafe(32)
        expires_at = _now() + self.relay.feishu_oauth_ttl(platform)
        await self.relay.state.save_bitable_pending(
            state=state,
            platform=platform,
            document_mode=document_mode,
            source_platform=source_platform,
            redirect_uri=callback_uri,
            conversation_key=conversation_key,
            source_message_id=str(event["message_id"]),
            source_chat_id=str(event["chat_id"]),
            requester_open_id=requester_open_id,
            requester_union_id=str(event.get("union_id") or ""),
            requester_user_id=str(event.get("user_id") or ""),
            input_text=str(event.get("text") or "").strip(),
            expires_at=expires_at,
        )
        if callable(append_item):
            await append_item(state=state, item=item)
        prefix = platform.upper()
        return (
            f"{self._auth_base(platform)}/open-apis/authen/v1/authorize?"
            + urlencode(
                {
                    "app_id": _env(self.relay.env, f"{prefix}_APP_ID"),
                    "redirect_uri": callback_uri,
                    "scope": self._scope(platform),
                    "state": state,
                }
            )
        )

    async def _reply_auth(
        self, source_platform: str, event: dict[str, Any], url: str
    ) -> None:
        # The bot that received the external-group message sends the link.
        # The OAuth app used by the link can be the other platform.
        api = self.relay.api_for_conversation(f"{source_platform}:workflow")
        platform = "Lark" if url.startswith("https://accounts.larksuite.com/") else "Feishu"
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "请点击下方按钮授权，允许本次以你的账号写入多维表格。"
                            f"\n授权平台：**{platform}**\n授权完成后会自动继续。"
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
                            "url": url,
                        }
                    ],
                },
            ],
        }
        await api.send_ephemeral_card(
            chat_id=str(event["chat_id"]),
            open_id=str(event["open_id"]),
            card=card,
        )

    async def _write_row(
        self,
        *,
        platform: str,
        source_platform: str = "feishu",
        document_mode: str | None = None,
        requester_open_id: str,
        requester_union_id: str = "",
        requester_user_id: str = "",
        text: str,
        access_token: str,
    ) -> dict[str, Any]:
        api = self.relay.api_for_conversation(f"{platform}:workflow")
        identity = await api.user_info(access_token)
        authorized_open_id = str(identity.get("open_id") or "").strip()
        identity_ids = {
            str(identity.get(key) or "").strip()
            for key in ("open_id", "union_id", "user_id")
            if str(identity.get(key) or "").strip()
        }
        requester_ids = {
            value
            for value in (
                requester_open_id,
                requester_union_id,
                requester_user_id,
            )
            if str(value or "").strip()
        }
        # A Feishu webhook can carry a Lark user's sender identity when the
        # bot is in an external group, but the Lark OAuth callback necessarily
        # returns Lark-scoped IDs.  In that cross-platform path there is no
        # common open_id namespace to compare.  The one-time state is bound to
        # the triggering message and the link is sent to that sender.  Keep a
        # strict identity intersection whenever both sides share a namespace.
        cross_platform = source_platform != platform
        if not authorized_open_id or (not cross_platform and not identity_ids.intersection(requester_ids)):
            raise RuntimeError(
                "OAuth 用户与发起 @ 的用户不一致；为避免越权，未写入多维表格"
            )
        # The original direct workflow always targeted the Feishu table; the
        # explicit document_mode is what selects the new Lark table.
        mode = str(document_mode or "feishu").strip().lower()
        wiki_token, table_id, text_field, assignee_field = self.target(mode)
        app_token = await api.resolve_wiki_bitable_app_token(access_token, wiki_token)
        fields = await api.user_bitable_fields(
            access_token,
            app_token=app_token,
            table_id=table_id,
        )
        field_names = {str(item.get("field_name") or "") for item in fields}
        missing = {text_field, assignee_field} - field_names
        if missing:
            raise RuntimeError(
                "测试表缺少字段：" + "、".join(sorted(missing))
            )
        record = await api.create_user_bitable_record(
            access_token,
            app_token=app_token,
            table_id=table_id,
            fields={
                text_field: text,
                assignee_field: [{"id": authorized_open_id}],
            },
        )
        # Keep the platform-scoped assignee available to the caller without
        # changing the upstream record payload returned by Feishu/Lark.
        result = dict(record)
        result["_authorized_open_id"] = authorized_open_id
        return result

    async def handle_event(
        self,
        *,
        platform: str,
        source_platform: str = "feishu",
        document_mode: str | None = None,
        conversation_key: str,
        event: dict[str, Any],
    ) -> None:
        await self.relay.state.ensure_feishu_oauth_schema()
        requester_open_id = str(event.get("open_id") or "").strip()
        text = str(event.get("text") or "").strip()
        # ``platform`` is the requester's actual account platform and controls
        # OAuth, token storage, identity verification, and API client choice.
        # ``document_mode`` only selects the destination document/table.
        auth_platform = str(platform or "feishu").strip().lower()
        selected_mode = str(document_mode or "feishu").strip().lower()
        if auth_platform not in {"feishu", "lark"}:
            auth_platform = "feishu"
        if selected_mode not in {"feishu", "lark"}:
            selected_mode = "feishu"
        if not requester_open_id or not text:
            return
        cached = await self.relay.state.user_token(auth_platform, requester_open_id)
        if cached and int(cached.get("expires_at") or 0) > _now() + 60:
            try:
                record = await self._write_row(
                    platform=auth_platform,
                    source_platform=source_platform,
                    document_mode=selected_mode,
                    requester_open_id=requester_open_id,
                    requester_union_id=str(event.get("union_id") or ""),
                    requester_user_id=str(event.get("user_id") or ""),
                    text=text,
                    access_token=str(cached.get("access_token") or ""),
                )
                await self.relay.api_for_conversation(f"{source_platform}:workflow").reply(
                    str(event["message_id"]),
                    f"已使用你之前的 {auth_platform} 授权写入 {self.mode_label(selected_mode)}，任务执行人="
                    f"{record.get('_authorized_open_id') or requester_open_id}，"
                    f"record_id={record.get('record_id')}",
                )
                return
            except Exception as exc:
                # A revoked/expired token should fall through to a fresh
                # authorization instead of silently using a different user.
                print(f"Cached {auth_platform} user token was not usable: {_safe_error(exc)}")
        url = await self._authorization_url(
            platform=auth_platform,
            document_mode=selected_mode,
            source_platform=source_platform,
            conversation_key=conversation_key,
            event=event,
        )
        if url:
            await self._reply_auth(source_platform, event, url)

    async def complete_oauth(
        self, pending: dict[str, Any], token_data: dict[str, Any]
    ) -> dict[str, Any]:
        platform = str(pending.get("platform") or "").strip().lower()
        requester_open_id = str(pending.get("requester_open_id") or "").strip()
        access_token = str(token_data.get("access_token") or "").strip()
        if platform not in {"feishu", "lark"} or not access_token:
            raise RuntimeError("OAuth callback data is missing platform or access_token")

        # Requests received while the authorization card is open are stored
        # as a JSON array on the same pending OAuth state.  Keep the old
        # single-request row shape as a fallback so states created before the
        # queue column was deployed still complete normally.
        raw_items = pending.get("pending_items_json")
        if isinstance(raw_items, str):
            try:
                queued_items = json.loads(raw_items)
            except (TypeError, ValueError):
                queued_items = []
        else:
            queued_items = raw_items
        items = (
            [dict(item) for item in queued_items if isinstance(item, dict)]
            if isinstance(queued_items, list)
            else []
        )
        if not items:
            items = [dict(pending)]

        processed: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for item in items:
            item_platform = str(item.get("platform") or platform).strip().lower()
            item_mode = str(item.get("document_mode") or "feishu").strip().lower()
            item_source_platform = str(
                item.get("source_platform") or platform
            ).strip().lower()
            item_open_id = str(
                item.get("requester_open_id") or requester_open_id
            ).strip()
            item_message_id = str(item.get("source_message_id") or "").strip()
            try:
                if item_platform != platform:
                    raise RuntimeError("待处理请求的授权平台与当前授权不一致")
                record = await self._write_row(
                    platform=platform,
                    source_platform=item_source_platform,
                    document_mode=item_mode,
                    requester_open_id=item_open_id,
                    requester_union_id=str(item.get("requester_union_id") or ""),
                    requester_user_id=str(item.get("requester_user_id") or ""),
                    text=str(item.get("input_text") or "").strip(),
                    access_token=access_token,
                )
            except Exception as exc:
                message = _safe_error(exc, access_token)
                failures.append(
                    {
                        "source_message_id": item_message_id,
                        "document_mode": item_mode,
                        "message": message,
                    }
                )
                try:
                    if item_message_id:
                        await self.relay.api_for_conversation(
                            f"{item_source_platform}:workflow"
                        ).reply(
                            item_message_id,
                            f"授权完成，但未能写入 {self.mode_label(item_mode)}：{message}",
                        )
                except Exception as reply_exc:
                    print(
                        "Bitable queued error reply failed: "
                        f"{_safe_error(reply_exc)}"
                    )
                continue

            result = {
                "source_message_id": item_message_id,
                "document_mode": item_mode,
                "source_platform": item_source_platform,
                "open_id": str(
                    record.get("_authorized_open_id") or item_open_id
                ),
                "record_id": str(record.get("record_id") or ""),
            }
            processed.append(result)
            try:
                if item_message_id:
                    await self.relay.api_for_conversation(
                        f"{item_source_platform}:workflow"
                    ).reply(
                        item_message_id,
                        f"授权成功，已按你的 {platform} 账号写入 "
                        f"{self.mode_label(item_mode)}，任务执行人="
                        f"{result['open_id']}，record_id={result['record_id']}",
                    )
            except Exception as reply_exc:
                print(
                    "Bitable queued success reply failed: "
                    f"{_safe_error(reply_exc)}"
                )

        if not processed:
            details = failures[0]["message"] if failures else "没有可处理的请求"
            raise RuntimeError(f"所有待处理请求均写入失败：{details}")

        expires_in = int(token_data.get("expires_in") or 7200)
        await self.relay.state.save_user_token(
            platform=platform,
            open_id=requester_open_id,
            access_token=access_token,
            refresh_token=str(token_data.get("refresh_token") or ""),
            expires_at=_now() + max(expires_in, 60),
        )
        first = processed[0]
        return {
            "platform": platform,
            "document_mode": first["document_mode"],
            "source_platform": first["source_platform"],
            "open_id": first["open_id"],
            "record_id": first["record_id"],
            "processed_count": len(processed),
            "failed_count": len(failures),
            "records": processed,
            "failures": failures,
        }


class AgentRelayWorkflow:
    """Keep the pre-existing Workspace Agent relay path isolated.

    The dedicated Bitable test-group flow is selected before this class is
    called. Every other group continues through this unchanged relay contract.
    """

    def __init__(self, relay: CloudflareRelay) -> None:
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
        input_text = _conversation_input(
            request_id=request_id,
            conversation_key=conversation_key,
            text=str(event.get("text") or ""),
            continuation=continuation,
            image_keys=event.get("image_keys") or [],
        )
        await self.relay.state.create_run(
            request_id=request_id,
            conversation_key=conversation_key,
            source_message_id=str(event["message_id"]),
            input_markdown=input_text,
            image_keys=event.get("image_keys") or [],
        )
        prefix = platform.upper()
        await self.relay._enqueue(
            {
                "kind": "agent",
                "request_id": request_id,
                "idempotency_key": f"{_env(self.relay.env, f'{prefix}_APP_ID')}:{event['message_id']}",
            }
        )


class CloudflareRelay:
    def __init__(self, env: Any, ctx: Any, db_state: D1State) -> None:
        self.env = env
        self.ctx = ctx
        self.state = db_state
        self.feishu = FeishuAPI(env)
        self.lark = (
            FeishuAPI(env, "lark")
            if _env(env, "LARK_APP_ID") and _env(env, "LARK_APP_SECRET")
            else None
        )
        self.bitable_workflow = BitableGroupWorkflow(self)
        # The dedicated Bitable test group is handled synchronously below. All
        # other groups use the Workspace Agent relay, including the calendar
        # Agent configured by WORKSPACE_AGENT_RELAY_TRIGGER_URL.
        self.agent_workflow = AgentRelayWorkflow(self)

    async def bitable_automation_webhook(self, request: Any) -> Response:
        """Receive a Bitable AI/workflow HTTP action and post it to 小 C's group.

        This endpoint intentionally uses the Feishu bot tenant token.  The
        workflow is an outbound notification from the table, so it must not
        consume or impersonate any user's OAuth token.
        """

        if str(request.method or "").upper() != "POST":
            return _response(
                {
                    "success": False,
                    "error": "method_not_allowed",
                    "message": "Bitable automation webhook accepts POST requests only",
                },
                status=405,
                headers={"allow": "POST"},
            )

        expected = str(_env(self.env, "BITABLE_AUTOMATION_WEBHOOK_TOKEN", "") or "").strip()
        if expected:
            provided = str(
                request.headers.get("x-bitable-webhook-token")
                or request.headers.get("authorization")
                or ""
            ).strip()
            if provided.lower().startswith("bearer "):
                provided = provided[7:].strip()
            if not hmac.compare_digest(provided, expected):
                return _response(
                    {"success": False, "error": "invalid_webhook_token"},
                    status=401,
                )

        try:
            # Cloudflare Request bodies are single-use streams.  Reading
            # ``json()`` and then falling back to ``text()`` can raise
            # ``Body already used`` even when the original payload is valid.
            # Read the stream once and decode it locally instead.
            raw = await request.text()
            try:
                body = json.loads(raw)
            except (TypeError, ValueError):
                body = raw
            if body in (None, "", [], {}):
                return _response(
                    {
                        "success": False,
                        "error": "missing_body",
                        "message": "The HTTP action must send the AI analysis result in its request body",
                    },
                    status=400,
                )

            text = _format_bitable_automation_text(body)
            chat_id = str(
                _env(
                    self.env,
                    "BITABLE_WORKFLOW_GROUP_CHAT_ID",
                    BITABLE_WORKFLOW_GROUP_CHAT_ID,
                )
                or BITABLE_WORKFLOW_GROUP_CHAT_ID
            ).strip()
            message_id = await self.feishu.send_text(chat_id, text)
            return _response(
                {
                    "success": True,
                    "chat_id": chat_id,
                    "message_id": message_id,
                }
            )
        except Exception as exc:
            print(f"Bitable automation webhook forwarding failed: {_safe_error(exc, expected)}")
            return _response(
                {
                    "success": False,
                    "error": "forward_failed",
                    "message": _safe_error(exc, expected),
                },
                status=502,
            )

    def api_for_conversation(self, conversation_key: str) -> FeishuAPI:
        """Select the API client from the event's platform-prefixed key."""
        platform = str(conversation_key or "").split(":", 1)[0].lower()
        if platform == "lark":
            if self.lark is None:
                raise RuntimeError("Lark is not configured: set LARK_APP_ID and LARK_APP_SECRET")
            return self.lark
        return self.feishu

    async def detect_user_platform(
        self, event: dict[str, Any], source_platform: str
    ) -> str:
        """Choose the OAuth platform for a sender seen by a bot webhook.

        A Lark bot cannot be added to a Feishu external group, so the Feishu
        bot is the receiver for both cases.  The event does not expose a
        universal ``feishu``/``lark`` brand field.  The stable signal available
        to the receiver is whether the sender belongs to the webhook tenant:
        a sender from another tenant is treated as the Lark external-user path
        when a Lark app is configured.  Same-tenant senders stay on Feishu.
        Deployments with a known tenant mapping can override this without code
        changes through ``LARK_EXTERNAL_TENANT_KEYS``.
        """
        source = str(source_platform or "feishu").strip().lower() or "feishu"
        if source not in {"feishu", "lark"}:
            source = "feishu"
        if self.lark is None:
            return source

        explicit = str(
            event.get("tenant_brand")
            or event.get("platform")
            or event.get("brand")
            or ""
        ).strip().lower()
        if explicit in {"feishu", "lark"}:
            return explicit

        sender_tenant = str(event.get("sender_tenant_key") or "").strip()
        event_tenant = str(event.get("tenant_key") or "").strip()
        configured = {
            item.strip()
            for item in _env(self.env, "LARK_EXTERNAL_TENANT_KEYS", "").split(",")
            if item.strip()
        }
        if sender_tenant and sender_tenant in configured:
            return "lark"
        if sender_tenant and event_tenant and sender_tenant != event_tenant:
            return "lark"

        # Some external-group payloads normalize both tenant fields to the
        # receiving Feishu tenant.  In that case resolve the sender ID through
        # each configured platform's contact API.  The IDs are app/tenant
        # scoped, so a successful resolution is a stronger signal than the
        # webhook URL that happened to receive the event.
        identifiers = (
            (str(event.get("open_id") or "").strip(), "open_id"),
            (str(event.get("union_id") or "").strip(), "union_id"),
            (str(event.get("user_id") or "").strip(), "user_id"),
        )
        resolved: list[str] = []
        for candidate, api in (("feishu", self.feishu), ("lark", self.lark)):
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
        return source

    async def _bitable_document_mode(
        self, *, source_platform: str, event: dict[str, Any]
    ) -> str | None:
        """Resolve a two-turn document choice without changing account auth.

        A reply to a mode prompt is authoritative for that message. The
        per-user mode table is retained as a compatibility fallback for a
        fresh @ message that is not threaded under a prompt.
        """
        parent_id = str(event.get("parent_id") or "").strip()
        if parent_id:
            selected = await self.state.bitable_group_mode_for_prompt(
                prompt_message_id=parent_id,
                source_platform=source_platform,
                chat_id=str(event.get("chat_id") or ""),
                requester_open_id=str(event.get("open_id") or ""),
            )
            if selected in {"feishu", "lark"}:
                return selected
        selected = await self.state.bitable_group_mode(
            source_platform=source_platform,
            chat_id=str(event.get("chat_id") or ""),
            requester_open_id=str(event.get("open_id") or ""),
        )
        return selected if selected in {"feishu", "lark"} else None

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

    def platform_oauth_scope(self, platform: str = "feishu") -> str:
        # The test scripts resolve the supplied Wiki URL to a Bitable app
        # token before reading/writing records.  Request the Bitable write
        # permission and the Wiki node read permission up front.  The
        # optional offline_access scope lets the returned token include a
        # refresh token when the app has enabled it.
        prefix = str(platform or "feishu").upper()
        return _env(
            self.env,
            f"{prefix}_OAUTH_SCOPE",
            "bitable:app wiki:wiki:readonly offline_access",
        ) or "bitable:app wiki:wiki:readonly offline_access"

    def feishu_oauth_scope(self) -> str:
        """Backward-compatible Feishu scope accessor."""
        return self.platform_oauth_scope("feishu")

    def feishu_oauth_ttl(self, platform: str = "feishu") -> int:
        try:
            prefix = str(platform or "feishu").upper()
            return max(int(_env(self.env, f"{prefix}_OAUTH_STATE_TTL_SECONDS", "600")), 60)
        except ValueError:
            return 600

    async def feishu_oauth(
        self, request: Any, path: str, platform: str = "feishu"
    ) -> Response:
        """Handle Feishu or Lark user OAuth on the Cloudflare Worker.

        This is the Worker equivalent of the legacy Python ASGI routes.  The
        authorization nonce is kept in D1 so a cold start or a second Worker
        instance cannot lose the callback state.
        """
        if request.method != "GET":
            return _response(
                {"error": "method_not_allowed", "message": "Feishu OAuth accepts GET requests only"},
                status=405,
                headers={"allow": "GET"},
            )
        params = parse_qs(urlparse(request.url).query, keep_blank_values=True)
        normalized = str(platform or "feishu").strip().lower() or "feishu"
        if normalized not in {"feishu", "lark"}:
            return _response({"success": False, "error": "unsupported_platform"}, status=400)
        prefix = normalized.upper()
        auth_base = (
            "https://accounts.larksuite.com"
            if normalized == "lark"
            else FEISHU_AUTH_BASE_URL
        )
        if path == f"/{normalized}/oauth/authorize":
            callback_uri = str((params.get("callback_uri") or [""])[0]).strip()
            if not callback_uri:
                callback_uri = _env(
                    self.env,
                    f"{prefix}_OAUTH_REDIRECT_URI",
                    self.base_url() + f"/{normalized}/oauth/callback",
                )
            if not callback_uri:
                return _response(
                    {
                        "success": False,
                        "error": "missing_redirect_uri",
                        "message": (
                            "Pass ?callback_uri=... or configure FEISHU_OAUTH_REDIRECT_URI."
                        ),
                    },
                    status=400,
                )
            if not _allowed_redirect(callback_uri):
                return _response(
                    {"success": False, "error": "invalid_redirect_uri"}, status=400
                )
            await self.state.ensure_feishu_oauth_schema()
            state = f"{normalized}_state_" + secrets.token_urlsafe(32)
            expires_at = _now() + self.feishu_oauth_ttl(normalized)
            await self.state.save_feishu_oauth_state(state, callback_uri, expires_at)
            location = (
                f"{auth_base}/open-apis/authen/v1/authorize?"
                + urlencode(
                    {
                        "app_id": _env(self.env, f"{prefix}_APP_ID"),
                        "redirect_uri": callback_uri,
                        "scope": self.platform_oauth_scope(normalized),
                        "state": state,
                    }
                )
            )
            return _text_response("", 302, {"Location": location})

        if path == f"/{normalized}/oauth/callback":
            code = str((params.get("code") or [""])[0]).strip()
            state = str((params.get("state") or [""])[0]).strip()
            if not code:
                return _response(
                    {"success": False, "error": "missing_code"}, status=400
                )
            redirect_uri = ""
            pending: dict[str, Any] | None = None
            if state:
                await self.state.ensure_feishu_oauth_schema()
                consume_pending = getattr(self.state, "consume_bitable_pending", None)
                pending = await consume_pending(state) if consume_pending is not None else None
                record = None if pending is not None else await self.state.consume_feishu_oauth_state(state)
                if record is None and pending is None:
                    return _response(
                        {
                            "success": False,
                            "error": "unknown_or_already_consumed_state",
                        },
                        status=400,
                    )
                if int((pending or record or {}).get("expires_at", 0)) < _now():
                    return _response(
                        {"success": False, "error": "expired_state"}, status=400
                    )
                redirect_uri = str((pending or record or {}).get("redirect_uri") or "").strip()
            if not redirect_uri:
                redirect_uri = _env(
                    self.env,
                    f"{prefix}_OAUTH_REDIRECT_URI",
                    self.base_url() + f"/{normalized}/oauth/callback",
                ).strip()

            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": _env(self.env, f"{prefix}_APP_ID"),
                "client_secret": _env(self.env, f"{prefix}_APP_SECRET"),
                "redirect_uri": redirect_uri,
            }
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(
                        f"{auth_base}/oauth/v3/token",
                        data=payload,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
            except httpx.HTTPError as exc:
                return _response(
                    {"success": False, "error": "token_exchange_network_error", "message": _safe_error(exc)},
                    status=502,
                )
            try:
                body = response.json()
            except ValueError:
                body = {}
            data = body.get("data") if isinstance(body, dict) else None
            # OAuth v3 returns token fields at the top level;
            # keep the nested form for compatibility with older gateways.
            if not isinstance(data, dict):
                data = body if isinstance(body, dict) else {}
            if not data.get("access_token"):
                message = f"{normalized.title()} did not return an access_token"
                if isinstance(body, dict):
                    message = str(
                        body.get("error_description")
                        or body.get("msg")
                        or body.get("message")
                        or message
                    )
                return _response(
                    {"success": False, "error": "token_exchange_failed", "message": message},
                    status=502,
                )
            if pending is not None:
                try:
                    result = await self.bitable_workflow.complete_oauth(pending, data)
                except Exception as exc:
                    # Do not expose access tokens or raw upstream payloads in a
                    # browser response. The initiating message receives the
                    # success/error reply when the user account can be verified.
                    message = _safe_error(exc, str(data.get("access_token") or ""))
                    try:
                        source_platform = str(
                            pending.get("source_platform") or normalized
                        ).strip().lower()
                        await self.api_for_conversation(
                            f"{source_platform}:workflow"
                        ).reply(
                            str(pending.get("source_message_id") or ""),
                            f"授权完成，但未能写入多维表格：{message}",
                        )
                    except Exception as reply_exc:
                        print(f"Bitable workflow error reply failed: {_safe_error(reply_exc)}")
                    return _response(
                        {"success": False, "error": "bitable_write_failed", "message": message},
                        status=502,
                    )
                return _response(
                    {"success": True, "workflow": "bitable_group", **result}
                )
            return _response({"success": True, "token": data})

        return _response({"error": "not_found"}, 404)

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
                        "If input is required, use ask_user; it delivers the question to the "
                        "current Feishu/Lark reply and keeps the run resumable. "
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

    @staticmethod
    async def _image_bytes_from_args(args: dict[str, Any]) -> bytes:
        """Decode an image supplied to the outbound image MCP tool.

        Workspace Agents cannot pass a binary attachment directly as an MCP
        argument, so accept either a data URL/base64 payload or an HTTPS image
        URL.  The bytes are validated against their actual image signature
        before they are uploaded to Feishu/Lark.
        """
        image_url = str(
            args.get("image_url") or args.get("data_url") or ""
        ).strip()
        image_base64 = str(
            args.get("image_base64") or args.get("base64") or ""
        ).strip()
        data: bytes
        if image_url.startswith("data:"):
            try:
                header, encoded = image_url.split(",", 1)
            except ValueError as exc:
                raise ValueError("image_url data URL is malformed") from exc
            if ";base64" not in header.lower():
                raise ValueError("image_url data URL must use base64 encoding")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError("image_url data URL contains invalid base64") from exc
        elif image_url:
            parsed = urlparse(image_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("image_url must be an http(s) URL or a data URL")
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(image_url)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"image URL download failed with HTTP {response.status_code}"
                )
            data = response.content
        elif image_base64:
            if image_base64.startswith("data:"):
                try:
                    _, encoded = image_base64.split(",", 1)
                except ValueError as exc:
                    raise ValueError("image_base64 data URL is malformed") from exc
            else:
                encoded = image_base64
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise ValueError("image_base64 contains invalid base64") from exc
        else:
            raise ValueError("provide image_url or image_base64")
        if not data:
            raise ValueError("image is empty")
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("image exceeds Feishu's 10MB limit")
        _image_upload_metadata(data, filename_prefix="image")
        return data

    async def _send_agent_image(
        self, request_id: str, conversation_key: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Upload one Agent image and reply to the originating message."""

        run = await self._require_run(request_id, conversation_key)
        source_message_id = str(run.get("source_message_id") or "").strip()
        if not source_message_id:
            raise ValueError("the current relay run has no source message")
        data = await self._image_bytes_from_args(args)
        api = self.api_for_conversation(conversation_key)
        image_key = await api.upload_message_image(data)
        outbound_id = await api.reply_image(source_message_id, image_key)
        await self.state.save_reply(outbound_id, conversation_key)
        caption = str(args.get("caption") or "").strip()
        caption_id = None
        if caption:
            caption_id = await api.reply(source_message_id, caption)
            await self.state.save_reply(caption_id, conversation_key)
        return {
            "success": True,
            "request_id": request_id,
            "conversation_key": conversation_key,
            "message_id": outbound_id,
            "caption_message_id": caption_id,
        }

    @staticmethod
    def _result_image_args(args: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize optional image payloads accepted by ``record_result``."""

        values: list[dict[str, Any]] = []
        images = args.get("images")
        if isinstance(images, dict):
            images = [images]
        if isinstance(images, list):
            values.extend(item for item in images if isinstance(item, dict))
        if args.get("image_url") or args.get("image_base64"):
            values.insert(
                0,
                {
                    key: args[key]
                    for key in ("image_url", "image_base64", "mime_type", "caption")
                    if args.get(key)
                },
            )
        return values

    def tool_definitions(self) -> list[dict[str, Any]]:
        string = {"type": "string"}
        read_only = {"readOnlyHint": True}
        return [
            {
                "name": "server_info",
                "description": "Return Cloudflare Worker relay information.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": read_only,
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
                "description": (
                    "Record the final result exactly once. If the Agent produced an image, "
                    "send it in images (or image_url/image_base64) so the relay forwards it "
                    "to the quoted Feishu/Lark message before the text result is delivered."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key", "status", "title", "markdown"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "status": {"type": "string", "enum": ["done", "failed", "blocked"]},
                        "title": string,
                        "markdown": string,
                        "image_url": string,
                        "image_base64": string,
                        "mime_type": string,
                        "caption": string,
                        "images": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "image_url": string,
                                    "image_base64": string,
                                    "mime_type": string,
                                    "caption": string,
                                },
                            },
                        },
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
                "description": (
                    "Pause the current turn with a question for the Feishu/Lark user. "
                    "The relay delivers it to the current reply and resumes the same "
                    "conversation when the user answers."
                ),
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
                "annotations": read_only,
            },
            {
                "name": "get_requester_info",
                "description": "Return the Feishu user who mentioned the bot.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string},
                },
                "annotations": read_only,
            },
            {
                "name": "get_user_images",
                "description": (
                    "Return the image attachments from the current Feishu/Lark user message "
                    "as MCP image content so the Agent can inspect them. Call this before "
                    "claiming to have seen or analyzed a user image."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                    },
                },
                "annotations": read_only,
            },
            {
                "name": "send_image",
                "description": (
                    "Send an Agent-produced image back to the user's quoted Feishu/Lark "
                    "message. Provide image_url (HTTPS or a base64 data URL) or image_base64. "
                    "The image is uploaded through the correct platform bot and sent as an "
                    "image reply; use this tool instead of only embedding a Markdown image "
                    "link in record_result."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["request_id", "conversation_key"],
                    "properties": {
                        "request_id": string,
                        "conversation_key": string,
                        "image_url": string,
                        "image_base64": string,
                        "mime_type": string,
                        "caption": string,
                    },
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
                "name": "search_contacts",
                "description": (
                    "Resolve a Feishu contact by mobile number or email and return candidate "
                    "open_ids. Name-only lookup requires a user token and is unavailable "
                    "here. Show candidates and obtain explicit user confirmation before "
                    "inviting anyone."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key", "query"],
                    "properties": {"conversation_key": string, "query": string},
                },
                "annotations": read_only,
            },
            {
                "name": "create_group",
                "description": (
                    "Create a Feishu group containing exactly the explicitly confirmed "
                    "member_open_ids. The requester who @mentioned the bot is not added "
                    "automatically. Call search_contacts first, show the candidate(s), "
                    "and wait for explicit confirmation before this write operation. "
                    "Returns the new chat_id."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key", "name", "member_open_ids"],
                    "properties": {
                        "conversation_key": string,
                        "name": string,
                        "member_open_ids": {"type": "array", "items": string},
                    },
                },
            },
            {
                "name": "add_group_members",
                "description": (
                    "Add exactly the explicitly confirmed member_open_ids to an existing "
                    "Feishu group. chat_id may be omitted to reuse the persisted group. "
                    "Do not add the requester or any other person automatically; resolve "
                    "contacts first and obtain explicit confirmation. Uses open_id values "
                    "and returns added, invalid, and pending-approval IDs."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key", "member_open_ids"],
                    "properties": {
                        "conversation_key": string,
                        "chat_id": string,
                        "member_open_ids": {"type": "array", "items": string},
                    },
                },
            },
            {
                "name": "get_group_info",
                "description": (
                    "Read current settings for an existing regular Feishu group. chat_id may "
                    "be omitted to reuse the persisted group. Use this before changing group "
                    "permissions; this tool never creates or changes a group."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {
                        "conversation_key": string,
                        "chat_id": string,
                    },
                },
                "annotations": read_only,
            },
            {
                "name": "update_group",
                "description": (
                    "Update the SAME existing regular Feishu group; never create a new one. "
                    "chat_id may be omitted to reuse the persisted group. Supported fields: "
                    "name (<=60 chars), description (<=100), avatar_image_key, "
                    "set_avatar_from_stored, i18n_names, "
                    "add_member_permission, share_card_permission, at_all_permission, "
                    "edit_permission, owner_id, join_message_visibility, "
                    "leave_message_visibility, membership_approval, chat_type, "
                    "group_message_type, urgent_setting, video_conference_setting, "
                    "pin_manage_setting, and hide_member_count_setting. Values are checked "
                    "against Feishu's documented enum values; add_member_permission and "
                    "share_card_permission must be consistent."
                ),
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {
                        "conversation_key": string,
                        "chat_id": string,
                        "name": string,
                        "avatar_image_key": string,
                        "set_avatar_from_stored": {"type": "boolean"},
                        "description": string,
                        "i18n_names": {"type": "object", "additionalProperties": {"type": "string"}},
                        "add_member_permission": string,
                        "share_card_permission": string,
                        "at_all_permission": string,
                        "edit_permission": string,
                        "owner_id": string,
                        "join_message_visibility": string,
                        "leave_message_visibility": string,
                        "membership_approval": string,
                        "chat_type": string,
                        "group_message_type": string,
                        "urgent_setting": string,
                        "video_conference_setting": string,
                        "pin_manage_setting": string,
                        "hide_member_count_setting": string,
                    },
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
                "annotations": read_only,
            },
            {
                "name": "get_stored_image",
                "description": "Read whether an avatar is stored in R2.",
                "inputSchema": {
                    "type": "object",
                    "required": ["conversation_key"],
                    "properties": {"conversation_key": string},
                },
                "annotations": read_only,
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
            image_results: list[dict[str, Any]] = []
            try:
                for image_args in self._result_image_args(args):
                    image_results.append(
                        await self._send_agent_image(
                            request_id, conversation_key, image_args
                        )
                    )
            except Exception as exc:
                # Keep the run open when an explicitly supplied image could not
                # be delivered. The Agent can retry or return an explicit
                # failed result with the actual blocker.
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
            return self._tool_result(
                {
                    "success": True,
                    "request_id": request_id,
                    "status": status,
                    "images_sent": image_results,
                }
            )
        if name == "update_conversation_title":
            await self._require_run(request_id, conversation_key)
            await self.state.update_run(request_id, title=str(args.get("title") or ""))
            return self._tool_result({"success": True, "title": args.get("title", "")})
        if name == "ask_user":
            await self._require_run(request_id, conversation_key)
            question = str(args.get("question") or "")
            choices = args.get("choices") or []
            question_text = _format_user_question(question, choices)
            await self.state.update_run(
                request_id, status="needs_user", progress_message=question_text
            )
            # ``ask_user`` is a pause point, not a terminal Agent result.  Put
            # a separate delivery job on the same queue so the question is
            # visible in Feishu while the run stays resumable.
            await self._enqueue({"kind": "deliver_question", "request_id": request_id})
            return self._tool_result(
                {
                    "success": True,
                    "status": "needs_user",
                    "question": question,
                    "choices": choices,
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
        if name == "get_user_images":
            run = await self._require_run(request_id, conversation_key)
            image_keys = run.get("image_keys") if isinstance(run.get("image_keys"), list) else []
            source_message_id = str(run.get("source_message_id") or "").strip()
            if not image_keys or not source_message_id:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "user_images_missing",
                            "message": "the current Feishu/Lark message has no image attachment",
                        },
                    },
                    True,
                )
            api = self.api_for_conversation(conversation_key)
            content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": _json(
                        {
                            "success": True,
                            "request_id": request_id,
                            "conversation_key": conversation_key,
                            "image_count": len(image_keys),
                            "message": "The following image blocks are the user's current attachments.",
                        }
                    ),
                }
            ]
            metadata: list[dict[str, Any]] = []
            try:
                for image_key in image_keys[:3]:
                    key = str(image_key or "").strip()
                    if not key:
                        continue
                    data = await api.download_image(source_message_id, key)
                    if len(data) > 10 * 1024 * 1024:
                        raise RuntimeError("an attached image exceeds Feishu's 10MB limit")
                    _filename, mime_type = _image_upload_metadata(
                        data, filename_prefix="image"
                    )
                    metadata.append(
                        {"image_key": key, "mime_type": mime_type, "size": len(data)}
                    )
                    content.append(
                        {
                            "type": "image",
                            "data": base64.b64encode(data).decode("ascii"),
                            "mimeType": mime_type,
                        }
                    )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "user_image_download_failed",
                            "message": _safe_error(exc),
                        },
                    },
                    True,
                )
            return self._tool_result(
                {
                    "success": True,
                    "request_id": request_id,
                    "conversation_key": conversation_key,
                    "images": metadata,
                },
                content=content,
            )
        if name == "send_image":
            try:
                result = await self._send_agent_image(
                    request_id, conversation_key, args
                )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "image_send_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            return self._tool_result(result)
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
                chat_id = await self.api_for_conversation(conversation_key).create_private_group(
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
        if name == "search_contacts":
            query = str(args.get("query") or "").strip()
            if not query:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_query",
                            "message": "contact search query is required",
                        },
                    },
                    True,
                )
            try:
                candidates = await self.api_for_conversation(conversation_key).search_contacts(query)
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "search_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            return self._tool_result(
                {
                    "success": True,
                    "conversation_key": conversation_key,
                    "query": query,
                    "candidates": candidates,
                }
            )
        if name == "create_group":
            group_name = str(args.get("name") or "").strip()
            raw_members = args.get("member_open_ids")
            if not group_name:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "invalid_name", "message": "group name is required"},
                    },
                    True,
                )
            if not isinstance(raw_members, list):
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_members",
                            "message": "member_open_ids must be an array",
                        },
                    },
                    True,
                )
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
            members: list[str] = []
            for open_id in raw_members:
                value = str(open_id or "").strip()
                if value and value not in members:
                    members.append(value)
            if not members:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_members",
                            "message": "at least one member open_id is required",
                        },
                    },
                    True,
                )
            try:
                chat_id = await self.api_for_conversation(conversation_key).create_group(
                    group_name, members
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
                    "member_open_ids": members,
                }
            )
        if name == "add_group_members":
            target_chat_id = str(args.get("chat_id") or "").strip()
            if not target_chat_id:
                row = await self.state.requester(conversation_key)
                target_chat_id = str((row or {}).get("group_chat_id") or "").strip()
            if not target_chat_id:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "group_missing",
                            "message": "no existing group found; pass chat_id or create it first",
                        },
                    },
                    True,
                )
            raw_members = args.get("member_open_ids")
            if not isinstance(raw_members, list):
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_members",
                            "message": "member_open_ids must be an array of confirmed open_id values",
                        },
                    },
                    True,
                )
            members: list[str] = []
            for open_id in raw_members:
                value = str(open_id or "").strip()
                if value and value not in members:
                    members.append(value)
            if not members:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_members",
                            "message": "member_open_ids must contain at least one confirmed open_id",
                        },
                    },
                    True,
                )
            if len(members) > 50:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "invalid_members",
                            "message": "at most 50 members can be added in one request",
                        },
                    },
                    True,
                )
            try:
                result = await self.api_for_conversation(conversation_key).add_group_members(
                    target_chat_id, members
                )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "chat_id": target_chat_id,
                        "requested_member_open_ids": members,
                        "error": {"code": "add_members_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            return self._tool_result(
                {
                    "success": True,
                    "conversation_key": conversation_key,
                    **result,
                }
            )
        if name == "get_group_info":
            target_chat_id = str(args.get("chat_id") or "").strip()
            if not target_chat_id:
                row = await self.state.requester(conversation_key)
                target_chat_id = str((row or {}).get("group_chat_id") or "").strip()
            if not target_chat_id:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "group_missing",
                            "message": "no created group found; create it first or pass chat_id",
                        },
                    },
                    True,
                )
            try:
                settings = await self.api_for_conversation(conversation_key).get_chat(
                    target_chat_id
                )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "read_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            return self._tool_result(
                {"success": True, "chat_id": target_chat_id, "settings": settings}
            )
        if name == "update_group":
            target_chat_id = str(args.get("chat_id") or "").strip()
            if not target_chat_id:
                row = await self.state.requester(conversation_key)
                target_chat_id = str((row or {}).get("group_chat_id") or "").strip()
            if not target_chat_id:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "group_missing",
                            "message": "no created group found; create it first or pass chat_id",
                        },
                    },
                    True,
                )
            update_fields = {
                "name",
                "avatar_image_key",
                "description",
                "i18n_names",
                "add_member_permission",
                "share_card_permission",
                "at_all_permission",
                "edit_permission",
                "owner_id",
                "join_message_visibility",
                "leave_message_visibility",
                "membership_approval",
                "chat_type",
                "group_message_type",
                "urgent_setting",
                "video_conference_setting",
                "pin_manage_setting",
                "hide_member_count_setting",
            }
            use_latest_image = bool(args.get("set_avatar_from_stored"))
            changes = {
                key: args[key]
                for key in update_fields
                if key in args and args[key] is not None
            }
            if use_latest_image:
                latest = await self.state.latest_image_run(conversation_key)
                image_keys = (latest or {}).get("image_keys") if latest else []
                source_message_id = str((latest or {}).get("source_message_id") or "")
                if not image_keys or not source_message_id:
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "avatar_image_missing",
                                "message": "no image is stored for this conversation; ask the user to send an image",
                            },
                        },
                        True,
                    )
                try:
                    image_data = await self.api_for_conversation(conversation_key).download_image(
                        source_message_id, str(image_keys[-1])
                    )
                except Exception as exc:
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {
                                "code": "avatar_source_download_failed",
                                "message": _safe_error(exc),
                            },
                        },
                        True,
                    )
                try:
                    changes["avatar_image_key"] = await self.api_for_conversation(
                        conversation_key
                    ).upload_avatar_image(image_data)
                except Exception as exc:
                    return self._tool_result(
                        {
                            "success": False,
                            "error": {"code": "avatar_upload_failed", "message": _safe_error(exc)},
                        },
                        True,
                    )
            if not changes:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {
                            "code": "no_updates",
                            "message": "provide at least one group field to update",
                        },
                    },
                    True,
                )
            invalid = _validate_group_updates(changes)
            if invalid:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "invalid_setting", "message": invalid},
                    },
                    True,
                )
            try:
                await self.api_for_conversation(conversation_key).update_chat(
                    target_chat_id, changes
                )
            except Exception as exc:
                return self._tool_result(
                    {
                        "success": False,
                        "error": {"code": "update_failed", "message": _safe_error(exc)},
                    },
                    True,
                )
            return self._tool_result(
                {
                    "success": True,
                    "chat_id": target_chat_id,
                    "updated_fields": sorted(changes),
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
            latest = await self.state.latest_image_run(conversation_key)
            return self._tool_result(
                {
                    "success": True,
                    "has_avatar": bool(row or latest),
                    "size": row.get("size") if row else None,
                    "source_message_id": (latest or {}).get("source_message_id"),
                }
            )
        return self._tool_result(
            {
                "success": False,
                "error": {"code": "unknown_tool", "message": f"unknown tool {name}"},
            },
            True,
        )

    @staticmethod
    def _tool_result(
        value: dict[str, Any],
        is_error: bool = False,
        *,
        content: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "content": content or [{"type": "text", "text": _json(value)}],
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
        elif body.get("kind") == "deliver_question":
            try:
                await self.deliver_question(str(body.get("request_id") or ""))
            except Exception as exc:
                print(f"Local question delivery failed: {_safe_error(exc)}")
        else:
            await self.run_agent_job(body)

    async def _enqueue_feishu_event(self, body: dict[str, Any], platform: str) -> None:
        """Move full Feishu/Lark event processing out of the webhook request.

        Feishu requires the developer-server acknowledgement within a few
        seconds.  Queue delivery is the durable hand-off; the local fallback
        keeps tests and non-Queue development environments functional.
        """
        queue = getattr(self.env, "AGENT_QUEUE", None)
        payload = {
            "kind": "feishu_event",
            "platform": platform,
            "body": body,
        }
        if queue is not None:
            await queue.send(payload)
            return
        await self._process_feishu_event(body, platform)

    async def _schedule_feishu_event(self, body: dict[str, Any], platform: str) -> None:
        """Schedule event processing without delaying the webhook response."""
        task = self._enqueue_feishu_event(body, platform)
        waiter = getattr(self.ctx, "wait_until", None) or getattr(
            self.ctx, "waitUntil", None
        )
        if callable(waiter):
            waiter(task)
            return
        # The Cloudflare runtime always exposes wait_until.  Awaiting here is
        # only a deterministic fallback for local tests/dev harnesses.
        await task

    async def _store_run_images(self, run: dict[str, Any]) -> None:
        """Persist only explicitly attached Feishu images in the optional R2 bucket."""
        image_keys = run.get("image_keys") if isinstance(run.get("image_keys"), list) else []
        bucket = getattr(self.env, "AVATARS", None)
        if not image_keys or bucket is None:
            return
        for image_key in image_keys[:3]:
            if not isinstance(image_key, str) or not image_key:
                continue
            data = await self.api_for_conversation(str(run["conversation_key"])).download_image(
                str(run["source_message_id"]), image_key
            )
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
                placeholder_id = await self.api_for_conversation(
                    str(run["conversation_key"])
                ).reply(
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
                        "OpenAI-Beta": TRIGGER_RUNS_BETA,
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
                    await self.api_for_conversation(str(run["conversation_key"])).update(
                        placeholder, text
                    )
                    outbound = placeholder
                except Exception as exc:
                    # Keep delivery reliable if an old message is no longer
                    # editable (for example after Feishu's edit window). New
                    # messages use the PUT text-edit path above and normally
                    # stay in place.
                    print(f"Feishu message update failed; sending a reply: {_safe_error(exc)}")
                    outbound = await self.api_for_conversation(
                        str(run["conversation_key"])
                    ).reply(str(run["source_message_id"]), text)
            else:
                outbound = await self.api_for_conversation(
                    str(run["conversation_key"])
                ).reply(str(run["source_message_id"]), text)
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

    async def deliver_question(self, request_id: str) -> None:
        """Deliver an ``ask_user`` question without closing the Agent run.

        The in-progress placeholder is the reply anchor for the current
        conversation.  Editing it preserves the existing thread/reply
        mapping; if Feishu rejects the edit, send a new reply and bind that
        message to the same conversation so a quoted answer still resumes
        the run's conversation.
        """
        run = await self.state.get_run(request_id)
        if not run or str(run.get("status") or "") != "needs_user":
            return
        text = str(run.get("progress_message") or "请补充必要信息。")
        try:
            placeholder = str(run.get("placeholder_message_id") or "")
            if placeholder:
                try:
                    await self.api_for_conversation(str(run["conversation_key"])).update(
                        placeholder, text
                    )
                    outbound = placeholder
                except Exception as exc:
                    print(f"Feishu question update failed; sending a reply: {_safe_error(exc)}")
                    outbound = await self.api_for_conversation(
                        str(run["conversation_key"])
                    ).reply(str(run["source_message_id"]), text)
                    await self.state.update_run(
                        request_id, placeholder_message_id=str(outbound)
                    )
            else:
                outbound = await self.api_for_conversation(
                    str(run["conversation_key"])
                ).reply(str(run["source_message_id"]), text)
                await self.state.update_run(request_id, placeholder_message_id=str(outbound))
            await self.state.save_reply(outbound, str(run["conversation_key"]))
        except Exception as exc:
            await self.state.update_run(request_id, trigger_error=_safe_error(exc))
            raise

    async def handle_feishu(self, request: Any, platform: str = "feishu") -> Response:
        """Acknowledge a Feishu/Lark webhook before doing durable work.

        The platform retries when this handler takes longer than its three
        second request window.  Keep only body parsing, signature/challenge
        validation, and the bot-id guard on the request path.  All D1, API,
        reply, and Agent work is handed to the Queue consumer below.
        """
        normalized = str(platform or "feishu").strip().lower() or "feishu"
        if normalized not in {"feishu", "lark"}:
            return _response({"code": 1, "error": "unsupported_platform"}, status=400)
        prefix = normalized.upper()
        body = await self._body_json(request)
        header = body.get("header") if isinstance(body.get("header"), dict) else {}
        verify = _env(self.env, f"{prefix}_VERIFY_TOKEN")
        if verify and str(header.get("token") or "") != verify:
            return _response({"code": 1}, 403)
        if body.get("challenge"):
            return _response({"challenge": body["challenge"]})
        if str(header.get("event_type") or "") != "im.message.receive_v1":
            return _response({"code": 0})
        bot_id = _env(self.env, f"{prefix}_BOT_OPEN_ID")
        if not bot_id:
            # Do not make a Feishu API call in the webhook request.  Resolve
            # the value once with /open-apis/bot/v3/info and store it as a
            # Secret before enabling message processing.
            print(f"{prefix}_BOT_OPEN_ID is not configured; event ignored")
            return _response({"code": 0})
        await self._schedule_feishu_event(body, normalized)
        return _response({"code": 0})

    async def _process_feishu_event(
        self, body: dict[str, Any], platform: str = "feishu"
    ) -> None:
        """Run the former synchronous webhook body in the Queue consumer."""
        normalized = str(platform or "feishu").strip().lower() or "feishu"
        prefix = normalized.upper()
        bot_id = _env(self.env, f"{prefix}_BOT_OPEN_ID")
        if not bot_id:
            print(f"{prefix}_BOT_OPEN_ID is not configured; event ignored")
            return
        try:
            raw_event = body.get("event") if isinstance(body.get("event"), dict) else {}
            raw_message = (
                raw_event.get("message")
                if isinstance(raw_event.get("message"), dict)
                else {}
            )
            target_chat_id = str(raw_message.get("chat_id") or "")
            event = _normalize_event(
                body,
                bot_id,
                allow_unmentioned_reply=self.bitable_workflow.is_target_group(
                    target_chat_id
                ),
            )
            if event is None or not event["open_id"]:
                return _response({"code": 0})
            parent = event["parent_id"]
            conversation_key = await self.state.reply_conversation(parent) if parent else None
            if not conversation_key:
                conversation_key = f"{normalized}:{_env(self.env, f'{prefix}_APP_ID')}:{event['chat_id']}:{secrets.token_hex(6)}"
            request_id = _request_id(normalized)
            # Both the direct Bitable flow and the Agent relay use the same
            # durable event/run tables. Ensure them before claiming any event,
            # including messages from non-test groups that enter the Agent path.
            await self.state.ensure_schema()
            # Feishu rows already use the raw message id. Prefix only Lark
            # dedupe keys so old Feishu state remains readable while the two
            # platforms cannot suppress each other's messages.
            event_key = (
                event["message_id"]
                if normalized == "feishu"
                else f"{normalized}:{event['message_id']}"
            )
            if not await self.state.claim_event(
                message_id=event_key,
                request_id=request_id,
                conversation_key=conversation_key,
                chat_id=event["chat_id"],
                open_id=event["open_id"],
            ):
                return _response({"code": 0})
            await self.state.save_requester(
                conversation_key,
                event["open_id"],
                event["name"],
                event["chat_id"],
                normalized,
            )
            if self.bitable_workflow.is_target_group(event["chat_id"]):
                command = self.bitable_workflow.parse_document_command(event["text"])
                if command is not None:
                    selected_mode, payload = command
                    await self.state.save_bitable_group_mode(
                        source_platform=normalized,
                        chat_id=str(event["chat_id"]),
                        requester_open_id=str(event["open_id"]),
                        mode=selected_mode,
                    )
                    if not payload:
                        prompt_message_id = await self.bitable_workflow.reply_mode_prompt(
                            normalized, event, selected_mode
                        )
                        # Keep each outstanding two-turn choice attached to the
                        # exact bot prompt (and the command message itself, in
                        # case the client quotes that message) that the user
                        # can reply to. The per-user mode above remains the
                        # fallback for a new unthreaded @ message.
                        await self.state.save_bitable_group_mode_prompt(
                            prompt_message_id=prompt_message_id,
                            source_platform=normalized,
                            chat_id=str(event["chat_id"]),
                            requester_open_id=str(event["open_id"]),
                            mode=selected_mode,
                        )
                        if prompt_message_id != str(event["message_id"]):
                            await self.state.save_bitable_group_mode_prompt(
                                prompt_message_id=str(event["message_id"]),
                                source_platform=normalized,
                                chat_id=str(event["chat_id"]),
                                requester_open_id=str(event["open_id"]),
                                mode=selected_mode,
                            )
                        return _response({"code": 0})
                    event = dict(event)
                    event["text"] = payload
                else:
                    selected_mode = await self._bitable_document_mode(
                        source_platform=normalized,
                        event=event,
                    )
                    if selected_mode is None:
                        await self.bitable_workflow.reply_mode_required(normalized, event)
                        return _response({"code": 0})
                # The command chooses the destination document only. Resolve
                # Feishu vs Lark from the sender/event identity so selecting
                # ``[lark文档]`` can never force a Feishu user into Lark OAuth.
                account_platform = await self.detect_user_platform(event, normalized)
                await self.bitable_workflow.handle_event(
                    platform=account_platform,
                    source_platform=normalized,
                    document_mode=selected_mode,
                    conversation_key=conversation_key,
                    event=event,
                )
                return _response({"code": 0})
            await self.agent_workflow.handle_event(
                platform=normalized,
                conversation_key=conversation_key,
                event=event,
                request_id=request_id,
            )
        except Exception as exc:
            # The webhook has already been acknowledged.  Keep the error in
            # Worker logs and let Queue retry the message when it escapes.
            print(f"Feishu event processing failed: {_safe_error(exc)}")
            raise


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
                    "endpoints": [
                        BITABLE_AUTOMATION_WEBHOOK_PATH,
                        "/feishu/events",
                        "/feishu/oauth/authorize",
                        "/feishu/oauth/callback",
                        "/lark/events",
                        "/lark/oauth/authorize",
                        "/lark/oauth/callback",
                        "/mcp",
                        "/oauth/token",
                    ],
                }
            )
        if path == BITABLE_AUTOMATION_WEBHOOK_PATH:
            return await relay.bitable_automation_webhook(request)
        if path in {"/feishu/oauth/authorize", "/feishu/oauth/callback"}:
            return await relay.feishu_oauth(request, path, "feishu")
        if path in {"/lark/oauth/authorize", "/lark/oauth/callback"}:
            return await relay.feishu_oauth(request, path, "lark")
        if path in {"/feishu/events", "/feishu/event", "/lark/events", "/lark/event"}:
            if request.method == "POST":
                platform = "lark" if path.startswith("/lark/") else "feishu"
                return await relay.handle_feishu(request, platform)
            return _response(
                {
                    "error": "method_not_allowed",
                    "message": "Platform webhook endpoint accepts POST requests only",
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
                if body.get("kind") == "feishu_event":
                    await relay._process_feishu_event(
                        body.get("body") if isinstance(body.get("body"), dict) else {},
                        str(body.get("platform") or "feishu"),
                    )
                elif body.get("kind") == "deliver_result":
                    await relay.deliver_result(request_id)
                elif body.get("kind") == "deliver_question":
                    await relay.deliver_question(request_id)
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
