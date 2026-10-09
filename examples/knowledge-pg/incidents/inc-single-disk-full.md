> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 磁盘写满：第一件不能做的事是删 pg_wal

## 现象：磁盘写到 100%，业务开始报 No space left on device（虚构）

虚构的订单库 `orders_prod` 跑在 `pg-node-01` 上，数据盘 500GB。某个白天，磁盘使用率在 20 分钟里从 82% 冲到 100%，业务写入开始零星失败，应用日志混着两类报错：

```
ERROR:  could not extend file "base/16385/26123": No space left on device
HINT:  Check free disk space.
ERROR:  could not write to file "pg_wal/xlogtemp.12345": No space left on device
```

值班的人手速很快，已经在敲 `rm /var/lib/pgsql/16/data/pg_wal/00000001000000...`。这是本案例最想拦住的动作：`pg_wal` 里的段是主库崩溃恢复的依赖，删它会把一次磁盘告警升级成一次数据恢复事故。正确顺序是先**量清楚是谁占的**，再从**不该删的东西**之外释放空间。

## 先收集什么证据：先量"是谁在长"，不是"谁最大"

系统层，两个命令都要跑——`df -h` 看空间，`df -i` 看 inode，它们会给出两个完全不同的答案：

```bash
df -h /var/lib/pgsql/16/data
df -i /var/lib/pgsql/16/data
du -sh /var/lib/pgsql/16/data/pg_wal
du -sh /var/lib/pgsql/16/data/log
ls -1 /var/lib/pgsql/16/data/pg_wal | wc -l
ls -l /var/lib/pgsql/16/data/base/pgsql_tmp | head
```

数据库层，先抓"能不能回收"的关键状态：

```sql
SELECT slot_name, active, restart_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots ORDER BY restart_lsn;

SELECT archived_count, failed_count, last_failed_time FROM pg_stat_archiver;

SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('archive_mode','archive_command','archive_library',
                'max_wal_size','min_wal_size','max_slot_wal_keep_size');

SELECT pg_size_pretty(sum(size)) AS wal_total FROM pg_ls_waldir();
SELECT pg_size_pretty(sum(size)) AS log_total FROM pg_ls_logdir();

SELECT datname, temp_bytes, temp_files
  FROM pg_stat_database ORDER BY temp_bytes DESC LIMIT 5;
```

再补一条把最大对象找出来的查询，用于判断"是不是某张表或索引在膨胀"：

```sql
SELECT n.nspname, c.relname, c.relkind, pg_size_pretty(pg_total_relation_size(c.oid)) AS sz
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r','m','i')
 ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 10;
```

## 定位过程：四个嫌疑人，先算增长率再定罪

磁盘满的四个常见嫌疑人是 **WAL、表/索引、日志、临时文件**。它们能同时存在，所以要逐个量体积，并分清"谁最大"和"谁在长"——一张 200GB 的表是正常业务，一个每小时涨 20GB 的 `pg_wal` 才是事故。

**嫌疑人一：WAL。** `du -sh pg_wal` 给出了 260GB、目录里 1.6 万个文件，明显在堆积。但堆积是结果不是原因。`pg_wal` 里的段只有在"不再被需要"时才会被回收或重命名复用（`Reliability and the Write-Ahead Log`），拦住回收的通常是两种：**归档失败**或**复制槽**。查 `pg_stat_archiver.failed_count`，发现它正在持续增长，`last_failed_time` 就是几分钟前；再 `pg_settings` 看 `archive_command`，指向的是一块已经写满的备份盘。归档写不出去，旧段就不能回收——这条坐实。

**嫌疑人二：表/索引膨胀。** `pg_total_relation_size` 排序后，最大的表也就 40GB，没有异常膨胀；排除。

**嫌疑人三：日志目录。** `pg_ls_logdir()` 汇总只有 3GB，`log_rotation_age`、`log_rotation_size` 都配了；排除。

**嫌疑人四：临时文件。** `pg_stat_database.temp_bytes` 排前的库只有几 GB，`base/pgsql_tmp` 目录没有大量残留；排除。

**两个必须同时排除的误判：**

- **inode 也可能是"满"的那个。** `df -h` 正常但 `df -i` 用光完全可能：复制槽滞留、子事务密集、WAL 段数量暴涨，都会让文件数失控，从而把 inode 表吃光。这时"空间还有富余"却任何新文件都建不出来。所以两份 `df` 都要看。
- **"写满的是数据盘，所以删数据盘里的东西就行"不成立。** 本次真正的源头在另一块备份盘：它先满，导致 `archive_command` 失败，进而让主数据盘的 WAL 无法回收。只看数据盘会误判成"WAL 自己涨得太快"。

**为什么不能直接删 `pg_wal` 文件，这一点必须讲透：**

1. `pg_wal` 里最近的段是主库**自身崩溃恢复**要用的数据。手工删掉，下次崩溃时恢复会因为缺段而失败。
2. 即使删的是"看起来最旧"的段，只要归档或复制槽还指向它们，删除只是把问题从"占盘"变成"更早占盘、且无法解释自己去了哪"。
3. 目录里可能有正在被发送/归档的段，删除会直接打断在线流程。正确对象从来不是文件，而是**让回收恢复运转**。

## 结论与处置：先修归档，再让 checkpoint 自己回收 WAL

根因：`archive_command` 的目标备份盘写满，归档持续失败，`pg_wal` 中旧段无法回收而不断堆积，最终把主数据盘撑满。

处置动作，按"先释放、后治本"排：

- **先释放非关键空间**：清理已轮转的旧日志、清掉不再需要的临时文件，必要时给数据盘做一次紧急扩容，先把水位压到安全线以下，让业务能写。
- **修归档链路**：扩容或更换备份盘，确保 `archive_command` 能写成功。归档一旦追上，旧 WAL 段就会被回收或复用，不需要你手删任何 `pg_wal` 文件。
- **不要 `rm pg_wal`**：这是底线。
- **长期治理**：把日志目录和大对象单独挂盘，避免和 WAL 抢同一块盘；设 `max_slot_wal_keep_size` 给复制槽兜底上限；对 `pg_wal` 体积和 `pg_stat_archiver.failed_count` 做告警。

验证指标：`df -h` 使用率回落到安全水位且不再上涨；`pg_stat_archiver.failed_count` 停止增长、`archived_count` 持续前进；`pg_ls_waldir()` 的总大小与文件数回落；业务侧不再出现 `No space left on device`。

## 复盘要点

- **磁盘满的第一反应不是删文件**。先量"谁在长"，再看"哪些东西不能删"，最后从非关键对象里释放空间。
- **`pg_wal` 永远不在可删清单里**。它是崩溃恢复依赖的数据；要处理的是拦住回收的归档和复制槽。
- **真正的源头可能不在告警那块盘上**。归档盘先满导致数据盘的 WAL 堆积，是典型的"病灶在外、症状在内"。
- **`df -h` 和 `df -i` 要一起看**。空间和 inode 是两个独立的耗尽维度，只查一个会漏判一半的磁盘类故障。
- **WAL 回收是"自动的，但有前提"**。归档与复制槽不堵，checkpoint 自己会回收；监控这两个前提，比事后救火便宜得多。
