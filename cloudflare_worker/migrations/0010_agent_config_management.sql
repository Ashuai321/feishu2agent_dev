CREATE TABLE IF NOT EXISTS agent_config_current (
    agent_scope TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    impact TEXT NOT NULL,
    value_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    current_version INTEGER NOT NULL,
    updated_by_platform TEXT NOT NULL,
    updated_by_open_id TEXT NOT NULL,
    updated_request_id TEXT NOT NULL,
    updated_conversation_key TEXT NOT NULL,
    last_operation TEXT NOT NULL,
    last_proposal_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (agent_scope, rule_key)
);

CREATE TABLE IF NOT EXISTS agent_config_proposals (
    proposal_id TEXT PRIMARY KEY,
    agent_scope TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    impact TEXT NOT NULL,
    action TEXT NOT NULL,
    proposed_value_json TEXT,
    target_version INTEGER,
    observed_current_value TEXT NOT NULL DEFAULT '',
    expected_version INTEGER NOT NULL,
    requester_platform TEXT NOT NULL,
    requester_open_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_config_versions (
    agent_scope TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    rule_name TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    impact TEXT NOT NULL,
    value_json TEXT,
    is_active INTEGER NOT NULL,
    operation TEXT NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE,
    actor_platform TEXT NOT NULL,
    actor_open_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (agent_scope, rule_key, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_config_proposals_lookup
    ON agent_config_proposals(agent_scope, conversation_key, requester_open_id, status, created_at DESC);

CREATE TRIGGER IF NOT EXISTS agent_config_version_after_insert
AFTER INSERT ON agent_config_current
BEGIN
    INSERT INTO agent_config_versions
        (agent_scope, rule_key, version, rule_name, source_ref, impact,
         value_json, is_active, operation, proposal_id, actor_platform,
         actor_open_id, request_id, conversation_key, created_at)
    VALUES
        (NEW.agent_scope, NEW.rule_key, NEW.current_version, NEW.rule_name,
         NEW.source_ref, NEW.impact, NEW.value_json, NEW.is_active,
         NEW.last_operation, NEW.last_proposal_id, NEW.updated_by_platform,
         NEW.updated_by_open_id, NEW.updated_request_id,
         NEW.updated_conversation_key, NEW.updated_at);
    UPDATE agent_config_proposals SET status = 'applied'
        WHERE proposal_id = NEW.last_proposal_id AND status = 'applying';
END;

CREATE TRIGGER IF NOT EXISTS agent_config_version_after_update
AFTER UPDATE ON agent_config_current
BEGIN
    INSERT INTO agent_config_versions
        (agent_scope, rule_key, version, rule_name, source_ref, impact,
         value_json, is_active, operation, proposal_id, actor_platform,
         actor_open_id, request_id, conversation_key, created_at)
    VALUES
        (NEW.agent_scope, NEW.rule_key, NEW.current_version, NEW.rule_name,
         NEW.source_ref, NEW.impact, NEW.value_json, NEW.is_active,
         NEW.last_operation, NEW.last_proposal_id, NEW.updated_by_platform,
         NEW.updated_by_open_id, NEW.updated_request_id,
         NEW.updated_conversation_key, NEW.updated_at);
    UPDATE agent_config_proposals SET status = 'applied'
        WHERE proposal_id = NEW.last_proposal_id AND status = 'applying';
END;
