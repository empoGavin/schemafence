> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 计划器与代价模型：估算从哪来、join 怎么选、EXPLAIN 怎么读

## 代价是一套"相对单位"，锚点是顺序读一页

计划器比较不同执行路径时用的是代价，而代价是**无量纲的相对值**。官方的约定是把顺序读一页的成本记为 1.0（即 `seq_page_cost = 1.0`），其它成本都相对它来设。所以把所有代价值按同一比例放大或缩小，计划器做出的选择不变。

几个关键常数的默认值：

- `seq_page_cost = 1.0`：顺序读取一页
- `random_page_cost = 4.0`：非顺序（随机）读取一页
- `cpu_tuple_cost = 0.01`：处理每一行的成本
- `cpu_index_tuple_cost = 0.005`：处理每一条索引项的成本
- `cpu_operator_cost = 0.0025`：执行每个运算符或函数的成本
- `effective_cache_size = 4GB`：计划器对"单个查询可用磁盘缓存"的假设
- `parallel_setup_cost = 1000`、`parallel_tuple_cost = 0.1`：并行相关的起步与传递成本

调节方向很明确：**调低 `random_page_cost`（相对 `seq_page_cost`）会让计划器更偏好索引扫描，调高则相反**。如果数据基本都在内存里，把这两个值一起调低（相对 CPU 类参数）更合理；如果存储的随机读代价确实高，可以调高 `random_page_cost`。但要记住，官方明确说这些值应当视为整个工作负载的**平均值**，只凭几次实验就改是很危险的。

`effective_cache_size` 只影响估算、不分配任何内存。调大它会让索引扫描看起来更便宜，调小则更容易走顺序扫描。

## 行数估算是从哪来的：pg_class 加 pg_statistic

计划器要算成本，先得知道每张表有多少行、多少页。这个来自 `pg_class` 的 `reltuples` 和 `relpages`：

```sql
SELECT relname, relkind, reltuples, relpages
  FROM pg_class WHERE relname LIKE 'orders_2024%';
```

注意 `reltuples` 和 `relpages` **不是实时更新的**，它们由 VACUUM、ANALYZE 和 CREATE INDEX 一类操作更新；而且一次不扫描全表的 VACUUM/ANALYZE 只会按扫到的部分做增量估算。所以这两个数通常偏旧，计划器还会按当前物理表大小做缩放来补偿。

WHERE 条件能过滤掉多少行，靠的是 `pg_statistic`（更易读的视图是 `pg_stats`）里的选择性统计，比如 `n_distinct`、`most_common_vals`、`histogram_bounds`。这些统计**永远是近似的**，即便刚 ANALYZE 完也一样。

## 三种 join 方式怎么选

- 嵌套循环（nested loop）：对左表的每一行，去扫一次右表。实现简单，但代价随行数增长快。**如果右表能用索引扫，它就很有优势**——可以用左表当前行的值当索引键。
- 归并连接（merge join）：两边先按连接键排序，再并行扫描合并。每个关系只需扫一次，所以大表之间的连接常常划算；排序可以由显式 Sort 完成，也可以靠索引顺序省掉。
- 哈希连接（hash join）：先把右表装进哈希表，再用左表逐行探测。

选哪个，取决于计划器算出的行数估算和可用索引。**换句话说是：join 方式的"选择错误"往往根因是行数估算先错了。**

## join 顺序与 GEQO

两表以上连接时，最终结果由一棵 join 树拼出来，计划器要挑成本最低的顺序。连接数少于 `geqo_threshold`（默认 12）时，它做近似穷举；达到或超过阈值时，改用遗传算法 GEQO（`geqo` 默认 `on`）做启发式搜索，用"可能略差的计划"换"可以接受的计划时间"。

另外两个相关参数是 `from_collapse_limit` 和 `join_collapse_limit`，默认都是 8：它们决定计划器能否把子查询或显式 JOIN 折叠成更长的关系列表。值设得接近或高于 `geqo_threshold`，就可能触发 GEQO，产出非最优计划。

## EXPLAIN 的字段怎么读

未加 ANALYZE 时，EXPLAIN 给出的是估算：`(cost=startup..total rows=N width=W)`。四个数的含义分别是：**启动成本**（输出第一行之前要花的）、**总成本**（跑完整个节点的估算，且包含所有子节点的成本）、**本节点输出的行数**、**输出行的平均宽度（字节）**。

一个可以手算的例子：`Seq Scan on tenk1 (cost=0.00..458.00 rows=10000 width=244)`，因为该表有 358 页、10000 行，成本就是 `358 × seq_page_cost + 10000 × cpu_tuple_cost = 358 × 1.0 + 10000 × 0.01 = 458`。

这里有个特别容易读错的点：**`rows` 是这个节点"输出"的行数，不是它"扫描"的行数**。加了 WHERE 后，即使输出行数被估算降低，顺序扫描的成本也不降——因为它仍要读完全部行，还多了检查条件的 CPU 成本。计划里显示为 `Filter:` 的条件就是这样：它只减少输出，不减少扫描。

加上 ANALYZE 后，会多出 `(actual rows=... loops=...)`。这里的 `actual rows` 是**每个循环的平均值**，真正的总行数是 `actual rows × loops`。把估算的 `rows` 和它对比，是判断估算准不准最直接的方法。

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT tenant_id, sum(amount) FROM orders_2024
 WHERE created_at >= now() - interval '1 day' GROUP BY tenant_id;
```

## 落到可观察：估算偏差怎么判别

判别方法只有一条主线：**逐层比较估算 `rows` 与实际 `rows × loops`**。如果某一层差了一到两个数量级，就往它下面找根因：是统计过期、是多列相关性没被捕获、还是条件用了计划器无法估算的表达式。不要一上来就动代价常数。

## 核心判断：先改统计，再改 SQL，最后才碰代价常数

遇到"计划选错了"，最没风险的排查顺序是：先确认统计是否新鲜（对比 `rows` 估算与实际），再看是不是多列相关导致（见统计那一篇），然后才考虑改写 SQL 让条件可估算。**`seq_page_cost` 基本不要动**，它是整套相对代价的锚点，动它会让所有估算整体漂移；真要调，也只调 `random_page_cost` 或 `effective_cache_size`，并且用有代表性的查询回放验证，而不是拿一两条语句试。
