# WAL、checkpoint 与复制延迟

## WAL 的基本盘

每一次数据页修改先写 WAL，再写数据文件。这个"先日志后数据"的顺序是崩溃恢复能力的来源，
代价是所有写操作都要等 WAL 落盘。`synchronous_commit = off` 能显著提速，
但机器掉电会丢掉最近一小段已提交事务——这个取舍必须由业务方点头，不能由 DBA 自己决定。

## checkpoint 调优的经验值

checkpoint 太频繁：IO 抖动明显，因为每次都要把所有脏页刷盘。
checkpoint 太稀疏：崩溃恢复时间变长，同时 WAL 总量膨胀。

先看 `checkpoint_warning` 有没有在日志里出现，再用 `pg_stat_bgwriter` 看
`checkpoints_req`（请求触发）与 `checkpoints_timed`（时间触发）的比例。
如果请求触发的占多数，说明 `max_wal_size` 太小，而不是 checkpoint 本身有问题。

## 复制延迟排查顺序

1. `pg_stat_replication` 的 `replay_lag` 与 `write_lag`：区分是"网络慢"还是"备库应用慢"
2. 备库上有长查询在跑，apply 会等它——`hot_standby_feedback` 会让主库的 VACUUM 也受影响
3. 主库上有超长事务：复制是事务提交后才生效，单个 40 分钟的事务在备库上就是 40 分钟的延迟
4. 检查是否开了 `synchronous_standby_names` 且备库掉线，主库写入会被阻塞而不是变慢

## 一句话结论

延迟的根因通常在主库的写模式上（长事务、大事务、批量更新），
换更快的网络或者更强的备库，只是把等待时间挪了个位置。
