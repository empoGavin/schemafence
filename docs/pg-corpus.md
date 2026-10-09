# PG 语料包：官方手册 + 内部书 + 运维笔记，三套尺子

`examples/knowledge-pg/`、`examples/knowledge-pg-official/` 和
`examples/knowledge-pg-internals/` 是三个**独立**的语料包，一次只能灌一个。
它们回答的是两类不同的问题：

| 语料包 | 回答什么 | 规模 | 评测 |
|---|---|---|---|
| `examples/knowledge-pg/` | 「现场出了这个现象，我按什么顺序查」 | 31 篇 / 147 切片 | `eval/questions-pg.md`（50 题）**100% top-5** |
| `examples/knowledge-pg-official/` | 「这个参数叫什么、官方怎么规定的」 | 98 篇 / 3312 切片 | `eval/questions-pg-official.md`（40 题）**82.5% top-5** |
| `examples/knowledge-pg-internals/` | 「它内部到底是怎么运作的」（MVCC / 缓存 / WAL 机理） | 13 篇 / 145 切片 | `eval/questions-pg-internals.md`（35 题）**100% top-5** |

## 一、手写包：`examples/knowledge-pg/`

```
handbook/   12 篇  PG 通识：进程与内存、物理存储、MVCC、WAL、checkpoint、
                    VACUUM、XID 回卷、索引族、计划器与代价、统计信息、
                    锁与死锁、等待事件
incidents/  14 篇  故障复盘（虚构案例）：单实例 8 + 复制 6
runbook/     5 篇  诊断流程：证据采集、单实例故障、复制故障、整库性能、SQL 与执行计划
```

三类内容的写法不同，各有分工：

- **handbook** 讲机制与取舍，落到「DBA 在哪个视图里能看到它」；
- **incidents** 按 `现象 → 先收集什么证据 → 定位过程 → 结论与处置 → 复盘要点` 写，
  重点是**定位过程**里的排除动作——哪条假设被什么证据否掉了，比最终答案更可复用；
- **runbook** 是**流程**，不是知识。它规定排查的**顺序与分岔点**，
  这正是 hand-written 语料最能补上官方手册缺口的地方：手册不会告诉你先看哪个。

来源与合规：公开 PostgreSQL 知识整理（对照 PG 16 官方文档核对参数名与默认值）+
完全虚构的案例。案例中的库名、主机名（`orders_prod`、`pg-primary-01`）均为虚构，
不含任何公司内部信息。判定标准与检查清单见 [`synthetic-corpus.md`](synthetic-corpus.md)。

用法：

```bash
python agent_cli.py --corpus examples/knowledge-pg \
                    --store .schemafence/pg.json \
                    --ingest examples/knowledge-pg \
                    --eval --eval-file eval/questions-pg.md
```

## 二、官方包：`examples/knowledge-pg-official/`（生成物）

从 *PostgreSQL 16.15 Documentation*（The PostgreSQL Global Development Group，
PostgreSQL License）PDF 重新排版而来，**一章一文件**，按 Part 分子目录：

```
preface/  part-i-tutorial/  part-ii-the-sql-language/  part-iii-server-administration/
part-iv-client-interfaces/  part-v-server-programming/  part-vi-reference/
part-vii-internals/  part-viii-appendixes/
_manifest.json      # 每篇的章节、页码范围、字符数
```

生成器是 `scripts/convert_pg_official.py`，**输出不进仓库**（`.gitignore`）：
它是第三方文档的派生物，6.9 MB / 3312 切片，把生成器入版本、产物不入，和
`scripts/make_scale_corpus.py` 的做法一致。

```bash
python scripts/convert_pg_official.py --pdf postgresql-16-A4.pdf \
                                      --out examples/knowledge-pg-official
```

抽取时踩到的三个坑，每一个都会静默毁掉语料：

**1. 行首的 `#` 不是标题。** `chunk_markdown()` 把 `^(#{1,6})\s+` 当章节边界，
而手册里满是 shell 会话与 SQL 注释。不转义的话，一章会被切成几百个单行切片。
脚本把这类行写成 `\#`。

**2. 页眉与页码必须剥掉。** 每页开头的文档标题和结尾的页码如果留着，
会成为全语料词频最高的 token，把 IDF 直接带偏。

**3. 小节标题必须重新变成标题。** 手册自己的 `19.3. Starting the Database Server`
从 PDF 出来是纯文本。用 PDF 大纲（3945 条）把小节标题提回 `##` / `###`，
它们才会成为切片边界、才会内联进 `chunk_text` ——否则按小节提问检索不到。

另外：DocBook 生成的 Part 名里含不换行空格（`Part\u00a0III.\u00a0…`），
肉眼看不见，但会让任何字符串比较失败。脚本在写 manifest 前统一折叠。

## 三、内部书包：`examples/knowledge-pg-internals/`（生成物）

从 *PostgreSQL 14 Internals*（Parts I & II，Egor Rogov，Postgres Professional
2022，出版社免费分发）PDF 重新排版而来，**一章一文件**，按 Part 分子目录：

```
introduction/                  数据组织、进程与内存、客户端协议
part-i-isolation-and-mvcc/     隔离级别、页面与元组、快照、页修剪与 HOT、
                               VACUUM、冻结、重建表
part-ii-buffer-cache-and-wal/  缓冲池、WAL、WAL 模式
_manifest.json                 每篇的章节、页码范围、字符数
```

生成器是 `scripts/convert_pg_internals.py`，**输出不进仓库**（`.gitignore`）。
注意合规差异：官方手册是 PostgreSQL License（允许再分发），这本书**不是开源许可**
——出版社免费分发 ≠ 允许转发，所以产物只留本机、只作个人检索用，不入仓库。

```bash
python scripts/convert_pg_internals.py \
    --pdf postgresql_internals-14_parts1-2_en.pdf \
    --out examples/knowledge-pg-internals
```

这本书的 PDF 有一个官方手册没有的坑，值得单独记一笔：

**4. 字体子集的 ToUnicode 映射坏了，而且是按字体坏的。** 抽出来满屏是
西里尔区字符（`PostgreԯԭԨ`）。同一码点在不同字体里偏移量不同：
PT Serif 的 `U+052F−0x4DC=S`，PT Sans 的 `U+0526−0x4DD=I`（INSERT），
斜体的 `U+0539−0x4E1=X`（virtual XID）——数字块又是另一套偏移。
修法是**逐字体 × 逐码点区间的常量偏移表**，且每一项都要拿渲染出来的页面图
对过（JPEG images、PGDATA、VACUUM、ANALYZE、NOT NULL、version 10、
64-bit、9.4、1976、$800、2022 扉页）才算数；转换器把没映射到的字符
计数并在结尾报错，宁可不产出也不静默出垃圾。生成结果：全书 120 个小节标题
全部提级回 `##`/`###`，零残留西里尔字符。

另有两个小陷阱：**词间空格丢在字符层**（斜体接正体处空格字形缺失，
只剩 1.0–1.5pt 的字间空隙；正文断词处的行尾连字符要合并回来），
代码行（`ps -o pid,command`、`(pageno,lp)`）绝不能碰。

评测：**35/35 = 100% top-5**，检索 11 ms。首轮 94.3%，2 个 MISS 都是
提问用词没贴着书的用词（"bloated table" vs 书里的 "full vacuuming /
REINDEX"；书里 2 页的小章根本没提 work_mem）——书是 ground truth，
改题不改书。13 章 / 145 切片的规模正好是"形状好"的语料：没有
参考手册那种 700 页的巨章来淹没检索。

## 四、评测结果，以及那个更重要的发现

手写包首轮只有 **86.0%（43/50）**。逐个诊断后，7 个失败分两类，**修复方向完全相反**：

- **3 个是标注问题**：`pg-transaction-id-wraparound` 与 `inc-single-xid-wraparound`
  都真正对口，单标注把真命中记成了失败 → 改题库写成 `甲 / 乙`。
- **4 个是词汇缺口**：「连接被**占满**」对不上「连接数被**打满**」，
  「IO 尖峰和延迟抖动」接不住「IO 抖动怎么**缓解**」。
  离线哈希词袋靠**共享 token** 匹配，语料没写过的说法就是取不回来
  ——**这要改语料，不是改题**：真实知识库本来就该同时收得住术语和口语。

补写 7 处表述（每处都是能独立成立的内容，不是堆关键词）+ 1 处标注后升到 98.0%；
再补一处「整体写入变慢被误判成 SQL 问题、实为 checkpoint 过频」的判别后到 **100.0%**。

官方包全量 **82.5%**，但诊断显示 7 个失败里有 6 个的目标章节仍在 top-30 内
（rank 6~23）——**不是知识不在语料里，是被大章节淹了**：top-1 几乎总是
`pg16-sql-commands`（全书最大，约 700 页）或 `chapter-9-functions-and-operators`。
只灌 Part III（18 篇 / 503 切片）后，同一组题 **18/18 命中**。

> **语料形状比语料规模更能决定检索质量。**
> 覆盖面和区分度是反向拉扯的：全量手册什么都查得到，也就什么都查不准。
> 这正好实测印证了 `agent_cli.py` 里那句 `Corpora are searched one directory at a time`。

所以官方包的推荐用法是**按 Part 灌**：

```bash
python agent_cli.py --corpus examples/knowledge-pg-official/part-iii-server-administration \
                    --store .schemafence/pg-p3.json \
                    --ingest examples/knowledge-pg-official/part-iii-server-administration
```

`rglob` 会递归，所以 `--ingest examples/knowledge-pg-official` 仍然收全量，按需选粒度。

### 两把尺子：诊断题 ≠ 覆盖题

`questions-pg.md` 问的是「出了这个现象按什么顺序查」；`eval/questions-pg-qa.md`（16 题）
问的是**「这个 SQL 怎么写、这个参数叫什么」** —— 后者是手写诊断包**一个字都没有**的另一半。
两把尺子必须分开量，因为结论正好相反：诊断包在自己那套题上 100%，在覆盖题上 **0/16**。
完整对照见 §五 的「合并的真实代价」。

> **题库文件的解析规则（写新题库前先读这条）**：`load_questions()` 收的是
> 「**任何**以 `|` 开头、列数 ≥ 3」的行，只跳过第一列正好是 `#` 的表头行。
> 所以**题目表格上方不要再放任何表格** —— 那张说明用的表会被当成题目收进去，
> 分母虚高、命中率被静默拉低（我在 `questions-pg-qa.md` 上踩过一次：16 题被读成 20 题）。
> 写说明请用无序列表。

## 五、灌进 pgvector：两种形状，先选形状

`--db` 一挂上，存储就从 JSON 换成 pgvector（表名固定 `doc_chunks`，HNSW 余弦索引）。
灌之前先决定一件事：**一个库装一套语料，还是一个库装全部。**

```bash
# 前置：psycopg 是 --db 唯一的第三方依赖（离线 demo 与 JSON 模式都不需要它）
python -m pip install -r requirements.txt

# 形状一（默认）：五套语料 → 五个库，各自的 IDF 独立
python scripts/ingest_pgvector.py --base postgresql://postgres:pw@localhost:5432

# 形状二：五套语料 → 一个库，一个问题问遍全部
python scripts/ingest_pgvector.py --base postgresql://postgres:pw@localhost:5432 --single-db
```

| | 形状一（默认） | 形状二 `--single-db` |
|---|---|---|
| 库 | 5 个：`kb_default` / `kb_dba` / `kb_hand` / `kb_official` / `kb_internals` | 1 个：`kb_schemafence`（`--db-name` 可改） |
| 适合 | **量尺** —— 每套语料本身好不好用，基线可逐条复现 | **干活** —— agent 手上就一份知识库，不用先猜该问哪套 |
| 代价 | 你得自己决定问题该进哪个库 | 检索被稀释，`--k` 要调大 |

> **`--db` 同时是「知识库」和「被诊断的对象」。** agent 的 live 模式会去读这个库里真实的 schema，
> 所以**想让 agent 诊断哪个库，语料就得灌进哪个库**。想看它诊断 demo 那 7 张 shop 表，就
> `--single-db --db-name fence_demo`；灌进另外的库，它诊断的也就只是那个库（里面只有 `doc_chunks`，
> 没什么可诊断的）。

只灌其中几套、先空跑看计划、或复用已合并的目录：

```bash
python scripts/ingest_pgvector.py --base ... --only kb_hand --only kb_internals
python scripts/ingest_pgvector.py --base ... --dry-run
python scripts/ingest_pgvector.py --base ... --single-db --reuse-merged   # 不重新合并
```

### 按用途分家：诊断一个库、问答另一个库

**分家的单位是「库」，不是「表」。** `PgStore` 的表名写死 `doc_chunks`
（`DOC_CHUNKS_DDL`，且不带 schema 前缀），所以一个库里所有语料共用一张表、
共用一份 `<--store 的父目录>/idf.json` —— 「同库两张表」现在做不到，而「分库」本来就是形状一。

它值得推荐的原因不只是隔离，还有上面那条**双重身份**：诊断那类语料**必须**待在
「被诊断的那个库」里，问答那类放哪儿都行。所以按用途分家正好顺路：

```bash
# ① 诊断库 = 你真正要诊断的那个库里放诊断语料（Runbook + 内部书）
python scripts/ingest_pgvector.py --base postgresql://postgres:pw@localhost:5432 \
       --single-db --db-name fence_demo --only kb_hand --only kb_internals

# ② 问答库 = 单独一个库，放"查文档"那一类（手册 + 通识）
python scripts/ingest_pgvector.py --base postgresql://postgres:pw@localhost:5432 \
       --single-db --db-name kb_qa --only kb_official --only kb_default --only kb_dba

# ③ 用的时候按目的换 DSN
python agent_cli.py --db postgresql://postgres:pw@localhost:5432/fence_demo \
                    --store .schemafence/pgv/fence_demo/store.json \
                    --ask "主从复制延迟高，先看哪些指标？"           # 诊断库
python agent_cli.py --db postgresql://postgres:pw@localhost:5432/kb_qa \
                    --store .schemafence/pgv/kb_qa/store.json \
                    --ask "How do I write INSERT ... ON CONFLICT?"   # 问答库，配 --k 20
```

> 两次 `--single-db` 运行会互相覆盖 `examples/knowledge-merged/`（除非 `--reuse-merged`），
> 但那时灌库已经灌完，所以没有影响 —— `--only` 只决定这一次往哪个库灌什么。

**真想要「同一个库、两张表」，两条路：**

- **加 `--schema` 支持**（推荐，改动小）：`PgStore.__init__` 里在 `ensure_schema()` 之前执行
  `CREATE SCHEMA IF NOT EXISTS <schema>`，再 `SET search_path TO <schema>, public`。
  因为 DDL 里的表名本来就不带前缀，`search_path` 一挂，`doc_chunks` 就落到那个 schema 里，
  下面所有 `DELETE/INSERT/SELECT` 自动跟随。`search_path` 里必须留着 `public`，
  否则 `CREATE EXTENSION vector` 建出来的 `vector` 类型解析不到。
- **不改代码的等价物**：先手工 `CREATE SCHEMA diag`，再在 DSN 上带
  `options=-c%20search_path%3Ddiag%2Cpublic`。这条我**没有在活库上验过**
  （开发机没有 PostgreSQL），你在虚拟机里试一次即可判定。

### 形状二为什么必须「先并目录、再灌一次」

`PgStore.replace()` 是「按 source 删旧的、再插新的」，不是清表 —— 所以分 5 次灌
**不会**留下重复行，这部分本来就支持（它 docstring 里写着 "lets two corpora share
one table"）。真正会坏掉的是**词权重**：离线嵌入的 IDF 落在
`<--store 的父目录>/idf.json`，每次 ingest 都整份覆盖它。

| 动作 | `idf.json` | 库里已存的向量 |
|---|---|---|
| 灌第 1 套 | = 第 1 套的权重 | 按第 1 套权重存的 ✓ |
| 灌第 2 套 | 被换成第 2 套的 | 第 1 套的仍按旧权重 —— **对不上了** |
| … | … | … |
| 查询 | = **最后一套**的权重 | 只有最后一套是对的 |

→ 排序悄悄漂移，**不报错**。

所以 `--single-db` 的做法是：先把 164 篇并进一个目录，再灌**一次**。
`build_idf` 在并集上只算一次，所有向量共用同一份权重，错配就不存在。
合并这一步不碰数据库，所以它排在连库之前 —— 没有 psycopg、没有 PG 的机器上也能先把目录搭好。

> 并目录时会**断言全局 0 处文件名重名**。`chunk_markdown(source=path.stem)` 按 stem 认文档，
> 两篇同名会被压成一个 source：旧的在表里被删掉、题库指向错的文本，而查询时毫无征兆。
> 当前五套共 164 篇、164 个唯一 stem，可以合。

### 换成 API 嵌入：IDF 这条约束直接消失

`--mode api`（`SF_EMBED_API_KEY`）下 IDF **完全不参与**，代码里三处都写着：

- `ingest_directory()`：`if embedder.mode == "offline": embedder.idf = build_idf(texts)`
  —— API 模式**从不计算** IDF。
- `agent_cli.py` 灌库收尾：`elif embedder.mode == "offline": save_idf(...)`
  —— API 模式**不产生 `idf.json`**。
- `open_store()`：加载 sidecar 只在 offline 分支；表头也直接返回 `api / <model> / 1024d`。

**于是「先并目录、再灌一次」这条约束在 API 模式下不存在。** 那条约束的唯一来源就是
`idf.json` 被每次 ingest 整份覆盖；没有 idf.json，`PgStore.replace()` 本来就是
「按 source 删旧插新」，可以**一套语料一套地分次灌进同一个库**，向量空间照样一致（同一个模型）。
不想为了一次全量重灌而重跑时，这很省事。跨语言也顺带解决了：中文问英文语料，离线不行，API 行。

**但约束换成了两条新的，两条都不报错：**

1. **换嵌入件必须整套重灌，而且没有任何防线。** store 里**不记录 mode**
   （`JsonStore.save()` 只写 `dim` / `idf` / `chunks`；`PgStore` 连元数据表都没有），
   而离线默认 `DIM = 1024` 与 `text-embedding-v3` 默认维度**恰好一样**。
   所以「拿离线灌的库用 `--mode api` 查」既不缺维度、也不报错，只会把两套不可比的向量
   算余弦 —— 排名全是噪声。**换嵌入件 = 重新灌库。**
2. **相关度地板要重校。** `tools.py` 按 `embedder.mode` 取地板：offline **0.0**（不设），
   api **0.45**。这个 0.45 是给 dashscope 那类模型定的；换模型后余弦分布不同，0.45 可能
   **静默丢掉全部 passage**（`tools.py` 的注释里记着同一个坑的反向版本：离线分数低 5–10 倍，
   套 API 地板把刚灌完的语料全丢了）。校法：看 `--eval` 输出的 top-1 score 列，再定 `--min-score`。

> **合并稀释在 API 模式下还剩多少，我没量过** —— 开发机没有 `SF_EMBED_API_KEY`，
> 上面那张「大库污染小库」的表全是离线哈希词袋的结果。真实嵌入对体量的耐受度明显更好，
> 但「检索是排名竞争」这条不会消失。有 key 之后值得重跑一遍 §五 那几张表。

### 合并的真实代价（实测）

离线嵌入、512/64 切片、`--k 5`、201 题。先看**诊断题**随库长大的退化阶梯 ——
每一步只加语料，别的都不动：

| # | 库的构成 | 切片 | `questions-pg.md`（诊断）@5 |
|---|---|---|---|
| A | 只有手写诊断包（31 篇） | 147 | **100.0%** |
| B | + 原包 + dba 包 + 内部书 | 348 | 92.0% |
| C | + 官方手册 Part III + VII | 1230 | 78.0% |
| D | + 官方手册全部（= 全量合并） | 3660 | 70.0% |

**代价是方向的、不对称的。** 3312 片的手册加进 147 片的诊断包，诊断题 −30pt；
反过来，147 片的诊断包加进 3312 片的手册，覆盖题 **±0**：

| 覆盖题 `questions-pg-qa.md`（16 题） | @5 | @20 |
|---|---|---|
| 只有手写诊断包 | 0.0% | 0.0% |
| 只有官方手册 | 81.2% | 93.8% |
| 全量合并 | 81.2% | 93.8% |

结论很直白：**大库污染小库，不是互相污染。** 检索是排名竞争而不是查表，
加语料就是加对手，而对手的杀伤力与它的体量成正比。

再看五套尺子一起量（新增一列：全量库里删掉 `pg16-sql-commands` 这一篇）：

| 题库 | 题数 | 各自的库 @5 | 全量合并 @5 | 全量 **−** `pg16-sql-commands` @5 |
|---|---|---|---|---|
| `questions.md` | 16 | **100.0%** | 68.8% | 68.8% |
| `questions-dba.md` | 44 | **97.7%** | 79.5% | 79.5% |
| `questions-pg.md`（诊断） | 50 | **100.0%** | 70.0% | **72.0%** |
| `questions-pg-official.md` | 40 | **82.5%** | 80.0% | **82.5%** |
| `questions-pg-internals.md` | 35 | **100.0%** | 68.6% | 68.6% |
| `questions-pg-qa.md`（覆盖） | 16 | 0.0%（诊断包） | 81.2% | **93.8%** |
| **合计** | **201** | — | 74.6% | **76.6%** |

三条结论：

1. **知识没丢，只是被挤下去了。** 正确篇章平均下沉 2–3 名，所以 `--k 5` 低估了这个库，
   `--k 20` 捞回绝大部分（诊断题 70%→90%，官方题 80%→97.5%）。**合并库配 `--k 20` 用** ——
   `--k` 是「给模型看几段」，不是「语料有几篇」。
2. **砍 Part 没用，但删那一篇巨型参考是纯赚。** 砍掉官方 Part II/IV/V/VI/VIII 只留
   III+VII：诊断题涨到 78.0%，官方题反而掉到 65.0%，加权 74.6% vs 74.1% —— **换手气而已**。
   而只删**一个文件** `pg16-sql-commands`（982 KB、506 片，占全语料 14%，纯语法速查）
   是 Pareto 改进：**五套尺子无一下降，两项明显上升**，加权 74.6% → 76.6%。
   机理和 §四 那个发现是同一条 —— 巨型泛化章节会抢走「具体章节」的名次，
   而它自己几乎不被任何一道具体问题当作答案。
3. **「不问文档、直接问助手」这个用法只能靠全量。** 诊断包答不了 16 道 SQL 写法题中的任何一道，
   全量答对 15 道。所以全量不是坏事，它是**能力上限**；要付的代价是排名下沉，
   而排名下沉可以用 `--k 20` 和小库分治来还。

延迟：合并库 **390–570 ms**（3660 片暴力点积 + 排序）；单库是 2 ms（原包）/ 13 ms（手写包、
内部书）；官方包自己一个库也要 318 ms。几百毫秒对交互式提问够用。

> **想同时要「一个库」和「不稀释」**：走 `--mode api`（`SF_EMBED_API_KEY`）。真实嵌入没有
> IDF 这回事，分几次灌都一致，而且跨语言检索（中文提问 vs 英文语料）本来就更强。
> 离线哈希词袋只是零 key 的默认件。

### 连你的虚拟机

`quickstart-oraclelinux-vm.md` 装出来的那台 Oracle Linux 10，**PG 只监听 `127.0.0.1:5432`**
（该指南 §9.4 验证过：`ss -lntp` 只见 127.0.0.1，没有 0.0.0.0）—— 这是有意的，库不对外暴露。

- **推荐：在虚拟机里跑。** 把仓库和语料弄进去（指南 §4：WinSCP SFTP 整目录拖，或 `scp -r`），
  然后在虚拟机里执行上面的命令，`--base` 用 `localhost`。
- **想从 Windows 直连**（`--base postgresql://postgres:pw@192.168.181.128:5432`）要**三处一起改**：
  `listen_addresses = '*'`（postgresql.conf）→ pg_hba 加一行
  `host all all 192.168.181.0/24 scram-sha-256` → `firewall-cmd --add-port=5432/tcp --permanent`
  再 `--reload`。**只改前两处，firewalld 照样丢包**。VMware NAT 下宿主机能直达虚拟机 IP，
  不用配端口转发。

### 三个前提，还有一个提醒

1. `--db` 需要 psycopg。报 `ModuleNotFoundError: No module named 'psycopg'` 就是它没装。
2. 目标库要有 `vector` 扩展。`ensure_schema()` 自己会 `CREATE EXTENSION IF NOT EXISTS vector`，
   所以用超级用户跑即可；不是超级用户就先手动建一次（见 `scripts/setup_rag.sql`）。
3. **查询时的 `--store` 必须和灌库时一模一样** —— live 模式下它的作用只剩「决定 `idf.json`
   落哪个目录」，路径变了就读不到同一份权重，排序会漂。

> **持久化**：Cloud Studio 的工作空间是容器，文件系统不保证持久化，重启后库可能就没了
> （`quickstart-cloudstudio.md` 的 FAQ 里写着）。要把语料长期留住，得把 PGDATA 放到
> 持久化卷上，或者落到本机 / 虚拟机 / 其它常驻实例 —— **「灌进去」和「存住了」是两件事**。

## 六、换成你自己的内容

1. 手写包最小路径：先写 3–5 个你真正踩过的坑（走「脱环境经验」那一路），
   参照 `incidents/` 的五段骨架——**定位过程**那段是别人抄不走的部分。
2. 官方包按需生成：`--pdf` 指向你手上的手册，`--subsection-level` 控制小节提级深度。
3. `--ingest` 灌库，`--genq` 生成题库草稿，改完跑 `--eval`；命中率不达标时先看
   失效样本那一列，再决定动语料还是动切片。
4. 发布前过一遍 [`synthetic-corpus.md`](synthetic-corpus.md) 末尾的自查清单。
