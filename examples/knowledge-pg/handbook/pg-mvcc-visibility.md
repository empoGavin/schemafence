> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# MVCC 与可见性：xmin、xmax、快照、HOT 与长事务为什么危险

## 每一行都自带两个事务号：xmin 和 xmax

PostgreSQL 用多版本并发控制（MVCC）来实现读写不互相阻塞。它的实现方式是：一次 UPDATE 不是就地改写，而是**插入一个新版本、把旧版本标记为删除**。为了记录这件事，每个行版本的行头里有两个 4 字节字段：

- `t_xmin`：插入这个行版本的事务 ID
- `t_xmax`：删除或更新这个行版本的事务 ID（未被删除时为 0）
- `t_ctid`：指向本行版本自身，或指向更新的那一版

还有一个 32 位的 `CommandId`（`t_cid`），用来区分同一事务内先后两条命令产生的版本。判断一行对某个快照是否可见，就是拿这两个 XID 和快照的可见范围去比。

```sql
SELECT ctid, xmin, xmax, tenant_id, amount FROM orders_2024 LIMIT 5;
```

这里的 `xmin`/`xmax` 是系统列，任何表都能直接查，是理解"这行还能不能被其他事务看到"最直接的入口。

## 事务号是 32 位的，所以它是环形的

内部事务 ID 是 32 位，每 40 亿个事务回卷一次（epoch 每回卷一次加一，另有 64 位的 `xid8` 不回卷）。比较用的是模 2³² 运算，意思是：对任意一个普通 XID，都有约 20 亿个"更老"的和 20 亿个"更新"的。所以 XID 空间是**环形的、没有端点的**——这正是回卷问题的根源（详见回卷那一篇）。

另外要区分两种事务标识：虚拟事务 ID（virtualxid，形如 `4/12532`，由 backend ID 加本进程内序号组成）和真实 XID（如 278394，全局分配）。只有真正写过库的事务才会被分配真实 XID；只读事务往往只有 virtualxid。这一点在 `pg_locks` 里会直接体现出来。

## 快照：可见性是"相对某个时刻"说的

一个快照规定了"哪些事务已提交、哪些还在跑"。三种隔离级别的差别，本质上就是快照在哪里取：

- `READ COMMITTED`（默认）：**每条语句**开始时取一个新快照。所以同一事务里两条 SELECT 可能看到不同数据。
- `REPEATABLE READ`：整个事务共用事务开始时（准确说，是第一条非事务控制语句开始时）的那一个快照，因此同一事务里的查询结果稳定。它实现的就是学术界说的快照隔离（Snapshot Isolation）。
- `SERIALIZABLE`：在可重复读的基础上再叠加读写依赖检测（SSI），用于阻止序列化异常。

在 `READ COMMITTED` 下还有一个反直觉的点：一条 UPDATE 可能在同一次执行里**看到不一致的快照**——它能看见并发更新对自己要改的行造成的影响（会等对方提交后，基于新版本重新检查 WHERE），却看不见这些并发命令对库中其它行的影响。所以它不适合复杂的搜索条件，却非常适合"按主键加减余额"这种简单情形。

`REPEATABLE READ` 下如果一条行在事务开始后被别的事务改过，你会直接收到：

```
ERROR:  could not serialize access due to concurrent update
```

`SERIALIZABLE` 检测到读写依赖时则会报 SQLSTATE `40001`，错误信息里通常带 `could not serialize access due to read/write dependencies among transactions`。这两种都不是 bug，而是设计的一部分：**应用必须准备好重试整个事务**，而且重试要包含"决定执行哪条 SQL、用哪组参数"的全部逻辑，所以 PostgreSQL 不提供自动重试。

## 更新链条与 HOT：为什么有的 UPDATE 不写索引

一次普通 UPDATE 会产生一个新版本，`ctid` 把旧版本指到新版本，形成一条版本链。默认情况下，每个版本的每条索引都要加一条索引项，代价很大。

PostgreSQL 有一个优化叫 HOT（heap-only tuple），能避开新增索引项。它成立的条件是两条：

1. UPDATE 没有修改**任何被索引引用的列**（不计汇总型索引，核心发行版里唯一的汇总型索引方法是 BRIN）；
2. 旧版本所在的那一页上有足够空闲空间放下新版本。

条件满足时，新版本留在同一页里，索引项不用新增，之后由 VACUUM 清理这条链。条件不满足（比如改了一个带索引的列），就要为每一条索引新增索引项。

```sql
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0 * n_tup_hot_upd / nullif(n_tup_upd,0), 1) AS hot_pct
  FROM pg_stat_all_tables
 WHERE relname = 'orders_2024';
```

如果 `hot_pct` 很低，通常说明这张表的更新总是碰到索引列，或者页上没有空闲空间（表太满）。

## 长事务为什么危险：它钉住了清理进度

一个还没结束的事务，会把它的快照对应的最老 XID 变成整个系统的"可见性下限"（xmin horizon）。只要有事务可能还需要看见某个死版本，VACUUM 就不能回收它。

所以长事务的直接后果不是它自己慢，而是：

- 死元组无法回收，表不断膨胀，索引也不断膨胀；
- 清理进度被整体拖后，间接抬高回卷风险；
- 如果还有子事务，情况会更糟（见子事务那一篇）。

`idle in transaction` 状态尤其隐蔽：会话没在跑 SQL，但事务开着，快照就一直挂着。这正是 `idle_in_transaction_session_timeout` 存在的理由。

```sql
SELECT pid, state, now() - xact_start AS xact_age,
       age(backend_xid) AS xid_age, age(backend_xmin) AS xmin_age,
       left(query, 60) AS query
  FROM pg_stat_activity
 WHERE xact_start IS NOT NULL
 ORDER BY xact_start;
```

`age(backend_xmin)` 就是这一行钉住的可见性下限有多老，是判断"它在拖累谁"的关键数字。

## 落到可观察：三个入口

一是 `pg_stat_activity` 看 `xact_start`、`backend_xid`、`backend_xmin`；二是 `pg_locks` 看它在等什么锁、持有什么锁；三是等 `Lock:transactionid` 这类等待事件冒头时，说明有人在等某个事务结束。

## 核心判断：长事务的代价不在它自身耗时，而在它挡住的回收

排障时最容易忽略"一个挂着不动的只读事务"。它不占 CPU、不占锁，看起来无害，但它是整个实例垃圾回收的进度条卡点。**当膨胀、索引变大、回卷告警同时出现时，优先去 `pg_stat_activity` 里找 `xact_start` 最早的那一行**，而不是先怀疑 autovacuum 参数配错。
