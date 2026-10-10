-- Standard OAuth refresh tokens keep MCP connections usable without a
-- recurring manual authorization. expires_at=0 means the refresh token does
-- not expire; access tokens retain the configured finite lifetime.
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    refresh_token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_client
    ON oauth_refresh_tokens(client_id);
