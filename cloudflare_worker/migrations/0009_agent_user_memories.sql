CREATE TABLE IF NOT EXISTS agent_user_memories (
    agent_scope TEXT NOT NULL,
    platform TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    memory_value TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (agent_scope, platform, requester_open_id, memory_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_user_memories_lookup
    ON agent_user_memories(agent_scope, platform, requester_open_id, updated_at DESC);
