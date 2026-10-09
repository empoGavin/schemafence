> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 备库延迟只涨不落：先分清是发不出去还是回放不动

## 现象

某订单库 `orders_prod`（虚构）凌晨 02:10 起复制延迟告警。架构是主库 `pg-primary-01`，同机房备库 `pg-standby-a`（承载报表只读流量），异地备库 `pg-standby-b`。两条告警同时触发：`pg-standby-a` 的 `replay_lag` 在 10 分钟内从 0.3s 爬到 90s，`pg-standby-b` 更高，超过 5 分钟。

业务侧的表现很典型：报表同学反馈"刚下单的数据在只读库查不到"，但下单接口本身一切正常，主库写入没有任何报错。也就是说，**故障只在读侧**，写入链路是健康的——这个细节把排查范围直接缩小了一半。

## 先收集什么证据

复制延迟不是一个单一数字，得在主库和备库两侧同时抓，否则永远分不清是哪一段慢。

主库侧抓 `pg_stat_replication`，一行一个 WAL sender：

```sql
SELECT application_name, state, sync_state,
       sent_lsn, write_lsn, flush_lsn, replay_lsn,
       write_lag, flush_lag, replay_lag,
       backend_xmin
  FROM pg_stat_replication;
```

同时对比主库当前写位置：

```sql
SELECT pg_current_wal_lsn();
```

备库侧抓接收与回放两个位置，以及 WAL receiver 的状态：

```sql
SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();
SELECT status, receive_start_lsn, written_lsn, flushed_lsn,
       received_tli, latest_end_lsn, latest_end_time, sender_host, slot_name
  FROM pg_stat_wal_receiver;
```

OS 层最少抓三样：`iostat -x 1`（看备库盘是否有回放瓶颈）、备库的 `top -H`（看 startup 进程 CPU）、以及主备之间的网络 RTT 与重传。别急着下结论，先把这四组数字摆在一起。

## 定位过程

第一步，把端到端延迟拆成三段差值，而不是盯着一个 `replay_lag` 看：

1. **主库发送段**：`pg_current_wal_lsn() - sent_lsn`。这段大，说明 WAL 生成的比发出去的多，是发送侧或主库产出问题。
2. **网络传输段**：`sent_lsn - pg_last_wal_receive_lsn`。这段大，才可能是网络。
3. **备库回放段**：`flush_lsn - replay_lsn`（备库侧即 `pg_last_wal_receive_lsn()` 与 `pg_last_wal_replay_lsn()` 之差）。这段大，说明收下来了但重放不动。

**反直觉的地方就在这里**：值班同事看到 `replay_lag` 90 秒，第一反应是"网络抖动"。但实测 `sent_lsn` 与 `pg_last_wal_receive_lsn` 几乎相等，备库的 `written_lsn` / `flushed_lsn` 仍在毫秒级前进——网络这条假设第一个被排除。同时 `flushed_lsn` 紧跟着 `written_lsn`，说明备库的写到盘也没问题。

真正的差值出在第三段：`pg_last_wal_receive_lsn()` 已经追到很靠前，但 `pg_last_wal_replay_lsn()` 长时间钉在同一个值上不动，`flush_lsn - replay_lsn` 一路拉大。于是方向从"数据没到"转向"数据到了但重放不动"。

接下来要回答"为什么重放不动"。这里有个容易被误判的现象：**`replay_lsn` 长时间不前进，不等于备库卡死**。物理流复制是按事务原子重放的——一个巨大事务在备库里必须整体应用完才会推进 `replay_lsn` 并让数据可见。也就是重放中途它在动，但对外可见的推进位置不变。

去主库追 WAL 生成的来源，很快就对上了：02:05 有一个运维发起的批量数据补录作业，用单个事务导入约 4000 万行；02:08 又叠加了一次 `ALTER TABLE orders ADD COLUMN ... DEFAULT ...` 的批量 DDL。前者制造了一个超长事务，后者在重写表时产生峰值 WAL。两者叠加，主库 WAL 生成速率在几分钟内涨到平时十几倍——`pg_current_wal_lsn()` 与 `sent_lsn` 的差值随之一并拉大，说明主库本身也处于"产出远大于消化"的状态。

排除掉的两个假设要写清楚：一是网络，依据是接收段差值近零、备库 `flushed_lsn` 持续推进；二是备库磁盘慢，依据是 `flushed_lsn` 紧跟 `written_lsn`，`iostat` 的 `%util` 与 `await` 都在正常区间。剩下的才是"主库产出暴增 + 大事务原子重放"这一条。

顺带澄清一个经常被混淆的点：**这是物理流复制的行为**。物理复制按 WAL 字节重放，没有"跳过某个大事务"的选项。如果这类"只关心少数几张表变更"的场景改用逻辑复制，发布端按表、按操作投递，就不会因为一次无关的全库大 DDL 把整条复制链路拖住——但逻辑复制不复制 DDL，需要额外维护，两者不能混着谈。

## 结论与处置

根因：主库侧 WAL 生成速率因批量作业与批量 DDL 暴增，叠加一个大事务的原子重放，导致备库 `replay_lsn` 长时间不推进、可见性延迟升高。

处置动作：

- 拆分补录作业，把单事务 4000 万行改为分批提交（按主键区间每 20 万行一提交），把"一个大事务"变成"许多小事务"，让备库能持续推进可见位置。
- 批量 DDL 放到业务低峰，并用 `CREATE INDEX CONCURRENTLY` 之类对复制更友好的方式替代会长时间持锁、整表重写的形式。
- 给异地备库 `pg-standby-b` 保留复制槽，避免它短暂跟不上时主库提前回收 WAL 而被迫重做基础备份。
- 在监控里把"三段差值"分别打点，不再只报一个 `replay_lag`。

验证指标：拆分作业后，`pg_last_wal_replay_lsn()` 持续前进并追平 `pg_last_wal_receive_lsn()`；`pg_stat_replication.replay_lag` 回落到秒级；`sent_lsn` 重新贴近 `pg_current_wal_lsn()`；`pg-standby-b` 的 `write_lag / flush_lag / replay_lag` 三者同步收敛。

## 复盘要点

- **延迟要拆段看**。`pg_current_wal_lsn → sent_lsn → receive_lsn → replay_lsn` 是一条链，任何一段都可能积压；只报端到端一个数字，会把"主库写太多"误判成"网络差"。
- **`replay_lsn` 不动 ≠ 备库挂了**。大事务在物理重放中是原子的，中途 `replay_lsn` 不前进是正常现象，别据此误重启备库。
- **长事务与批量 DDL 是复制的头号敌人**。它们同时抬高 WAL 生成量、拉长原子重放窗口，还会拖住主库的清理。
- **物理复制与逻辑复制适用的场景不同**。重放大事务、全库 DDL 这类冲击是物理复制的固有代价；只关心子集数据、跨版本、跨平台时逻辑复制更合适，但它不复制 DDL。
- **下次怎么提前发现**：把"主库 WAL 生成速率"和"单事务最大 WAL 量"做日常打点，在批量作业启动前就判断它会不会把备库拖住，而不是等 `replay_lag` 告警。
