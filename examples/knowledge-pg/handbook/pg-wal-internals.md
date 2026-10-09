> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# WAL 机制：LSN、写入与落盘时机、wal_level 与归档

## 核心一句话：先写日志，再改数据

预写式日志（Write-Ahead Logging，WAL）的中心思想很短：**对一个数据文件的修改，必须在描述这个修改的日志记录已经落盘之后，才能写回数据文件**。遵守这条规则后，事务提交时就不必把每个被改动的数据页都刷盘，只需要刷 WAL；崩溃后用 WAL 把没落盘的改动重做（roll-forward，也就是 REDO）即可。

这带来两个直接好处：一是写放大显著下降，因为 WAL 是顺序写的，同步一份顺序日志比同步一堆分散的数据页便宜得多；二是热点并发的多个小事务，常常可以靠"一次 WAL flush"一起提交；三是归档 WAL 就天然支持在线备份和按时间点恢复（PITR）。

## LSN 与 WAL 段：位置怎么表示、文件怎么切

WAL 记录是追加写入的，写入位置用一个叫 LSN（Log Sequence Number）的字节偏移表示，单调递增。LSN 的类型是 `pg_lsn`，两个 LSN 可以相减算出它们之间有多少 WAL 字节，复制延迟和恢复进度都是用它度量的。

WAL 文件放在数据目录的 `pg_wal` 下，是**一组段文件，每段默认 16MB**（可以在 initdb 时用 `--wal-segsize` 改），每个段内部再按 8KB 分页。段文件名是递增的十六进制串，从 `000000010000000000000001` 开始。

```sql
SELECT pg_current_wal_lsn() AS now_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),
                                      '0/0'::pg_lsn)) AS generated;
```

## 写入与落盘：wal_buffers、wal_writer 与 synchronous_commit

WAL 先写进共享内存里的 WAL 缓冲区，再由不同路径推向磁盘。三个参数决定了这个过程：

- `wal_buffers`：默认 `-1`，含义是自动取 `shared_buffers` 的 1/32（约 3%），但不小于 64kB、不大于一个 WAL 段的大小。高写入量时，`pg_stat_wal.wal_buffers_full` 不为零就说明缓冲区被写满逼着提前刷，可以考虑手动指定一个更大的值。
- `wal_writer_delay`：默认 200ms。walwriter 每刷一次就睡这么久（除非被异步提交提前唤醒）。
- `wal_writer_flush_after`：默认 1MB。距上次刷盘不足 `wal_writer_delay`、且新产生的 WAL 不足这个量时，只写到操作系统缓存，不真正刷盘。

什么时候必须真正落盘？由 `synchronous_commit` 决定，默认 `on`，意思是提交要等本地 WAL flush 完成才向客户端返回成功。把它改成 `off` 就是异步提交：**只丢数据、不损坏数据库**——崩溃后恢复到一个自洽状态，只是最近几个事务没了。风险窗口最长约为 `wal_writer_delay` 的三倍，因为 walwriter 在繁忙时会成页写。

注意区分：异步提交（`synchronous_commit = off`）和关闭 `fsync` 完全不是一回事。`fsync` 默认 `on`，关掉它会让操作系统级崩溃可能造成任意损坏；异步提交不会带来损坏风险。`wal_sync_method` 控制用哪种系统调用刷盘，Linux/FreeBSD 上默认是 `fdatasync`。

## wal_level 三档：minimal / replica / logical

`wal_level` 决定 WAL 里记多少信息，只能在启动时设定：

- `minimal`：只记崩溃恢复必需的信息。对"创建或整体重写表"的事务，不记行级信息，因此 CREATE TABLE、CLUSTER、REINDEX、TRUNCATE、REFRESH MATERIALIZED VIEW（不带 CONCURRENTLY）这类操作会明显更快。代价是**无法做 PITR、无法流复制**；只要 `max_wal_senders` 非零，服务器在 minimal 下根本起不来。
- `replica`：**默认值**。记录足以支持 WAL 归档和流复制、并让从库跑只读查询的信息。
- `logical`：在 replica 的基础上，再加上逻辑解码需要的信息。如果很多表设了 `REPLICA IDENTITY FULL`、又有大量 UPDATE/DELETE，这一档会让 WAL 体积明显增大。

## WAL 归档：把段文件送出去

归档由 `archive_mode` 控制，取值 `off`、`on`、`always`。它和归档命令是分开的两件事，这样可以只改命令而不离开归档模式。归档命令由 `archive_command`（或 `archive_library`）提供，其中的 `%p` 会被替换成待归档文件的路径、`%f` 替换成文件名。命令必须"只有成功时才返回 0 退出码"。

如果写入量很小，可能要等很久才写满一个段、才触发归档。可以用 `archive_timeout` 强制周期性地切换新段（例如一分钟量级）。但要注意：**因为超时而提前关闭的归档文件，长度和写满的一样**，所以设得太短会把归档存储白白撑大。

```sql
SELECT archived_count, failed_count, last_failed_time
  FROM pg_stat_archiver;
```

`failed_count` 持续增长说明归档链路出问题了，此时旧段会一直堆在 `pg_wal` 里不回收。

## WAL 生成量由什么决定

除了业务本身的写入量，还有几个经常被忽略的放大器：

- `full_page_writes` 默认 `on`：checkpoint 之后对某页的第一次修改会写入整页。所以 checkpoint 越频繁，全页写越多，WAL 也被推高。
- `wal_compression` 默认 `off`：打开后可以对写入 WAL 的整页镜像做压缩，用一点 CPU 换 WAL 体积。
- `wal_level = logical` 叠加大量 `REPLICA IDENTITY FULL` 与 UPDATE/DELETE。
- 大量创建或重写表的操作（在 minimal 下反而是省 WAL 的）。

```sql
SELECT wal_records, wal_fpi, pg_size_pretty(wal_bytes::bigint) AS wal_bytes,
       wal_buffers_full, wal_write, wal_sync
  FROM pg_stat_wal;
```

`wal_fpi` 就是全页镜像（full page image）的数量。如果它的量级和 `wal_records` 接近，说明全页写占了很大比重，调整方向应该是 checkpoint 相关参数，而不是去"优化 SQL 里的 update 条数"。

## 落到可观察：文件、函数、视图各看一处

```sql
SELECT count(*) AS segments,
       pg_size_pretty(sum(size)) AS total
  FROM pg_ls_waldir();

SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots;
```

`pg_ls_waldir()` 看物理文件，`pg_current_wal_lsn()` 与 `pg_wal_lsn_diff()` 算位置差，`pg_replication_slots` 看有没有复制槽把 WAL 钉住不放。

## 核心判断：WAL 涨得快，先看全页写而不是业务写入

遇到 `pg_wal` 体积异常，第一反应通常是"业务写太多了"。但更常见的情况是全页写与 checkpoint 频率的耦合：checkpoint 触发得太密，每次之后的第一笔页修改都写整页，WAL 于是被成倍放大。**判断方法是把 `pg_stat_wal.wal_fpi` 和 `wal_records` 放在一起看**——如果 fpi 占比很高，该调的是 `max_wal_size` 和 checkpoint 参数，而不是去追业务代码。
