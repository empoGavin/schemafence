> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 备库查询被反复取消：到底是延迟清理还是查询太贪心

## 现象

某订单库 `orders_prod`（虚构）的只读备库 `pg-standby-a` 从上午 10:00 开始，报表平台的查询频繁报错。报错原文（虚构标识符）：

```
ERROR:  canceling statement due to conflict with recovery
DETAIL:  User query might have needed to see row versions that must be removed.
```

受影响的都是跑得较久的聚合查询，短的查询基本没事。架构是主库 `pg-primary-01` 加两个物理流复制备库，`pg-standby-a` 专供报表只读，`pg-standby-b` 只做高可用、不接业务查询。同一时刻主库写入正常，复制延迟 `replay_lag` 只有几百毫秒——也就是说，**这不是延迟问题，是冲突问题**。

## 先收集什么证据

第一步分清楚报错来源。`canceling statement due to conflict with recovery` 是恢复冲突取消，和 `statement_timeout` 超时、和锁等待超时是三种完全不同的东西，别混。

备库侧抓冲突统计（这个视图只存在于备库）：

```sql
SELECT datname, confl_tablespace, confl_lock, confl_snapshot,
       confl_bufferpin, confl_deadlock, confl_active_logicalslot
  FROM pg_stat_database_conflicts;
```

再看两个决定行为的参数现值：

```sql
SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('max_standby_streaming_delay','max_standby_archive_delay',
                'hot_standby_feedback','hot_standby','log_recovery_conflict_waits');
```

`log_recovery_conflict_waits`（默认 `off`）打开后，重放因冲突等待超过 `deadlock_timeout` 时会记日志，是判断"到底在等什么"的直接证据。主库侧看一眼备库反馈的 xmin 水位：

```sql
SELECT application_name, backend_xmin, state, sync_state
  FROM pg_stat_replication;
```

OS 层顺带确认备库 IO 正常（`iostat`、`vmstat`），避免把回放慢误算进来。

## 定位过程

第一步排除超时类误判：日志文本明确是 `conflict with recovery`，不是 `statement timeout`，所以不是查询自己被设了超时。

第二步看冲突类型。`pg_stat_database_conflicts` 里 `confl_snapshot` 一枝独秀地增长，其余 `confl_lock`、`confl_bufferpin`、`confl_deadlock` 几乎为零。`confl_snapshot` 对应的是"旧快照"冲突——备库上的查询所用快照，要比主库当前的状态旧，而主库的清理动作正准备删掉它还需要看的行版本。这就把方向锁定到**"早清理"（early cleanup）**：主库按 MVCC 规则清理老行版本时，只考虑主库自己有没有事务需要它们，考虑不到备库上还在跑的查询。

网络和备库 IO 这两条假设在这里被一起排除：`replay_lag` 只有几百毫秒、`iostat` 正常，延迟并不高，说明冲突不是因为备库落后太多，而是因为主库清理动作与备库快照的时序撞上了。冲突集中在频繁 `UPDATE / DELETE` 的订单状态表，也印证了这一点——**是主库清理得勤，不是备库跑得慢本身**。但要补一句：长查询放大了冲突窗口，同样一张热表上，跑 5 秒的查询大多能躲过去，跑 5 分钟的才反复中招。

再看参数现场：`max_standby_streaming_delay` 是默认的 30 秒，`hot_standby_feedback` 是 `off`。这就解释了行为——WAL 重放要推进时，如果冲突查询已经拖了超过 30 秒，重放不再等，直接取消查询。

**这里是本案例最反直觉的一处**：很自然的想法是"把 `max_standby_streaming_delay` 设成 `-1`，让备库无限等，不就再也不会取消查询了吗"。**这个做法会打开另一个更糟的口子**。参数的本意不是"查询最多能跑多久"，而是"允许把重放推迟多久"——它是从 WAL 数据被收到那一刻开始算的一个总预算。设成 `-1`，重放会无期限地为一个慢查询让路，结果就是备库迟迟追不上主库，延迟越滚越大；预算被一个查询吃掉后，紧接着到达的 WAL 里所有其他查询的宽限时间都会骤减，反而牵连更多查询被取消。延迟清理和查询稳定性是一对**此消彼长**的量，把一头推到极限，另一头必然恶化，不存在"两个都拉满"的配置。

同时也排除"直接开 `hot_standby_feedback = on` 就万事大吉"的简化想法。它确实能消除这类 conflict——主库据此知道备库还需要哪些老行版本，就不会把它们清掉——但它有代价：主库的老行版本被推迟清理，表会膨胀，autovacuum 回收效率下降。如果备库上还挂着长事务或长查询，`backend_xmin` 会一直压着主库的清理水位，膨胀会持续累积，进而让主库 WAL 变多，反过来又给复制加压。所以它是一个要配着监控用的取舍，不是一键开关。

## 结论与处置

根因：主库对热表做高频更新、并伴随 vacuum 早清理，而备库上运行的长只读查询所需快照与之冲突；默认的 `max_standby_streaming_delay = 30s` 到点后即取消冲突查询。

处置动作（组合取舍，不是单点开关）：

- `hot_standby_feedback = on`：消除因 `UPDATE / DELETE` 清理引起的冲突，代价是监控主库膨胀。
- `max_standby_streaming_delay` 设为一个**有限但宽松**的值（例如 5 分钟），而不是 `-1`；既给长查询更多宽限，又不让重放无限期被拖住。
- 把超长报表迁到用**逻辑复制**搭建的专用只读库：订阅端是一个独立的可写实例，按表复制，不会因为"恢复冲突"取消查询；代价是逻辑复制不复制 DDL、初始同步要单独处理。
- 优化报表本身，减少全表扫描与超长事务，必要时给只读查询设合理的服务端超时，让"跑不完"尽早失败而不是拖到冲突。
- 对会产生大量行版本清理的批量更新，尽量放到低峰，并观察主库 `n_dead_tup`。

验证指标：`pg_stat_database_conflicts.confl_snapshot` 不再持续增长；报表长查询成功率上升；同时主库的热表 `n_dead_tup` 与表膨胀率保持在可接受范围，`replay_lag` 未因放宽 `max_standby_streaming_delay` 而恶化。

## 复盘要点

- **先分清是"取消"还是"超时"**。`conflict with recovery` 与 `statement_timeout` 是两条完全不同的排查线。
- **恢复冲突只在物理备库存在**。逻辑复制的订阅端不会有 recovery conflict，这是"要跑长查询的只读库"值得考虑逻辑复制的一条实质理由。
- **`max_standby_streaming_delay = -1` 不是灵丹**。它是"允许推迟重放多久"的预算，设成无限会让备库追不上主库，属于拆东墙补西墙。
- **`hot_standby_feedback` 是取舍**：用主库膨胀换备库查询稳定，且备库断开期间失去保护。
- **下次怎么提前发现**：同时监控备库 `confl_snapshot` 的增速和主库热表的 `n_dead_tup`，把"要跑长查询的库是否配了 `hot_standby_feedback`"作为上线检查项。
