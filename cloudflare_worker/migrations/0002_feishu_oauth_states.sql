CREATE TABLE IF NOT EXISTS feishu_oauth_states (
    state TEXT PRIMARY KEY,
    redirect_uri TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
