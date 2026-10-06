# 基准测试操作手册

按顺序照做的清单：从推代码到出报告。每条命令后面写「应该看到什么」和「不对时怎么办」。

参数含义、指标口径、每个脚本回答什么问题，在 [bench-guide.md](bench-guide.md)；
这份只管操作顺序。离线那一半已经在 Windows 上跑过，数字见
[bench-report.md](bench-report.md)；这份手册的重点是 live 那一半——
本机没有 PostgreSQL，pgvector 侧、真库工具执行、API 嵌入都只能在 VM 上做。

**要测「CPU 与内存差多少」而不是「延迟差多少」，看
[bench-scale-runbook.md](bench-scale-runbook.md)**：40 片时两个后端的资源差
被噪声淹没，那份手册把语料放大 100 倍后重测，并且解释了为什么必须这么做。

---

## 0. 本机：推代码

```bash
cd ~/schemafence
git status --short          # 应该干净
git push origin main
```

- **应该看到**：`main -> main`。
- 待推的提交：`git log --oneline origin/main..HEAD` 看一眼清单，应该是 4 个
  （审计日志 fix、基准脚本、报告与指南、指南连接身份修正）加上本次的 ingest 变更。
- **不对时**：`git status` 有意外改动就先看清是什么，别 `-A` 一把提。

---

## 1. VM：拉代码与依赖

```bash
cd ~/schemafence
git pull
source .venv/bin/activate
pip install -r requirements.txt
python3 -c "import psycopg; print('psycopg', psycopg.__version__)"
```

- **应该看到**：psycopg 3.x 的版本号。
- **不对时**：`ModuleNotFoundError: psycopg` 说明 venv 没激活或 pip 装到了别的解释器，
  先 `which python3` 确认自己在哪个环境里。

---

## 2. VM：库侧准备

```bash
sudo -u postgres psql -d fence_demo -f scripts/setup_rag.sql

export DSN_RW='postgresql://postgres:pgvec123@localhost:5432/fence_demo'
export DSN_RO='postgresql://agent_ro:ro_only@localhost:5432/fence_demo'

psql "$DSN_RW" -c "SELECT 1"
psql "$DSN_RO" -c "DELETE FROM shop.orders WHERE 1=1"
```

- `setup_rag.sql` 是幂等的，重复跑没事，它建 `doc_chunks` + HNSW 索引 + 只读角色 `agent_ro`。
- 最后一条**必须报错**：`ERROR:  permission denied for table orders`。看到 ERROR 才是对的，
  这是七层护栏里 L7a 的物理兜底。
- **两个连接身份别混**：写库与读 `pg_stats` 用 `DSN_RW`，只读验证用 `DSN_RO`。
  `agent_ro` 只有 `doc_chunks` 的 SELECT，拿它跑存储基准会在建表时就被拒。见 guide §1.2。

---

## 3. 测试一：向量化（三个批次）

```bash
# 3.1 超参网格：切片 × overlap × IDF = 18 种配置
python3 scripts/bench_embedding.py --mode offline \
    --chunks 256,512,1024 --overlaps 0,64,128 --idf on,off \
    --out bench/embedding-offline.json

# 3.2 维度折衷：同一批切片，只换向量维度
python3 scripts/bench_embedding.py --mode offline --dims 128,256,512,1024 \
    --chunks 512 --overlaps 64 --idf on --out bench/embedding-dims.json

# 3.3 语义嵌入（需要 SF_EMBED_API_KEY）
python3 scripts/bench_embedding.py --mode api --dims 1024 \
    --chunks 512 --overlaps 64 --out bench/embedding-api.json
```

- **应该看到**：每个批次一张 Markdown 表，列里有 `hit@5`、`MRR@5`、`q p50/p95`、`store`。
- **关注三件事**：
  1. 3.1 与 3.2 复现出本机那组数（chunk 256/512 = 97.7%，1024 = 95.5%；维度越低命中越低）；
  2. 3.3 的 `hit@5` 比离线高多少——这是词法 vs 语义的量化差；
  3. 那道同义题（"…占一半…" vs 语料里的 "50%"）在 3.3 里是否翻盘。离线 18 种配置它全败。
- **不对时**：3.3 报 key 相关错误，先用 guide §1.3 的 `curl` 确认通路。
  注意 API 那条每行配置都要全量嵌入一次，成本按行数算，别把网格开太大。

---

## 4. 测试二：存储对比（两个后端分进程跑）

```bash
python3 scripts/bench_store.py --backend json --repeat 10 --out bench/store-json.json

python3 scripts/bench_store.py --backend pg --repeat 10 --db "$DSN_RW" \
    --reference bench/store-json.json --out bench/store-pg.json
```

- **应该看到**：每个阶段一行——wall、CPU、CPU 占 wall 比、RSS 增量与峰值、进程读写字节。
  pg 那次末尾还有 top-1 一致率与 overlap@k。
- **关注三件事**：
  1. 灌库代价——pgvector 在 autocommit 连接上逐行 INSERT，对比 JSON 写一个文件；
  2. `search` 与 `search_seqscan` 的 p50 比值，这是 HNSW 值不值的答案；
  3. top-1 一致率——HNSW 是近似索引，低于 100% 要能说出差在哪几题。
- **注意**：pg 这一路会**按 source 重灌** `doc_chunks`（见 guide §1.4），
  跑完库里就是 `chunk 512 / overlap 64` 那一版。要恢复自己的版本：
  `python3 agent_cli.py --ingest examples/knowledge-dba --db "$DSN_RW" --rebuild`。

---

## 5. 测试三：七层检查 + 七层护栏

```bash
# 两条命令写同一个 --out，第二个会与第一个合并（不会互相覆盖）
python3 scripts/test_layers.py --suite guard --out bench/layers.json

# 补跑库侧的 check 5（读 pg_stats）
python3 scripts/test_layers.py --suite checks --db "$DSN_RW" --out bench/layers.json

# 也可以一条命令跑完，但库侧那条还得单独补
python3 scripts/test_layers.py --out bench/layers.json

# 打印库侧那一层需要的 SQL 与 psql 验证命令
python3 scripts/test_layers.py --show-env
```

- **应该看到**：逐条 `PASS`。第一条是 `44/44 cases passed`，第二条是 `17/17`（1 条 SKIP，
  因为 check 5 的另一半没接库）+ 一句 `note: no guard rows in this file`——**这句是提醒，不是错误**：
  它说的是"这次没跑 guard，文件里那份是上一次的"。第二条跑完 `bench/layers.json` 里就是 44 + 18。
- 如果某条命令打印 `note: no guard rows …`，而你又没先跑过 guard，那报告里就会缺这一半。
  报告会明确写出"未包含"哪个套件，不会装作它跑过了。
- 断言的不只是"拒绝了没有"，还有**停在哪一层**和**理由里有没有关键词**。

库侧那两条要亲眼看输出，它们是 L7 唯一的物证：

```bash
# L7a：只读身份写不进去
psql "$DSN_RO" -c "DELETE FROM shop.orders WHERE 1=1"
# 期待: ERROR:  permission denied for table orders

# L7b：实例真的执行 statement_timeout
psql "$DSN_RW" -c "SET statement_timeout='10s'; SELECT pg_sleep(20)"
# 期待: 约 10 秒后 ERROR:  canceling statement due to statement timeout

# L7c：工具调用有没有留下审计记录（decision / layer 都该有值）
tail -n 5 agent_trace.jsonl
```

- **关注**：`agent_trace.jsonl` 里被拦下的写操作应该显示 `decision=blocked` 和具体层号。
  如果这里全是 `ok` 加空 layer，说明回填又断了——这正是 `28791b7` 修的那个 bug。

---

## 6. 测试四：生成报告

```bash
python3 scripts/bench_report.py          # → docs/bench-report.md
```

- **应该看到**：`docs/bench-report.md` 被重写，第 2、3 节数字来自你刚跑出来的 JSON。
- 没跑过的部分（比如没有 key 时的 API 批次）不会消失，会渲染成**产生它的那条命令**，
  这样"测过"与"没测"分得清。
- **别碰** `docs/bench-findings.md`：那是手写的结论解读，脚本不覆盖，重跑也不该被覆盖。

---

## 7. 收尾：把结果带回来

```bash
git status --short                       # bench/ 不在库里（已 gitignore）
git diff --stat docs/bench-report.md     # 只有报告被重写
git add docs/bench-report.md && git commit -m "docs: refresh the benchmark report from the VM run"
git push
```

- 原始 JSON 留在 `bench/` 即可，不必提交；报告提交就有证据链。
- 如果 VM 上的数字与本机差得多（比如 CPU 占 wall 比、p50），那是机器差异，
  在结论里写清在哪台机器上量的，别当成回归。

---

## 附 A：手动验证项（本机无法覆盖的）

| 编号 | 要验什么 | 怎么验 |
|---|---|---|
| M1 | pgvector 读写与 HNSW | 第 4 节，看索引与顺序扫描的比值、top-1 一致率 |
| M2 | 四个工具真执行 | `demo.py --db "$DSN_RW"`，看 `get_table_stats`/`explain_sql`/`run_sql` 的实际返回 |
| M3 | `search_path` 补 public 在真库的名称解析 | `--ask "统计订单数" --db "$DSN_RW" --search-path shop`，`doc_chunks` 应仍能解析 |
| M4 | L7 只读会话对"写形态 SELECT"的兜底 | 第 5 节那两条 psql |
| M5 | API 嵌入与 0.45 地板校准 | 第 3.3 节；看 `--eval` 打印的 top-1 分数落在什么区间 |
| M6 | API LLM 的多轮 function calling | `--ask "..." --llm openai --db "$DSN_RW"`，看 trace 里工具选择是否合理 |
| M7 | `--tune`/`--report` 在大语料上的区分度 | 换自备语料（≥100 片）重跑；离线小语料 spread 为 0 |

## 附 B：常见问题

| 现象 | 原因 | 处置 |
|---|---|---|
| `permission denied for table doc_chunks` | 用了 `agent_ro` 跑写入路径 | 换 `DSN_RW` |
| `psycopg` 找不到 | venv 没激活 | `source .venv/bin/activate` |
| ingest 打出 `orphan rows` | 换过语料目录，旧笔记还在表里 | 只放一套语料就加 `--rebuild`；有意共存则忽略 |
| `--dim` 改了之后 INSERT 报维度不符 | 表已按旧维度建好，`CREATE TABLE IF NOT EXISTS` 不会改它 | `DROP TABLE doc_chunks` 后重灌，或 `--rebuild` 前先改表 |
| 报告里某节只有命令没有数字 | 那一批没跑 | 按第 3、4 节把缺的批次跑掉再生成 |
| 数字与上一次差很多 | 机器不同，或后台有别的重活 | 本套基准的差异量级到毫秒，跑对比时别开编译、别同步大文件 |
