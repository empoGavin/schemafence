# 读懂 PostgreSQL 执行计划

## 三个必须先看的东西

1. **估算行数 vs 实际行数**：`EXPLAIN ANALYZE` 里 `rows=` 与 `actual rows=` 差一个数量级以上，
   说明统计信息失真，优化器在这张表上的所有选择都不可信
2. **扫描方式**：`Seq Scan` 不一定是坏事——取回比例超过约 5%–10% 时，顺序扫描比索引扫描更划算
3. **连接方式**：`Nested Loop` 适合小结果集，`Hash Join` 适合大表，`Merge Join` 适合已排序输入。
   选错通常意味着估算行数错了，而不是优化器笨

## 为什么要及时 ANALYZE

优化器只有统计信息这一个信息来源。`pg_statistic` 里的直方图、`n_distinct`、`null_frac`
都是 ANALYZE 采样的结果，采样率默认只有 30000 行（`default_statistics_target`）。

数据分布倾斜的列（比如"状态"这种只有几个值、但某个值占 90% 的列）需要更高的
statistics target，否则优化器会按平均值估算，选出完全错误的计划。

## 常见误判

- 看到 `Seq Scan` 就加索引：小表上这是负优化
- 只看总耗时数字，不看 `actual time` 的层级结构——真正的瓶颈往往在最内层节点
- 忽略 `rows removed by filter`：过滤掉 99% 的行说明索引选错了列，或者 WHERE 条件写法让索引失效
- 在测试库调优、到生产库失效：数据分布不一样，统计信息也不一样

## 与向量检索的关系

pgvector 的 HNSW 索引同样依赖 `ORDER BY embedding <=> query LIMIT k` 这种"索引即排序"的写法。
把 `<=>` 包在函数里、或者加上额外的过滤条件再排序，都可能让 HNSW 索引失效退化成顺序扫描——
这和关系型索引失效的机理是同一件事。
