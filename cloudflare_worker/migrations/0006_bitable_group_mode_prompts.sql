-- Bind each two-turn document selection to the bot reply that prompted the
-- user's next message. This prevents concurrent [飞书文档]/[lark文档]
-- selections from overwriting one another in the per-user fallback row.
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
