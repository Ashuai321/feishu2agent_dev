ALTER TABLE relay_runs ADD COLUMN processing_reaction_id TEXT;
ALTER TABLE relay_runs ADD COLUMN processing_reaction_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE relay_runs ADD COLUMN processing_reaction_updated_at INTEGER;
ALTER TABLE relay_runs ADD COLUMN result_message_id TEXT;
