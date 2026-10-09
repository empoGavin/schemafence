> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# PostgreSQL 进程与内存架构：连接一来谁在干活、内存怎么分

## 一个连接对应一个 backend，这是排障的起点

PostgreSQL 采用的是"每用户一进程"（process per user）模型：客户端连上的那一个 backend 进程，只服务这一个连接。连接由 postmaster 负责接收——它是一个监督进程（supervisor process），在指定的 TCP 端口上监听，一旦检测到连接请求，就 fork 出一个新的 backend。backend 之间要通过共享内存和信号量交换状态，才能保证并发访问的一致性。

这条机制直接决定了几件事：

- 并发连接数就是操作系统进程数。1000 个连接意味着接近 1000 个进程，调度开销、页表内存、上下文切换都会跟着涨。
- 每个 backend 是独立地址空间，所以进程之间没有廉价的方式共享私有状态，跨会话的全局结构只能放共享内存。
- backend 崩了只影响它自己那条连接；但 postmaster 崩了（典型场景是被内核 OOM killer 杀掉），实例会停止接受新连接。

`max_connections` 官方默认值是 100，`superuser_reserved_connections` 默认再预留 3 个，用来在连接打满时留给超级用户做急救。这两个参数都只能在服务器启动时设定。

这里有一个明确的取舍：**盲目调大 max_connections 换不来吞吐**。官方文档本身就指出，很多场景下更好的办法是把连接数降下来，改用外部连接池。因为真正贵的不只是内存，还有每个连接带来的进程切换与锁竞争。

## 辅助进程都有谁：pg_stat_activity 里能直接看到

除了 postmaster 和 backend，实例里还有一批常驻的辅助进程。它们不是装饰品，很多"看不见的慢"就发生在它们身上：

- checkpointer：执行 checkpoint，把脏页集中刷盘（参数与观测方法见 checkpoint 那一篇）
- background writer：持续、小批量地把脏缓冲刷出去，减少 backend 被迫自己写盘
- walwriter：把 WAL 缓冲周期性地写出并刷盘
- autovacuum launcher：调度 autovacuum worker 去清理死元组
- walsender：给从库或逻辑订阅者发送 WAL，每个复制连接一个
- archiver：执行归档命令，把写满的 WAL 段送走

怎么看到它们？`pg_stat_activity` 的 `backend_type` 一列会直接告诉你这行是谁。可能的取值包括 `client backend`、`background writer`、`checkpointer`、`archiver`、`autovacuum launcher`、`autovacuum worker`、`walwriter`、`walsender`、`walreceiver`、`startup`、`logical replication launcher`、`logical replication worker`、`parallel worker`、`standalone backend` 等。

一个常见误判：看到 `pg_stat_activity` 里有 `checkpointer` 处于 active，就以为是异常。**辅助进程本来就常常处于 active，它 active 本身不代表故障**，要结合它当处在哪一步（例如是否卡在 `CheckpointWriteDelay`）再判断。

```sql
SELECT backend_type, count(*)
  FROM pg_stat_activity
 GROUP BY 1 ORDER BY 2 DESC;
```

## shared_buffers：真正共享的那一大块内存

`shared_buffers` 是 PostgreSQL 自己管理的共享缓冲池，默认 128MB，只能在启动时设定。官方给的经验值是：专用数据库服务器上取物理内存的 25% 起步。再往上收益递减，因为 PostgreSQL 同时依赖操作系统页缓存，**给 shared_buffers 分配超过 40% 的内存，很少比小一点更好**。

还有一处容易被忽略的耦合：把 `shared_buffers` 调大，通常要配套调大 `max_wal_size`，否则大批脏页会在更短时间里被强制刷出，反而制造出更尖的写盘波峰。

```sql
SELECT name, setting, unit, source
  FROM pg_settings
 WHERE name = 'shared_buffers';
```

`source` 这一列很关键：它区分这个值是默认值、配置文件里写的，还是被 `ALTER SYSTEM` 改过。诊断"某个参数到底是谁改的"，只能靠它。

## 本地内存：work_mem / maintenance_work_mem / temp_buffers

这三类内存不共享，按会话或按操作分配，算容量时最容易算错的就是它们。

`work_mem` 默认 4MB。它的准确语义是"**每一个排序或哈希操作**最多用这么多，而不是每条查询"。一条复杂查询可能同时存在多个排序和哈希节点，每个都能各自用到这个上限；再叠加多个并发会话，实际占用就是 `work_mem × 并发数 × 每查询算子数`。哈希类操作的上限还要再乘 `hash_mem_multiplier`（默认 2.0），也就是默认能用到 8MB。排序用于 ORDER BY、DISTINCT、merge join；哈希表用于 hash join、哈希聚合、memoize 以及 IN 子查询的哈希处理。

`maintenance_work_mem` 默认 64MB，服务于 VACUUM、CREATE INDEX、ALTER TABLE ADD FOREIGN KEY 这类维护操作。它可以明显大于 `work_mem`，因为一个会话同一时刻只会跑一种维护操作。但要注意两点：autovacuum 运行时，最多可能有 `autovacuum_max_workers` 份这个内存同时被占用；以及 VACUUM 收集死元组标识时最多只能用 1GB 内存，超出部分设了也用不上。需要单独给 autovacuum 设值时，用 `autovacuum_work_mem`。

`temp_buffers` 默认 8MB，只服务于临时表。它可以在会话内修改，但**必须在会话里第一次使用临时表之前改**，之后再改对当前会话不再生效。

## 落到可观察：进程与内存各看哪里

进程层面看 `pg_stat_activity`，内存层面看 `pg_settings`，用量层面看 `pg_stat_database`：

```sql
SELECT name, setting, unit
  FROM pg_settings
 WHERE name IN ('shared_buffers','work_mem','maintenance_work_mem',
                'temp_buffers','max_connections','hash_mem_multiplier');

SELECT datname, numbackends, xact_commit, blks_hit, blks_read
  FROM pg_stat_database
 WHERE datname IS NOT NULL;
```

`numbackends` 是这张视图里唯一反映"当前瞬时状态"的列，其余都是累计值。用它做连接数监控比数进程更直接。

## 核心判断：内存预算的坑在乘数，不在主项

排查内存问题时，大家习惯先盯 `shared_buffers`，但真正会把实例压到 OOM 的，往往是 `work_mem` 被"乘以并发、再乘以每查询算子数"之后的结果。一个 4MB 的默认值，在 200 并发、每条查询 6 个排序或哈希节点的假设下，理论峰值就是约 4MB × 2.0 × 6 × 200 ≈ 9.6GB。

所以调 `work_mem` 的正确姿势是会话级 `SET`，或者配合连接池按需设置，**不要图省事直接在 postgresql.conf 里把全局值抬到 64MB**——那等于给每个算子、每个会话都发了一张大额支票，而签发时并不知道要兑多少张。
