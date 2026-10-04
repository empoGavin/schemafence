> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# autovacuum 调优：它为什么跟不上，以及怎么监控

## autovacuum 跟不上的四个原因

1. **长事务**：任何持有旧快照的事务都会挡住死元组回收
2. **复制槽失效**：槽没推进，WAL 和清理都卡住
3. **阈值太大**：默认按表的 20% + 50 行触发，亿级大表要等到两千万行死元组才动手
4. **成本限制太保守**：`autovacuum_vacuum_cost_limit` 默认 200，跑得很慢

## 关键参数与经验值

- `autovacuum_vacuum_scale_factor`：大表建议 0.05，超大表用表级参数单独设
- `autovacuum_vacuum_insert_scale_factor`：只增不改的表也要清理，PG13+ 才有效
- `autovacuum_max_workers`：默认 3，表多的时候可以提到 5–6
- 表级覆盖优先于全局：

```sql
ALTER TABLE orders SET (autovacuum_vacuum_scale_factor = 0.02,
                        autovacuum_vacuum_threshold  = 1000);
```

## 监控要看的三处

```sql
-- 死元组与上次清理时间
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
  FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;

-- 正在跑的清理进度
SELECT * FROM pg_stat_progress_vacuum;

-- 清理进程有没有被阻塞
SELECT pid, wait_event_type, wait_event, query
  FROM pg_stat_activity WHERE backend_type = 'autovacuum worker';
```

死元组占比长期超过 20%，说明清理跟不上写入速度，不要只看绝对数量。

## 长事务与复制槽：最常见的两个阻塞源

```sql
-- 谁把 xmin 钉住了
SELECT pid, backend_xmin, now() - xact_start AS age, state, query
  FROM pg_stat_activity WHERE backend_xmin IS NOT NULL ORDER BY age DESC;

-- 失效的复制槽会一直压着 WAL
SELECT slot_name, active, restart_lsn FROM pg_replication_slots;
```

建议同时设 `idle_in_transaction_session_timeout` 和 `max_slot_wal_keep_size`，
把"忘记关事务"和"忘记删槽"这两类人为故障挡在爆发之前。

## 什么时候才该上 VACUUM FULL / pg_repack

膨胀率（表实际大小 / 有效数据估算大小）超过 50%，且已经被空间或扫描放大困扰时。
`VACUUM FULL` 会持有 ACCESS EXCLUSIVE 锁，业务高峰不能做；
`pg_repack` 可以在线整理，但需要额外磁盘和主键。

## 应急处理顺序

1. 先看是不是长事务或复制槽卡住，是就先解阻塞，否则清理再快也没用
2. 放开成本限制，让清理先跑起来：临时提高 cost_limit 并观察 IO
3. 仍不收敛再考虑在线整理，并安排到低峰窗口
4. 事后把触发阈值按表调到合理值，避免下次再犯
