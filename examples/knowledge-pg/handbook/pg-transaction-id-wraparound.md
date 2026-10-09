> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 事务 ID 回卷：为什么单返回卷能拖垮整个实例

## 32 位的事务号是环形的，这是问题的起点

PostgreSQL 的普通事务 ID（XID）是 32 位，比较时用模 2³² 运算。含义是：对任意一个普通 XID，都有大约 20 亿个"更老"的、和 20 亿个"更新"的。也就是说 XID 空间是**环形的、没有端点的**。

后果是：一个行版本被某个 XID 创建后，在接下来的约 20 亿个事务里都表现为"在过去"；一旦存活超过这个跨度，它就会突然被判定为"在未来"，于是**变得不可见**。这不是数据被删了，数据还在磁盘上，只是你再也查不出来——官方原文用的词是灾难性数据丢失。

规避办法是定期冻结（freeze）：VACUUM 把足够老的行版本标记为冻结，让它们对过去和未来的一切事务都恒可见。所以硬性要求是：**每一个库里的每一张表，每 20 亿个事务至少要 VACUUM 一次**。为了让这点绝对成立，PostgreSQL 保留了 `FrozenTransactionId`，它不遵守普通比较规则，永远被认为比任何普通 XID 更老。

（顺带：9.4 之前的版本是真的把行的 `xmin` 改写成 `FrozenTransactionId`，之后改为只置一个标志位，保留原始 `xmin` 供取证。所以升级上来的库里可能还看得到 `xmin = 2` 这种行，属正常。）

## 几个 freeze 相关参数与它们的默认值

- `autovacuum_freeze_max_age`：默认 2 亿。一张表的 `pg_class.relfrozenxid` 到了这个年龄，就会**强制**触发防回卷 autovacuum，**即使 autovacuum 被关了也照样触发**。
- `vacuum_freeze_min_age`：默认 5000 万。VACUUM 只会冻结年龄超过它的行版本。设太大，会导致本来还该冻的没冻；设太小，会白冻那些很快又要被更新的行。
- `vacuum_freeze_table_age`：默认 1.5 亿。`relfrozenxid` 到了这个年龄，VACUUM 做 aggressive 扫描（连 all-visible 但未 all-frozen 的页也扫）。有效值会被静默限制在 `autovacuum_freeze_max_age` 的 95%。
- `vacuum_failsafe_age`：默认 16 亿。这是 VACUUM 的"最后手段"：触发后不再应用代价限流、跳过非必需的维护（比如索引 vacuum），全力防止回卷。

一个反向的取舍要注意：把 `autovacuum_freeze_max_age` 调大会减少强制 vacuum 的频率（对静态大表友好），但会让 `pg_xact` 和 `pg_commit_ts` 目录变大，因为它要保存到这条地平线为止的所有事务状态。默认的 2 亿大约对应 50MB 的 `pg_xact`。

## 为什么单表的问题会威胁整个实例

这是容易被低估的一点。XID 计数器是**整个集群全局共享**的，不是每个库、每张表各自算。而一个库的 `pg_database.datfrozenxid` 只是它内部所有表 `relfrozenxid` 的**最小值**。

所以只要集群里有一张表长期没被 VACUUM 到、它的 `relfrozenxid` 一直很老，整个数据库的清理进度就被它钉住；当集群推进到接近 40 亿时，这个"最老"的值就会逼近回卷边界。**你不用管这张表大小如何、业务重不重要，它一个人就能把全库、乃至全实例拖进拒绝服务状态。**

**监控要盯的是年龄，不是等到报错**：判断离回卷还有多远，唯一可靠的量是 `age(datfrozenxid)`（表级对应 `age(relfrozenxid)`），它表示最老的那个未冻结事务号距今已经过了多少个事务。**不要等日志里出现 WARNING 才动手**——WARNING 阶段说明已经贴近上限、处置窗口只剩很窄的一段。把 `age(datfrozenxid)` 做成常态指标（例如超过 2 亿就告警），才谈得上从容处置。

## 早期信号：先 WARNING，再 ERROR

这套机制给了缓冲期，关键是别把 WARNING 当噪音。当日志里出现：

```
WARNING:  database "mydb" must be vacuumed within 39985967 transactions
HINT:  To avoid a database shutdown, execute a database-wide VACUUM in that database.
```

说明最老 XID 距离回卷还有约 4000 万（官方文案是"reach forty million transactions from the wraparound point"）。如果继续无视，剩余不足 300 万时系统会升级为：

```
ERROR:  database is not accepting commands to avoid wraparound data loss in database "mydb"
```

此时已经**拒绝分配新 XID**：进行中的事务可以继续，但只能启动只读事务，所有写操作和 TRUNCATE 都会失败，VACUUM 仍可正常执行。

## 安全处置顺序：先清障碍，再 VACUUM，别用 VACUUM FULL

官方给的步骤很明确，顺序不能乱，因为前三步都是在解除"清理障碍"：

1. **处理老的 prepared 事务**。查 `pg_prepared_xacts`，看 `age(transactionid)` 很大的那些，把它们提交或回滚。
2. **结束长事务**。查 `pg_stat_activity` 里 `age(backend_xid)` 或 `age(backend_xmin)` 很大的会话，让它们提交/回滚，必要时用 `pg_terminate_backend` 终止。
3. **清理老复制槽**。查 `pg_replication_slots` 里 `age(xmin)` 或 `age(catalog_xmin)` 很大的槽，很多是给早已不存在的从库建的；删之前要确认对应从库不再用这个槽，否则该从库可能需要重建。
4. **在目标库里执行 VACUUM**。全库 VACUUM 最省心；为了省时间，也可以只对 `relfrozenxid` 最老的若干表手工 VACUUM。**这一步绝对不要用 VACUUM FULL**——它需要分配 XID，会失败；即使在超级用户模式下成功，也会消耗一个 XID，反而加重回卷风险。同样**不要用 VACUUM FREEZE**，它会做多于最低限度的工作。
5. **恢复正常后，把目标库的 autovacuum 配置修好**，避免复发。

```sql
SELECT c.oid::regclass AS table_name,
       greatest(age(c.relfrozenxid), age(t.relfrozenxid)) AS age
  FROM pg_class c
  LEFT JOIN pg_class t ON c.reltoastrelid = t.oid
 WHERE c.relkind IN ('r', 'm')
 ORDER BY age DESC LIMIT 20;

SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC;
```

第一句找单表最老的 `relfrozenxid`（顺带把 TOAST 表一起考虑），第二句看每个库的 `datfrozenxid` 年龄，是判断"还有多少缓冲"最直观的两个查询。

## 核心判断：回卷是"最慢那张表"集体买单，所以要在 WARNING 阶段处理

回卷最反直觉的地方在于它的全局性：XID 是全局计数器，`datfrozenxid` 取所有表的最小值，因此**治理的短板是最落后那一张表，而不是平均值**。另一个判断是时机：日志里先出现的是 WARNING、其次是拒绝写，中间隔着 4000 万到 300 万事务的窗口。**在 WARNING 阶段处理，是一次普通的 VACUUM；拖到 ERROR 阶段，就是一次全库停机救援**。优先级不该按表大小排，而要按 `age(relfrozenxid)` 排。
