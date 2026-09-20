-- Keep the selected destination document separate from the requester's OAuth platform.
-- Existing pending rows continue to target the original Feishu document.
ALTER TABLE bitable_pending_requests
ADD COLUMN document_mode TEXT NOT NULL DEFAULT 'feishu';
