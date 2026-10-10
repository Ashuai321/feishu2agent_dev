ALTER TABLE feishu_events
    ADD COLUMN reaction_emoji TEXT NOT NULL DEFAULT '';

ALTER TABLE relay_runs
    ADD COLUMN confirmation_required INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS reply_message_context (
    outbound_message_id TEXT PRIMARY KEY,
    message_text TEXT NOT NULL DEFAULT '',
    confirmation_required INTEGER NOT NULL DEFAULT 0,
    request_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
