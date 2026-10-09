> 语料来源：公开 PostgreSQL 通用知识整理 + 虚构案例，不含任何公司内部信息。

# 复制故障诊断流程：先分清"数据还在不在路上"

## 总入口：三个视图定位故障在链路的哪一段

复制出问题，第一件事不是去看延迟数字，而是**确认链路各段是否还活着**。一条流复制链路可以拆成四段：主库 walsender 生成/发送、网络传输、备库 walreceiver 接收落盘、备库 startup 回放。

```sql
-- 主库：发送端状态
SELECT application_name, client_addr, state, sync_state,
       sent_lsn, write_lsn, flush_lsn, replay_lsn,
       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS total_lag_bytes,
       pg_wal_lsn_diff(sent_lsn, flush_lsn)  AS net_lag_bytes
  FROM pg_stat_replication;

-- 备库：接收端状态
SELECT status, receive_start_lsn, received_lsn, latest_end_lsn,
       last_msg_send_time, last_msg_receipt_time
  FROM pg_stat_wal_receiver;
```

按结果分四类，后续处理完全不同：

| 观察 | 含义 | 走哪条 |
|---|---|---|
| `pg_stat_replication` 里没有该备库 | 连接断了 | 路 B |
| 有备库，但 `replay_lsn` 长期不推进 | 数据到了、回放不动 | 路 A（回放段） |
| 四个 LSN 差距持续扩大 | 发送或传输跟不上 | 路 A（发送段） |
| 备库查询报 `canceling statement due to conflict` | 冲突取消 | 路 C |
| 需要切换或已切过 | 高可用动作 | 路 D |

**关键判读**：`sent_lsn - replay_lsn` 是端到端总滞后，`sent_lsn - flush_lsn` 是网络与接收段的滞后。两个差值分开看，才能知道**是发不出去，还是回放不动**——只看总延迟会在这两者之间反复猜。

## 路 A：延迟持续升高——三段差值定位法

### 现象收集
延迟是缓慢爬升还是阶跃跳变？是全天持续还是集中在某个时间窗？业务侧是否已经开始读不到刚写入的数据？**阶跃跳变通常对应一个具体事件**（大批量操作、DDL、网络抖动），缓慢爬升通常对应持续性的写入量增长或备库资源不足。

### 日志与指标收集
```bash
# 备库端
tail -100 $PGDATA/log/postgresql-*.log
iostat -x 5 5                     # 备库的写盘能力和 util
dmesg -T | grep -i "blocked for more than"
```
```sql
-- 备库回放进度与冲突统计
SELECT * FROM pg_stat_replication;         -- 在备库上不可用，用下面的
SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn(),
       pg_last_xact_replay_timestamp(), now() - pg_last_xact_replay_timestamp() AS lag;
```

### 分层定位
- **`sent_lsn - flush_lsn` 大** → 发送段或网络段慢。看主库 walsender 进程的 CPU、网络带宽是否打满、是否有 `TCP window` 相关重传。**主库生成的 WAL 量超过链路带宽时，延迟会无上限增长**，此时优化备库无用。
- **`flush_lsn - replay_lsn` 大** → 备库收到了但落盘慢，查备库磁盘写能力。
- **`sent_lsn` 与 `replay_lsn` 齐头并进但都远离 `pg_current_wal_lsn()`** → 主库写入太快，是**源头问题**。这时候要看的是主库这一小时的 WAL 生成量，而不是备库：
```sql
SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') AS total_wal_bytes;
```
- **`replay_lsn` 长时间完全不动，延迟却不再增长** → 大概率不是卡死，而是**正在原子地回放一个巨大的事务**。流复制下，一个事务在备库是整体生效的，一个千万行的批量操作会让回放进度条看起来很"死"。用 `pg_stat_activity` 在备库上看到 `startup` 进程处于活动状态即可确认。
- **长事务 / 长 DDL 放大延迟** → 主库上跑了一个几小时的事务，它提交前备库什么都看不到（除 `hot_standby_feedback` 相关机制外），提交后又要一口气全部回放。

### 知识库对照
用"复制延迟""备库回放慢""WAL 生成量"等描述检索，对照同类案例的分段判据。

### 结论
结论必须是**段级**的："延迟在回放段（收到 4GB 未回放），根因是备库磁盘写能力不足"，而不是"复制延迟高"。

## 路 B：复制断了——先判断能不能续，再决定重建

### 现象收集
`pg_stat_replication` 里备库消失的时间点、当时主备两侧是否有重启或网络变更。

### 日志收集
```bash
# 主库
grep -E "replication|walsender|terminating connection" $PGDATA/log/postgresql-*.log | tail -30
# 备库
grep -E "walreceiver|recovery|fatal|requested WAL segment" $PGDATA/log/postgresql-*.log | tail -30
```

### 分层定位
备库日志里 `requested WAL segment ... has already been removed` 或 `could not receive data from WAL stream` 是关键分水岭：

- **所需 WAL 还在** → 网络或认证导致的临时断开，重启 walreceiver（重启备库服务或 `SELECT pg_wal_replay_resume();` 视情况）通常能自动接上。
- **所需 WAL 已被删除**（归档中没有、复制槽也回不去）→ **必须重做基础备份**。此时任何"手工接一下"的尝试都是在制造数据不一致，不要做。
- **`pg_replication_slots` 里槽的 `wal_status` 变成 `lost`** → 该槽已无法提供所需 WAL，等同上面一条。

复制的连接认证失败（`pg_hba.conf` 未放行复制连接、密码错）在日志里表现为 walreceiver 反复重连失败，**不会自动恢复**，属于配置问题而非故障。

### 结论
输出"能否续接"的明确判断，以及依据（WAL 是否可得 / 槽状态 / timeline 是否同分叉）。若需重建，给出重做流程与验证点。

## 路 C：备库查询被冲突取消

### 现象
只读查询在备库上偶发报错 `canceling statement due to conflict with recovery`，查询时间越长越容易中招。

### 定位
本质是 MVCC 冲突：备库上的长查询持有快照，而主库传来的 vacuum 记录想清理它还需要看的行版本。两者不可兼得。

```sql
SELECT * FROM pg_stat_database_conflicts;   -- 按冲突类型计数
SHOW max_standby_streaming_delay;
SHOW max_standby_archive_delay;
SHOW hot_standby_feedback;
```

- `max_standby_streaming_delay` 调大 → 查询更稳，但延迟增长（回放被推迟）。
- `hot_standby_feedback = on` → 主库会顾及备库快照，减少冲突，但**代价是主库的清理被推迟，表膨胀风险回到主库**。

**延迟清理与查询稳定性此消彼长，没有免费的两全。** 先把长时间运行的只读查询本身管住（限时、错峰），再谈参数。

## 路 D：切换与故障转移

切换的**第一步是隔离旧主，不是提升新主**。顺序颠倒会让旧主在恢复后继续接受写入，形成两个分叉历史。

```sql
SELECT pg_is_in_recovery();          -- 确认当前角色
SELECT pg_promote(wait => true);     -- 提升备库
```

判断新旧主的唯一权威是 timeline：`pg_controldata` 里的 `Latest checkpoint's TimeLineID`。**切换后旧主要么 `pg_rewind` 追上，要么从新主重做基备**；直接让旧主起来继续服务，就是双主。

## 全流程的共同纪律

1. **判断先于动作**：不确认段级位置，不改任何参数。
2. **WAL 不可再生**：任何要删 `pg_wal`、改 timeline、手工伪造 LSN 的动作，先问"能不能回退"。
3. **切换必须演练过**：没演练过的切换预案，在真故障时的成功率远低于预期；演练的价值在于把"应该能"变成"验证过"。
