> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# checkpoint 与后台写：触发条件、参数耦合与 checkpoint 尖峰

## checkpoint 是什么，为什么它必然很贵

checkpoint 是事务序列里的一个点，保证"这个点之前的所有信息都已经写进了堆文件和索引文件"。到达 checkpoint 时，所有脏数据页被刷到磁盘，并写入一条 checkpoint 记录到 WAL。崩溃恢复时，从最近的 checkpoint 记录给出的 redo 位置开始重做，之前的 WAL 段就可以回收了。

它的代价来自两件事：一是要把当前所有脏缓冲写出去；二是它会让之后的 WAL 变多。后者常常被忽略：因为 `full_page_writes` 默认是 `on`，checkpoint 之后对某页的第一次修改要把整页写进 WAL。所以**checkpoint 越频繁，全页写越多，WAL 体积反而被推高**，部分抵消了"更频繁 checkpoint 缩短恢复时间"的收益。

checkpoint 由独立的 checkpointer 辅助进程执行，它和 background writer 是两个不同的进程。

## 什么时候会触发 checkpoint

自动 checkpoint 由两个条件中先到的一个触发：

- `checkpoint_timeout`：两次自动 checkpoint 之间的最长时间，默认 5 分钟，可设范围 30 秒到一天。
- `max_wal_size` 即将被超过：默认 1 GB。

如果自上次 checkpoint 以来没有写入任何 WAL，即使 `checkpoint_timeout` 到了也会跳过。也可以手动用 SQL 的 `CHECKPOINT` 强制触发。

`max_wal_size` 是**软上限**：在重负载、归档失败、或 `wal_keep_size` 设得较高的特殊情况下，实际 WAL 是可以超过它的。所以永远要留出磁盘余量，不能把它当硬限制来规划容量。

`min_wal_size` 默认 80MB。它的作用是：只要 WAL 用量低于这个值，旧段就不删除而是在 checkpoint 时被改名回收，留给未来复用；这样能吸收批量任务带来的 WAL 尖峰。

从库（恢复/standby 模式）执行的对应动作叫 restartpoint。它只能在 checkpoint 记录处进行，所以频率不会比主库的 checkpoint 更高；也正因如此，**恢复期间 `max_wal_size` 常常被超过最多一个 checkpoint 周期的量**。

## checkpoint 尖峰：出现时的三个信号

如果 checkpoint 触发得过于频繁，就会表现为周期性的 IO 尖峰和延迟抖动。官方给了一个自检手段：`checkpoint_warning` 默认 30 秒，当"因 WAL 段写满而触发的 checkpoint"间隔短于这个值时会往日志里写一条消息，提示应该调大 `max_wal_size`。偶尔出现不必紧张，频繁出现就该动参数了。把 `log_checkpoints` 打开，可以让每次 checkpoint 的统计都进日志。

```sql
SELECT checkpoints_timed, checkpoints_req, checkpoints_req::float
       / nullif(checkpoints_timed + checkpoints_req, 0) AS req_ratio,
       checkpoint_write_time, checkpoint_sync_time,
       buffers_checkpoint, buffers_clean, maxwritten_clean,
       buffers_backend
  FROM pg_stat_bgwriter;
```

这几个列的含意要分清：`checkpoints_timed` 是"到点触发"，`checkpoints_req` 是"因为 WAL 涨满被迫触发"，`buffers_checkpoint` 是 checkpoint 期间写出的缓冲数，`buffers_clean` 是 background writer 写出的，`buffers_backend` 是 backend 被迫自己写的。

实践中最容易被误判的一类：**"数据库整体写入变慢"经常被归因到某条 SQL 或锁上，而实际原因是 checkpoint 触发得太频繁。** 每次 checkpoint 都要把 `shared_buffers` 里的脏页整体刷一遍，频率一高，后台写与业务写就会持续争抢 IO，表现为**全局性的写延迟上升**，而不是某一条语句变慢。判别依据是 `checkpoint_warning` 打出的日志，加上 `pg_stat_bgwriter` 里 `checkpoints_req` 与 `checkpoints_timed` 的比例——**被"要求"触发的占多数，说明周期被 `max_wal_size` 卡住了**，而不是业务写得太多。

## checkpoint_completion_target：把 IO 摊平

默认 `0.9`。它的含义是：让 checkpoint 在下一个 checkpoint 到来之前的这段时间里尽可能匀速完成，把写盘摊平到整个区间。官方明确**不建议调小这个值**，因为调小意味着 checkpoint 在更短时间内干完，于是出现"checkpoint 期间 IO 很高、之后一段很空"的锯齿。虽然理论上可以设到 1.0，但通常不建议超过 0.9，因为 checkpoint 除了写脏页还有别的工作，设成 1.0 很可能完不成、反倒造成 WAL 段数量波动。

`checkpoint_flush_after` 默认在 Linux 上是 256kB，作用是每写到这个量就催一次操作系统把缓存刷下去，避免在 checkpoint 末尾 fsync 时被一大坨脏页堵住。这是改善事务延迟的常用手段，但对"比 shared_buffers 大、又比操作系统页缓存小"的工作负载有时反而会变差，需要实测。

**checkpoint 期间 IO 抖动特别大时，缓解手段有先后顺序**：先把 `checkpoint_completion_target` 提到 0.9，把一次 checkpoint 的写入摊到接近整个周期；再回到上面确认它不是因为 `max_wal_size` 太小而触发得太频繁——**频率才是抖动的放大器**，摊平只改变单次的形状；最后才用 `checkpoint_flush_after` 处理末尾 fsync 被一大坨脏页堵住的问题。顺序反了（先动 `checkpoint_flush_after`）通常看不出效果，因为主因还在频率上。

## background writer：和 checkpointer 分工不同

background writer 的目标是**提前把脏缓冲变干净**，这样处理用户查询的 backend 需要缓冲时，不至于找不到干净缓冲而被迫自己写盘（那会阻塞交互查询）。它的参数：

- `bgwriter_delay`：默认 200ms，每轮之间的休眠。
- `bgwriter_lru_maxpages`：默认 100，每轮最多写这么多缓冲；设为 0 就关闭后台写（注意 checkpoint 不受影响，因为它是另一个进程）。
- `bgwriter_lru_multiplier`：默认 2.0，预测下一轮需要多少干净缓冲；1.0 表示"刚好够"，更大留缓冲，更小则把写留给 backend。
- `bgwriter_flush_after`：默认在 Linux 上是 512kB，作用与 checkpoint 的那项类似。

这里有一处必须知道的副作用：**background writer 会让 IO 总量净增加**。一个被反复弄脏的页，本来一个 checkpoint 周期内只需要写一次，但 background writer 可能把它写好几次。所以它不是"免费加速"，而是"用更多 IO 换更平滑的延迟"。观测 `pg_stat_bgwriter.maxwritten_clean` 非零，说明 background writer 在一轮里写满上限被迫停下，可以考虑调大 `bgwriter_lru_maxpages`。

## 核心判断：checkpoints_req 高，问题在 max_wal_size 不在 checkpoint 参数

一个非常常见的误判，是看到 checkpoint 频繁就去调 `checkpoint_timeout`。但如果 `checkpoints_req` 远大于 `checkpoints_timed`，说明 checkpoint 是被 WAL 涨满逼出来的，根因是 WAL 生成太快或 `max_wal_size` 太小，**这时该做的是把 `max_wal_size` 调大（并确认磁盘能承受），或者去查 WAL 为什么涨得这么快**。只有在 `checkpoints_timed` 占主导、且确实需要缩短恢复时间时，才轮到动 `checkpoint_timeout`——而且要接受"恢复更快、但平时写更多 WAL"这个交换。
