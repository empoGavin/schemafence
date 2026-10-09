> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 事务 ID 回卷告警：别等到"停止接受写命令"才动手

## 现象：日志里开始出现"必须尽快 vacuum"的告警（虚构）

虚构的订单库 `orders_prod`（主机 `pg-node-01`）日志里出现这类内容：

```
WARNING:  database "orders_prod" must be vacuumed within 39985967 transactions
HINT:  To avoid a database shutdown, execute a database-wide VACUUM in that database.
```

这是事务 ID 回卷（XID wraparound）的正式告警。值班的人准备直接上 `VACUUM FULL`——这是错的，而且错得很危险。文档讲得很清楚：进入这个状态后，`VACUUM FULL` 需要分配一个 XID 才能跑，会失败；即便以超级用户身份跑起来，它消耗 XID 反而**加剧**回卷风险（`Routine Database Maintenance Tasks`）。

## 先收集什么证据：早期信号在"年龄"，不在"告警"

告警只是最后阶段。真正该盯的是"最老的未冻结 XID 有多老"，也就是年龄。库级和表级都要看：

```sql
SELECT datname, age(datfrozenxid) AS xid_age
  FROM pg_database ORDER BY xid_age DESC;

SELECT n.nspname, c.relname, age(c.relfrozenxid) AS xid_age,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS sz
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r','m')
 ORDER BY age(c.relfrozenxid) DESC LIMIT 15;
```

`age()` 给的是"从该 XID 到当前事务"：`pg_database.datfrozenxid` 是库级下界，`pg_class.relfrozenxid` 是表级下界；文档的查询范式还会把 TOAST 表的 `relfrozenxid` 一并纳入（`Routine Database Maintenance Tasks`）。再看与冻结相关的参数：

```sql
SELECT name, setting FROM pg_settings
 WHERE name IN ('autovacuum','autovacuum_freeze_max_age','vacuum_freeze_table_age',
                'vacuum_freeze_min_age','autovacuum_max_workers','autovacuum_naptime');
```

**判读阈值**（文档数值）：`age` 逼近 `autovacuum_freeze_max_age`（默认 2 亿）时，系统会**强制**对该表启动 autovacuum；当库内最老 XID 距离回卷点还有 4000 万事务时，开始打 WARNING；少于 300 万时，进入拒绝写命令的状态：

```
ERROR:  database is not accepting commands to avoid wraparound data loss in database "orders_prod"
HINT:  Stop the postmaster and vacuum that database in single-user mode.
```

注意那条 HINT——文档特别说明，**实际上并不需要停机进单用户模式**，正常在线处理即可。

## 定位过程：不是"没跑 vacuum"，是"跑了也冻不动"

**第一步：确认 autovacuum 在跑。** 查 `pg_stat_activity` 里的 autovacuum worker，能看到它在读某张表。但 `age(datfrozenxid)` 却长期不动——这就把方向从"没跑"引到"跑了也无效"。

**第二步：找出钉住 freeze 的钉子。** 为什么冻不动？因为只要还有一个"更老的 XID 可见或可能还在用"，VACUUM 就不能把比它新的行冻结，`relfrozenxid` 也就推不动。文档明确列出三类钉子，逐个查：

```sql
SELECT gid, age(transactionid) AS age, prepared, owner, database
  FROM pg_prepared_xacts ORDER BY age DESC;

SELECT pid, usename, state,
       age(backend_xid) AS xid_age, age(backend_xmin) AS xmin_age,
       left(query, 60) AS query
  FROM pg_stat_activity
 WHERE backend_xid IS NOT NULL OR backend_xmin IS NOT NULL
 ORDER BY greatest(age(backend_xid), age(backend_xmin)) DESC NULLS LAST;

SELECT slot_name, slot_type, active,
       age(xmin) AS xmin_age, age(catalog_xmin) AS catalog_xmin_age,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots ORDER BY greatest(age(xmin), age(catalog_xmin)) DESC NULLS LAST;
```

本次的答案是第三类：一个逻辑复制槽（订阅方早已下线未清理）长期 `active = f`，它的 `catalog_xmin` 停在很久以前，把 freeze 进度钉死了。

**反直觉点一：多跑几次 VACUUM 没用。** 钉子不拔，VACUUM 再勤快也推不动 `relfrozenxid`。要治的是那个长事务/预备事务/复制槽，而不是"加大 vacuum 力度"。

**反直觉点二：为防回卷触发的 autovacuum 不能被冲突锁中断。** 文档说，当一个 autovacuum 的任务名在 `pg_stat_activity` 里以 `(to prevent wraparound)` 结尾时，它不会像普通 autovacuum 那样"被冲突锁打断就退出"，而是会坚持跑完。这有两层含义：一是别指望用锁把它挤走；二是不能简单地"手动 VACUUM 抢在它前面就算解决"，它该跑还是会跑、该和业务抢资源还是会抢。

**反直觉点三：不能 `VACUUM FULL`，也不该 `VACUUM FREEZE`。** 前者要 XID、会失败或加剧风险；后者做的比"恢复裸运行所必需"的更多，是浪费。

**排除：不是"表太大所以慢"这么简单。** 3 亿行的大表确实是难点，但本次 `age` 长时间不动的直接原因是那个槽的 `catalog_xmin`，不是表体积。先拔钉子，再看耗时。

## 结论与处置：按文档给的顺序，先拔钉子再 VACUUM

根因：一个被遗忘的逻辑复制槽长期 `active = f`，其 `catalog_xmin` 停留在很久以前，钉住了全库的 freeze 进度，使 `age(datfrozenxid)` 持续逼近回卷告警线。

处置顺序很重要，因为它是有序的——**钉子不拔，后面的 VACUUM 也推不动**：

1. 处理老预备事务（`pg_prepared_xacts`）：能提交就提交，能回滚就回滚。
2. 结束长事务：对 `pg_stat_activity` 里 `age(backend_xid)`／`age(backend_xmin)` 很大的会话，提交或回滚，必要时 `pg_terminate_backend`。
3. 删除老的复制槽：对 `age(xmin)`／`age(catalog_xmin)` 很大的槽，确认订阅方不会原样恢复后 `pg_drop_replication_slot`。
4. 执行 `VACUUM`：库级最省事，或针对 `relfrozenxid` 最老的表逐个来。**不要 `VACUUM FULL`，不要 `VACUUM FREEZE`。**
5. 恢复后检查 autovacuum 配置，避免复发。用 `pg_stat_progress_vacuum` 观察大表 freeze 的进度。

验证指标：`age(datfrozenxid)` 开始快速下降、`relfrozenxid` 前进；日志 WARNING 停止；若已进入 `ERROR:  database is not accepting commands` 状态，则数据库重新接受写命令。

## 复盘要点

- **盯年龄，不盯告警**。`age(datfrozenxid)` 逼近 `autovacuum_freeze_max_age` 就该介入，别等 4000 万事务的 WARNING。
- **autovacuum 不是没跑，而是冻不动**。冻结的前提是"没有更老的 XID 还可见"；长事务、预备事务、老复制槽都会钉死它。
- **处置是有序的**：先清预备事务、结束长事务、删老槽，最后才 VACUUM；顺序反了等于白做。
- **不要 `VACUUM FULL`／`VACUUM FREEZE`**。前者要 XID 会失败或加剧回卷，后者做了多余的工作。
- **防回卷的 autovacuum 不能被锁中断**，所以别指望用锁把它挤走，也别把"手动 VACUUM 抢跑"当成解决方案。
