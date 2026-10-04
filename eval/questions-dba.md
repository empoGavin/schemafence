# 评测题库（合成 DBA 语料包）

规则：**每一题的"期望来源"必须是一篇真实存在的笔记**。改语料的时候同步改这张表。

- 语料目录：`examples/knowledge-dba/`（12 篇合成语料，虚构场景 + 公开 PostgreSQL 知识）
- 用法：`python agent_cli.py --corpus examples/knowledge-dba --store .schemafence/dba.json --ingest examples/knowledge-dba --eval --eval-file eval/questions-dba.md`
- 判定：返回的 top-k 里出现"期望来源"记为命中

| # | 问题 | 期望来源 |
|---|------|---------|
| 1 | 索引明明建了，为什么查询还是走全表扫描？ | pg-index-not-used |
| 2 | 列上用了函数导致索引用不上，应该怎么改写？ | pg-index-not-used |
| 3 | 统计信息过期会让执行计划变差吗？ | pg-index-not-used |
| 4 | max_connections 调大一点是不是更好？ | pg-connection-pool |
| 5 | pgbouncer 的 transaction 池模式有什么限制？ | pg-connection-pool |
| 6 | 连接数被打满，应该从哪里开始排查？ | pg-connection-pool |
| 7 | autovacuum 为什么清不掉死元组？ | pg-vacuum-tuning |
| 8 | 大表的 autovacuum 触发阈值应该怎么调？ | pg-vacuum-tuning |
| 9 | 膨胀到多少才需要上 VACUUM FULL 或者 pg_repack？ | pg-vacuum-tuning |
| 10 | 怎么确认 WAL 归档是正常的？ | pg-backup-pitr |
| 11 | RPO 和 RTO 应该怎么定？ | pg-backup-pitr |
| 12 | 为什么说没演练过的备份等于没有备份？ | pg-backup-pitr |
| 13 | 复制延迟应该看哪个指标才准？ | pg-replication-failover |
| 14 | 主备切换的步骤是什么，为什么必须先隔离旧主？ | pg-replication-failover |
| 15 | 复制槽为什么会把 WAL 撑着不清理？ | pg-replication-failover |
| 16 | 慢查询应该按什么排序，找出最该优化的一条？ | pg-slow-query-method |
| 17 | pg_stat_statements 里的 SQL 复现不出问题怎么办？ | pg-slow-query-method |
| 18 | 什么情况下才值得上分区表？ | pg-partitioning |
| 19 | 分区裁剪失效通常是什么原因？ | pg-partitioning |
| 20 | 大版本升级的停机窗口怎么估？ | pg-upgrade |
| 21 | 升级后发现性能回归，回滚预案要准备什么？ | pg-upgrade |
| 22 | 应用账号的权限应该给到什么程度？ | pg-privilege-model |
| 23 | 新建表就报权限不足，怎么免掉每次手工授权？ | pg-privilege-model |
| 24 | 磁盘还有一半空间，为什么还要提前扩容？ | pg-capacity-planning |
| 25 | 容量告警的阈值应该怎么定？ | pg-capacity-planning |
| 26 | 循环体里写了 EXCEPTION 为什么会造成大量子事务？ | pg-subtransaction-overflow |
| 27 | 子事务为什么不能超过 64 个？ | pg-subtransaction-overflow |
| 28 | 子事务太多会把 inode 用光吗，应该怎么排查？ | pg-subtransaction-overflow |
| 29 | 存储过程里的异常处理应该怎么改才能减少子事务？ | pg-subtransaction-overflow |
| 30 | 哪些框架和工具会偷偷产生子事务？ | pg-subtransaction-overflow |
| 31 | CPU 很高但等待事件看不出异常，应该从哪里查？ | pg-spinlock-old-snapshot |
| 32 | perf top 里看到 s_lock 占比很高说明什么？ | pg-spinlock-old-snapshot |
| 33 | old_snapshot_threshold 设成多少才是真正禁用？ | pg-spinlock-old-snapshot |
| 34 | old_snapshot_threshold 引起的 CPU 问题在哪个版本被修复？ | pg-spinlock-old-snapshot |
| 35 | 为什么升级或者迁移之后容易出现性能故障？ | pg-spinlock-old-snapshot |
| 36 | 数据库 CPU 很高，perf top 显示 spin lock 占 90%，是什么原因？ | pg-spinlock-old-snapshot |
| 37 | 把参数从配置文件里删掉和设成 0，效果一样吗？ | pg-spinlock-old-snapshot |
| 38 | 怎么确认一个参数已经回到默认值，而不是被人改过？ | pg-spinlock-old-snapshot |

## 说明

这 38 题是**合成语料包自带的量尺**，用来验证新语料能不能被检索到，
不是最终题库。换成你自己真实被问过的问题后，命中率才有说服力。
