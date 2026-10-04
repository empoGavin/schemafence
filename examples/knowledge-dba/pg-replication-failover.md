> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 流复制、复制延迟与一次切换演练

## 复制的基本形态

- **物理流复制**：整库字节级复制，最简单可靠，主备必须同大版本
- **同步复制**：`synchronous_commit = on` 配合 `synchronous_standby_names`，
  写入要等备库确认，RPO 接近 0，代价是主库延迟受备库影响
- **逻辑复制**：表级、可跨版本、可双向，但 DDL 不复制，需要额外维护

选择原则：要"不丢数据"就用同步复制配法定人数（`ANY 1 (a, b)`），
不要用单点同步，否则备库抖动会直接拖慢主库写入。

## 延迟怎么量才准

```sql
SELECT client_addr, state, sync_state,
       write_lag, flush_lag, replay_lag
  FROM pg_stat_replication;
```

`write_lag` 和 `flush_lag` 说明网络和磁盘的传输情况，
**`replay_lag` 才是用户真正能看到的延迟**——备库还没重放的 WAL，查询就看不到。
只盯一个指标容易误判。

## 复制槽：不能删，但会吃盘

```sql
SELECT slot_name, active, restart_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots;
```

槽不推进会让 WAL 无限堆积，最终撑爆磁盘，这是复制架构里最常见的自伤事故。
务必备好 `max_slot_wal_keep_size`，让失控的槽有上限而不是拖着整个实例一起死。

## 切换清单（演练用）

1. 确认备库延迟已经收敛（replay_lag 接近 0）
2. 切断应用写入（改连接串或摘掉 VIP / DNS）
3. 提升备库：`pg_ctl promote`，确认进入可写状态
4. 校验：序列当前值、扩展版本、只读视图、复制槽
5. 切换应用连接，观察错误率与延迟
6. 原主修复后以新备库身份重建，重建前先确认不再有旧写入

## 拆脑裂：为什么必须先隔离旧主

如果旧主还能接受写入，两个节点各自产生 WAL，数据就分叉了，
事后合并的代价远高于停机。所以切换流程的第一步不是"提升备库"，
而是**确保旧主不会再被写入**。顺序错了，演练就变成了事故。

## 演练的价值

备份和备用节点的可信度只有一个来源：实际切过一次。
建议每季度做一次完整的切换演练，包括应用侧连接切换，并把耗时记录下来作为 RTO 依据。
