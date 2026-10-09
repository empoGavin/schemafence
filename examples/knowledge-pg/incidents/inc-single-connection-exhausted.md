> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 连接被占满：先分清是没人放，还是有人在拖

## 现象：应用大面积报"连接被拒"（虚构）

工作日 10:00 前后，虚构的订单库 `orders_prod`（主机 `pg-node-01`）上，多个应用同时报连不上。应用侧日志里的原文是：

```
FATAL:  sorry, too many clients already
FATAL:  remaining connection slots are reserved for non-replication superuser connections
```

值班第一反应是"连接数不够，加到 500 再重启"。在高峰期重启是重罚，而且这次未必对症：连接被占满常常不是"没人用"，而是"很多人拿了不还"或"少数人拿了不放"。先分清是哪一种，再决定要不要动 `max_connections`。

## 先收集什么证据：把连接按状态分类，而不是只数总数

只数一个总数说明不了问题，关键是看这些连接处在什么状态、由谁持有：

```sql
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;

SELECT backend_type, count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC;

SELECT usename, application_name, client_addr, state, count(*)
  FROM pg_stat_activity
 GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT 20;
```

再量一下"距离上限还有多远"，把三个保留区参数一并读出来：

```sql
SELECT count(*) AS used,
       current_setting('max_connections')::int AS max_conn,
       current_setting('superuser_reserved_connections')::int AS su_reserved,
       current_setting('reserved_connections')::int AS reserved
  FROM pg_stat_activity;
```

最后，把"占用时间长的连接"单独列出来——它们是真正的嫌疑人：

```sql
SELECT pid, usename, application_name, state, wait_event_type, wait_event,
       now() - state_change AS idle_for,
       now() - xact_start   AS xact_for,
       left(query, 80) AS query
  FROM pg_stat_activity
 WHERE state <> 'idle' OR now() - state_change > interval '5 min'
 ORDER BY xact_start NULLS LAST LIMIT 30;
```

**连接数被打满时，先分清两种完全不同的成因**：一种是有大量**空闲连接**——`state = 'idle'`、很久没有查询，典型的连接池泄漏、应用拿了连接不还，或者上游并发数配得过大；另一种是少数**慢查询占着连接不放**——`state = 'active'` 且 `query_start` 已经很旧，连接不是不够用，是被拖住了。前者要治连接池，后者要治那条 SQL 或那个长事务，**两者的处置方向完全相反**，分错了就会在错误的地方加机器。

## 定位过程：先分类，再决定"腾"还是"扩"

**第一刀：看 `state` 分布。** 这一步直接决定后面所有动作。

- 若大量是 `idle`／`idle in transaction`：连接有主，但没人干活的占着。这是"没人放"。
- 若大量是 `active` 且 `wait_event_type` 指向 `Lock` 或 `IO`：连接是被慢查询/阻塞拖住的。这是"有人在拖"。

本次是两者叠加、但以前者为主：`idle` 与 `idle in transaction` 占了大头，且不少会话的 `idle_for` 已经几十分钟；同时有一个慢报表把剩下的连接拖在 `active`。

**三个保留区参数的真实含义**（`Server Configuration`）：

- `max_connections`：能同时存在的连接总数上限。
- `superuser_reserved_connections`：默认 3，只留给超级用户，是"最后的应急位"。
- `reserved_connections`：默认 0，留给拥有 `pg_use_reserved_connections` 角色的连接。

分配逻辑是：空闲位还多于 `superuser_reserved_connections` 时，谁都可能连上；一旦空闲位落到 `superuser_reserved_connections + reserved_connections` 以内，**只有超级用户和被授予 `pg_use_reserved_connections` 的角色**还能连；再降到只剩 `superuser_reserved_connections` 以内，就只有超级用户。日志里出现 "reserved for non-replication superuser connections" 这句，说明已经被吃到只剩超级用户位了。

**注意那条消息里的 "non-replication"**：保留区规则管的是普通连接，复制连接（walsender）由 `max_wal_senders` 单独约束、不在这套预留逻辑里。所以"谁吃掉了预留位"要从**普通连接**这边找：重点查 `pg_use_reserved_connections` 这个角色的成员——大概率是有人在某次救火时把它授予了业务角色，事后忘了收回。

**第二刀：判断"该不该扩 `max_connections`"。** 这里有一个反直觉结论：**连接打满时，最先该做的通常不是调大 `max_connections`。** 因为每个 backend 都是独立进程，各自持有 `work_mem` 级别的私有内存和栈；连接数上去后，内存占用、上下文切换、锁竞争都会同步上去，而且改 `max_connections` 只能在服务启动时生效——意味着要重启，高峰期代价很大。它还要求 `max_locks_per_transaction` 之类的共享内存结构跟着调整（`High Availability` 章节列出的相关参数），不是改一个数那么简单。

**第三刀：连接池与 `max_connections` 是什么关系。** `max_connections` 是服务端总闸，连接池是客户端侧的复用层。如果每个应用实例都直连、各自开几十个连接，20 个实例就能把 100 个名额吃满。这时把闸门从 100 抬到 500，只是让"池子失效"这个病更晚发作。更有效的方向是连接池——尤其是事务模式（transaction pooling），它让一个物理连接在不同事务间被复用，从根上把"持有连接数"降下来。

## 结论与处置：腾出连接、修连接池，最后才谈抬上限

根因：一个刚上线的服务没有接入连接池，每个请求直连且不归还，制造了大量的 `idle` 连接长期占用名额，叠加一个慢报表把剩余连接拖在 `active`，最终打满 `max_connections` 并侵入预留区。

处置动作：

- **先腾连接（安全优先）**：对拖时间的慢查询先软取消，`SELECT pg_cancel_backend(pid);`；对长期 `idle in transaction` 的会话，确认无副作用后再硬终止，`SELECT pg_terminate_backend(pid);`。先取消、后终止，能少丢一次事务就少丢一次。
- **补兜底超时**：给会话设 `idle_in_transaction_session_timeout`，让"拿了连接不开事务边界"的会话自动退出；必要时再加 `idle_session_timeout`。
- **修连接池**：让该服务接入事务模式连接池，把每实例的直连数压下来。
- **最后才谈抬上限**：确实要调 `max_connections` 时，小步调整，并同步评估后端内存与共享内存参数，选低峰重启。
- **护住预留位**：收窄 `pg_use_reserved_connections` 的成员，只留给运维应急，不给业务角色。

验证指标：`pg_stat_activity` 的连接总数稳定低于 `max_connections - superuser_reserved_connections`；`idle`／`idle in transaction` 占比明显下降；应用侧不再出现 `too many clients`。

## 复盘要点

- **连接打满先分类再动手**。`idle` 多说明"没人放"，`active` 多说明"有人在拖"，两者的处置不同。
- **不要条件反射调大 `max_connections`**。它要重启、要连带调整共享内存参数、还要为每个连接付内存与切换成本；池化通常更划算。
- **预留位是应急通道，别被业务吃掉**。收窄 `pg_use_reserved_connections` 的成员，让它只服务于运维应急。
- **超时是连接治理的保险丝**。`idle_in_transaction_session_timeout` 能自动回收"拿了不还"的连接，比人工杀会话稳定。
- **连接池（尤其事务模式）是连接数与 `max_connections` 之间真正的缓冲层**；没有它，抬上限只是把下一次事故推迟。
