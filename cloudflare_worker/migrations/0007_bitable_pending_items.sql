-- Keep one OAuth authorization request while queuing all messages received
-- before that authorization completes.
ALTER TABLE bitable_pending_requests
ADD COLUMN pending_items_json TEXT NOT NULL DEFAULT '[]';
