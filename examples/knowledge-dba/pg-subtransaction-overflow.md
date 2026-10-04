> 语料来源：公开技术文章整理 + 脱敏后的生产案例（无表名、无系统标识、无内部信息）。

# 子事务：从把库拖 hang 到把 inode 用光

## 先讲机制：子事务为什么会拖垮整个实例

子事务（subtransaction）让事务可以部分回滚，SQL 层是 `SAVEPOINT` / `ROLLBACK TO SAVEPOINT`，
PL/pgSQL 层是 `BEGIN ... EXCEPTION ... END` 块。关键点是：**子事务同样会分配 XID**，
父子对应关系记录在 `pg_subtrans` 这个 SLRU 里。

`pg_subtrans` 的内存缓存是源码写死的 32 个 buffer（`NUM_SUBTRANS_BUFFERS = 32`），
每页 8KB、每个 XID 占 4 字节，也就是**每页 2048 个子事务、总共最多缓存 65536 个 XID（约 256KB）**。
一旦活跃子事务的量超出这个缓存，判断可见性就要反复从磁盘加载 `pg_subtrans` 页
（cache miss），消耗大量 IO 与 CPU，表现为整体变慢甚至 hang。

## 64 这个数字是怎么来的

每个 backend 有一份子事务缓存 `PGPROC_MAX_CACHED_SUBXIDS`，源码里定义为 **64**，不是可调参数。
不超过 64 时，任何 XID 都能直接判断"是否还在运行中"；
一旦溢出（源码里的 suboverflowed 标记被置起），**每次可见性判断都必须去查 `pg_subtrans`**。
实测现象是：子事务刚超过 64 个的时候性能出现断崖，不是线性下降。

所以"控制在 64 以内"是硬底线，但实践上要留余量——有工具类产品默认就是 50 个子事务，
把它降到 10–20 之后数据库压力立刻缓解。**目标是远低于 64，而不是贴着 64 跑。**

## 案例一：循环体里的 EXCEPTION 把库拖 hang

**现象**：某个存储过程执行期间，整个实例响应急剧劣化，其他会话大面积等待，
看起来像"卡死"但又不是锁等待。

**定位**：这个过程是一个大循环，**循环体内部带了 EXCEPTION 处理块**。
进入带 EXCEPTION 的块就会建立一个子事务，于是循环每迭代一次就产生一个子事务：
循环几万次，就是几万个子事务，远超 64 的缓存上限，可见性判断全部退化成查 `pg_subtrans`。
等待事件指向子事务的 SLRU 争用（`SubtransSLRU` 一类），而不是行锁。

**处置**：
1. **尽量去掉 EXCEPTION**。逐行"出错就跳过"的写法，多数可以改成先校验再写入，
   或者用 `INSERT ... ON CONFLICT` 之类的原子语句一次搞定，根本不需要异常兜底。
2. 确实需要异常处理时，**把任务合理分组、把事务拆开**：不要在一个大事务里循环几万次，
   而是每 N 条提交一次，让子事务在每次提交时被清掉，确保单个事务内的子事务数量远低于 64。
3. 分组大小要可配置，而不是写死在代码里——数据量和并发都会变。

**复盘**：这类问题的隐蔽性在于代码看起来完全正常，异常处理甚至被认为是"健壮性"的体现。
**它是被"防御性编程"引入的性能故障。**

## 案例二：子事务暴涨，WAL 文件把 inode 用光

**现象**：数据库故障，**但磁盘空间还有富余**——`df -h` 看不出问题。

**定位**：查 inode 用量（`df -i`）才发现**inode 已经耗尽**。根因是 `pg_wal` 目录下
文件数量暴涨：子事务密集分配 XID，每个 XID 的分配都会写 WAL（事务分配、运行事务快照一类记录），
WAL 生成速率被显著拉高，WAL 段文件被大量新建。当回收跟不上生成时，
`pg_wal` 下文件数只增不减，把文件系统的 inode 表吃光。
inode 用光之后，所有需要"新建文件"的操作都失败——包括数据文件、临时文件、新连接相关文件，
故障面从 WAL 扩散到整个实例。

**处置**：清掉滞留的 WAL（确认归档与复制槽状态后），inode 立刻回收；
根治仍然是减少子事务，把 WAL 生成速率降下来。

**这条链路里最容易被忽略的一点**：inode 耗尽通常还需要一个"不回收"的条件配合——
归档失败、复制槽滞留、长事务挡住清理。子事务密集本身不一定会用光 inode，
但它会把"回收能力不足"这个问题提前引爆。所以两个方向都要治：
**源头少产生子事务，链路保证 WAL 能及时回收。**

## 怎么确认是子事务的问题

```sql
-- 1. 子事务 SLRU 的读放大（统计是累计值，先 pg_stat_reset_slru() 再观察窗口）
SELECT name, blks_zeroed, blks_hit, blks_read, blks_written
  FROM pg_stat_slru WHERE name = 'Subtrans';

-- 2. 正在等的会话是不是卡在子事务上
SELECT pid, wait_event_type, wait_event, state, query
  FROM pg_stat_activity WHERE wait_event ILIKE '%subtrans%';

-- 3. pg_subtrans 目录的文件数（间接反映子事务规模）
SELECT count(*) FROM pg_ls_dir('pg_subtrans');

-- 4. WAL 是不是回收不掉：文件数 + 归档 + 复制槽
SELECT count(*), min(modification) FROM pg_ls_waldir();
SELECT archived_count, failed_count, last_failed_time FROM pg_stat_archiver;
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots;
```

```bash
# 5. inode 必须单独看，-h 看不出 inode 问题
df -i $PGDATA/pg_wal
```

补充一点容易踩的认知：**没有直接暴露"当前子事务数量"的官方视图**，
只能通过 SLRU 读放大、子事务等待事件、`pg_subtrans` 目录增长这几个代理指标间接判断。

## 怎么改：四条落地建议

1. **先找子事务的产源，而不是先调参数**。除了显式 SAVEPOINT，至少四类地方会偷偷产生子事务：
   PL/pgSQL 的 EXCEPTION 块、ORM 框架的嵌套事务（Django 的 `atomic()` 嵌套、
   SQLAlchemy 的 `begin_nested()`）、ETL/同步工具的内置分批（有工具默认 50 个）、
   PL/Python 的 `plpy.subtransaction()`。
2. **能不用异常就不用**。用预校验、`ON CONFLICT`、批量写入替代逐行异常兜底。
3. **必须用就分组**：拆分事务边界，让每个事务内的子事务远低于 64，建议 10–20 量级，并且可配置。
4. **有只读副本时更保守**：子事务会显著放大从库的查询开销（快照需要查 `pg_subtrans`），
   主库还撑得住的时候从库可能已经断崖下滑，所以有从库查询业务时尽量不用子事务。
5. 顺带一提：子事务还会钉住清理进度（与 autovacuum 那一节相互影响），
   长事务 + 子事务的组合是清理跟不上写入的常见成因之一。

## 参考来源

- 《PostgreSQL 子事务详解及其对性能的影响》 https://liuzhilong.blog.csdn.net/article/details/130783474
- postgres.ai《PostgreSQL subtransactions considered harmful》 https://postgres.ai/blog/20210831-postgresql-subtransactions-considered-harmful
- GitLab《Why we spent the last month eliminating PostgreSQL subtransactions》 https://about.gitlab.com/blog/2021/09/29/why-we-spent-the-last-month-eliminating-postgresql-subtransactions/
- CyberTec《Subtransactions and performance in PostgreSQL》 https://www.cybertec-postgresql.com/en/subtransactions-and-performance-in-postgresql/
