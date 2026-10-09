# 评测题库（PG 运维语料包 · 手写部分）

规则：**每一题的"期望来源"必须是一篇真实存在的语料文件**（写文件名，不带 `.md`）。
一题若有相邻的两篇都真正对口，写成 `甲 / 乙` —— 命中任一即算命中，
否则单标注的尺子会把"答对了但用了另一篇"记成失败。

- 语料目录：`examples/knowledge-pg/`（handbook 12 + incidents 14 + runbook 5 = 31 篇）
- 用法：
  ```bash
  python agent_cli.py --corpus examples/knowledge-pg --store .schemafence/pg.json \
                      --ingest examples/knowledge-pg \
                      --eval --eval-file eval/questions-pg.md
  ```
- 判定：返回的 top-k 里出现"期望来源"记为命中
- 注意：本包与 `examples/knowledge-dba/` 是**两套语料**，IDF 不同，不要混装到一个 store 里评测

| # | 问题 | 期望来源 |
|---|------|---------|
| 1 | 一个连接对应一个进程还是线程？为什么连接数一多内存就危险？ | pg-process-architecture |
| 2 | work_mem 调大以后机器内存被吃爆了，为什么单个参数会这样？ | pg-process-architecture |
| 3 | 表里的大文本字段存在哪里，为什么读它反而变慢？ | pg-physical-storage |
| 4 | 一张表最大能有多大，超过 1GB 之后文件是怎么组织的？ | pg-physical-storage |
| 5 | 为什么更新频繁的表特别容易膨胀？ | pg-mvcc-visibility |
| 6 | 长事务到底卡住了什么，为什么说它的危害和自身耗时无关？ | pg-mvcc-visibility |
| 7 | WAL 日志增长特别快，一般从哪几个方向找原因？ | pg-wal-internals |
| 8 | wal_level 三个档位分别支持什么，什么时候必须调高？ | pg-wal-internals |
| 9 | checkpoints_req 远多于 checkpoints_timed，说明什么该调？ | pg-checkpoint-and-bgwriter |
| 10 | checkpoint 期间 IO 抖动特别大，怎么缓解？ | pg-checkpoint-and-bgwriter |
| 11 | autovacuum 的触发阈值是怎么算的，大表为什么总是清不动？ | pg-vacuum-autovacuum |
| 12 | VACUUM 和 VACUUM FULL 怎么选，什么情况下必须用后者？ | pg-vacuum-autovacuum |
| 13 | 事务 ID 回卷是怎么回事，收到警告后该按什么顺序处理？ | pg-transaction-id-wraparound |
| 14 | 为什么监控要盯 age(datfrozenxid) 而不是等到报错？ | pg-transaction-id-wraparound / inc-single-xid-wraparound |
| 15 | B-tree 和 GIN 索引分别适合什么场景？ | pg-index-families |
| 16 | BRIN 索引适合什么样的表，为什么它体积特别小？ | pg-index-families |
| 17 | random_page_cost 调低会带来什么连锁影响？ | pg-planner-cost-model |
| 18 | 计划里的 rows 估算和实际差了几个数量级，该怎么查？ | pg-planner-cost-model / pg-statistics-and-extended-stats |
| 19 | 两个列之间有相关性的时候，单列统计为什么会算错行数？ | pg-statistics-and-extended-stats |
| 20 | 统计信息的采样精度怎么调，调高有什么代价？ | pg-statistics-and-extended-stats |
| 21 | 一个 ALTER TABLE 为什么能把整张表的读都堵住？ | pg-lock-modes-and-deadlock / inc-single-lock-blocking |
| 22 | lock_timeout 和 statement_timeout 应该设哪个，冲突吗？ | pg-lock-modes-and-deadlock |
| 23 | 等待事件里 Client:ClientRead 占比很高，能说明数据库慢吗？ | pg-wait-events-reference |
| 24 | 怎么用等待事件把瓶颈快速分层？ | pg-wait-events-reference / rb-perf-database-wide |
| 25 | 数据库起不来，日志最后一行报 permission denied，怎么查？ | inc-single-startup-failure |
| 26 | 实例崩溃后自动恢复特别慢，怎么判断它是在重放还是在等？ | inc-single-crash-recovery |
| 27 | 数据盘满了，第一件不能做的事是什么？ | inc-single-disk-full |
| 28 | 连接数被打满，怎么区分是空闲连接还是有慢查询占着？ | inc-single-connection-exhausted |
| 29 | 业务大面积卡住、连接越来越多，怎么找到是谁挡着谁？ | inc-single-lock-blocking |
| 30 | CPU 一直跑满，应该从哪一层开始拆？ | inc-single-cpu-saturation |
| 31 | 数据库变慢但 CPU 内存都不高，怎么确认是不是存储的问题？ | inc-single-io-hang |
| 32 | 收到事务 ID 回卷告警，为什么不能直接 VACUUM FULL？ | inc-single-xid-wraparound |
| 33 | 备库延迟一直往上走，怎么分清是发不出去还是回放不动？ | inc-repl-lag-high |
| 34 | pg_wal 目录暴涨把磁盘撑满，怎么确认是复制槽造成的？ | inc-repl-slot-retains-wal |
| 35 | 备库上的查询总被取消，报 conflict with recovery，怎么取舍？ | inc-repl-standby-conflict |
| 36 | 复制断了以后，什么情况下必须重做基础备份？ | inc-repl-broken-reinit |
| 37 | 备库掉线之后主库也写不进去了，是为什么？ | inc-repl-sync-hang |
| 38 | 切换之后发现两个库都在接受写入，怎么办？ | inc-repl-split-brain |
| 39 | 故障排查时应该先收集哪些证据，OS 日志和数据库日志各看什么？ | rb-evidence-collection |
| 40 | 数据库连不上的完整排查流程是什么？ | rb-fault-single-instance |
| 41 | 复制出问题应该按什么顺序排查？ | rb-fault-replication |
| 42 | 整库变慢，从操作系统到数据库的排查顺序是什么？ | rb-perf-database-wide |
| 43 | 一条 SQL 慢，怎么定位瓶颈卡在第几个节点？ | rb-perf-sql-and-plan |
| 44 | 执行计划里 join 方式选错了，原因一般有哪些？ | rb-perf-sql-and-plan / pg-planner-cost-model |
| 45 | SQL 优化改完之后，怎么证明真的变好了？ | rb-perf-sql-and-plan |
| 46 | 整库诊断时，操作系统那一层具体要看哪些指标？ | rb-perf-database-wide |
| 47 | 表顺序扫描被当成缺索引，结果加了索引还是不走，为什么？ | pg-statistics-and-extended-stats / pg-index-families |
| 48 | 数据库整体写入变慢，是不是 checkpoint 太频繁了？ | pg-checkpoint-and-bgwriter |
| 49 | 备库上的长查询会把主库的清理拖住吗？ | inc-repl-standby-conflict / pg-mvcc-visibility |
| 50 | 排查完一个故障，要留下哪些东西才算收口？ | rb-evidence-collection |

## 说明

这 50 题是**新语料包自带的量尺**，用来验证语料能不能被检索到，不是最终题库。
换成你自己真实被问过的问题后，命中率才有说服力。

命题时有意混入了两种提问方式：**术语式**（问 `datfrozenxid`、`random_page_cost`）
与**口语式**（问"业务卡住了怎么办"）。离线的哈希词袋嵌入对写法差异很敏感，
两种问法都能命中，才说明语料里的术语与口语表述都写到了。
