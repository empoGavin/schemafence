> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 统计信息：ANALYZE、pg_stats、统计目标与多列相关性

## 统计从哪来：ANALYZE 采样，不是全表扫描

计划器的行数估算依赖两类统计：一类来自 `pg_class` 的 `reltuples` / `relpages`（每张表/索引的行数和页数），另一类来自 `pg_statistic`（各列的选择性统计）。前者由 VACUUM、ANALYZE 和 CREATE INDEX 一类操作更新；后者由 ANALYZE 更新。

关键细节：**ANALYZE 用的是对行的统计随机采样，不是读全部行**。所以统计"新鲜"不等于"精确"，它天生只是近似。autovacuum 在表内容变化足够大时会自动触发 ANALYZE，但它的触发只按"插入/更新/删除的行数"来判断，**并不知道这次变化是否真的改变了统计分布**。例如一个只更新 `updated_at` 的表，autovacuum 会频繁 analyze，但那个真正影响查询的 URL 列分布其实没怎么变。

日常查询统计用 `pg_stats`：

```sql
SELECT attname, null_frac, n_distinct, most_common_vals
  FROM pg_stats
 WHERE tablename = 'orders_2024'
 ORDER BY attname;
```

`pg_stats` 对所有人可读，而底层的 `pg_statistic` 只有超级用户能读（避免普通用户通过统计反推别人的数据）。`n_distinct` 的正负号有特殊含义：**大于零是"不同值的估计个数"，小于零是"不同值个数 ÷ 行数"的负值**——负数形式用于那些"值会随表增长而增多"的列，例如唯一性接近的列会接近 -1。

## 统计目标的取舍：default_statistics_target

`pg_statistic` 里每列最多存多少 `most_common_vals` 和 `histogram_bounds`，由统计目标决定。全局默认是 `default_statistics_target = 100`，也可以按列覆盖：

```sql
ALTER TABLE orders_2024 ALTER COLUMN status SET STATISTICS 500;
```

调节的取舍很直接：**对分布很不规则的列提高统计目标，能让估算更准，代价是占更多 `pg_statistic` 空间、以及 ANALYZE 稍慢**；对分布简单的列，降低目标就够了。实践上更值得做的是"按列调"，而不是把全局默认一口气抬高。

## 多列相关性：单列统计的固有盲区

单列统计有一个绕不过的洞：**它假设多个条件之间相互独立**。当列之间实际相关时，这个假设会把选择率乘出严重偏差。

一个经典例子：表 `t` 的 `a` 和 `b` 两列取值完全相同、各有 100 个不同值。单独看 `a = 1` 的选择率是 1%，`b = 1` 也是 1%；计划器把它们相乘得到 0.01%，于是一条实际返回 100 行的查询被估算成只返回 1 行——**低了两个数量级**。

多列分组也有同样问题：`GROUP BY a` 的估算很准，但 `GROUP BY a, b` 在没有多列统计时可能高估一个数量级。

解法是显式创建扩展统计对象，用 `CREATE STATISTICS` 声明"我要这几列在一起时的统计"。它支持三种类型：

- `dependencies`：函数依赖，最便宜，只在列级别建模
- `ndistinct`：多列的不同值组合数
- `mcv`：多列的最常见值列表，最精确也最贵

```sql
CREATE STATISTICS stts (dependencies, ndistinct) ON tenant_id, region FROM orders_2024;
ANALYZE orders_2024;
```

创建统计对象本身只是往目录里写一条"我关心这几列"的记录，**真正的数据仍要由 ANALYZE 去采集**。采集结果放在 `pg_statistic_ext` 与 `pg_statistic_ext_data` 里，多列 MCV 列表可以用 `pg_mcv_list_items()` 展开查看：

```sql
SELECT m.*
  FROM pg_statistic_ext
  JOIN pg_statistic_ext_data ON (oid = stxoid),
       pg_mcv_list_items(stxdmcv) AS m
 WHERE stxname = 'stts';
```

## 统计过期导致计划劣化：怎么确认

判断的锚点是"估算与实际是否对得上"，以及"上次分析是什么时候"：

```sql
SELECT relname, last_analyze, last_autoanalyze, n_mod_since_analyze
  FROM pg_stat_all_tables
 WHERE relname = 'orders_2024';
```

`n_mod_since_analyze` 是自上次 ANALYZE 以来被改动的行数（半精确计数，高负载下可能丢一部分）。它很大、而 `last_autoanalyze` 很旧，就说明统计大概率已经跟不上数据了。再把 `EXPLAIN (ANALYZE)` 的估算 `rows` 与实际 `rows × loops` 对照，就能确认偏差是不是来自统计。

## 手工 ANALYZE 的适用场景与几个盲区

autovacuum 负责大部分 ANALYZE，但它有几处覆盖不到，需要手工排程：

- **分区父表**：autovacuum 不对分区表跑 ANALYZE；继承树的父表也只有在自身被改动时才会被分析，子表变化不会触发父表的 autoanalyze。需要查询父表统计时，要手工 `ANALYZE`。
- **外部表**：autovacuum 无法判断该多久分析一次，因此不会对它们跑 ANALYZE。
- **临时表**：autovacuum 访问不到。

ANALYZE 很轻（它是采样、不是全表扫描），所以实践上"整库 ANALYZE 一遍"通常是划算的。也可以只针对特定表甚至特定列做，把统计预算花在真正被 WHERE 频繁使用、且分布不规则的列上。

这里还有一层与"统计新鲜度"相关但要区分开的机制：ANALYZE 更新的是选择性统计（`pg_statistic`），而 `pg_class.reltuples` / `relpages` 是行数页数，两者来源不同、更新时机也不同。看到估算偏差时，要先判断错的是"行数总量"还是"选择率"——前者对应 `reltuples` 陈旧，后者对应列统计陈旧或缺失，处置动作不一样。

## 落到可观察：三处入口

一是 `pg_stats` 看单列统计；二是 `EXPLAIN (ANALYZE)` 看逐层估算偏差；三是 `pg_stat_all_tables` 看 `last_analyze` / `last_autoanalyze` / `n_mod_since_analyze` 判断新鲜度。三者结合，就能把"计划变差"归因到"统计过期"还是"统计本身缺失"。

## 核心判断：相关性只能靠 CREATE STATISTICS 补，别指望调统计目标

一个常见误区是"计划不准 → 把 `default_statistics_target` 从 100 提到 1000"。这能改善单列分布不规则的问题，**但它对多列相关性完全无效**——因为单列统计在数学上就假设了独立，采样再密也不会知道"a 和 b 总是一起出现"。所以当你说不清是哪种偏差时，先看是不是多列条件叠加的场景；如果是，正确动作是 `CREATE STATISTICS`，而不是继续抬统计目标。
