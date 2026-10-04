# 生产库半夜卡死：15 分钟定位流程

## 0–3 分钟：先确认"卡死"是哪一种

- 所有连接都超时 → 可能是连接数打满，或者实例已经不可用
- 部分业务报错、部分正常 → 大概率是锁等待，被某一张表挡住
- 只是变慢 → 并发上来之后的资源竞争，属于性能问题不是故障

## 3–6 分钟：三张视图定方向

```sql
-- 谁在等谁
SELECT pid, wait_event_type, wait_event, state, xact_start, query
  FROM pg_stat_activity WHERE wait_event_type IS NOT NULL;

-- 锁等待树（9.6+ 有 pg_blocking_pids）
SELECT pid, pg_blocking_pids(pid) AS blocked_by, state, query
  FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;

-- 有没有长事务把 xmin 钉住
SELECT pid, backend_xmin, now() - xact_start AS age, query
  FROM pg_stat_activity ORDER BY xact_start LIMIT 10;
```

## 6–10 分钟：判断能不能动手

- 锁等待的源头如果是"空闲事务"（state = idle in transaction），
  杀掉它对业务通常无影响，可以 `pg_terminate_backend`
- 源头如果是正在执行的长查询，先看它在做什么：DDL、VACUUM FULL、还是一个大 UPDATE。
  直接杀可能回滚几十分钟，反而更慢
- 源头是复制槽或者备份进程，先停备份再谈

## 10–15 分钟：止血与记录

1. 止血：`pg_cancel_backend`（软取消）优先于 `pg_terminate_backend`（硬杀）
2. 记录：把 `wait_event`、锁树、SQL 原文、时间点写进事故单
3. 复盘要回答的问题是"为什么这个会话能持有一小时的空闲事务"，
   答案通常在连接池配置或某个把外部调用放进事务的代码里

## 一条经验

夜间故障里，超过一半的根因是同一个：某个定时任务在事务里做了外部调用，
超时重试机制把它变成了一个永远不提交的长事务。加一条
`idle_in_transaction_session_timeout` 就能挡住这一类故障。
