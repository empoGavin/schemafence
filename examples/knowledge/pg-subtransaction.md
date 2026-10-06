# 子事务、空闲事务与长事务

## 为什么子事务值得单独讲

应用框架在异常处理里习惯性地开 SAVEPOINT（子事务）。每一次 SAVEPOINT 都会在事务日志里分配一个
子事务 ID，即使它最终没有回滚。

后果有三条：

1. **PGPROC 与子事务缓存被耗尽**：单个事务里超过 64 层子事务就会溢出到磁盘，性能断崖式下跌
2. **VACUUM 无法推进**：子事务的 XID 也是 XID，长期未提交的子事务会把 `xmin` 钉死，
   死元组清理不进，膨胀随之而来
3. **日志量放大**：一个只读的 SELECT 被包在 SAVEPOINT 里，也会产生 WAL

典型症状是：`pg_stat_activity` 里看到某个 backend 的 `state = idle in transaction`，
`backend_xmin` 长期不推进，同时表的 `n_dead_tup` 一路涨。

## 怎么定位

```sql
SELECT pid, state, xact_start, now() - xact_start AS age, query
  FROM pg_stat_activity
 WHERE state <> 'idle'
 ORDER BY xact_start
 LIMIT 20;
```

配合 `pg_stat_activity.backend_xmin` 与 `pg_stat_activity.backend_xid`，
找出那个把全局 `xmin` 拖住的会话。

## 怎么防

- 连接池必须配 `idle_in_transaction_session_timeout`（本项目护栏里默认 30s）
- 事务里不要夹带 RPC 调用、HTTP 请求、文件操作——这类"事务里的外部依赖"是长事务的主要来源
- 框架层的"自动 SAVEPOINT"要看清楚再开，ORM 的 `transaction.atomic()` 嵌套就是子事务
- 批处理任务改成"小事务多次提交"，而不是"大事务一次提交"

## 为什么值得单独盯

长事务比慢查询更值得单独盯：慢查询只是慢，长事务会同时造成膨胀、复制延迟和 DDL 阻塞。
