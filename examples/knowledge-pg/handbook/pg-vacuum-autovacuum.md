> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# VACUUM 与 autovacuum：死元组、触发阈值与三个命令怎么选

## 死元组从哪来，标准 VACUUM 到底回收了什么

MVCC 决定了 UPDATE 和 DELETE 不会立刻删掉旧行版本，而是留下一个"死元组"。标准 VACUUM 的职责就是在确认没有任何事务还需要这些旧版本之后，把它们占的空间标记为可复用，并顺带更新统计信息和可见性映射。

关键区分点：**标准 VACUUM 只把空间标记为"可复用"，不把它还给操作系统**。唯一的例外是：表末尾有一个或多个整页全空，且能轻松拿到排他锁时，才可能把末尾页截断归还。所以"表文件不缩小"不等于 VACUUM 没干活——它只是把空间留在表里给新行复用。想让表文件真正变小，那是 VACUUM FULL、CLUSTER 或者某些会重写表的 ALTER TABLE 的事。

日常目标不是把表压到最小，而是保持磁盘用量的稳态：每张表占"最小尺寸 + 两次 vacuum 之间新增的空间"。**频繁的标准 VACUUM 比偶尔的 VACUUM FULL 更划算**，尤其对高频更新的表。

## 三种"清理"命令的取舍

- 标准 `VACUUM`：可以和线上读写并行，几乎不阻塞业务；但要拿 `SHARE UPDATE EXCLUSIVE` 锁，会挡住 DDL 和 VACUUM。
- `VACUUM FULL`：重写整张表，能真正把空间还给操作系统，但**需要 `ACCESS EXCLUSIVE` 锁**，期间该表完全不可用，而且需要约等于表大小的额外磁盘空间。
- `CLUSTER`：同样按索引顺序重写整张表，也是 `ACCESS EXCLUSIVE` 锁，同时还会重建索引。

结论很直接：**能用标准 VACUUM 就别用 VACUUM FULL；高频更新的表靠"勤快的小 VACUUM"而不是"偶尔的大 VACUUM FULL"**。并且要注意，autovacuum 永远不会发起 VACUUM FULL。

```sql
SELECT relname, n_dead_tup, n_live_tup,
       pg_size_pretty(pg_relation_size(relid)) AS heap
  FROM pg_stat_all_tables
 WHERE n_dead_tup > 0
 ORDER BY n_dead_tup DESC LIMIT 20;
```

**这三个命令怎么选**：日常清理一律用标准 VACUUM；只有两种情况才轮到 VACUUM FULL 或 CLUSTER —— 一是表已经严重膨胀、靠复用内部空间再也压不下去，而磁盘水位已经告警；二是需要重建物理顺序做批量范围扫描。除此之外，**频繁的标准 VACUUM 几乎总是比偶尔的 VACUUM FULL 更划算**：后者要拿排他锁并重写整表，代价与表大小成正比，过程中还会再生成一份等量的 WAL。

## autovacuum 是怎么被触发的：两个阈值公式

autovacuum 由常驻的 autovacuum launcher 调度，它可以同时跑最多 `autovacuum_max_workers` 个 worker（默认 3 个），大约每 `autovacuum_naptime`（默认 1 分钟）为每个数据库安排一次工作。

worker 判断是否要跑 VACUUM，用的是这个公式：

```
vacuum 阈值 = autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples
```

其中基础阈值 `autovacuum_vacuum_threshold` 默认 50，比例 `autovacuum_vacuum_scale_factor` 默认 0.2（也就是表大小的 20%）。除此之外还有一个"插入阈值"：

```
vacuum 插入阈值 = autovacuum_vacuum_insert_threshold + autovacuum_vacuum_insert_scale_factor × reltuples
```

基础值默认 1000、比例默认 0.2。ANALYZE 也类似，基础值 `autovacuum_analyze_threshold` 默认 50、比例 `autovacuum_analyze_scale_factor` 默认 0.1。

## scale_factor 的陷阱：大表几乎永远触发不了

把上面的公式代进大表就明白了：一张 1 亿行的表，`autovacuum_vacuum_scale_factor = 0.2` 意味着要积累 2000 万个死元组才触发一次 VACUUM。在触发之前，这张表可能已经膨胀到正常大小的好几倍，索引也随之膨胀。

另外几个容易被忽略的盲区：

- autovacuum **不会对分区父表跑 ANALYZE**，而对子分区会正常处理；继承树的父表在自身没被改动时也不会被分析。
- 外部表（foreign table）也不会被 autovacuum ANALYZE，需要手工排程。
- 临时表 autovacuum 根本访问不到。

这些坑的解法都不是改全局参数，而是**按表覆盖存储参数**，例如：

```sql
ALTER TABLE orders_2024 SET (autovacuum_vacuum_scale_factor = 0.02,
                            autovacuum_vacuum_threshold = 5000,
                            autovacuum_analyze_scale_factor = 0.01);
```

表级设置优先于全局设置。

## visibility map 与 freeze：VACUUM 的另一半价值

VACUUM 维护每个表的可见性映射（VM），它每页存两个 bit：第一个 bit 表示"这一页上的元组对所有事务都可见"（all-visible），第二个 bit 表示"这一页上的元组都已被冻结"（all-frozen）。

第一个 bit 的意义很直接：**index-only scan 能否跳过堆访问，就取决于它**。如果查询只需要索引里有的列，且对应堆页是 all-visible，就可以只读索引、不读堆，大幅省 IO；否则还是要去堆里确认可见性，index-only scan 的优势就没了。

第二个 bit 影响的是回卷防护：如果一页已经 all-frozen，连防回卷的 aggressive vacuum 也不必再访问它。所以冻结不只是"防回卷"，它还降低后续 VACUUM 的工作量。

这里有个细节：普通 VACUUM 会用 VM 跳过没有死元组的页，因此**它不会冻结所有旧的元组**。当 `relfrozenxid` 比 `vacuum_freeze_table_age` 还老时，VACUUM 才会做 aggressive 扫描，把 all-visible 但还没 all-frozen 的页也扫一遍。`vacuum_freeze_min_age` 决定多老的 XID 才值得冻结。

## cost 限流：让 VACUUM 别把 IO 打满

VACUUM 和 ANALYZE 有一个基于代价的限流机制：累计代价达到 `vacuum_cost_limit`（默认 200）就休眠 `vacuum_cost_delay`。单页代价由 `vacuum_cost_page_hit`（默认 1）、`vacuum_cost_page_miss`（默认 2）、`vacuum_cost_page_dirty`（默认 20）决定。

要注意默认值的语义差异：手工 VACUUM 的 `vacuum_cost_delay` 默认是 0，也就是**手工 VACUUM 默认不限流**；而 autovacuum 用的是 `autovacuum_vacuum_cost_delay`，默认 2ms，所以 autovacuum 默认是被限流的。`autovacuum_vacuum_cost_limit` 默认 -1，表示沿用 `vacuum_cost_limit`。有多个 worker 时，限流额度会在它们之间**按比例分摊**，这样无论几个 worker 在跑，对系统的总 IO 影响大体一致。

```sql
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('vacuum_cost_delay','vacuum_cost_limit',
                'autovacuum_vacuum_cost_delay','autovacuum_vacuum_cost_limit',
                'autovacuum_vacuum_scale_factor','autovacuum_naptime',
                'autovacuum_max_workers','log_autovacuum_min_duration');
```

`log_autovacuum_min_duration` 默认 -1（不记录）。把它设成 0 或一个毫秒值，可以让每次 autovacuum 的详情进日志，是排查"autovacuum 到底跑了没"最直接的开关。

## 核心判断：大表膨胀先改阈值，而不是先改 worker 数

遇到大表膨胀，很多人第一反应是加 `autovacuum_max_workers`。但更常见的真相是：**默认的 `autovacuum_vacuum_scale_factor = 0.2` 让大表要攒够 20% 的死元组才动手**，加再多 worker 也等不到触发。正确顺序是先按表把 scale_factor 和 threshold 降下来，让它"该触发时能触发"；再确认限流是不是太紧（看 `pg_stat_progress_vacuum` 有没有长时间低效率的扫描）；最后才考虑要不要加 worker 或调内存。
