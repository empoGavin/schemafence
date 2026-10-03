-- ============================================================
-- Day 3 · 最小向量检索验证
--
--   sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
--
-- 用 3 维手写向量做验证，是为了不依赖任何 embedding API
-- ——今天要确认的只有一件事：pgvector 能装、能查、索引能建。
-- Day 4 再把 3 维换成 1024 维真实 embedding。
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS demo_vectors;
CREATE TABLE demo_vectors (
  id        SERIAL PRIMARY KEY,
  label     TEXT NOT NULL,
  embedding vector(3)
);
COMMENT ON TABLE demo_vectors IS 'pgvector 语法验证表，Day 4 换成 doc_chunks';

INSERT INTO demo_vectors (label, embedding) VALUES
  ('订单金额',   '[0.90, 0.10, 0.00]'),
  ('退款原因',   '[0.80, 0.20, 0.10]'),
  ('黑名单用户', '[0.10, 0.90, 0.20]');

-- 余弦距离 <=> ：值越小越相似；1 - 距离 = 相似度
SELECT label,
       round((1 - (embedding <=> '[0.85,0.15,0.05]'::vector))::numeric, 4) AS similarity
FROM demo_vectors
ORDER BY embedding <=> '[0.85,0.15,0.05]'::vector
LIMIT 2;

-- 索引能建起来，说明 HNSW 可用（小表上依旧走顺序扫描，这是正常的）
CREATE INDEX IF NOT EXISTS idx_demo_vectors_hnsw
  ON demo_vectors USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'demo_vectors';

-- ============================================================
-- 只读账号：七层护栏的第一层，物理上写不进去
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
    CREATE ROLE agent_ro LOGIN PASSWORD 'ro_only';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE fence_demo TO agent_ro;
GRANT USAGE  ON SCHEMA public TO agent_ro;
GRANT USAGE  ON SCHEMA shop   TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop   TO agent_ro;

SELECT rolname, rolcanlogin, rolcreatedb, rolsuper FROM pg_roles WHERE rolname = 'agent_ro';

-- 验证一下：这个账号确实写不了（应报 permission denied）
--   psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
--        -c "DELETE FROM shop.orders WHERE 1=1"
