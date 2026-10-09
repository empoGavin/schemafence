> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 锁与死锁：八种表锁、行锁、advisory lock 与超时取舍

## 先分清两套锁：表级和行级

PostgreSQL 有两套显式锁。表级锁一共八种模式，即便名字里带 "row"（比如 ROW SHARE）它们**也全都是表级锁**，名字是历史遗留，唯一的实质区别是各自和哪些模式冲突。行级锁则真正作用在行上，它不阻止读，只阻止对同一行的写和加锁。

先说表级八种模式，及其主要来源：

- `ACCESS SHARE`：只与 `ACCESS EXCLUSIVE` 冲突。普通 SELECT 就是它。
- `ROW SHARE`：与 `EXCLUSIVE`、`ACCESS EXCLUSIVE` 冲突。`SELECT ... FOR UPDATE / FOR NO KEY UPDATE / FOR SHARE / FOR KEY SHARE` 会拿它。
- `ROW EXCLUSIVE`：与 `SHARE`、`SHARE ROW EXCLUSIVE`、`EXCLUSIVE`、`ACCESS EXCLUSIVE` 冲突。INSERT、UPDATE、DELETE、MERGE 拿它。
- `SHARE UPDATE EXCLUSIVE`：自冲突，另与 `SHARE`、`SHARE ROW EXCLUSIVE`、`EXCLUSIVE`、`ACCESS EXCLUSIVE` 冲突。VACUUM（非 FULL）、ANALYZE、CREATE INDEX CONCURRENTLY、CREATE STATISTICS、REINDEX CONCURRENTLY 拿它。
- `SHARE`：与 `ROW EXCLUSIVE` 及以上多种冲突。CREATE INDEX（非 CONCURRENTLY）拿它。
- `SHARE ROW EXCLUSIVE`：自冲突，另外与 `ROW EXCLUSIVE` 及以上冲突。CREATE TRIGGER 及某些 ALTER TABLE 拿它。
- `EXCLUSIVE`：只允许并发的 `ACCESS SHARE`，也就是只允许读。REFRESH MATERIALIZED VIEW CONCURRENTLY 拿它。
- `ACCESS EXCLUSIVE`：与所有模式冲突，保证持有者独占。DROP TABLE、TRUNCATE、REINDEX、CLUSTER、VACUUM FULL、REFRESH MATERIALIZED VIEW（非 CONCURRENTLY）、以及很多 ALTER TABLE 形式都拿它；LOCK TABLE 不写模式时默认也是它。

一个极其实用的判断：**只有 `ACCESS EXCLUSIVE` 会阻塞不带 FOR UPDATE/SHARE 的 SELECT**。这就是"加个索引把整张表的读都卡住"这类现象的根。

## 行级锁：四种模式与它们的强弱

- `FOR UPDATE`：最强的行锁，阻止其他事务对它 UPDATE、DELETE、加锁。UPDATE 修改"有唯一索引且可用于外键"的列时、以及所有 DELETE，都会拿到它。
- `FOR NO KEY UPDATE`：比 FOR UPDATE 弱，不阻塞 `FOR KEY SHARE`。不涉及键值的普通 UPDATE 拿它。
- `FOR SHARE`：共享行锁，阻止写和 `FOR UPDATE`/`FOR NO KEY UPDATE`，但不阻止 `FOR SHARE`/`FOR KEY SHARE`。
- `FOR KEY SHARE`：最弱，阻止 DELETE 和改键值的 UPDATE，不阻止普通 UPDATE，也不阻止 `FOR NO KEY UPDATE` / `FOR SHARE` / `FOR KEY SHARE`。

行锁在事务结束（或回滚到保存点）时释放。它在内存里不保留"改了哪些行"的记录，因此加锁行数没有上限；但要留意：**加行锁可能引起磁盘写**，例如 `SELECT FOR UPDATE` 会写行上的标记。

## advisory lock：应用自定义的锁

advisory lock 是"由应用自己赋予含义"的锁，系统不校验你怎么用。它适合那些用 MVCC 表达起来别扭的串行化需求（比如模拟悲观锁）。相比在表里放一个标志位，它更快、不产生表膨胀，而且会话结束时会被服务器自动清理。

它有两种粒度：会话级和事务级。**会话级锁一旦获得，会一直持有到显式释放或会话结束，并且不遵守事务语义**——事务回滚了锁还在；事务级锁则像普通锁一样，在事务结束时自动释放，也没有显式解锁操作。二者针对同一个标识符会互相阻塞；而同一个会话重复申请自己已持有的锁总是成功。所有 advisory lock 都能在 `pg_locks` 里查到。

## 死锁：检测、代价与处理

死锁是两个以上事务各持对方想要的锁。PostgreSQL 会自动检测并中止其中一个事务（中止哪个难以预测，不要依赖）。死锁也可能完全由行锁造成，不一定用了显式锁才会发生。

检测不是每次等待都做，因为**检查死锁相对昂贵**：服务器先乐观地等一会儿，超过 `deadlock_timeout`（默认 1 秒）才去查一次。调大它减少无谓检查，但会拖慢真实死锁的报错；调小则相反。

死锁错误和序列化失败的 SQLSTATE 值得记住：死锁是 `40P01`（deadlock_detected），序列化失败是 `40001`（serialization_failure）。**应用应当对这两类错误做重试**；但对唯一键冲突（`23505`）和排他约束冲突（`23P01`）要更谨慎，因为它们也可能是持久的业务错误而非瞬时冲突。

最好的防御是让所有应用按**一致的顺序**去锁多个对象：如果上面示例里两个事务都按同样顺序更新行，就不会死锁。另外，一个对象上第一次拿的锁，应该是它整个事务里需要的最强模式。

## 三个超时参数的取舍

三个超时默认都是 0（不启用），必须按场景显式开启：

- `statement_timeout`：从语句到达服务器算起，超时即中止整条语句。它不是专门的锁超时——任何耗时都会算进去。**不建议写进 postgresql.conf**，因为它会影响所有会话。
- `lock_timeout`：只在等锁时计时，每次申请锁单独计算。同样不建议写进全局配置。还有一个细节：**如果 `statement_timeout` 非零，把 `lock_timeout` 设成大于等于它的值就没意义**，因为语句超时会先触发。
- `idle_in_transaction_session_timeout`：终止"开着事务但空闲"过久的会话，用来防止会话长期占着锁、拖住 vacuum。`idle_session_timeout` 针对的是"没有事务的空闲会话"，那种情况服务器负担小得多，所以更没那么必要。

```sql
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('deadlock_timeout','lock_timeout','statement_timeout',
                'idle_in_transaction_session_timeout','idle_session_timeout');
```

**`lock_timeout` 和 `statement_timeout` 该设哪个？** 两者不冲突，管的也不是同一件事，所以不是二选一而是配合使用：`statement_timeout` 限制**整条语句**的总时长，任何耗时都算进去；`lock_timeout` 只限制**等锁**的那一段，语句真正开始干活之后就不再管它。DDL 与批量维护脚本应该设 `lock_timeout`（抢不到锁就快速失败、稍后重试），因为它们的危险在于**排队**而不在于跑得久；反过来给这类脚本设一个很大的 `statement_timeout` 毫无帮助，它照样会堵在队首把整张表冻住。

## 落到可观察：谁在等谁

`pg_locks` 给出全局锁视图，字段包括 `locktype`、`mode`、`granted`、`relation`、`transactionid`、`virtualxid`、`pid`。要定位阻塞链，用 `pg_blocking_pids()`：

```sql
SELECT pid, pg_blocking_pids(pid) AS blocked_by, state, wait_event_type, wait_event,
       left(query, 60) AS query
  FROM pg_stat_activity
 WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

打开 `log_lock_waits` 后，等待超过 `deadlock_timeout` 的锁会进日志，这是事后复盘"当时是谁挡住了谁"最省事的证据。

## 核心判断：DDL 被长事务挡住，是"锁队列"在放大影响

为什么一个挂着不动的长事务，能让一条简单的 `ALTER TABLE` 卡很久？因为 DDL 要 `ACCESS EXCLUSIVE`，而这个模式与**包括普通 SELECT 的 `ACCESS SHARE` 在内的所有模式都冲突**。长事务不结束，DDL 就一直等。更麻烦的是排队效应：DDL 一旦进入等待队列，后续到达、与它冲突的请求（哪怕是只想 SELECT 的会话）也倾向于排在它后面，于是**一张表的读也会跟着卡住，故障面被放大**。

所以对 DDL 的正确做法是**不要让它无限等**：先设一个短 `lock_timeout`（例如 2–5 秒），拿不到锁就快速失败、稍后重试；而不是让它挂在队列里，把后面的读一起拖下水。
