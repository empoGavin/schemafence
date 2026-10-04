> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 表膨胀导致的顺序扫描误诊：看着像缺索引，其实是死元组

## 一次典型的误诊路径

虚构场景：订单表 `orders` 查询突然变慢，`EXPLAIN` 显示 `Seq Scan`，
开发的第一反应是"加个 status 索引"。但翻 `pg_stat_user_tables` 发现：

- 表 300 MB，`n_live_tup` 只有 1 万
- `n_dead_tup` 是活元组的 9 倍（死元组占比 90%）
- `last_autovacuum` 是 `never`

结论：不是缺索引，是膨胀。表里 90% 的页面是死元组，
顺序扫描在读大量根本不该存在的行，加了索引也只是把膨胀复制一份。

## 判定标准：三个数字一起看

```sql
SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid))  AS size,
       n_live_tup, n_dead_tup,
       round(n_dead_tup::numeric / nullif(n_live_tup + n_dead_tup, 0), 2) AS dead_ratio,
       last_autovacuum
  FROM pg_stat_user_tables
 ORDER BY dead_ratio DESC NULLS LAST;
```

- **dead_ratio 长期 > 20%**：清理跟不上写入，先查原因再动手
- **表大小与活元组数明显不成比例**（每活行几百 KB 以上）：膨胀已实质化
- **last_autovacuum 为 never 或很久以前**：autovacuum 可能被长事务或失效复制槽挡住

注意：`reltuples`（执行计划里的估算行数）在 ANALYZE 之前不会更新，
膨胀表的估算行数与实际严重偏离，会连带把 JOIN 顺序也带偏。

## 处理顺序

1. **普通 `VACUUM (VERBOSE, ANALYZE) 表名`**：回收可复用空间、更新统计信息，
   不锁写、可业务时间执行。先做这个，再重新 `EXPLAIN` 对比。
2. **膨胀率超 50% 且空间必须还给操作系统**时才考虑：
   - `VACUUM FULL`：持有 **ACCESS EXCLUSIVE 锁**，读写全停，业务高峰禁止
   - `pg_repack`：在线整理，但需要一份额外的磁盘空间和表上的主键
3. **治本**：给这张表设更激进的表级 autovacuum 参数
   （见 autovacuum 调优笔记），并排查长事务与失效复制槽。

## 经验教训

- 看到 Seq Scan 先查膨胀再谈索引：在 90% 死元组的表上建索引，
  索引本身也会膨胀，查询大概率还是慢
- 查询慢 + `idx_scan = 0` + 死元组占比高，三者的解释是同一个：
  表的物理布局坏了，不是访问路径选错了
- VACUUM 之后必须重新 ANALYZE 并复查计划，否则优化器还在用旧统计
