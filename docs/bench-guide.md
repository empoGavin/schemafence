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

## 0. 完整顺序

从零到报告，一次跑完大致是这些命令；每一段的细节和参数在下面各节。

```bash
# ---- 环境（只需一次）----
pip install -r requirements.txt                    # psycopg，只有 live 路径需要
export DSN_RW='postgresql://postgres:pgvec123@localhost:5432/fence_demo'
sudo -u postgres psql -d fence_demo -f scripts/setup_rag.sql   # doc_chunks + agent_ro

# ---- 1 向量化：离线网格 → 维度折衷 → 向量模型 ----
python scripts/bench_embedding.py --mode offline \
    --chunks 256,512,1024 --overlaps 0,64,128 --idf on,off \
    --out bench/embedding-offline.json
python scripts/bench_embedding.py --mode offline --dims 128,256,512,1024 \
    --chunks 512 --overlaps 64 --idf on --out bench/embedding-dims.json
python scripts/bench_embedding.py --mode api --dims 1024 \
    --chunks 512 --overlaps 64 --out bench/embedding-api.json     # 需要 key

# ---- 2 存储对比：两个后端分进程跑 ----
python scripts/bench_store.py --backend json --repeat 10 --out bench/store-json.json
python scripts/bench_store.py --backend pg --repeat 10 --db "$DSN_RW" \
    --reference bench/store-json.json --out bench/store-pg.json

# ---- 3 七层检查与七层护栏 ----
python scripts/test_layers.py --out bench/layers.json
python scripts/test_layers.py --suite checks --db "$DSN_RW"      # 补跑库侧的 check 5

# ---- 4 出报告 ----
python scripts/bench_report.py                     # → docs/bench-report.md
```

两处容易踩的：`--out` 要给不同文件名（报告按文件名分节，同名互相覆盖）；
只有第 4 步有依赖，它读 `bench/*.json`，所以别把它排到前面。

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

知识层的表与只读账号由一条幂等脚本一次建完（`doc_chunks` + HNSW 索引 + role `agent_ro`）：

```bash
sudo -u postgres psql -d fence_demo -f scripts/setup_rag.sql
```

**两种连接身份，用途不同，不要混用：**

| 连接 | 权限 | 用在哪 |
|---|---|---|
| `postgresql://postgres:<pw>@localhost:5432/fence_demo` | 读写 | 存储基准（`ensure_schema()` 要建表、灌库要 INSERT）、`test_layers --db`（要读 `shop` 表的 `pg_stats`） |
| `postgresql://agent_ro:ro_only@localhost:5432/fence_demo` | 只读 | L7a 的物理兜底验证 |

用 `agent_ro` 跑存储基准会在建表那一步就 `permission denied` —— 它被授予的只有 `doc_chunks` 的 SELECT。

后面的命令统一用变量，避免抄错（`pgvec123` 是 `scripts/setup_pg.sh` 的默认口令）：

```bash
export DSN_RW='postgresql://postgres:pgvec123@localhost:5432/fence_demo'
export DSN_RO='postgresql://agent_ro:ro_only@localhost:5432/fence_demo'
```

只读身份写不进去，这一条要用命令看，不要假设：

```bash
psql "$DSN_RO" -c "DELETE FROM shop.orders WHERE 1=1"
# expect: ERROR:  permission denied for table orders
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

### 1.4 灌库语义与 `--rebuild`

`PgStore.replace()` 做的是「删掉这批语料提到的 source，再插新的」，不是清空重来。
所以同一个目录重跑、或者只改 `--chunk/--overlap` 重跑，都是幂等的。但**换了语料目录**时，
上一批笔记的文件名不在新语料里，`DELETE ... WHERE source = ANY(...)` 一行都删不掉，
它们会留在表里继续被检索——而且不报错。

所以 ingest 结束时会自己检查一次，有残留就打印：

```
  orphan rows : 8 source(s) held but not in this corpus: data-governance.md, … 
  what now    : keep them if a second corpus lives here; otherwise re-run with --rebuild
```

看到这行，三种处置：

```bash
# 1. 这个库只放一套语料 —— 清空再灌
python agent_cli.py --ingest examples/knowledge-dba --db "$DSN_RW" --rebuild

# 2. 两套语料有意共存 —— 忽略警告即可（JSON 后台不会有这行：
#    JsonStore.replace 是整体替换，天然没有孤儿）

# 3. 事后手工处理
psql "$DSN_RW" -c "SELECT source, count(*) FROM doc_chunks GROUP BY 1 ORDER BY 1"
psql "$DSN_RW" -c "TRUNCATE doc_chunks"                     # 全清
psql "$DSN_RW" -c "DELETE FROM doc_chunks WHERE source <> ALL(ARRAY['a.md','b.md'])"
```

只用 `DELETE` 的话补一句 `VACUUM (ANALYZE) doc_chunks`：反复重灌会累积死元组，
这正是语料里 `pg-bloat-seq-scan.md` 那一篇讲的事。

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

# pgvector：需要库与可写 DSN；HNSW 与顺序扫描都会被测到
python scripts/bench_store.py --backend pg --repeat 10 --db "$DSN_RW"

# 想看没有索引的代价（建表时不建 HNSW）
python scripts/bench_store.py --backend pg --db "$DSN_RW" --no-index

# 或者一次跑两个后端（内存差值仍有效，峰值无效）
python scripts/bench_store.py --backend both --db "$DSN_RW"
```

跑完之后，把两次结果放在一起对比（脚本会报 top-1 一致率与 overlap@k）：

```bash
python scripts/bench_store.py --backend pg --db "$DSN_RW" \
    --reference bench/store-json.json
```

> pg 这一路会**按 source 重灌 `doc_chunks`**（见 §1.4），用默认语料跑完，库里就是
> `chunk 512 / overlap 64` 那一版。要恢复成你自己 ingest 的版本：
> `python agent_cli.py --ingest examples/knowledge-dba --db "$DSN_RW" --rebuild`。

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
python scripts/test_layers.py --suite checks --db "$DSN_RW"

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
