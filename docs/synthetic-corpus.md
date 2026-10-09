# 语料合规指南：带不出来的经验，怎么变成能用的语料

## 问题

企业里做 RAG，第一个卡住的往往不是技术问题，而是语料从哪来。

如果笔记、工单、内部规范文档不允许带出公司（尤其是还在职的情况），
那么"把你的资料灌进去"这条最自然的路径直接断掉。
另外注意：**离职之后的保密义务通常仍然存在**（常见 2–5 年），
所以"我记得某个集群当时是怎么配的"这类细节也不应该写进语料。

判断标准只有一条：

> **这段话如果公开发布（写成博客、讲给同行听），会不会暴露公司信息？**
> 会的，就不要进语料库。

## 三类可用素材（按风险从低到高）

| 类型 | 内容 | 风险 | 说明 |
|---|---|---|---|
| 公域知识整理 | PostgreSQL 官方文档、社区博客、开源项目文档，用自己的话整理成方法论笔记 | 零 | 最安全，量也够 |
| 通用经验（脱环境） | 只保留方法论，去掉表名、拓扑、参数值、系统代号、时间点 | 低 | 经验里最值钱的部分，脱掉环境后不违规 |
| 合成案例 | 虚构业务（如"某零售订单系统"）+ 虚构故障复盘 | 零 | 可控性最强，演示效果最好 |

脱环境的写法示例：

- ✗ 不可用：`把集群 X 的 work_mem 从 64MB 调到 256MB 之后不卡了`
- ✓ 可用：`锁等待告警时，先看 pg_stat_activity 的 wait_event，再定位阻塞源，
  最后判断该软取消还是硬杀`

一样的经验，前者是内部信息，后者是行业通用知识。

## 本仓库的合成语料包

本仓库共有五套语料，详细说明见各自的文档：

| 语料包 | 内容 | 文档 |
|---|---|---|
| `examples/knowledge/` | 8 篇脱敏 runbook（最初的包） | README |
| `examples/knowledge-dba/` | 14 篇合成笔记，本篇余下部分详解 | 本文 |
| `examples/knowledge-pg/` | 31 篇 PG 运维包：通识 + 故障复盘 + 诊断流程 | [`pg-corpus.md`](pg-corpus.md) |
| `examples/knowledge-pg-official/` | PG 16 官方手册按章重排（生成物，不入仓库） | [`pg-corpus.md`](pg-corpus.md) |
| `examples/knowledge-pg-internals/` | *PostgreSQL 14 Internals*（Parts I–II，Egor Rogov）按章重排（生成物，不入仓库） | [`pg-corpus.md`](pg-corpus.md) |

另有 `examples/knowledge-merged/` —— 上面五套的**派生合并树**，由
`python scripts/ingest_pgvector.py --single-db` 生成（也需要时才会存在），
用途是灌进**一个** pgvector 库，见 [`pg-corpus.md`](pg-corpus.md) 第五节。
它不是第六套语料，别单独评测。

`examples/knowledge-dba/` 共 14 篇，全部属于上面第一类和第三类：

| 文件 | 主题 |
|---|---|
| pg-index-not-used.md | 索引建了却不走：六种原因 |
| pg-connection-pool.md | 连接池与 max_connections |
| pg-vacuum-tuning.md | autovacuum 调优与膨胀监控 |
| pg-backup-pitr.md | 备份与 PITR |
| pg-replication-failover.md | 流复制与切换演练 |
| pg-slow-query-method.md | 慢查询定位方法论 |
| pg-partitioning.md | 分区表策略与维护 |
| pg-upgrade.md | 大版本升级与回滚 |
| pg-privilege-model.md | 最小权限模型 |
| pg-capacity-planning.md | 容量评估与告警阈值 |
| pg-subtransaction-overflow.md | 子事务溢出：hang 与 inode 耗尽 |
| pg-spinlock-old-snapshot.md | 自旋锁争用：old_snapshot_threshold 烧 CPU |
| pg-bloat-seq-scan.md | 膨胀导致的顺序扫描误诊：像缺索引，其实是死元组 |
| pg-idle-in-transaction.md | idle in transaction：起因、后果与处置 |

- **来源**：公开的 PostgreSQL 通用知识，无任何公司内部信息
- **场景**：「某零售订单系统」「某物流轨迹库」等均为虚构，用于承载案例
- **题库**：`eval/questions-dba.md`（44 题，与语料一一对应）
- **报告**：`eval/report-dba.md`（top-5 命中 97.7%，1 例同义词失效，原因已写明）

> `pg-subtransaction-overflow.md` 是「公开文章 + 脱敏案例」的混合写法示范：
> 机制部分来自公开技术文章（文末列出出处），案例部分只保留现象、定位路径和处置动作，
> 去掉了表名、系统标识和时间点。真实的坑要写进语料，走的就是这条路线：
> 读者能学到方法，但反查不到任何具体环境。
>
> `pg-spinlock-old-snapshot.md` 用同一手法，另外演示了一件事——**记忆里的结论也要用源码核一遍**：
> 现场的印象是"把参数设成 0 就好了"，而官方文档写明 `-1` 才是禁用、`0` 只是"测试用小值"；
> 对照 PG 12 源码后确认 `== 0` 的分支发生在取锁**之后**，所以设 0 根本绕不开自旋锁。
> 核对下来真实的动作是**取消该参数的显式设置、回落到默认的 -1**——
> 两者效果等价，但讲法完全不同。写进语料的那一版要能拿 `pg_settings.source` 验证，
> 否则一句"设 0 解决"经不起一次复现。


用法：

```bash
python agent_cli.py --ingest examples/knowledge-dba \
                    --corpus examples/knowledge-dba \
                    --store .schemafence/dba.json
python agent_cli.py --report eval/report-dba.md \
                    --corpus examples/knowledge-dba \
                    --store .schemafence/dba.json \
                    --eval-file eval/questions-dba.md
```

## 发布前的合规自查清单

语料进仓库或对外演示之前，逐条过一遍：

- [ ] 没有内部文档原文段落（含改写后仍可识别的句子）
- [ ] 没有真实表名、库名、集群名、内部系统代号
- [ ] 没有真实 IP、域名、账号、连接串、密钥
- [ ] 没有内部参数配置的具体数值（可以讲"调大成本限制"，不要讲"调到 2000"）
- [ ] 没有工单号、故障时间点、可反查到具体事件的细节
- [ ] 没有客户/业务数据的真实样例
- [ ] 案例中的业务场景明确标注为虚构
- [ ] 每篇文件顶部有合规模识（本语料包已统一加上）

## 怎么替换成自己的内容

你不需要从零写 10 篇。最小路径是：

1. 选用类别二（脱环境经验），先把 3–5 个你真正踩过的坑写成一页
2. 用 `scripts/convert_notes.sh` 把已有的 docx / pdf / html 转成 md；
   注意脚本只负责格式转换，**脱敏必须由你人工确认**，工具不会替你把关
3. `--ingest` 灌进去，`--genq` 生成题库草稿，改完跑 `--eval`
4. 命中率不达标时，先看失效样本那一节，再决定改语料还是改切片

## 这套流程解决什么问题

企业内 RAG 落地，第一道门槛不是模型，是语料合规。本仓库的语料全部来自公开知识整理
和完全虚构的合成案例，没有一处内部信息；同时留了一套评测题库和评测报告，
语料改动后命中率会变化，这个变化可以被量化。

它换来三条可检验的性质：来源可公开、规模可衡量、改动可回归。
反过来，拿内部文档做语料，风险不在检索效果——是它随时可能变成一次事故。
