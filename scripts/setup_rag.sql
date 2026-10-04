-- The knowledge layer, on the database side.
--
-- Run inside the demo database, as a superuser-ish role:
--   sudo -u postgres psql -d fence_demo -f scripts/setup_rag.sql
--
-- Idempotent: safe to run again after a rebuild.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS doc_chunks (
  id          BIGSERIAL PRIMARY KEY,
  source      TEXT        NOT NULL,          -- the note this came from
  section     TEXT,                          -- heading path, e.g. "表膨胀 / 成因"
  content     TEXT        NOT NULL,
  token_count INT,
  embedding   VECTOR(1024),
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- HNSW over cosine distance.  m / ef_construction are the two knobs that
-- matter: higher m = better recall, more memory.  16/64 is the documented
-- starting point and is fine up to roughly a million vectors.
CREATE INDEX IF NOT EXISTS idx_chunks_hnsw ON doc_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- A plain index on source makes the re-ingest DELETE cheap.
CREATE INDEX IF NOT EXISTS idx_chunks_source ON doc_chunks (source);

-- The agent's own role: SELECT only, no writes at all.  This is the layer
-- that cannot be argued with — the database itself refuses.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
    CREATE ROLE agent_ro LOGIN PASSWORD 'ro_only';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE fence_demo TO agent_ro;
GRANT USAGE  ON SCHEMA public TO agent_ro;
GRANT SELECT ON doc_chunks TO agent_ro;

-- Prove it, do not assume it:
--   psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
--        -c "DELETE FROM doc_chunks"        -- must fail: permission denied
--   psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
--        -c "SELECT count(*) FROM doc_chunks"   -- must succeed
