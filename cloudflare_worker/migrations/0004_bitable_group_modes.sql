-- Persist the document mode selected by each requester in the dedicated group.
-- The next @ message from that requester uses this mode until another command
-- switches it to the other document.
CREATE TABLE IF NOT EXISTS bitable_group_modes (
    source_platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (source_platform, chat_id, requester_open_id)
);
