> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 复制接不上了就得重做：什么时候必须 pg_basebackup，什么时候能续上

## 现象

某订单库 `orders_prod`（虚构）的异地备库 `pg-standby-b` 复制中断，两条复制链路里的 `pg_stat_replication` 只剩 `pg-standby-a` 一行。备库日志（虚构标识符）：

```
FATAL:  could not receive data from WAL stream: ERROR:  requested WAL segment 0000000100000003000000AB has already been removed
LOG:  started streaming WAL from primary at 3/AB000000 on timeline 1
FATAL:  could not connect to the primary server: ... connection ... timed out
```

值班先重启了一次备库，备库能启动、能接受只读连接，但 `pg_last_wal_replay_lsn()` 卡住不动，永远追不上主库。此时一个关键的问题是：**这次是"接上就继续"，还是"必须重做基础备份"？** 判断错了，轻则白折腾一晚，重则引入数据损坏。

## 先收集什么证据

备库侧先确认它到底能恢复到哪、停在哪个时间线：

```bash
pg_controldata /var/lib/postgresql/16/data
ls /var/lib/postgresql/16/data/pg_wal/
cat /var/lib/postgresql/16/data/standby.signal 2>/dev/null
```

`pg_controldata` 里的 `Latest checkpoint location`、`Latest checkpoint's TimeLineID` 是判断起点的关键。

主库侧看槽的死活和 WAL 可达范围：

```sql
SELECT slot_name, slot_type, active, restart_lsn, confirmed_flush_lsn,
       wal_status, safe_wal_size
  FROM pg_replication_slots;

SELECT name, setting, source FROM pg_settings
 WHERE name IN ('wal_level','archive_mode','archive_command','restore_command',
                'wal_keep_size','max_slot_wal_keep_size','max_wal_senders',
                'recovery_target_timeline');
```

再去归档目录确认"缺的那一段 WAL 还在不在"：

```bash
ls /var/lib/postgresql/16/archive/ | grep 0000000100000003000000AB
```

这一步是整个判断的分水岭：**能不能增量续接，本质上是"备库缺的那段 WAL 是否还得着"**。

## 定位过程

第一条判据：**缺失的 WAL 是否可得**。日志要求的是 `0000000100000003000000AB`，去归档目录和主库 `pg_wal` 两处都没找到——归档只保留最近 24 小时，而这台备库离线超过两天。既然这段 WAL 已经不存在，备库就无法把日志拼回去，增量续接这条路直接堵死。

第二条判据：**复制槽是否 lost**。主库 `pg_replication_slots` 里 `standby_b_slot` 的 `wal_status` 显示 `lost`。`lost` 的含义很明确：槽不再可用，它所需要的 WAL 已经被删除。这是"必须重做"的硬信号。如果槽当时还是 `reserved` 或 `extended`，说明 WAL 还在被保留，那才有续接的可能。

第三条判据：**时间线（timeline）是否一致**。用 `pg_controldata` 对比主备：两边都还是 timeline 1，没有发生 failover，所以这次不涉及时间线分叉，问题纯粹是 WAL 缺失。这条虽然这次没触发，但每次都要看，因为它决定的是"要不要用 `pg_rewind`"这类完全不同的问题。

顺带排除两个容易被误判的方向。一是网络：连接超时早就恢复了，`pg_stat_replication` 里没有该备库是因为它自己起不来复制，不是网络不通。二是磁盘：`df` 显示备库空间充足。都不是。

**这里有一个必须拦住的反直觉操作**：现场有人提议"既然缺的是一段 WAL、timeline 号也一样，那就手工把 timeline 相关的文件或名字改一改，让备库从主库更早的位置接着拉"。**手工改 timeline 是绝对禁止的**。timeline 不是随便一个编号，它是"同一段 WAL 之后走了哪条恢复分支"的标识：一次 `promote` 会让新主库启用一个新的 timeline 号，从此与旧时间线分叉。如果手工把备库的 timeline 改小、改名或补 `.history` 文件，诱导它去重放另一条分叉上的 WAL，PostgreSQL 不会报错拒绝——它会**静默地把两条分叉的数据混着应用**，结果是数据被污染，比"缺 WAL"危险得多。timeline 只能由 `pg_ctl promote` / `pg_promote()` 正常推进，由 `recovery_target_timeline` 决定备库跟随到哪个分叉（高可用场景应保持默认 `latest`）。任何绕过这两个机制的改动都是禁区。

三条判据合起来，结论明确：**必须重做基础备份**，没有别的选择。

## 结论与处置

根因：`pg-standby-b` 离线时间超过归档保留窗口，且没有可用的复制槽保护其 `restart_lsn`，槽最终变为 `lost`，所需 WAL 被删除，无法增量续接。

处置动作（重做流程）：

- 停掉备库，确认不再有写入它的连接。
- 用新数据目录（或清空旧目录）重建，避免残留文件干扰：

```bash
pg_basebackup -h pg-primary-01 -p 5432 -U repl_user \
  -D /var/lib/postgresql/16/data_new \
  -X stream -S standby_b_slot -R -P -c fast
```

- `-R` 会自动生成 `primary_conninfo`（含 `primary_slot_name`）和 `standby.signal`，省去手工写配置的出错机会；`-X stream` 让备份期间产生的 WAL 一并拉走，保证恢复起点一致。
- 若原槽已 `lost`，先按新建备库的方式创建**新的**物理槽再让备库使用它（名字可复用，但元数据要重建）；不要试图复用 `lost` 槽。
- 启动备库，观察日志出现"consistent recovery state reached"和可接受只读连接。

验证指标：`pg_last_wal_replay_lsn()` 追平 `pg_last_wal_receive_lsn()` 并持续前进；主库 `pg_stat_replication` 出现该备库且 `state = streaming`、`replay_lag` 收敛；`pg_controldata` 的 timeline 与主库一致；对关键表做一次行数与校验和比对（例如对订单表做 `count(*)` 与抽样聚合），确认数据一致。

## 复盘要点

- **"能不能续接"看三条**：缺的 WAL 是否可得、槽是否 `lost`、timeline 是否同分叉。前两条任一不满足（或分叉了），就要重做。
- **别手工改 timeline**。它会诱导备库混放两条分叉的 WAL，造成静默数据损坏，比缺 WAL 更不可救。
- **保持"可得性"的手段是设计出来的**：连续归档、复制槽、以及 `max_slot_wal_keep_size` 的上限，共同决定备库能离线多久还能接上。
- **`pg_rewind` 与重做不是一回事**。`pg_rewind` 针对"切换后旧主回来当新备"的场景，靠对比 `pg_wal` 增量重放，能省掉整份基础备份；而本例是备库自己缺了 WAL，`pg_rewind` 也救不了，只能基础备份重建。
- **下次怎么提前发现**：监控槽的 `wal_status` 从 `reserved / extended` 滑向 `unreserved / lost` 的过程，并对备库离线时长设阈值告警，在归档窗口耗尽前就介入。
