> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 锁阻塞：一个没提交的长事务，把整张表堵成队列

## 现象：订单写入整片挂起，连接越堆越多（虚构）

14:20，虚构的订单库 `orders_prod`（主机 `pg-node-01`）上，所有涉及 `orders` 表的写入都卡住了，读也开始变慢，应用连接数快速堆积。监控报的是"大量查询 active"。用一句话描述现场：**它们不是在算，是在等**。

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS running_for, left(query, 60) AS query
  FROM pg_stat_activity
 WHERE wait_event_type = 'Lock'
 ORDER BY query_start;
```

结果一大片 `wait_event_type = 'Lock'`。到这一步就可以确定：问题不在 CPU、不在 IO，而在锁排队。

## 先收集什么证据：先画阻塞链，再看锁明细

光看"谁在等"不够，要能回答"谁挡着谁"。最直接的函数是 `pg_blocking_pids()`：

```sql
SELECT pid,
       pg_blocking_pids(pid) AS blockers,
       state, left(query, 60) AS query
  FROM pg_stat_activity
 WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

再看锁对象的明细，确认是表级锁还是别的：

```sql
SELECT l.pid, l.locktype, l.relation::regclass AS rel, l.mode,
       l.granted, l.waitstart, a.state, left(a.query, 60) AS query
  FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
 WHERE l.relation IS NOT NULL
 ORDER BY l.granted, l.waitstart;
```

同时把锁相关的参数与"日志里有没有死锁"一并取到：

```sql
SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('deadlock_timeout','log_lock_waits','lock_timeout','statement_timeout');
```

如果开了 `log_lock_waits`，日志里会记录等待超过 `deadlock_timeout` 的锁，可直接看到阻塞方与被阻塞方，比事后拼视图更快。

## 定位过程：顺着队列往上找，而不是盯着第一个 pid

**第一步：区分"等锁"和"等 IO／CPU"。** `wait_event_type = 'Lock'` 表示在等重量级锁（表级对象）；如果是 `LWLock` 或 `IO`，那是另一条线。本次整片 `Lock`，锁定方向。

**第二步：用 `pg_blocking_pids` 画链。** 这里有个必须理解的细节：这个函数返回的既有 **hard block**（对方正持有与你冲突的锁），也有 **soft block**（对方排在你前面、正等同一把锁）。也就是说，**返回的多个 pid 里，真正拦路的往往不是第一个，而是队列更前面的那个**（`pg_blocking_pids` 函数说明）。看到某会话被三个 pid 挡着就只盯第一个，会导致"杀错了人"。

**第三步：本次的链条。** `pg_blocking_pids` 把现场拼出来后是这样的：

- 一个会话已经跑了近 1 小时、状态是 `idle in transaction`，它早先对 `orders` 加过锁并一直持有——因为文档明确：**锁一旦获取，通常持有到事务结束**（`Concurrency Control`）。
- 一个 `ALTER TABLE orders ADD COLUMN ... DEFAULT ...` 需要 `AccessExclusiveLock`，被上面那个长事务挡住，只能在队列里等。
- 而 `AccessExclusiveLock` 与所有锁模式都冲突，于是排在它后面的所有对该表的普通读写，又都排在这条 DDL 之后——**一条 DDL 就能把整张表变成一个长队列**。

**第四步：逐个排除其他可能。** 不是死锁：日志里没有 `deadlock detected`，`pg_blocking_pids` 画出来的是链不是环。不是连接打满：连接总数还没到 `max_connections`，堆积是结果不是原因。不是 IO：`iostat` 显示设备空闲。三条都排除后，方向只剩"锁队列里的 DDL 与长事务"。

**第五步：该不该杀，怎么杀——这里有个反直觉点。** 第一直觉是"先把那个长事务杀掉"。但要先分清两件事：

- **`pg_cancel_backend`（软取消）发 SIGINT，只取消当前查询，不释放事务级锁。** 如果目标是让 DDL 从队列里退出，取消它就够了，因为 DDL 是一条独立语句。
- **`pg_terminate_backend`（硬杀）发 SIGTERM，终止整个会话、回滚事务、释放锁。** 对那个持有锁的长事务，软取消往往没用：锁在事务级别，取消语句不会放锁，必须终止会话才放。

所以**反直觉的取舍**是：先取消那条 DDL（它是刚发的、可重试、代价小），让业务立刻恢复；长事务则要判断它所属的业务是不是关键作业，再决定是让它提交/回滚，还是终止。反过来"先杀长事务"如果杀错了对象（比如它是某个不能中断的批处理），代价可能比让业务多等几十秒更大。

**第六步：autovacuum 要单独看。** autovacuum 一般不会造成这种杀伤面，但注意文档的一条规则：**为防 XID 回卷而触发的 autovacuum 不会被冲突锁中断**（`Routine Database Maintenance Tasks`），所以它一旦在跑，你对它加锁相关的处置都可能无效，别把精力浪费在它身上。

## 结论与处置：让队列立刻放行，再处理长事务

根因：一个未提交的长事务长期持有 `orders` 表的锁，随后发起的 `ALTER TABLE` 抢占 `AccessExclusiveLock` 被它挡住并排队；由于该锁模式与所有模式冲突，其后所有对该表的读写请求全部排在 DDL 之后，形成大范围阻塞。

处置动作：

- 先取消 DDL，让队列放行：`SELECT pg_cancel_backend(<ddl_pid>);`。
- 定位长事务的归属（`usename`、`application_name`），能提交/回滚就让它自己结束；确认无副作用后再终止：`SELECT pg_terminate_backend(<pid>);`。
- DDL 改进：以后做表结构变更时先设 `SET lock_timeout = '3s';`，抢不到锁就快速失败重试，避免长期占据队首。
- 兜底：给会话设 `idle_in_transaction_session_timeout`，让"开了事务不干活"的连接自动释放锁。

验证指标：`pg_blocking_pids` 对目标会话返回空数组；`pg_locks` 里没有 `granted = false` 的 relation 锁；业务写入恢复且连接数回落。

## 复盘要点

- **大范围卡顿先看 `wait_event_type`**：`Lock` 是排队，`IO`／`LWLock` 是别的问题，方向不同。
- **用 `pg_blocking_pids` 画链，别只盯第一个 pid**。它同时给出 hard block 与 soft block，真正的源头常在队列更前面。
- **一条 `AccessExclusiveLock` 能冻结整张表**。DDL 务必配 `lock_timeout`，抢不到就退让重试，不要排队堵门。
- **软取消不等于放锁**。锁持有到事务结束，对长事务往往要 `pg_terminate_backend` 才会释放；先取消、后终止能少付代价。
- **先问"谁是根因、它能不能结束"再决定杀谁**。杀掉队列里的 DDL 通常最快恢复业务，但要治的是那个长期不提交的事务。
