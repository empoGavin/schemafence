# 基准测试：脚本与命令

四个脚本，各管一件事。全部命令在仓库根目录执行。

| 脚本 | 回答的问题 | 离线可跑 |
|---|---|---|
| `scripts/bench_embedding.py` | 语料在不同超参下向量化的速度、体积、命中率 | 是 |
| `scripts/bench_store.py` | JSON 与 pgvector 两种存储的检索性能与内存/IO/CPU | JSON 是，pg 需库 |
| `scripts/test_layers.py` | 七层静态检查与七层运行时护栏，逐层正反例 | 是（库侧 1 项需 `--db`） |
| `scripts/bench_report.py` | 把上面三者的 JSON 汇总成 `docs/bench-report.md` | 是 |

`scripts/bench_util.py` 是被这三个脚本共用的测量工具（RSS / CPU / 进程 I/O），
不单独运行。

---

## 1. 环境准备

### 1.1 离线路径（零依赖）

```bash
python scripts/bench_embedding.py --chunks 512 --overlaps 64 --idf on
```

需要 Python 3.10+。不需要 pip install、不需要 key、不需要数据库。

### 1.2 live 路径（pgvector）

```bash
pip install -r requirements.txt          # 唯一的第三方依赖：psycopg
```

数据库侧（在 VM 上以 postgres 用户执行）：

```bash
sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
```

再建只读账号——这是七层护栏里 L7a 的物理兜底，也是存储基准里应该用的连接身份：

```sql
CREATE ROLE agent_ro LOGIN PASSWORD 'ro_only';
GRANT CONNECT ON DATABASE fence_demo TO agent_ro;
GRANT USAGE  ON SCHEMA public TO agent_ro;
GRANT USAGE  ON SCHEMA shop   TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop   TO agent_ro;
```

验证只读账号真的写不了（应报 `permission denied`）：

```bash
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
     -c "DELETE FROM shop.orders WHERE 1=1"
```

### 1.3 向量模型（API 嵌入）

```bash
export SF_EMBED_API_KEY=sk-...                     # DashScope compatible-mode
export SF_EMBED_MODEL=text-embedding-v3            # 可选，默认即此
export SF_EMBED_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# export SF_EMBED_DIMENSIONS=512                   # 仅 Matryoshka 模型支持，bge-m3 会 400
```

用之前先确认这条路是通的，比跑完基准才发现 key 错了便宜：

```bash
curl -s "$SF_EMBED_BASE_URL/embeddings" \
  -H "Authorization: Bearer $SF_EMBED_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$SF_EMBED_MODEL\",\"input\":[\"hi\"]}" | head -c 300
```

---

## 2. 向量化基准

```bash
# 默认网格：chunk 256/512/1024 × overlap 0/64/128 × idf on/off = 18 种配置
python scripts/bench_embedding.py

# 等价写法，显式给出网格
python scripts/bench_embedding.py --mode offline \
    --chunks 256,512,1024 --overlaps 0,64,128 --idf on,off --repeat 3

# 维度折衷（同一批切片，只换向量维度）
python scripts/bench_embedding.py --mode offline \
    --dims 128,256,512,1024 --chunks 512 --overlaps 64 --idf on \
    --out bench/embedding-dims.json

# 语义嵌入（需要 key；每行一次全量嵌入，注意成本）
python scripts/bench_embedding.py --mode api --dims 1024 --chunks 512 --overlaps 64

# 两种嵌入同一张表里对比（key 存在时）
python scripts/bench_embedding.py --mode both --chunks 512 --overlaps 64
```

常用参数：`--corpus`（默认 `examples/knowledge-dba`）、`--eval-file`（默认 `eval/questions-dba.md`）、
`--ks 3,5,10`、`--repeat`（每题查询重复次数）、`--limit-questions`（冒烟用）。

输出：`bench/embedding-<mode>.json`，stdout 上同时打印 Markdown 表。

指标口径：`hit@k` 用题库的多来源标注（`甲 / 乙` 命中任一即算），
`MRR@k` / `mean rank@k` 用来分辨"命中率饱和但排序变了"的情况；
`ms/piece`、`pieces/s` 是嵌入吞吐；`store`、`B/piece` 是落盘体积；
`q p50/p95` 是"嵌入查询 + 检索"的端到端延迟。

---

## 3. 存储对比

分两个进程各跑一次——同一进程里先后跑两种后端，峰值 RSS 不可比。

```bash
# JSON：全量向量常驻内存，检索是全表扫描
python scripts/bench_store.py --backend json --repeat 10

# pgvector：需要库与 DSN；HNSW 与顺序扫描都会被测到
python scripts/bench_store.py --backend pg --repeat 10 \
    --db postgresql://agent_ro:ro_only@localhost:5432/fence_demo

# 想看没有索引的代价（建表时不建 HNSW）
python scripts/bench_store.py --backend pg --db "$DSN" --no-index

# 或者一次跑两个后端（内存差值仍有效，峰值无效）
python scripts/bench_store.py --backend both --db "$DSN"
```

跑完之后，把两次结果放在一起对比（脚本会报 top-1 一致率与 overlap@k）：

```bash
python scripts/bench_store.py --backend pg --db "$DSN" --reference bench/store-json.json
```

输出：`bench/store-<backend>.json`。每个阶段（`ingest_total` / `cold_open` / `search` /
`search_seqscan`）都记录 wall、CPU、CPU 占 wall 比、RSS 增量与峰值、进程读写字节数。

---

## 4. 七层检查与七层护栏

```bash
# 两个套件全跑：运行时护栏 44 例，静态检查 19 例
python scripts/test_layers.py

# 只跑其中一套
python scripts/test_layers.py --suite guard
python scripts/test_layers.py --suite checks

# 带上数据库，把 check 5（结果合理性，读 pg_stats）也跑掉
python scripts/test_layers.py --suite checks --db postgresql://agent_ro:ro_only@localhost:5432/fence_demo

# 打印库侧那一层需要的 SQL 与 psql 验证命令
python scripts/test_layers.py --show-env
```

输出：`bench/layers.json`，stdout 上逐条 `PASS / FAIL`，失败时打印"期望 / 实际"。退出码：
任一用例失败即 1，便于接进 CI。

用例断言的是三件事，不只是"拒绝了没有"：**停在哪一层**（`L2` 还是 `L3`）、
**理由里有没有关键词**、**放行时 SQL 被改成什么样**（补的 LIMIT 是不是 100）。

---

## 5. 汇总成报告

```bash
python scripts/bench_report.py                       # → docs/bench-report.md
python scripts/bench_report.py --bench bench --out docs/bench-report.md
```

报告是生成的，重跑即覆盖；**不要往报告里写结论**。
结论写在 `docs/bench-findings.md`，那份是手写的，脚本不碰。

---

## 6. 注意事项

- `bench/` 是可再生的运行产物（含 store 副本与 trace），已在 `.gitignore` 里；
  要留证据就把 `docs/bench-report.md` 与 `docs/bench-findings.md` 提交，别提交原始 JSON。
- **不要跑 `agent_cli.py --eval --report eval/report-dba.md`**：那会覆盖报告里手写的失败分析
  与结论段落。要测报告生成就换个输出路径。
- 进程 I/O 计数器只统计磁盘读写，不含 socket。API 嵌入的耗时因此不能从 `read_bytes` 解释，
  只能看 wall 与 CPU。
- 同一台机器上跑对比时别开别的重活；本套基准的差异量级到毫秒，后台编译会直接污染结果。
- Windows 上 RSS 与 I/O 走 psapi/kernel32，Linux 上走 `/proc`；两条路都实现，
  取不到就打印 `—`，不会让基准挂掉。
