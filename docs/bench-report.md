# schemafence 基准测试报告 · 2026-10-06

- 环境：Linux 6.12.0-206.104.4.4.el10uek.x86_64 · python 3.12.13 · 2 逻辑核
- 进程计数器：RSS 可用 · I/O 可用
- 生成方式：`python scripts/bench_report.py`（数据来自 bench/*.json，本文件不要手改）

## 0. 这份报告回答什么

| 问题 | 脚本 |
|---|---|
| 语料在不同超参下向量化的速度、体积、命中率如何 | `scripts/bench_embedding.py` |
| pgvector 与 JSON 两种存储的检索性能与内存/IO/CPU 对比 | `scripts/bench_store.py` |
| SQL 七层静态检查与运行时七层护栏，逐层行为是否正确 | `scripts/test_layers.py` |

复现命令见第 5 节；测量方法与口径见第 1 节。结论与解读写在 [`docs/bench-findings.md`](bench-findings.md)，重跑基准不会覆盖它。

## 1. 方法

三段测试都直接调用产品代码（`read_corpus`、`build_idf`、`Embedder`、`JsonStore`/`PgStore.search`、`guard()`、`analyze()`），
不重写被测量对象——重写一遍等于在测基准脚本自己。

- **阶段口径**：wall 为 `perf_counter`，CPU 为 `process_time`，RSS 取进程当前值与峰值，I/O 取进程累计读写字节的阶段差值。
- **命中率口径**：用 `eval/questions-dba.md` 题库，多来源标注（`甲 / 乙`）命中任一即算命中。
- **pgvector 的插入**是逐行 `INSERT`（autocommit），慢是真实行为，不是基准脚本的实现选择；HNSW 与顺序扫描分开测。
- 未在本机运行的部分在第 4 节标明命令与预期，不做推测性数字。

## 2. 向量化基准

### `embedding-api.json`（examples/knowledge-dba）

题库 44 题，每配置查询重复 3 次。

| embedder | dim | chunk | overlap | idf | pieces | avg tok | embed ms | ms/piece | pieces/s | store | B/piece | hit@5 | MRR@5 | mean rank@5 | q p50 ms | q p95 ms | peak RSS MB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| api | 1024 | 512 | 64 | off | 40 | 298.1 | 19998 | 499.95 | 2 | 942.1 KB | 24117 | 100.0% | 0.928 | 1.159 | 666.34 | 5536.25 | 35.82 |

### `embedding-offline.json`（examples/knowledge-dba）

题库 44 题，每配置查询重复 3 次。

| embedder | dim | chunk | overlap | idf | pieces | avg tok | embed ms | ms/piece | pieces/s | store | B/piece | hit@5 | MRR@5 | mean rank@5 | q p50 ms | q p95 ms | peak RSS MB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| offline | 1024 | 256 | 0 | on | 61 | 194.7 | 30 | 0.49 | 2020 | 487.4 KB | 8182 | 97.7% | 0.894 | 1.186 | 3.63 | 4.13 | 34.08 |
| offline | 1024 | 256 | 0 | off | 61 | 194.7 | 27 | 0.44 | 2261 | 485.2 KB | 8144 | 97.7% | 0.901 | 1.186 | 3.52 | 4.21 | 35.44 |
| offline | 1024 | 256 | 64 | on | 64 | 198.0 | 31 | 0.48 | 2078 | 515.3 KB | 8245 | 97.7% | 0.883 | 1.209 | 3.46 | 4.12 | 35.71 |
| offline | 1024 | 256 | 64 | off | 64 | 198.0 | 24 | 0.37 | 2719 | 512.1 KB | 8193 | 97.7% | 0.913 | 1.163 | 3.98 | 4.42 | 35.95 |
| offline | 1024 | 256 | 128 | on | 70 | 191.5 | 28 | 0.40 | 2475 | 557.2 KB | 8151 | 97.7% | 0.905 | 1.163 | 4.08 | 5.09 | 36.05 |
| offline | 1024 | 256 | 128 | off | 70 | 191.5 | 29 | 0.41 | 2428 | 553.9 KB | 8103 | 97.7% | 0.924 | 1.14 | 4.01 | 4.64 | 36.7 |
| offline | 1024 | 512 | 0 | on | 40 | 296.6 | 22 | 0.55 | 1828 | 371.9 KB | 9520 | 97.7% | 0.893 | 1.279 | 2.28 | 3.07 | 36.7 |
| offline | 1024 | 512 | 0 | off | 40 | 296.6 | 20 | 0.51 | 1962 | 369.6 KB | 9461 | 97.7% | 0.898 | 1.233 | 2.52 | 2.74 | 36.7 |
| offline | 1024 | 512 | 64 | on | 40 | 298.1 | 21 | 0.52 | 1927 | 372.1 KB | 9527 | 97.7% | 0.894 | 1.256 | 2.25 | 2.58 | 36.7 |
| offline | 1024 | 512 | 64 | off | 40 | 298.1 | 20 | 0.51 | 1964 | 370.2 KB | 9477 | 97.7% | 0.898 | 1.233 | 2.51 | 2.70 | 36.7 |
| offline | 1024 | 512 | 128 | on | 41 | 292.1 | 26 | 0.63 | 1583 | 378.5 KB | 9452 | 97.7% | 0.883 | 1.279 | 2.52 | 2.73 | 36.7 |
| offline | 1024 | 512 | 128 | off | 41 | 292.1 | 20 | 0.48 | 2100 | 376.4 KB | 9400 | 97.7% | 0.887 | 1.256 | 2.49 | 3.09 | 36.7 |
| offline | 1024 | 1024 | 0 | on | 24 | 493.8 | 19 | 0.77 | 1296 | 273.9 KB | 11687 | 95.5% | 0.898 | 1.119 | 1.42 | 1.96 | 36.7 |
| offline | 1024 | 1024 | 0 | off | 24 | 493.8 | 18 | 0.75 | 1332 | 271.6 KB | 11586 | 95.5% | 0.932 | 1.048 | 1.52 | 1.68 | 36.7 |
| offline | 1024 | 1024 | 64 | on | 24 | 496.3 | 20 | 0.81 | 1228 | 274.8 KB | 11725 | 95.5% | 0.909 | 1.095 | 1.64 | 1.75 | 36.7 |
| offline | 1024 | 1024 | 64 | off | 24 | 496.3 | 18 | 0.75 | 1339 | 272.1 KB | 11609 | 95.5% | 0.943 | 1.024 | 1.65 | 1.78 | 36.7 |
| offline | 1024 | 1024 | 128 | on | 24 | 498.5 | 22 | 0.92 | 1087 | 275.5 KB | 11754 | 95.5% | 0.898 | 1.119 | 1.51 | 1.68 | 36.7 |
| offline | 1024 | 1024 | 128 | off | 24 | 498.5 | 18 | 0.74 | 1349 | 272.9 KB | 11644 | 95.5% | 0.943 | 1.024 | 1.47 | 1.62 | 36.7 |

失效样本（chunk=256, overlap=0, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-spinlock-old-snapshot`, `pg-index-not-used`, `pg-upgrade`

失效样本（chunk=256, overlap=0, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-vacuum-tuning`, `pg-spinlock-old-snapshot`, `pg-index-not-used`

失效样本（chunk=256, overlap=64, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-spinlock-old-snapshot`, `pg-index-not-used`, `pg-upgrade`

失效样本（chunk=256, overlap=64, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-vacuum-tuning`, `pg-spinlock-old-snapshot`, `pg-index-not-used`

失效样本（chunk=256, overlap=128, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-slow-query-method`, `pg-index-not-used`, `pg-spinlock-old-snapshot`

失效样本（chunk=256, overlap=128, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-slow-query-method`, `pg-vacuum-tuning`, `pg-index-not-used`

失效样本（chunk=512, overlap=0, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-spinlock-old-snapshot`, `pg-upgrade`

失效样本（chunk=512, overlap=0, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-spinlock-old-snapshot`, `pg-index-not-used`, `pg-idle-in-transaction`

失效样本（chunk=512, overlap=64, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-spinlock-old-snapshot`, `pg-upgrade`

失效样本（chunk=512, overlap=64, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-spinlock-old-snapshot`, `pg-index-not-used`, `pg-idle-in-transaction`

失效样本（chunk=512, overlap=128, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-spinlock-old-snapshot`, `pg-upgrade`

失效样本（chunk=512, overlap=128, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-spinlock-old-snapshot`, `pg-index-not-used`, `pg-idle-in-transaction`

失效样本（chunk=1024, overlap=0, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

失效样本（chunk=1024, overlap=0, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

失效样本（chunk=1024, overlap=64, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

失效样本（chunk=1024, overlap=64, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

失效样本（chunk=1024, overlap=128, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

失效样本（chunk=1024, overlap=128, idf=off）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-replication-failover`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-replication-failover`, `pg-vacuum-tuning`, `pg-subtransaction-overflow`

### `embedding-dims.json`（examples/knowledge-dba）

题库 44 题，每配置查询重复 3 次。

| embedder | dim | chunk | overlap | idf | pieces | avg tok | embed ms | ms/piece | pieces/s | store | B/piece | hit@5 | MRR@5 | mean rank@5 | q p50 ms | q p95 ms | peak RSS MB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| offline | 128 | 512 | 64 | on | 40 | 298.1 | 19 | 0.48 | 2071 | 140.5 KB | 3596 | 81.8% | 0.547 | 2.167 | 0.39 | 0.47 | 31.71 |
| offline | 256 | 512 | 64 | on | 40 | 298.1 | 20 | 0.49 | 2051 | 190.9 KB | 4888 | 88.6% | 0.637 | 1.846 | 0.67 | 0.78 | 32.2 |
| offline | 512 | 512 | 64 | on | 40 | 298.1 | 30 | 0.74 | 1348 | 260.2 KB | 6660 | 95.5% | 0.764 | 1.69 | 1.28 | 1.55 | 32.34 |
| offline | 1024 | 512 | 64 | on | 40 | 298.1 | 23 | 0.59 | 1705 | 372.1 KB | 9527 | 97.7% | 0.894 | 1.256 | 2.39 | 2.71 | 33.29 |

失效样本（chunk=512, overlap=64, idf=on）：
- max_connections 调大一点是不是更好？ → 期望 `pg-connection-pool`，实际 `pg-subtransaction-overflow`, `pg-index-not-used`, `pg-slow-query-method`
- pgbouncer 的 transaction 池模式有什么限制？ → 期望 `pg-connection-pool`，实际 `pg-spinlock-old-snapshot`, `pg-idle-in-transaction`, `pg-spinlock-old-snapshot`
- 膨胀到多少才需要上 VACUUM FULL 或者 pg_repack？ → 期望 `pg-vacuum-tuning / pg-bloat-seq-scan`，实际 `pg-index-not-used`, `pg-idle-in-transaction`, `pg-connection-pool`
- 主备切换的步骤是什么，为什么必须先隔离旧主？ → 期望 `pg-replication-failover`，实际 `pg-subtransaction-overflow`, `pg-spinlock-old-snapshot`, `pg-partitioning`
- pg_stat_statements 里的 SQL 复现不出问题怎么办？ → 期望 `pg-slow-query-method`，实际 `pg-subtransaction-overflow`, `pg-upgrade`, `pg-spinlock-old-snapshot`
- 把参数从配置文件里删掉和设成 0，效果一样吗？ → 期望 `pg-spinlock-old-snapshot`，实际 `pg-partitioning`, `pg-index-not-used`, `pg-index-not-used`
- 怎么确认一个参数已经回到默认值，而不是被人改过？ → 期望 `pg-spinlock-old-snapshot`，实际 `pg-subtransaction-overflow`, `pg-backup-pitr`, `pg-index-not-used`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-backup-pitr`, `pg-vacuum-tuning`, `pg-upgrade`

失效样本（chunk=512, overlap=64, idf=on）：
- 连接数被打满，应该从哪里开始排查？ → 期望 `pg-connection-pool`，实际 `pg-spinlock-old-snapshot`, `pg-spinlock-old-snapshot`, `pg-upgrade`
- 膨胀到多少才需要上 VACUUM FULL 或者 pg_repack？ → 期望 `pg-vacuum-tuning / pg-bloat-seq-scan`，实际 `pg-subtransaction-overflow`, `pg-partitioning`, `pg-idle-in-transaction`
- 怎么确认 WAL 归档是正常的？ → 期望 `pg-backup-pitr`，实际 `pg-upgrade`, `pg-replication-failover`, `pg-replication-failover`
- 复制槽为什么会把 WAL 撑着不清理？ → 期望 `pg-replication-failover`，实际 `pg-vacuum-tuning`, `pg-bloat-seq-scan`, `pg-subtransaction-overflow`
- VACUUM FULL 和 pg_repack 应该怎么选？ → 期望 `pg-bloat-seq-scan`，实际 `pg-spinlock-old-snapshot`, `pg-spinlock-old-snapshot`, `pg-backup-pitr`

失效样本（chunk=512, overlap=64, idf=on）：
- 连接数被打满，应该从哪里开始排查？ → 期望 `pg-connection-pool`，实际 `pg-spinlock-old-snapshot`, `pg-spinlock-old-snapshot`, `pg-capacity-planning`
- 复制槽为什么会把 WAL 撑着不清理？ → 期望 `pg-replication-failover`，实际 `pg-vacuum-tuning`, `pg-subtransaction-overflow`, `pg-privilege-model`

失效样本（chunk=512, overlap=64, idf=on）：
- 磁盘还有一半空间，为什么还要提前扩容？ → 期望 `pg-capacity-planning`，实际 `pg-index-not-used`, `pg-spinlock-old-snapshot`, `pg-upgrade`

## 3. 存储对比

### `store-json.json`

嵌入方式 `offline / hashed-lexical / 1024d`，top-k 5，每题重复 10 次

| backend | pieces | ingest ms | open ms | on disk | peak RSS MB | p50 ms | p95 ms | max ms | p50 seqscan ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| json | 40 | 79 | 10.6 | 372.1 KB | 34.48 | 2.137 | 2.488 | 3.409 |  |

插入方式决定了两者的差距：JSON 写一个文件（372.1 KB），pgvector 在 autocommit 连接上逐行 INSERT。下表是这两种做法的CPU 与 I/O 代价。

**json 各阶段资源**（同一进程内测量）

| phase | wall ms | cpu ms | cpu % of wall | RSS delta MB | RSS peak MB | read bytes | write bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ingest_total | 79.4 | 78.116 | 98.3 | 2.57 | 34.48 | 0 | 385024 |
| cold_open | 10.6 | 10.462 | 98.8 | 1.55 | 36.27 | 0 | 0 |
| search | 1049.0 | 1034.53 | 98.6 | 0.02 | 36.75 | 0 | 0 |

### `store-pg.json`

嵌入方式 `offline / hashed-lexical / 1024d`，top-k 5，每题重复 10 次，HNSW 启用

| backend | pieces | ingest ms | open ms | on disk | peak RSS MB | p50 ms | p95 ms | max ms | p50 seqscan ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pg | 40 | 205 | 14.7 | 1.4 MB | 49.93 | 0.757 | 1.222 | 10.569 | 1.091 |

**pg 各阶段资源**（同一进程内测量）

| phase | wall ms | cpu ms | cpu % of wall | RSS delta MB | RSS peak MB | read bytes | write bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ingest_total | 204.5 | 85.893 | 42.0 | 2.59 | 49.93 | 0 | 0 |
| cold_open | 14.7 | 9.99 | 67.9 | 0.01 | 49.94 | 0 | 0 |
| search | 401.8 | 174.417 | 43.4 | 0.13 | 51.81 | 0 | 0 |
| search_seqscan | 564.9 | 249.713 | 44.2 | 0.02 | 51.89 | 0 | 0 |


## 4. 七层检查与七层护栏

### 静态七层检查 (checks.py)

18/18 passed

| case | layer | what it asserts | result |
| --- | --- | --- | :-: |
| C1-1 | check 1 | orders vs orders_archive is an archive pair | PASS |
| C1-2 | check 1 | column names one character apart | PASS |
| C1-3 | check 1 | two unrelated tables raise nothing | PASS |
| C2-1 | check 2 | nullable FK used in joins | PASS |
| C2-2 | check 2 | nullable key column with no FK at all | PASS |
| C2-3 | check 2 | a NOT NULL key is not a silent-drop risk | PASS |
| C3-1 | check 3 | money in floating point | PASS |
| C3-2 | check 3 | timestamp stored as text | PASS |
| C3-3 | check 3 | timestamp without time zone | PASS |
| C3-4 | check 3 | NUMERIC money and TIMESTAMPTZ raise nothing | PASS |
| C4-1 | check 4 | set-like table with a nullable key | PASS |
| C4-2 | check 4 | the same table with NOT NULL is safe from NOT IN | PASS |
| H-1 | check H | table with no comment | PASS |
| H-2 | check H | table described, columns not | PASS |
| H-3 | check H | two spellings for the same idea | PASS |
| H-4 | check H | described table with one time convention | PASS |
| C5-1 | check 5 | check 5 is not faked from a DDL file | PASS |
| C5-2 | check 5 | check 5 reads pg_stats on a live database | PASS |

### 需要数据库的环境准备

```sql
-- L7, database side: a role that physically cannot write (layer 7a)
CREATE ROLE agent_ro LOGIN PASSWORD 'ro_only';
GRANT CONNECT ON DATABASE fence_demo TO agent_ro;
GRANT USAGE  ON SCHEMA public TO agent_ro;
GRANT USAGE  ON SCHEMA shop   TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop   TO agent_ro;
```

验证：

```bash
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" -c "DELETE FROM shop.orders WHERE 1=1"
# expect: ERROR: permission denied for table orders
psql "postgresql://postgres@localhost:5432/fence_demo" -c "SET statement_timeout = '10s'; SELECT pg_sleep(20)"
# expect: ERROR: canceling statement due to statement timeout  (~10s)
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" -c "SHOW statement_timeout" -c "SHOW default_transaction_read_only"
# expect: the role connects; the defaults are the server's, not the agent's
tail -n 5 agent_trace.jsonl
# expect: one JSON line per tool call with tool/decision/layer/ok/ms, and a
#         blocked write shows decision=blocked with the layer that stopped it
```

## 5. 复现命令

```bash
# 环境：离线路径零依赖；live 路径需要 psycopg 与运行中的 PostgreSQL
pip install -r requirements.txt        # psycopg[binary] = 唯一的第三方依赖

# 1) 向量化：离线哈希 vs 向量模型，多超参网格
python scripts/bench_embedding.py --mode offline --chunks 256,512,1024 --overlaps 0,64,128 --idf on,off
SF_EMBED_API_KEY=... python scripts/bench_embedding.py --mode api --dims 1024 --chunks 512 --overlaps 64

# 2) 存储：JSON 与 pgvector 分进程各跑一次，内存数字才干净
python scripts/bench_store.py --backend json --repeat 10
python scripts/bench_store.py --backend pg --repeat 10 --db postgresql://agent_ro:ro_only@localhost:5432/fence_demo

# 3) 七层检查与护栏
python scripts/test_layers.py
python scripts/test_layers.py --show-env     # 打印库侧环境准备

# 4) 汇总成这份报告
python scripts/bench_report.py
```

> 本报告由脚本生成；结论性段落请写在 `docs/bench-findings.md`，重跑基准不会覆盖它。
