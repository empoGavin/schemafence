# 基准测试：数字与结论，以及怎么复现

三件事各测一段：**向量化的超参**怎样影响命中率、**JSON 与 pgvector** 两种存储的资源账、
**七层护栏与静态检查**的逐层行为。四个脚本，全部命令在仓库根目录执行。

| 脚本 | 回答的问题 | 离线可跑 |
|---|---|---|
| `scripts/bench_embedding.py` | 语料在不同超参下向量化的速度、体积、命中率 | 是 |
| `scripts/bench_store.py` | JSON 与 pgvector 两种存储的检索性能与内存/IO/CPU | JSON 是，pg 需库 |
| `scripts/test_layers.py` | 七层静态检查与七层运行时护栏，逐层正反例 | 是（库侧 1 项需 `--db`） |
| `scripts/bench_report.py` | 把上面三者的 JSON 汇总成 `docs/bench-report.md` | 是 |

`scripts/bench_util.py` 是被这三个脚本共用的测量工具（RSS / CPU / 进程 I/O），不单独运行。

数字来自 VM（Oracle Linux 10，2 核，Python 3.12）与本机（Windows，8 核）的完整运行，
每张表都标了机器——**绝对秒数不可跨机器比，同一台机器上的比值可以**。

第 1 至 6 节是量到了什么，第 7 至 14 节是怎么复现。逐配置的原始表格由脚本生成在
[`bench-report.md`](bench-report.md)，**重跑即覆盖，不要往里面写结论**。

---

## 1. 七条结论

1. **切片尺寸是命中率的唯一旋钮，但它的上限不是 100%。** chunk=256 与 512 全部 97.7%，
   chunk=1024 全部 95.5%。1024 那档把 40 片压成 24 片，一个 chunk 塞进两三件事，证据被稀释，
   于是多丢一道 `VACUUM FULL 和 pg_repack 应该怎么选？`——它只在 chunk=1024 的 6 个配置里失败，
   256/512 的 12 个配置全过。而另一道 `磁盘还有一半空间，为什么还要提前扩容？`
   在**全部 18 个配置**里都失败，与切片无关。两道题的性质完全不同，第 2 节分开说。
2. **语义嵌入把那条天花板拿掉了。** 同一份语料、同一套题，API 向量模型 **44/44 = 100%**，
   离线哈希 97.7%。MRR 0.928 对 0.894，top-1 相似度落在 0.541–0.818。
   第 2 节展开；这是整个项目里"词法 vs 语义"最干净的一条证据。
3. **维度是标价折衷，价目表在 VM 上复现了本机的数。** 同一批切片，1024d → 97.7%，
   512d → 95.5%，256d → 88.6%，128d → 81.8%；存储 372 → 260 → 191 → 141 KB。
   MRR 同步下降（0.894 / 0.764 / 0.637 / 0.547），说明压维度伤的不只是召回，还有排序。
4. **IDF 与 overlap 在这套语料上已经没有收益。** 同一切片尺寸下 idf on/off 的 hit@5 完全一致；
   MRR 上 off 反而略高（chunk=1024 差 0.034–0.045）。overlap 0/64/128 只买到更多重复片，
   命中率不动、片数与延迟一起涨。这否定的是**这份量尺的分辨力**，不是 IDF 本身——
   早期 27 片 / 16 题时它修好过排序。结论按语料规模写，不按记忆写。
5. **pgvector 检索快 2.8 倍，但 HNSW 索引本身没帮上忙。** pg p50 0.757 ms 对 JSON 2.137 ms——
   差距来自协议之外的实现差异（JSON 是每查询重建一次排序，pg 是数据库内的 C 代码扫 40 行）；
   而 pg 内部 HNSW 只比顺序扫描快 1.44×。索引本身 784 KB，是数据的 5.4 倍。
   **JSON 与 pg 的 top-1 在 44 题上完全一致**——换存储后端不改结论这条被证实了。第 3 节。
6. **把语料放大 100 倍，JSON 的资源账本立刻露底：内存 5.8×、检索 CPU 158×。**
   4000 片时 JSON 峰值 RSS 212 MB、冷启动后 376 MB、`search` 阶段烧掉 163 秒 CPU，
   且全程 98.3% 占 wall——**没有等待，全是在算**。这个增长是"没有索引 + 单线程线性扫描"
   的必然结果，不是调参能补的。更关键的是它指出了后端的真正分界：
   **JSON 慢在自己的 CPU 上（不可摊薄），pg 慢在网络往返上（可摊薄）**。第 3.1 节。
7. **七层护栏 44/44、静态检查 18/18 全过，又测出一个真 bug**：分两条命令跑时，
   第二次把第一次的战果覆盖掉了，报告却照实写"18/18 passed"。
   与之前那个审计日志 bug 同一类——跑的时候全绿，留下的产物是残缺的。第 4 节。

---

## 2. 向量化：三种嵌入放在一张表里

被测对象：`examples/knowledge-dba`（14 篇 / 40 片 @chunk=512,overlap=64，1024 维），
题库 `eval/questions-dba.md`（44 题，top-5，多来源标注）。三种嵌入都跑了：
离线哈希、API 向量模型（`text-embedding-v3`，1024 维）、以及维度折衷 128–1024。

| | 离线哈希 | API 向量模型 | 倍数 |
|---|---|---|---|
| hit@5 | 97.7%（43/44） | **100%（44/44）** | — |
| MRR@5 | 0.894 | 0.928 | — |
| mean rank@5 | 1.256 | 1.159 | — |
| 嵌入吞吐 | 0.5 ms/片（约 2000 片/秒） | 500 ms/片（2 片/秒） | 慢 1000× |
| 存储（40 片） | 372 KB（9527 B/片） | 942 KB（24117 B/片） | 2.5× |
| 查询 p50 | 2.25 ms | 666 ms | 慢 296× |
| 查询 p95 | 2.58 ms | 5536 ms | 慢 2146× |
| 单次嵌入耗时 | ~21 ms 全量 | ~20 s 全量 | — |

延迟里有个要拆开看的成分：API 的查询延迟主要是**调用嵌入接口**（一次 HTTPS 往返 + 排队），
不是向量检索本身——检索在两种模式下都是同一次全表扫描。所以 666 ms 里绝大部分是网络，
把它当"检索慢"会读错因果。**这条路径要上生产，查询侧必须做嵌入缓存或本地小模型**，
否则每次提问都要付一次网络往返。

### 2.1 那道题翻盘了

`磁盘还有一半空间，为什么还要提前扩容？`（期望 `pg-capacity-planning`，语料原文写的是"磁盘还有 **50%**"）：

- 离线哈希：**18 种配置全部失败**（chunk 256/512/1024 × overlap 0/64/128 × idf 开关）
- API 向量模型：**命中**，且整轮 `misses` 为空

同一个问题、同一份语料，唯一变量是嵌入方式。它回答了"什么时候该换方案"这个问题——
调切片、调 overlap、调 IDF 都救不回来，因为它们改的是切法，而缺陷在"一半"与"50%"之间
没有任何字面重合。**这是选型判据，不是调参空间。**

### 2.2 切片与维度的细节

| 观察 | 数据 | 读法 |
|---|---|---|
| 切片尺寸 | 256/512 → 97.7%；1024 → 95.5% | 证据被稀释，不是检索算法的问题 |
| 片数 vs 延迟（JSON） | 61 片 3.63 ms；40 片 2.25 ms；24 片 1.5 ms（p50） | 线性于片数：JSON 是全量扫描 |
| 片数 vs 体积 | 487 KB；372 KB；272 KB | 片少总体积小，单片体积反而大（向量是固定开销） |
| 嵌入吞吐 | 1087–2719 片/秒 | 离线哈希的成本可以忽略，瓶颈永远在别处 |
| 维度 | 1024→512→256→128 各掉 2.2 / 4.9 / 11.9 个百分点 | 低于 512 维开始明显伤召回 |
| overlap | 0/64/128 差异 < 0.5 个百分点，片数涨 5–9 片 | 只买到重复，代价是延迟 |
| IDF | hit@5 无差异；MRR 上 off ≥ on（1024 档差 0.034–0.045） | 保留即可，别再拿它当卖点 |

一个反直觉的细节：chunk=1024 的 MRR（0.898–0.943）**整体高于 chunk=256（0.883–0.924）**，
尽管它的 hit@5 更低。它多丢了一道题，但答对的那些题排得更靠前——长片把主题收得更紧。
逐 overlap × idf 配对比，6 组里 5 组是 1024 更高（唯一例外是 overlap=128/idf=on）。
含义是 **hit@5 与 MRR 要一起看**：只看命中率会得出"256 更好"，
只看看排序会得出"1024 更好"，实际是"256 多答对一道、但答对的题整体排得更散"。
在只有 44 题的量尺上，一道题的权重是 2.3 个百分点，和 MRR 那 0.03 的差是同一量级，
**不该据此做切片决策**——两个指标都还不稳。

---

## 3. 存储对比：JSON vs pgvector（两个进程各跑一次）

| 指标 | JSON | pgvector | 谁赢 |
|---|---|---|---|
| 灌库（40 片，含切片+嵌入） | 79.4 ms | 204.5 ms | JSON **2.6×** |
| 冷启动 | 10.6 ms | 14.7 ms | JSON 1.4× |
| 检索 p50 | 2.137 ms | **0.757 ms** | **pg 2.8×** |
| 检索 p95 | 2.488 ms | **1.222 ms** | **pg 2.0×** |
| 磁盘占用 | **381 KB** | 1456 KB（表 144 KB + 索引 784 KB） | JSON 3.9× |
| 进程峰值 RSS | **36.75 MB** | 51.81 MB | JSON 省 15 MB |
| 索引 vs 数据 | 无索引 | 索引是数据的 **5.4 倍** | — |

**检索端 pg 明显更快**，这点值得说清成因：JSON 的 `search()` 是 Python 里对 40 条向量
做一轮循环 + 排序，pg 是在数据库内核里扫 40 行。同样是一次全表扫描，
**解释器循环 vs C 循环**的差别就是那 1.4 ms。跟"HNSW 索引有没有用"是两个问题，见 3.2。

代价在别处：pg 每次查询多一个往返，`search` 阶段 CPU 只占 wall 的 43.4%，
JSON 占 98.6%——差出来的全是等待。灌库也是，逐行 INSERT 在 autocommit 上
是 2.6 倍的慢，而且按片数线性涨；体积是 JSON 的 3.9 倍，其中索引比数据本身还大 5.4 倍。

**"JSON 更省、pg 更快"这个对比的适用边界**是 40 行——**这一节的全部数字都受限于规模**。
下面的 3.1 把语料放大 100 倍重测了同一件事，结论变了。

### 3.1 放大到 4000 片：CPU 与内存的真正差距

40 片时两个后端差几 MB RSS、几百毫秒 CPU，和同机器两次运行的噪声同量级。
把语料按 100 倍放大（`examples/knowledge-scale`，2000 篇生成语料 → 4000 片 @chunk=512），
JSON 这一侧的数字立刻变得不容解释：

| JSON 后端 | 40 片 | 4000 片 | 倍数 |
|---|---|---|---|
| 峰值 RSS | 36.75 MB | **212.4 MB** | 5.8× |
| 冷启动后 RSS | 36.75 MB | **376.5 MB** | 10.2× |
| `search` 阶段 CPU | 1 034 ms | **162 859 ms** | 158× |
| `search` 阶段 CPU 占 wall | 98.6% | 98.3% | 恒定满载 |
| 检索 p50 | 2.137 ms | **336.8 ms** | 158× |
| 灌库 | 79.4 ms | 10 929 ms | 138× |
| 磁盘 | 381 KB | 34.0 MB | 91× |

1024 维的 float32 向量是 4 KB/片，40 片就是 160 KB 的裸向量数据，磁盘也才 0.4 MB。
「JSON 把数据放内存里，所以更费内存更费 CPU」这个判断在这个规模上根本测不出来，
**在 4000 片上直接被量成 5.8–10 倍内存和 158 倍延迟。**

**这张表回答了"JSON 存内存里是不是更费内存更费 CPU"——是，而且差距是结构性的：**

- **内存**：向量活在 Python 进程里，片数涨 100 倍，RSS 涨 5.8–10 倍。
  注意冷启动（376.5 MB）比灌库结束（212.4 MB）还高——**反序列化要同时持有
  解析中间的字符串和最终的 list**，峰值出现在那一刻。
- **CPU**：`search` 全程 98.3% 占 wall，说明进程一直在算，没有等待。
  检索延迟与 CPU 都是 **158×，与片数的 100× 同阶且略超**——
  略超是因为比较次数随候选集增长，排序与点积都是 O(N)。
- **这个增长是数据结构决定的，不是配置能救的**：JSON 没有索引，
  每次查询都是全量线性扫描；片数翻倍，单次查询成本就翻倍。

**pg 侧要验证的三个命题**（`store-pg-scale.json` 待 VM 补跑，命令见 12 节）：
pg 客户端 RSS 应**基本不随片数涨**（比 `cold_open` 的 `rss_delta`；向量在服务端进程，客户端不持有）；
pg 的 `search` CPU 应**远低于** JSON（比 `cpu_ms`，客户端不算、服务端算）；
pg 的 `cpu_pct_of_wall` 应**明显低于 100%**（差出来的是网络往返等待）。
**第三条是决策依据**：JSON 没有等待可摊薄——它慢，慢在自己的 CPU 上，
加并行也救不了（`search()` 是单线程的）；pg 的等待是网络往返，
连接池 + 并发能摊薄。**这是"该选哪个后端"比 p50 更有用的判据。**

**一个必须先说清的口径**：这两个后端比的不是同一件事。
JSON 把向量放在查询进程里，pg 把向量放在服务端进程里。
所以"谁更省内存"这个问题本身要拆成两问——**客户端侧谁更省**（JSON 的账全在自己头上，
pg 的账在服务端），以及**总账谁更省**（要把 pg 服务端的 shared_buffers
与后台进程算进来，那需要 `pg_stat_io`，客户端测不到）。

### 3.2 HNSW 与顺序扫描（pg 内部，同一份数据）

| | p50 | p95 | max |
|---|---|---|---|
| HNSW（索引开） | 0.757 ms | 1.222 ms | 10.569 ms |
| 顺序扫描（`use_index=False`） | 1.091 ms | 1.681 ms | 3.505 ms |

只快 **1.44×**，而且 **top-1 一致率 44/44，连 top-5 都 44/44 完全相同**。
两点读法：

- **HNSW 在这个规模上是精确的**，近似索引通常要担心的召回损失没有出现——这是好消息，
  说明 `m=16 / ef_construction=64` 的默认值在小表上绰绰有余。
- **它的收益被固定开销吞掉了**。索引本身值 784 KB，换来的 p50 差是 0.33 ms。
  在生产量级（百万向量）这个比值会反过来，但现在这套语料上，索引的性价比是负的。
  这不是"HNSW 没用"，而是**这套基准的语料还没大到能体现它的用处**。

### 3.3 一条测量口径

`store-pg.json` 里 pg 四个阶段的 `read_bytes` / `write_bytes` 全为 0。
不是没读写，而是**进程 I/O 计数器只统计本地磁盘，不含 socket**——
pg 的磁盘 I/O 发生在服务端进程里，客户端测不到。要量它得用
`pg_stat_statements` 或 `pg_stat_io`，或者看服务端的 `/proc/<pid>/io`。

---

## 4. 七层：结果与三个发现

**运行时护栏（guard.py）44/44 通过**，逐层都有正例与反例，断言的是"停在哪一层"和"理由是什么"：

| 层 | 用例 | 覆盖 |
|---|---|---|
| L1 | 5 | 空串、纯空白、纯注释、`;` 拼两条语句；带尾分号的单语句放行 |
| L2 | 10 | DROP/UPDATE 前缀被拒；四种行锁子句被拒；WITH / TABLE / VALUES / EXPLAIN 放行 |
| L3 | 6 | 藏在 CTE 里的写操作、`SELECT … INTO`；注释与字符串字面量里的关键词**不算** |
| L4 | 8 | pg_sleep / pg_read_file / nextval / pg_terminate_backend / set_config / pg_advisory_lock 全拒；无害的 `pg_size_pretty` 放行 |
| L5 | 5 | 白名单内放行；白名单外、未限定名、JOIN 夹带的第二张表全拒；不配白名单即该层关闭 |
| L6 | 5 | 无 LIMIT 补 `LIMIT 100`、随 `--max-rows` 变、已有 LIMIT 不动、字面量里的 `'limit 3'` 不算、小写 limit 识别 |
| L7 | 5 | 三条会话加固语句（超时值透传）、allow/block 的审计字段、每次工具调用落 `agent_trace.jsonl`、工具与 guard 共用同一道门 |

**静态检查（checks.py）18/18 通过**：近名表/近名列、可空外键与可空键、浮点金额、
文本时间戳、无时区时间戳、集合表的可空键、无注释表、无列注释、时间命名不一致——
每项都有"该报"和"不该报"两个用例，另有两条专门证明 check 5 的库侧那一半
不能从静态 DDL 里被伪造出来。**18 条里的库侧那条（C5-2）这次真的连上了库**（`--db`），
上一轮它是 SKIP。

### 4.1 发现一：被拦下的写操作，审计日志里记成了 `ok`（已修）

L7-4 / L7-5 同时失败：`Toolbox.call()` 把 guard 的裁决留在了 handler 返回的 dict 里
（`blocked` / `layer`），没有回填到 `ToolResult`。`agent_trace.jsonl` 里一次被拦下的
`DROP TABLE` 记为 `decision: "ok"`、`layer: ""`——这份日志存在的唯一目的就是记录
"哪一层拦住了什么"，字段却是空的。同时 `brief()` 打的是 `not run: <reason>` 而不是
`BLOCKED at L2: <reason>`，模型看到的措辞比实际发生的更模糊。

修复：`call()` 在 handler 返回 `blocked` 时回填 `decision` 与 `layer`，
并加进 `run_tool_selftest()` 第 7 例。**这个 bug 不在任何单测的覆盖面上，
因为单测直接调 `guard()`，线上路径走 `Toolbox.call()`。**

### 4.2 发现二：分两条命令跑，第二次覆盖了第一次（已修）

VM 上暴露的：`--suite guard` 不需要库，`--suite checks --db` 需要，所以天生是两条命令，
而两条命令共用默认输出 `bench/layers.json`。第二条把第一条覆盖了——
终端上 `44/44 cases passed` 打得清清楚楚，留下的文件里却只有 18 条 checks，
`suites.guard` 是空数组，报告也就照实写"18/18 passed"，表格在 check H 之后停住。
**没有任何地方报错。**

修复：`merge_into_existing()` 在写入时合并——跑了的套件替换、没跑的保留、总数按合并后重算；
缺哪个套件命令会打 `note: no guard rows …`；报告缺套件时显式写"**未包含**"而不是少打一段。
重跑后 `bench/layers.json` 是 44 + 18 = 62 条，报告两节齐全。

这条与发现一是同一类：**产物与终端不一致**。两轮测试各抓到一个，说明这类缺陷
不能靠"跑的时候看着没问题"发现，只能靠**校验留下的产物本身**。

### 4.3 发现三：`current_setting` 被误杀（保留，但要知道）

L4-7：`SELECT current_setting('search_path')` 是只读的，却和 `set_config` 一起上了黑名单。
这是保守设计的副作用，属于**有意留下的假阳性**：宁可让"读会话参数"这类只读请求被拒，
也不放行一个能改会话状态的家族。用例里标了 `known_false_positive`，
要改就必须连带改理由文案，别只删一个词。报告里也会原样列出这条，不藏。

---

## 5. 这轮测试自身的局限

- **量尺只有 44 题、14 篇**：hit@5 在 256/512 两档已经完全饱和，被一条词形题锁在 97.7%。
  "IDF 与 overlap 无差异"的适用范围就是这个小语料。
- **fp16/量化、rerank、混合检索都没测**：这些是提升召回的下一个手段，
  但它们的效果要在大语料上才看得出来。
- **pgvector 只测到 40 行**：HNSW 的收益与索引代价比在这个规模上不能外推，
  需要 10⁴–10⁶ 量级才能测出索引的真实价值。
  **3.1 节已把 JSON 一侧放大到 4000 片**（那是本机就能做的），
  pg 一侧的对应数据待 VM 补跑——所以"JSON 与 pg 的资源差多少"目前只有半边是实测的。
- **3.1 节的规模语料是模板生成的**：2000 篇 / 4000 片 / 34 MB，片数是硬指标、
  内容是重复的。它测的是资源占用，**不是检索质量**，别拿它算命中率。
  要真正的万级语料得用自备文档，量级估计是 10 万片 → 400 MB 向量。
- **API 嵌入只跑了 1 种配置**（1024 维 / chunk 512 / overlap 64）：
  每行配置一次全量嵌入、约 20 秒，成本按配置数线性涨。
  所以"API 100%"这个数不能连带断言"API 在任何切片下都 100%"。
- **单机、单次、每题 3–10 轮**：够看量级，不够做显著性判断；
  差异小于 1 个百分点的一律按噪声处理。
- **进程 I/O 计数器不含 socket**（见 3.3）：API 嵌入与 pg 的 I/O 都不能从 `read_bytes` 解释。
- **本机 8 核 Windows 与 VM 2 核 Linux 不能混比**：只比同一台机器上两个后端的比值。
  JSON 的检索是单线程的，核多对它没用，所以本机的 JSON 数字不会比 VM 好看多少。
  `psutil: null` 也是同一回事：采样走 `bench_util.py` 里的原生 API
  （Linux 读 `/proc`，Windows 走 psapi），功能等价但没有跨平台可比性。
- **首次运行含冷页缓存**：要更干净的数就同一命令连跑两次，取第二次。
- **同机器上别开别的重活**：本套基准的差异量级到毫秒，后台编译或大文件同步会直接污染结果。

---

## 6. 下一步

1. **把 API 嵌入的切片网格补上**（chunk 256/512/1024 × overlap 0/64），
   看 100% 是稳定的还是恰好落在某个配置上——这是选型时最该知道的一条。
2. **语料涨到 100 片以上再重跑 `--tune`**：现在 18 种配置里多组打平，
   量尺还没到能分辨细粒度差异的规模。
3. **决定 `PgStore.replace()` 是否改批量写入**：逐行 INSERT 在 40 片时是 204 ms，
   按片数线性涨；`COPY` 或 `executemany` 能把它压下来，代价是换掉当前的错误粒度。
4. **给检索加一层嵌入缓存**：API 路径的 666 ms p50 主要是网络，
   查询侧缓存或本地小模型能把这条降一个数量级。
5. **pgvector 换大语料重测**：只在万级以上向量上，HNSW 与顺序扫描的比值才有意义。

---

## 7. 怎么跑：完整顺序

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

在 VM 上跑的话前面还有三步：`git pull` → `source .venv/bin/activate` →
`pip install -r requirements.txt`，再用 `python3 -c "import psycopg; print(psycopg.__version__)"`
确认依赖装进了这一个环境。`ModuleNotFoundError: psycopg` 说明 venv 没激活，先 `which python3` 看自己在哪。

跑完把结果带回来：`bench/` 不在库里（已 gitignore），要留证据就提交
`docs/bench-report.md`，`git diff --stat docs/bench-report.md` 应该只有报告被重写。
如果 VM 上的数字与本机差得多（CPU 占 wall 比、p50），那是机器差异，
在结论里写清在哪台机器上量的，别当成回归。

## 8. 环境与两种连接身份

离线路径零依赖：Python 3.10+，不需要 pip install、不需要 key、不需要数据库。
live 路径只有一个第三方依赖 psycopg，数据库侧两条幂等脚本：

```bash
sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
sudo -u postgres psql -d fence_demo -f scripts/setup_rag.sql   # doc_chunks + HNSW + role agent_ro
```

**两种连接身份，用途不同，不要混用：**

| 连接 | 权限 | 用在哪 |
|---|---|---|
| `postgresql://postgres:<pw>@localhost:5432/fence_demo` | 读写 | 存储基准（`ensure_schema()` 要建表、灌库要 INSERT）、`test_layers --db`（要读 `shop` 表的 `pg_stats`） |
| `postgresql://agent_ro:ro_only@localhost:5432/fence_demo` | 只读 | L7a 的物理兜底验证 |

用 `agent_ro` 跑存储基准会在建表那一步就 `permission denied`——它被授予的只有
`doc_chunks` 的 SELECT。后面的命令统一用变量，避免抄错（`pgvec123` 是
`scripts/setup_pg.sh` 的默认口令）：

```bash
export DSN_RW='postgresql://postgres:pgvec123@localhost:5432/fence_demo'
export DSN_RO='postgresql://agent_ro:ro_only@localhost:5432/fence_demo'
psql "$DSN_RW" -c "SELECT 1"
```

只读身份写不进去，这一条要用命令看，不要假设：

```bash
psql "$DSN_RO" -c "DELETE FROM shop.orders WHERE 1=1"
# expect: ERROR:  permission denied for table orders
```

看到 ERROR 才是对的，这是 L7a 的物理兜底。

**API 嵌入（需要 key）**：

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

## 9. 灌库语义与 `--rebuild`

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

## 10. 向量化基准

```bash
# 默认网格：chunk 256/512/1024 × overlap 0/64/128 × idf on/off = 18 种配置
python scripts/bench_embedding.py

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

输出：`bench/embedding-<mode>.json`，stdout 上同时打印 Markdown 表。指标口径：`hit@k` 用题库的
多来源标注（`甲 / 乙` 命中任一即算），`MRR@k` / `mean rank@k` 用来分辨"命中率饱和但排序变了"
的情况；`ms/piece`、`pieces/s` 是嵌入吞吐；`store`、`B/piece` 是落盘体积；
`q p50/p95` 是"嵌入查询 + 检索"的端到端延迟。

跑的时候关注三件事：

1. 网格与维度两批复现出本机那组数（chunk 256/512 = 97.7%，1024 = 95.5%；维度越低命中越低）；
2. API 那批的 `hit@5` 比离线高多少——这是词法 vs 语义的量化差；
3. 那道同义题（"…占一半…" vs 语料里的 "50%"）在 API 那批里是否翻盘。离线 18 种配置它全败。

## 11. 存储对比

分两个进程各跑一次——同一进程里先后跑两种后端，峰值 RSS 不可比。

```bash
# JSON：全量向量常驻内存，检索是全表扫描
python scripts/bench_store.py --backend json --repeat 10

# pgvector：需要库与可写 DSN；HNSW 与顺序扫描都会被测到
python scripts/bench_store.py --backend pg --repeat 10 --db "$DSN_RW"

# 想看没有索引的代价（建表时不建 HNSW）
python scripts/bench_store.py --backend pg --db "$DSN_RW" --no-index
```

跑完之后，把两次结果放在一起对比（脚本会报 top-1 一致率与 overlap@k）：

```bash
python scripts/bench_store.py --backend pg --db "$DSN_RW" \
    --reference bench/store-json.json
```

- **关注三件事**：灌库代价（pgvector 在 autocommit 连接上逐行 INSERT，对比 JSON 写一个文件）、
  `search` 与 `search_seqscan` 的 p50 比值（HNSW 值不值的答案）、
  top-1 一致率（HNSW 是近似索引，低于 100% 要能说出差在哪几题）。
- 输出：`bench/store-<backend>.json`。每个阶段（`ingest_total` / `cold_open` / `search` / `search_seqscan`）都记录 wall、CPU、
  CPU 占 wall 比、RSS 增量与峰值、进程读写字节数。
- **注意**：pg 这一路会**按 source 重灌 `doc_chunks`**（见第 9 节），跑完库里就是
  `chunk 512 / overlap 64` 那一版。要恢复成你自己 ingest 的版本：
  `python agent_cli.py --ingest examples/knowledge-dba --db "$DSN_RW" --rebuild`。

## 12. 规模对比：4000 片上 CPU 与内存差多少

原来的存储对比只跑了 40 片，两个后端差几 MB RSS、几百毫秒 CPU，和同机器两次运行的噪声
同量级，答不了"换后端 CPU 与内存差多少"。**答案不在参数里，在输入规模里**，所以先造语料。
数据见 3.1 节；这一节是怎么跑。

### 12.1 造语料

```bash
python3 scripts/make_scale_corpus.py --notes 2000 --out examples/knowledge-scale
```

- **应该看到**：`2000 notes · 3.50 MB`，`topics cycled: 24`。
- **口径**：2000 篇 @chunk=512/overlap=64 → **4000 片**，磁盘 34.0 MB。
  要更大的量就加 `--notes`，片数线性涨（约 2 片/篇）。
- **注意**：这是**资源基准**语料，由模板生成、刻意重复。
  **不要拿它评检索质量**——重复措辞会把命中率抬高，那没有意义。
  语料性质与合规说明见 [`synthetic-corpus.md`](synthetic-corpus.md)。
  `examples/knowledge-scale/` 已在 `.gitignore` 里，入库的是生成脚本。

### 12.2 跑两个后端（必须分进程）

```bash
# JSON：本机就能跑，不需要数据库
python3 scripts/bench_store.py --backend json --repeat 10 \
    --corpus examples/knowledge-scale --out bench/store-json-scale.json

# pgvector：需要库
python3 scripts/bench_store.py --backend pg --repeat 10 \
    --db "$DSN_RW" --corpus examples/knowledge-scale \
    --reference bench/store-json-scale.json --out bench/store-pg-scale.json
```

- **为什么要分进程**：RSS 是进程级的。`--backend both` 会让 pg 继承 JSON 已经
  涨上去的堆，两个数都变得没有意义。**这条在 40 片上只是"最好这样"，
  在 4000 片上是不这么做就白测**——JSON 会把堆抬到 376 MB，
  pg 的读数就再也分不清哪些是它自己的。
- **`--reference` 的作用**：让它读 JSON 那份产物，算两个后端的 top-1 一致率。
  省略就没有这一节。
- **耗时**：JSON 在 8 核本机约 3 分钟（其中 search 阶段 166 秒，
  因为 4000 片 × 440 次查询）。VM 只有 2 核，**预计 10–20 分钟**，后台跑，别在前台等。
- **不对时怎么办**：`permission denied for table doc_chunks` 是用了 `agent_ro`，换 `DSN_RW`；
  跑完库里语料被换了是正常的（pg 这一路按 source 重灌），恢复命令见 11 节末尾。

两边的原始数据（JSON 侧，本机 8 核已实测）：

| 阶段 | wall | CPU | CPU%wall | RSS 峰值 |
|---|---|---|---|---|
| ingest | 10.9 s | 9.4 s | 85.8% | 212.4 MB |
| cold_open | 1.26 s | 1.25 s | 99.2% | 376.5 MB |
| search | 165.6 s | 162.9 s | 98.3% | 372.6 MB |

### 12.3 看什么

每个阶段一行：wall、CPU 时间、CPU 占 wall 比、RSS 增量与峰值、进程读写字节。逐项读法：

| 指标 | 它在回答什么 | 怎么判读 |
|---|---|---|
| `rss_peak_mb` | 进程真实占用 | JSON 应随片数涨；pg 客户端侧应基本不涨 |
| `cpu_ms` | 实际烧掉的计算 | JSON 的 search 应随片数线性涨 |
| `cpu_pct_of_wall` | 在算还是在等 | JSON 接近 100%（在算）；pg 低得多（在等网络） |
| `read_bytes` | 本地磁盘读 | pg 恒为 0，见 3.3 节 |

**`cpu_pct_of_wall` 是这份对比里信息量最大的一个数。** CPU 时间高不代表效率低——
要结合 wall 看：JSON 是"CPU 时间 ≈ wall 时间"，说明进程一直在算；
pg 是"wall 远大于 CPU"，差出来的部分全在等。

## 13. 七层检查与七层护栏

```bash
# 只跑其中一套。两条命令的 --out 默认是同一个文件，
# 分两次跑时必须都指向它，第二次会与第一次合并
python scripts/test_layers.py --suite guard --out bench/layers.json
python scripts/test_layers.py --suite checks --out bench/layers.json

# 带上数据库，把 check 5（结果合理性，读 pg_stats）也跑掉
python scripts/test_layers.py --suite checks --db "$DSN_RW" --out bench/layers.json

# 打印库侧那一层需要的 SQL 与 psql 验证命令
python scripts/test_layers.py --show-env
```

- **应该看到**：逐条 `PASS`。只跑 checks 那条是 17/17（1 条 SKIP，因为 check 5 的另一半
  没接库）+ 一句 `note: no guard rows in this file`——**这句是提醒，不是错误**：
  它说的是"这次没跑 guard，文件里那份是上一次的"。两条都跑完 `bench/layers.json` 是 44 + 18。
- 文件里缺哪个套件，报告会明确写出"未包含"，不会让一个只跑了 checks 的文件看起来像
  "护栏也过了"。stdout 上逐条 `PASS / FAIL`，失败时打印"期望 / 实际"。
- 用例断言的是三件事，不只是"拒绝了没有"：**停在哪一层**（`L2` 还是 `L3`）、
  **理由里有没有关键词**、**放行时 SQL 被改成什么样**（补的 LIMIT 是不是 100）。
- 退出码：任一用例失败即 1，便于接进 CI。

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

`agent_trace.jsonl` 里被拦下的写操作应该显示 `decision=blocked` 和具体层号。
如果这里全是 `ok` 加空 layer，说明回填又断了——这正是 4.1 节那个 bug。

## 14. 汇总成报告

```bash
python scripts/bench_report.py                       # → docs/bench-report.md
# 输入目录不在默认位置时：--bench <dir> --out <file>
```

报告是生成的，重跑即覆盖；**不要往报告里写结论**。没跑过的部分（比如没有 key 时的
API 批次）不会消失，会渲染成**产生它的那条命令**，这样"测过"与"没测"分得清。
结论写在这一份 `docs/bench.md` 里，脚本不碰；数字补进 3.1 节那种表里，
报告则重跑生成。

---

## 附 A：手动验证项（本机无法覆盖的）

| 编号 | 要验什么                            | 怎么验                                                                        |
| -- | ------------------------------- | -------------------------------------------------------------------------- |
| M1 | pgvector 读写与 HNSW               | 11 节，看索引与顺序扫描的比值、top-1 一致率（数据见 3.2）                                        |
| M2 | 四个工具真执行                         | `demo.py --db "$DSN_RW"`，看 `get_table_stats`/`explain_sql`/`run_sql` 的实际返回 |
| M3 | `search_path` 补 public 在真库的名称解析 | `--ask "统计订单数" --db "$DSN_RW" --search-path shop`，`doc_chunks` 应仍能解析       |
| M4 | L7 只读会话对"写形态 SELECT"的兜底         | 13 节那两条 psql                                                                |
| M5 | API 嵌入与 0.45 地板校准               | 10 节的 API 批次；看 `--eval` 打印的 top-1 分数落在什么区间                                    |
| M6 | API LLM 的多轮 function calling    | `--ask "..." --llm openai --db "$DSN_RW"`，看 trace 里工具选择是否合理                |
| M7 | `--tune`/`--report` 在大语料上的区分度   | 换自备语料（≥100 片）重跑；离线小语料 spread 为 0（见 6 节第 2 条）                              |

## 附 B：常见问题

| 现象                                       | 原因                                         | 处置                                             |
| ---------------------------------------- | ------------------------------------------ | ---------------------------------------------- |
| `permission denied for table doc_chunks` | 用了 `agent_ro` 跑写入路径                        | 换 `DSN_RW`                                     |
| `psycopg` 找不到                            | venv 没激活                                   | `source .venv/bin/activate`                    |
| ingest 打出 `orphan rows`                  | 换过语料目录，旧笔记还在表里                             | 只放一套语料就加 `--rebuild`；有意共存则忽略（见 9 节）            |
| `--dim` 改了之后 INSERT 报维度不符                | 表已按旧维度建好，`CREATE TABLE IF NOT EXISTS` 不会改它 | `DROP TABLE doc_chunks` 后重灌，或 `--rebuild` 前先改表 |
| 报告里某节只有命令没有数字                            | 那一批没跑                                      | 按 10、11 节把缺的批次跑掉再生成                            |
| 数字与上一次差很多                                | 机器不同，或后台有别的重活                              | 本套基准的差异量级到毫秒，跑对比时别开编译、别同步大文件                   |
