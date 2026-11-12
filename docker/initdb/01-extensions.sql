-- Runs once, when the data volume is first created.
-- The real schema is owned by Alembic migrations (SPEC §6); this only makes pgvector available.
CREATE EXTENSION IF NOT EXISTS vector;
