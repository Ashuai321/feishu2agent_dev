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
