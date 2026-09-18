-- Persist platform-specific user authorization for the dedicated test-group flow.
-- Existing Feishu rows remain valid; the Worker also keeps an idempotent schema
-- fallback for deployments where migrations have not been applied yet.
CREATE TABLE IF NOT EXISTS bitable_pending_requests (
    state TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_platform TEXT NOT NULL DEFAULT 'feishu',
    redirect_uri TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_chat_id TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    requester_union_id TEXT NOT NULL DEFAULT '',
    requester_user_id TEXT NOT NULL DEFAULT '',
    input_text TEXT NOT NULL,
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
