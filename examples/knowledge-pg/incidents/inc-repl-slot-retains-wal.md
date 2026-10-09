> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 复制槽把磁盘吃满：先认出是哪个失效槽在拽住 pg_wal

## 现象

某订单库 `orders_prod`（虚构）凌晨 03:20 触发磁盘水位告警。主库 `pg-primary-01` 的数据盘使用率从 55% 涨到 93%，`pg_wal` 目录一个晚上涨了 280GB，业务写入开始间歇性报 `No space left on device`。架构是主库加三个物理流复制备库：`pg-standby-a`、`pg-standby-b`，以及前一天 22:00 因硬件维护被关停、尚未恢复的 `pg-standby-c`。

主库日志里反复出现这类内容（标识符均为虚构）：

```
WARNING:  oldest xmin is far in the past
LOG:  checkpoint starting: time
LOG:  checkpoint complete: wrote 41230 buffers (2.5%); ...
LOG:  could not receive data from WAL stream: ERROR:  requested WAL segment 0000000100000012000000A3 has already been removed
```

行内运维的第一反应是"把 `pg_wal` 里最旧的文件删掉救急"。这个动作如果做了，会把一次磁盘告警升级成一次需要重做整个备库的事故。

## 先收集什么证据

先量出"是谁占着盘"，再决定删什么。OS 层：

```bash
df -h /var/lib/postgresql/16/data
du -sh /var/lib/postgresql/16/data/pg_wal
ls -1 /var/lib/postgresql/16/data/pg_wal | wc -l
iostat -x 1 5
```

数据库层，把槽的状态一次抓全：

```sql
SELECT slot_name, slot_type, active, active_pid, restart_lsn,
       wal_status, safe_wal_size,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots
 ORDER BY restart_lsn;
```

再确认几个兜底参数的现值：

```sql
SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('max_slot_wal_keep_size','wal_keep_size','max_wal_size',
                'min_wal_size','archive_mode','archive_command');
```

最后留一份 `pg_stat_replication` 现场，看看当前实际连着的 sender 有几个、各自 `state` 是什么。

## 定位过程

先看 `pg_wal` 里的文件总数和总大小——几百 GB 说明确实在堆积，但"堆积"只是结果，不是原因。

第一个假设是 `archive_command` 卡住导致归档 WAL 无法释放。查 `pg_settings.source` 和归档状态后发现 `archive_command` 配置正常，最近的归档也都是成功状态，这条排除。

第二个假设是 checkpoint 太稀疏、`max_wal_size` 设得过大。看值：`max_wal_size` 默认 1GB，没有被改大；`checkpoint` 日志也按时间正常触发。这条也排除。

真正的原因在 `pg_replication_slots` 里：`standby_c_slot` 是一个物理槽，`active = f`（对应备库已关停），而它的 `restart_lsn` 停在整整一天前的 LSN 不动。**这就是反直觉的地方**——占盘的不是主库写得太多，也不是归档太慢，而是一个没人用了的槽，它把 `restart_lsn` 之后的所有 WAL 段都"钉"住不许回收。槽的存在意义是"保证这些 WAL 直到被消费者接收前不删除"，备库不在了，没人来推进这个位置，回收自然停摆。

读 `restart_lsn` 与 `wal_status` 各有含义，要分清：

- `restart_lsn`：这个槽还可能需要的最旧 WAL 位置，checkpoint 时不会删该位置之后的文件。
- `wal_status = reserved`：占用还在 `max_wal_size` 范围内。
- `wal_status = extended`：已超出 `max_wal_size`，但文件仍被保留（可能是槽或 `wal_keep_size` 拦着）。
- `wal_status = unreserved`：槽已不再保留所需文件，下一次 checkpoint 就要删，通常发生在设置了非负的 `max_slot_wal_keep_size` 时。这个状态还能回到 reserved 或 extended。
- `wal_status = lost`：槽已不可用，备库必须重做基础备份。

**为什么不能直接删 `pg_wal` 目录里的文件**，这是本案例最需要讲清的纪律：

1. `pg_wal` 里的段不是"过期的缓存"，它们是主库自身崩溃恢复的一部分。手工删掉最近的段，一旦主库崩溃，恢复会因缺失 WAL 而失败。
2. 对应槽的元数据仍然指向那个旧 LSN。你删了文件，等 `pg-standby-c` 回来，它照样会因为"requested WAL segment ... has already been removed"连不上，最终还是重做——删文件只是把问题从"占盘"变成"更早占盘且无法解释"。
3. 目录里可能有正在被发送的段，删除会直接打断在线的 `pg-standby-a`、`pg-standby-b`。

所以正确对象不是文件，是**槽**。

## 结论与处置

根因：`pg-standby-c` 下线维护时没有清理它对应的物理复制槽 `standby_c_slot`，槽保持 inactive，`restart_lsn` 不推进，主库无法回收该位置之后的 WAL，`pg_wal` 持续膨胀直至撑满磁盘。

处置动作：

- 确认 `pg-standby-c` 确定不会原样恢复（硬件维护后计划重建）后，在槽的 `active = f` 前提下删除它：

```sql
SELECT pg_drop_replication_slot('standby_c_slot');
CHECKPOINT;
```

- 不要用 `rm` 处理 `pg_wal`。删除槽之后由 checkpoint 自己回收。
- 加兜底参数：`max_slot_wal_keep_size = '10GB'`。它把单个槽能保留的 WAL 上限住，超限后槽会进入 `unreserved` 乃至 `lost`，用"这个备库需要重建"的代价换"主库不会因为一个槽而整库宕机"。默认值 `-1`（不限）在无人值守的机器上是危险的。

验证指标：删除槽并 checkpoint 后 `pg_wal` 目录体积回落，`df` 使用率降到安全水位；`pg_replication_slots` 里不再有 inactive 的旧槽；`pg_standby-a / pg_standby-b` 的 `state` 保持 `streaming`、`replay_lag` 正常。若将来 `pg-standby-c` 重建，走新的基础备份流程重新建槽。

## 复盘要点

- **槽是"防漏"机制，也会变成"自伤"机制**。它在保护复制的同时，天然会把 WAL 留住；没人推进的槽就是一颗定时炸弹。
- **失效备库下线前必须处理它的槽**。把"下线备库时删槽"写进运维清单，比事后救火便宜得多。
- **别手工删 `pg_wal`**。那是崩溃恢复依赖的数据，删了会把磁盘问题升级成数据恢复问题。
- **一定要设 `max_slot_wal_keep_size`**。用"最坏情况重做某个备库"换"主库不会因为一个槽写不进去"。
- **下次怎么提前发现**：对 `pg_replication_slots` 里 `active = f` 且 `restart_lsn` 长时间不前进的槽单独告警，并对 `pg_wal` 增长速度设斜率告警，在撑满磁盘之前就发现是哪个槽拖住了回收。
- **物理复制槽与逻辑复制槽用途不同**。物理槽服务于字节级流复制，逻辑槽服务于逻辑解码/逻辑复制，二者的 `wal_status`、`restart_lsn` 解读一致，但淘汰逻辑不能混用。
