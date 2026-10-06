# 规模对比运行手册：JSON vs pgvector 的 CPU 与内存

这份是 [`bench-runbook.md`](bench-runbook.md) 的补充，专测一件事：**换存储后端，
CPU 与内存到底差多少**。原来的存储对比只跑了 40 片，两个后端差几 MB RSS、
几百毫秒 CPU，和同机器两次运行的噪声同量级，答不了这个问题。

答案不在参数里，在输入规模里。所以先造语料，再回答。

---

## 0. 为什么必须放大语料

1024 维的 float32 向量是 4 KB/片。40 片就是 160 KB 的裸向量数据，
JSON 那份存到磁盘也才 0.4 MB。这个规模下：

| | 40 片 | 4000 片 |
|---|---|---|
| JSON 峰值 RSS | 36.75 MB | **212–376 MB** |
| JSON search 阶段 CPU | 1.03 s | **162.9 s** |
| JSON 检索 p50 | 2.137 ms | **336.8 ms** |

所以「JSON 把数据放内存里，所以更费内存、更费 CPU」这个判断，
**在 40 片上测不出来，在 4000 片上直接被测成 5.8 倍内存和 130 倍延迟**。
这不是调参能补的差距，是数据结构决定的。

### 先看清一件事：两个后端比的根本不是同一件事

- **JSON**：向量在 Python 进程的 list 里。每次查询遍历全部向量算点积，
  再排序取 top-k。**O(N) 且全在解释器里跑。**
- **pgvector**：向量在服务端进程的表里。客户端把查询向量发过去，
  由数据库内核扫（有 HNSW 索引时走索引），只把 top-k 传回来。
  **客户端进程自己不持有向量。**

结论直接推论出来：**JSON 的 CPU 与内存都随片数线性涨，pg 的客户端侧几乎不动**——
数据在别人家里。下面就是去验证这句话。

---

## 1. 造语料

```bash
python3 scripts/make_scale_corpus.py --notes 2000 --out examples/knowledge-scale
```

- **应该看到**：`2000 notes · 3.50 MB`，`topics cycled: 24`。
- **口径**：2000 篇 @chunk=512/overlap=64 → **4000 片**，磁盘 34.0 MB。
  要更大的量就加 `--notes`，片数线性涨（约 2 片/篇）。
- **注意**：这是**资源基准**语料，由模板生成、刻意重复。
  **不要拿它评检索质量**——重复措辞会把命中率抬高，那没有意义。
  语料性质与合规说明见 [`synthetic-corpus.md`](synthetic-corpus.md)。

改完语料记得看 `.gitignore`：`examples/knowledge-scale/` 已忽略，生成脚本入库。

---

## 2. 跑两个后端（必须分进程）

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
  因为 4000 片 × 440 次查询）。VM 只有 2 核，**预计 10–20 分钟**，
  后台跑，别在前台等。
- **不对时怎么办**：
  - `permission denied for table doc_chunks` → 用了 `agent_ro`，换 `DSN_RW`
  - 跑完库里语料被换了 → pg 这一路**按 source 重灌**，跑完库里是 scale 那版。
    恢复：`python3 agent_cli.py --ingest examples/knowledge-dba --db "$DSN_RW" --rebuild`

---

## 3. 看什么

每个阶段一行：wall、CPU 时间、CPU 占 wall 比、RSS 增量与峰值、进程读写字节。
逐项读法：

| 指标 | 它在回答什么 | 怎么判读 |
|---|---|---|
| `rss_peak_mb` | 进程真实占用 | JSON 应随片数涨；pg 客户端侧应基本不涨 |
| `cpu_ms` | 实际烧掉的计算 | JSON 的 search 应随片数线性涨 |
| `cpu_pct_of_wall` | 在算还是在等 | JSON 接近 100%（在算）；pg 低得多（在等网络） |
| `read_bytes` | 本地磁盘读 | pg 恒为 0，见下 |

**`cpu_pct_of_wall` 是这份对比里信息量最大的一个数。**
CPU 时间高不代表效率低——要结合 wall 看：JSON 是"CPU 时间 ≈ wall 时间"，
说明进程一直在算；pg 是"wall 远大于 CPU"，差出来的部分全在等。

**一个必须知道的口径**：pg 的 `read_bytes`/`write_bytes` 恒为 0，
不是没读写，而是**进程 I/O 计数器只统计本地磁盘、不含 socket**——
pg 的磁盘 I/O 发生在服务端进程里，客户端测不到。
要量它得用 `pg_stat_statements` / `pg_stat_io`，或看服务端的 `/proc/<pid>/io`。

---

## 4. 预期结果与判据

**JSON 侧（本机 8 核，已实测）**：

| 阶段 | wall | CPU | CPU%wall | RSS 峰值 |
|---|---|---|---|---|
| ingest | 10.9 s | 9.4 s | 85.8% | 212.4 MB |
| cold_open | 1.26 s | 1.25 s | 99.2% | 376.5 MB |
| search | 165.6 s | 162.9 s | 98.3% | 372.6 MB |

**pg 侧预期**：内存与 CPU 都不随片数明显涨（数据在服务端），
但每次查询多一个往返，`cpu_pct_of_wall` 会明显低于 JSON。

**要验证/推翻的三个命题**：

1. **JSON 内存随片数线性涨，pg 不涨** —— 比两边 `cold_open` 的 `rss_delta`。
2. **JSON 的 CPU 比 pg 高** —— 比两边 `search` 的 `cpu_ms`。
3. **pg 的 wall 里有大量等待** —— 比两边 `search` 的 `cpu_pct_of_wall`。

第 3 条是这份对比真正的产出：**它说明 pg 慢在哪、以及为什么扩并发能救它**。
JSON 没有等待，因为查询在进程内自己做；pg 的等待是网络往返，
连接池 + 并发就摊薄了。**这是"该选哪个后端"的真正判据，比 p50 更有决策价值。**

---

## 5. 已知会干扰结论的几点

- **本机是 8 核 Windows，VM 是 2 核 Linux**：绝对秒数不可跨机器比，
  只比**同一台机器上两个后端的比值**。JSON 的检索是单线程的，
  核多对它没用；所以本机的 JSON 数字不会比 VM 好看多少。
- **`psutil: null`**：资源采样走的是 `bench_util.py` 里的原生 API
  （Linux 读 `/proc`，Windows 走 psapi）。功能等价，但**没有跨平台可比性**。
- **首次运行含冷页缓存**：要更干净的数就同一命令连跑两次，取第二次。
- **pg 的索引在 4000 片时才开始有意义**：40 片时 HNSW 只快 1.44×、
  且完全精确（top-5 44/44 一致）。**4000 片是第一次有机会看到它真正的收益**，
  所以这轮的 `search` 与 `search_seqscan` 比值比 40 片那轮有价值得多。

---

## 6. 回填结论

跑完两个后端后，把数字补进 [`bench-findings.md`](bench-findings.md) 的第 3 节，
并按需要更新 `docs/bench-report.md`（重跑 `bench_report.py`）。
报告是脚本生成的、结论是手写的——**重跑报告会覆盖手写结论，跑完记得补回**。
