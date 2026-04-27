-- Reliability Phase A raw SQL migration reference.
-- The application executes these statements idempotently by checking
-- PRAGMA table_info(entries) before each ALTER TABLE.

ALTER TABLE entries ADD COLUMN last_embed_error TEXT;
ALTER TABLE entries ADD COLUMN last_embed_attempted_at TEXT;
ALTER TABLE entries ADD COLUMN embed_attempt_count INTEGER NOT NULL DEFAULT 0;

UPDATE entries
SET embed_attempt_count = COALESCE(embed_attempt_count, 0);
