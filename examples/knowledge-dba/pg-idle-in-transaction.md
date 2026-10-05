> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# idle in transaction：事务挂着不动，问题在哪、后果多重

## 状态是怎么来的

`idle in transaction` 的定义很直白：事务已经 BEGIN，当前没有任何语句在跑，
但也没有 COMMIT 或 ROLLBACK。事务还活着，快照还钉着，锁还握着。

四类常见来源：

1. **应用层把非数据库操作放进了事务**：先 BEGIN，然后调第三方接口、
   写文件、等用户输入，事务全程挂着——这是最常见的一类
2. **连接池的事务边界与应用请求边界错配**：请求处理完把连接还给池子，
   但事务没结束；下一个请求拿到这条脏连接，状态继续累积
3. **异常路径漏了回滚**：`try` 里 BEGIN，抛异常后走 `except` 直接返回，
   事务留在原地；这类通常表现为 `idle in transaction (aborted)`
4. **人为操作**：psql 里 BEGIN 之后去开会，或者交互式调试停在那儿

要和 `idle` 区分开：`idle` 表示连接空闲、不在事务里，不钉住 `xmin`，
危害小得多。只有带 `in transaction` 的才需要紧张。

## 后果（按严重度排序）

1. **钉住 xmin，VACUUM 收不动死元组**：任何活跃事务的最老快照都会阻止
   清理比它更旧的死元组，于是表单向膨胀。一个忘记提交的事务挂一晚上，
   足够让一张高频更新表膨胀到需要 VACUUM FULL
2. **锁不释放，把别人挡在门外**：事务持有的行锁、表锁一直不还，
   后续 UPDATE 排队等待，DDL 在锁队列里越积越长
3. **连接被占满**：每个挂起事务占住一个连接，池子慢慢枯竭，
   现象上表现为"连接数打满"，排查时容易误判成 max_connections 不够
4. **间接推高 WAL 与复制槽压力**：长事务叠加失效复制槽，
   会一直压住 WAL 无法回收，磁盘先满

注意第一和第四条是间接效应：`idle in transaction` 本身不写 WAL，
它是通过"让清理和截断都做不了"来制造空间问题的。

## 排查

```sql
SELECT pid, usename, application_name, client_addr, state,
       now() - xact_start     AS xact_age,
       now() - state_change   AS idle_age,
       left(query, 60)        AS last_query
  FROM pg_stat_activity
 WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
 ORDER BY xact_age DESC;
```

`xact_age` 比 `idle_age` 更能说明问题：事务开了多久，才是它钉快照的时长。

谁在等这个事务持有的锁：

```sql
SELECT waiting.pid   AS waiting_pid,
       holding.pid   AS holding_pid,
       now() - holding.xact_start AS holding_age,
       left(holding.query, 40)    AS holding_query
  FROM pg_locks waiting
  JOIN pg_locks holding
    ON holding.locktype = waiting.locktype
   AND holding.granted
   AND NOT waiting.granted
   AND holding.relation IS NOT DISTINCT FROM waiting.relation
  JOIN pg_stat_activity h ON h.pid = holding.pid;
```

## 处置与预防

- **先定位再动手**：杀连接之前确认它是谁的应用、开事务多久了。
  `pg_terminate_backend(pid)` 会连带回滚它的事务，业务要能承受
- **兜底参数**（两层保险）：
  - `idle_in_transaction_session_timeout`：事务空闲超时自动断开，默认 0（关）
  - `statement_timeout`：单条语句超时，防跑飞的查询，但管不住"挂着不动"
- **应用侧才是根**：事务里只放 SQL，不要等外部 IO；连接池开启
  "归还连接时回滚"；异常路径统一走回滚
- **监控**：对 `xact_age` 设阈值告警（例如超过 60 秒），
  比事后查膨胀便宜得多
