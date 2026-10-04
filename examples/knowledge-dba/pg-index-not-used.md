> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 索引建了却不走：六种常见原因与验证手段

## 第一步永远是看执行计划，别猜

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT ... ;
```

只看两件事：

- 走的是 Seq Scan 还是 Index Scan / Bitmap Heap Scan
- `rows` 估算值和 `actual rows` 差多少倍。差 10 倍以上，问题通常在统计信息，不在索引

## 原因一：列上套了函数或发生了隐式转换

`WHERE to_char(created_at,'YYYY-MM-DD') = '2026-01-01'` 走不了索引，
要改写成范围条件 `created_at >= '2026-01-01' AND created_at < '2026-01-02'`，
或者建表达式索引 `ON t (to_char(created_at,'YYYY-MM-DD'))`。

更隐蔽的是类型不匹配：`varchar` 列和数字比较、`int8` 列和 `int4` 参数比较，
在部分场景会退化成逐行转换。确认手段是看计划里的 Filter 是否出现在索引条件之外。

## 原因二：统计信息过期或采样不足

表刚批量导入、或者数据分布极度倾斜时，planner 会按错误的基数估算选错计划。
先 `ANALYZE 表名` 重算；如果单列统计不够（例如城市和订单量强相关），
用扩展统计信息：

```sql
CREATE STATISTICS s_city_order (dependencies, ndistinct) ON city, order_type FROM orders;
ANALYZE orders;
```

## 原因三：选择性太差，顺序扫描本来就更快

返回 30% 以上的行时，全表扫描比走索引再回表更便宜，这是 planner 对的判断，不是 bug。
如果确实是 OLAP 型查询，该考虑的是预聚合表或者覆盖索引，而不是逼它走索引。
SSD 上可以把 `random_page_cost` 从默认 4.0 调到 1.1 左右，让成本模型更贴近现实。

## 原因四：OR 条件与联合索引顺序不匹配

`WHERE a = 1 OR b = 2` 在没有对应联合索引时基本都是全表扫描。
可改写为 `UNION ALL` 两条走各自索引的查询，或者在 PG 里用位图 OR 期待 planner 自己合并。
联合索引的列序也要对齐最左前缀，`(a, b)` 支撑不了只带 `b` 的查询。

## 原因五：部分索引与失效索引

部分索引（`WHERE status = 'active'`）只在条件能被静态推导出来时才会用。
如果查询条件写成 `status IN ('active','pending')`，就可能用不上。
另外要定期确认索引是否被标记为 invalid（并发建索引失败会留下 invalid 索引）：

```sql
SELECT indexrelid::regclass, indrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

## 原因六：参数化查询走了通用计划

同一个 SQL 用不同参数反复执行时，PG 可能从自定义计划切到通用计划，
而通用计划对某些参数值恰好很糟。表现为"同一条 SQL 有时快有时慢"。
可用 `PREPARE` + 不同参数实测对比，必要时在连接池层面对该语句做计划固定。

## 一张排查清单

1. 看计划：Scan 类型、rows 估算 vs 实际
2. 看统计信息时间：`pg_stat_user_tables.last_analyze`
3. 看列上有没有函数/转换
4. 看选择性：返回行数占比
5. 看索引是否有效、是否匹配最左前缀
6. 看参数化计划是否漂移
