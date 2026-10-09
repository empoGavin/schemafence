> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 同步复制把主库卡住：为什么备库一挂主库就写不进去

## 现象

某支付类订单库 `orders_prod`（虚构）在某次机房网络调整后，主库 `pg-primary-01` 上的写入全部变慢直至完全挂起。表现非常有特征：`SELECT` 一直正常，但只要涉及 `INSERT / UPDATE / DELETE / COMMIT` 的事务，几乎全部停住，应用侧大面积超时。架构是主库加两个物理流复制备库 `pg-standby-a`（同机房）和 `pg-standby-b`（异地），`synchronous_standby_names` 配置为 `FIRST 1 (pg_standby_a, pg_standby_b)`，即需要一个同步备库确认。

这就很奇怪了：`pg-standby-b` 还连着，监控上它的延迟也很低，为什么主库还是写不进去？

## 先收集什么证据

主库侧先看"谁在等、等什么"。同步复制的等待在 `pg_stat_activity` 里有专属的等待事件 `SyncRep`：

```sql
SELECT wait_event_type, wait_event, state, count(*)
  FROM pg_stat_activity
 WHERE wait_event = 'SyncRep'
 GROUP BY 1,2,3;
```

再看 standby 列表与同步状态：

```sql
SELECT application_name, state, sync_state, sync_priority,
       sent_lsn, flush_lsn, replay_lsn, replay_lag
  FROM pg_stat_replication;
```

以及当前生效的同步参数（注意 `source` 列区分是配置文件还是 `ALTER SYSTEM`）：

```sql
SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('synchronous_standby_names','synchronous_commit','wal_sender_timeout');
```

备库侧确认谁还在 streaming、各自的 `primary_conninfo` 里的 `application_name` 是什么：

```sql
SELECT status, sender_host, slot_name FROM pg_stat_wal_receiver;
```

网络层用 `ss -tnp` 看主库到两个备库的 5432 连接是否还在、`ping` 的时延与丢包。

## 定位过程

第一步定位"卡在哪"。`pg_stat_activity` 显示一大批写事务的 `wait_event_type = IPC`、`wait_event = SyncRep`。`SyncRep` 的定义就是"正在等待远端服务器在同步复制中确认"，这把问题从"是不是锁、是不是 IO"直接锁定到"在等同步备库"。`pg_locks` 没有大面积锁等待，IO 也正常，这两个方向排除。

第二步看"名单里到底有几个同步备库"。`pg_stat_replication` 里只剩 `pg-standby-b` 一行，`pg-standby-a` 已经消失（网络分区导致 WAL sender 断开）。但关键是 `pg-standby-b` 的 `sync_state` 值——它显示 `potential`，也就是"它是异步的，只有当前同步备库挂掉才可能被提升为同步"。

**反直觉的地方就在这里**：很多人以为 `FIRST 1 (a, b)` 的意思是"a 和 b 里任意一个确认就行"，所以 a 挂了、b 还在，主库应该照常能写。**实际语义不是这样**。文档里写得很清楚：当某个同步备库断开时，它会被"下一个优先级更高的备库立即接替"——但这个接替要求接替者处于 `streaming` 状态（也就是真正在实时流复制），而不是 `catchup`。本例中 `pg-standby-b` 因为机房网络调整也刚刚重连，仍处于追赶阶段，`state` 不是 `streaming`，因此它还没资格成为同步备库。结果就是：名单里当前**没有任何一个处于 `streaming` 的同步候选**，wait 条件永远无法满足，所有提交无限期等待。

还有一层容易被忽略的坑也要一并检查：`synchronous_standby_names` 里的名字是备库的 **`application_name`**——物理复制下取自备库 `primary_conninfo` 里的 `application_name`（未设时取 `cluster_name`，再没有则是 `walreceiver`），逻辑复制下默认取订阅名。如果配置里写的 `pg_standby_a` 和备库实际报上来的 `application_name` 对不上（比如大小写、下划线/连字符差异），那么即使备库连得好好的、延迟为零，它也永远不会被算作同步备库，主库会一直等下去。本例里名字是对的，但这条每次都要核对，因为它同样会导致"明明有备库却永远等不到确认"。

顺带把 `FIRST` 和 `ANY` 的差别讲清，因为选错了直接决定故障面：

- `FIRST num (s1, s2, s3)`：按优先级取**前 num 个**作为同步备库，断开时由后继者接替。语义偏向"由指定节点承担强一致"。
- `ANY num (s1, s2, s3)`：只要名单里**任意 num 个**备库确认即可。对单点故障更宽容。

用 `FIRST 1 (a, b)` 而 a 是"唯一被寄予厚望的同步点"，一旦 a 出问题就容易演变成单点；`ANY 1 (a, b)` 或 `ANY 1 (a, b, c)` 则任一备库在线即主库可写。两者在**一致性保证与可见性语义上并不等价**，选择要基于"哪些节点必须持有数据"，不是纯粹的可用性偏好。

## 结论与处置

根因：唯一处于 `streaming` 的同步备库 `pg-standby-a` 因网络分区掉线，名单内其余备库尚未进入 `streaming`，`FIRST 1` 的确认条件无法满足，导致所有 `synchronous_commit = on` 的提交无限等待。

处置动作（顺序很重要）：

- 先恢复网络，让 `pg-standby-a` 或 `pg-standby-b` 尽快回到 `streaming`，同步复制自然会恢复。这是首选，因为它不牺牲任何保证。
- 若网络短时间无法恢复，且业务必须恢复写入，就降低对同步确认的要求：把 `synchronous_standby_names` 改成 `ANY 1 (pg_standby_a, pg_standby_b)` 或临时清空，然后 reload（无需重启）：

```sql
ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (pg_standby_a, pg_standby_b)';
SELECT pg_reload_conf();
```

**down 掉同步复制前必须先确认三件事**：一是现在有几个备库真的在 `streaming` 且数据领先，别在"没有任何备库追上"时关掉，否则等于裸奔；二是关掉同步等于**这段时间 RPO 不再为 0**，要明确业务能容忍丢多少数据并留下通知记录；三是记下改了什么，恢复后务必改回原配置——很多"事后忘了改回来"的事故都是这么来的。

- 长期改进：用 `ANY` 配多个候选备库，避免把可靠性押在单一节点上；并核对 `application_name` 与名单一致。

验证指标：主库写事务不再停在 `SyncRep` 等待，提交恢复正常时延；`pg_stat_replication` 里 `sync_state` 重新出现 `sync`；`synchronous_commit = on` 的语义恢复；备库的 `flush_lsn` 紧贴主库 `sent_lsn`。

## 复盘要点

- **同步复制是主库可用性与"零丢失"之间的取舍**。要 RPO 接近 0，就得接受"备库抖动会拖慢甚至卡住主库"。
- **`FIRST` 与 `ANY` 语义不同**。`FIRST` 按优先级取前 N 个，易形成单点；`ANY` 在名单内任取 N 个，更抗单点故障，但可见性语义不一样。
- **名单要对得上 `application_name`**。名字对不上等于名单为空，主库会永远等一个不会来的确认。
- **`potential` 不等于 `sync`**。处于追赶（非 `streaming`）的备库不能承担同步角色，别用"连接数"代替"同步状态"判断。
- **提前发现**：对 `pg_stat_replication.sync_state` 长期不存在 `sync`、以及对 `SyncRep` 等待会话数设告警，能在业务超时之前看见问题。
- **别把逻辑复制当同步复制的替代来做"零丢失"**。逻辑复制是表级、异步语义，无法提供物理同步复制那种"提交即落备库"的保证，两者解决的问题不同。
