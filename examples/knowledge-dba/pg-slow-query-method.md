> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 慢查询定位：从现象到 SQL 原文

## 别从日志一条条翻，先做聚合

日志里有的是"哪条 SQL 慢了一次"，真正要治理的是"哪条 SQL 累计吃掉了最多时间"。
这两个答案经常不是同一条 SQL。用归一化统计来找：

```sql
SELECT calls,
       round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 1)  AS mean_ms,
       round(100 * total_exec_time / sum(total_exec_time) OVER (), 1) AS pct,
       left(query, 80) AS query
  FROM pg_stat_statements
 ORDER BY total_exec_time DESC LIMIT 20;
```

## 该按什么排序：累计耗时还是平均耗时

两个视角都要看：

- 按 `total_exec_time` 排序 → 找"慢而频繁"的，治理收益最大
- 按 `mean_exec_time` 排序 → 找"偶发但极慢"的，通常是报表或批处理

只按其中一个排序都会漏掉另一半问题：只看平均耗时，会漏掉那条每秒跑一次、
每次 30ms 但累计吃掉一半 CPU 的语句；只看累计耗时，会漏掉每晚跑一次、
但把库拖住 20 分钟的报表。

## 采样细节：auto_explain 补上缺失的上下文

`pg_stat_statements` 只给统计，不给计划。用 auto_explain 采样记录计划，
注意只对超过阈值的语句生效，避免日志被淹没：

```sql
LOAD 'auto_explain';
SET auto_explain.log_min_duration = '500ms';
SET auto_explain.log_analyze = on;
SET auto_explain.log_buffers = on;
```

## 从统计回到可复现的计划

统计里的 SQL 是归一化后的（参数被替换成 `$1`），直接执行未必重现问题。
要拿到真实参数值，可以从 `pg_stat_statements` 的 `queryid` 关联日志，
或者用 `EXPLAIN (ANALYZE, BUFFERS)` 把几个典型参数值逐个试。
注意：不同参数值会得到不同计划，**复现时参数必须带上**。

## 慢查询的四大类根因

1. **缺索引 / 索引不匹配**：扫描行数远超返回行数
2. **基数估算偏差**：计划里的 rows 与实际差十倍以上
3. **锁等待或 IO 等待**：SQL 本身不慢，是在排队；看 `wait_event`
4. **资源抖动**：checkpoint 集中、IO 打满、内存不足导致的偶发慢

前三类看计划和等待事件就能区分，第四类要看时间维度上的相关性。

## 一个合成案例：月末报表拖垮库

某零售订单系统（虚构）在月末出现整体响应变慢。
用 `pg_stat_statements` 排序后发现一条按租户聚合的报表 SQL 占总执行时间的 40%，
平均 12 秒、每天执行 300 次。计划显示对 3 亿行的订单表做并行顺序扫描，
缺少 `(tenant_id, created_at)` 复合索引。

处置：补复合索引 → 该 SQL 平均耗时降到 180ms；
同时把报表类查询迁到只读副本，避免与在线业务抢 IO。

## 输出一份能复盘的清单

每次定位完，留下四样东西：SQL 原文与参数、执行计划、当时的关键指标
（命中率、等待事件、并发数）、以及修改前后的耗时对比。
没有这四样，三个月后同样的慢查询会再来一次，而你已经不记得上次怎么解决的。
