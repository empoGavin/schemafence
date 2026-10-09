> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 索引族：B-tree、Hash、GIN、GiST、SP-GiST、BRIN 怎么选

## B-tree：默认、万能、但也有边界

CREATE INDEX 不写引擎时默认就是 B-tree。它适合能排序的数据上的等值查询和范围查询，支持的比较运算符是 `< <= = >= >`，BETWEEN 和 IN 这类等价写法也能用；索引列上的 `IS NULL` / `IS NOT NULL` 同样可走。它还能直接给出排序结果，所以能替 ORDER BY 省掉一次显式排序，配 LIMIT 时尤其有价值。

它也有边界：

- 前后缀模式匹配只在前缀锚定时有效，比如 `col LIKE 'foo%'` 或 `col ~ '^foo'` 可以走，`col LIKE '%bar'` 不行。**如果数据库不是 C locale，还需要专门的操作符类**才能支持模式匹配索引。
- 多列 B-tree 只在最左前缀上有约束时才高效。等值约束的前缀列 + 第一个非等值列，决定了真正被扫描的索引区间；更右侧的列上的条件虽然能减少回表，但不减少索引扫描范围。

B-tree 还有一个可选优化：去重（deduplication）。当大量索引键重复时，它能把重复项合并成 posting list，让索引更紧凑。

## Hash：只服务等值，但很轻

Hash 索引存的是索引列值的 32 位哈希码，因此**只支持等值比较（=）**。它比 B-tree 小、写入更快，但功能面窄，也不支持排序输出。并发方面，它用的是桶级锁、锁持有时间比单次索引操作更久，理论上存在死锁可能。实践上大多数场景 B-tree 已经够用，Hash 主要用于纯等值、且明确想省空间的场景。

## GiST 与 SP-GiST：它们不是"一种索引"，而是框架

GiST 是一套基础设施，不同的操作符类实现不同的索引策略。标准发行版里带了二维几何类型的操作符类，支持 `<< &< &> >> @> <@ &&` 一类运算符；它还能做最近邻搜索（例如 `ORDER BY location <-> point '(101,456)' LIMIT 10`），是否支持取决于操作符类。

SP-GiST 同样是一套框架，允许实现各种**非平衡**的基于磁盘的数据结构：四叉树、k-d 树、基数树（trie）。标准版里有二维点的操作符类，同样支持最近邻。选它还是 GiST，通常取决于数据分布和操作符类的可用性：SP-GiST 对可分区的空间通常更省空间、更快。

## GIN：倒排索引，为"多值列"而生

GIN 是倒排索引，适合数组、全文检索这类**一个字段里有多个组成值**的数据：它为每个组成值建一条索引项，因此能高效回答"是否包含某个值"。数组操作符支持 `<@ @> = &&`。

GIN 有一个必须知道的行为叫 fast update：为了不拖慢写入，新条目先进一个临时的、未排序的 pending list，等到表被 vacuum/autoanalyze、或调用 `gin_clean_pending_list()`、或 pending list 超过 `gin_pending_list_limit` 时，才用批量方式并入主索引结构。好处是写快、且合并可以放到后台；代价是**查询必须额外扫 pending list，列表越大查询越慢**；而且偶尔一次"列表太大触发的前台清理"会让某个 update 明显变慢。想换取稳定响应时间，可以关掉索引的 `fastupdate` 存储参数。

另一个实践结论：对大批量导入，**先删掉 GIN 索引、导完再重建，通常比边导边维护索引快**；而且 GIN 的建索引时间对 `maintenance_work_mem` 非常敏感，别在内存上抠。

## BRIN：块范围摘要，怕"乱序"

BRIN（Block Range INdex）不存每个值，而是存**连续物理块区间内的摘要**（对可排序类型就是每段的最小值和最大值）。它极其小，但只在"索引列的值与行的物理顺序高度相关"时才有效——典型场景是自增/时间递增列。它支持 `< <= = >= >`。多列 BRIN 的查找效果与用哪一列无关，所以多个 BRIN 的唯一理由是可以为每个索引单独指定 `pages_per_range`。

判断要点很直接：**如果数据插入顺序和键值顺序一致（比如日志、时序表的自增主键），BRIN 用极小的空间换来明显的扫描裁剪；如果数据被频繁乱序更新，区间摘要会退化成一堆宽范围，几乎筛不掉任何页。**

## 索引膨胀与 REINDEX CONCURRENTLY

B-tree 页即使只剩少量键也不会被立即归还；另外非 B-tree 索引的膨胀规律研究得并不充分，所以要**定期盯着非 B-tree 索引的物理大小**。

重建索引用 REINDEX。它默认需要 `ACCESS EXCLUSIVE` 锁（会阻塞读写），而 **REINDEX CONCURRENTLY 只需要 `SHARE UPDATE EXCLUSIVE` 锁**，在线业务基本可以直接用：

```sql
REINDEX INDEX CONCURRENTLY idx_orders_tenant_created;
```

```sql
SELECT indexrelname, idx_scan, idx_tup_read, idx_tup_fetch,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
  FROM pg_stat_user_indexes
 WHERE relname = 'orders_2024'
 ORDER BY pg_relation_size(indexrelid) DESC;
```

把 `idx_scan = 0` 且体积很大的索引挑出来，往往是"建了但从没用过"的冗余索引。

## covering index 与 index-only scan

普通索引扫描要同时读索引和堆。index-only scan 的目标是**只读索引就够了**。它有两个前提：索引类型必须支持（B-tree 总是支持；GiST/SP-GiST 部分操作符类支持；GIN 因为每条索引项只存部分原值，不支持），以及查询只引用索引里有的列。

要让后者成立，可以用 INCLUDE 加"载荷列"：

```sql
CREATE INDEX idx_orders_cover ON orders_2024 (tenant_id, created_at) INCLUDE (amount);
```

这样 `SELECT tenant_id, created_at, amount FROM orders_2024 WHERE tenant_id='tenant_a'` 就有可能走 index-only scan。注意：INCLUDE 列不参与索引键，因此唯一性只作用于键列；而且只有 B-tree、GiST、SP-GiST 支持 INCLUDE，表达式列目前也不能放进 INCLUDE。

还有个隐藏前提：index-only scan 需要访问可见性映射确认页是 all-visible。如果堆页不是 all-visible，它还是得回堆，优势就没了。所以**index-only scan 的效果高度依赖 VACUUM 维护的可见性映射质量**。用 `EXPLAIN (ANALYZE)` 看计划里的 `Heap Fetches` 一列，它不为 0 就说明还是回堆了。

## 核心判断：索引建了却不走，先排除这四种可能

遇到"索引明明建了却不走"，不要急着加新索引，按下面顺序排除，能覆盖绝大多数情况：

1. **类型或操作符不匹配**：列上用的是 `LIKE '%x'`、或用了 GiST/GIN 才支持的操作符，却建了 B-tree。
2. **表达式或隐式类型转换**：WHERE 里写成 `f(col) = ...`，或者列是 text 而参数是别的类型，导致条件无法映射到索引列。
3. **统计过期**：行的估算严重偏低/偏高（见统计那一篇），计划器主动放弃索引。
4. **选择性太差**：条件实际会命中大半张表，此时顺序扫描确实更快，计划器不是在犯错。

把 `EXPLAIN (ANALYZE, BUFFERS)` 的"估算行数 vs 实际行数"和 `pg_stat_user_indexes.idx_scan` 一起看，基本能在前四条里定位到是哪一条。
