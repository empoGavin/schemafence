> 语料来源：公开 PostgreSQL 通用知识整理 + 虚构案例，不含任何公司内部信息。

# 性能诊断流程（整库）：从操作系统往下走，五层收窄

## 阶段 0：先把"慢"定义清楚，否则后面全是白工

"系统慢"这句话里混着两种完全不同的现象，先在入口处分开：

| 现象 | 度量 | 常见根因方向 |
|---|---|---|
| 响应时间变长（单条变慢） | p95 / p99 延迟 | 锁等待、计划劣化、IO 抖动 |
| 吞吐下降（每秒完成数变少） | QPS / TPS | 连接不足、CPU 饱和、IO 打满 |

两者可以同时恶化，也可以只坏一个。**先分清是哪一个，后面的优先级就不一样**：响应时间问题优先查等待，吞吐问题优先查资源上限。

同时定一个基准：正常时段的这个指标是多少。没有基线的"慢"无法度量，也就无法证明修好了。

## 阶段 1：操作系统层——先把数据库之外的嫌疑排掉

数据库慢的根因有相当比例不在数据库里。这一层的任务是回答一个问题：**机器本身还有多少余量。**

### CPU：不要只看使用率，要看花在哪
```bash
vmstat 1 5
mpstat -P ALL 1 3
```
看 `us / sy / wa / st / id` 五个数：
- `us` 高 → 用户态忙，多半在跑查询
- `sy` 高 → 内核态忙，可能是上下文切换过多（连接数极多、短连接频繁）或系统调用密集
- **`wa` 高 → CPU 在等 IO，此时加 CPU 没有意义**
- **`st` 高 → 被宿主机或其他虚拟机抢走了 CPU**（虚拟化环境专属）。这一条最容易被忽略：数据库自己在等 CPU，而它并不知道自己是被"偷"走的，任何数据库侧调优都无效
- 上下文切换次数（`cs`）异常高时，先怀疑连接数

### 内存：看有没有换出
```bash
free -m
vmstat 1 5      # 关注 si / so（换入换出）
```
`si`/`so` 持续非零说明发生了 swap——**数据库一旦被换出，延迟会呈现数量级的抖动**，这比"内存不够"更严重。同时看 `available` 而不是 `free`：Linux 的 buffer/cache 是可回收的。

### IO：分离"队列深度"和"单次延迟"
```bash
iostat -x 5 5     # 关注 await、r_await、w_await、%util、aqu-sz
```
- **`await` 高但队列浅（`aqu-sz` 小）→ 单次 IO 就慢**，是存储层问题（网络存储抖动、磁盘老化、云盘 IOPS 被限流）
- **`await` 高且队列深 → 并发压力大**，是负载问题
- `%util` 接近 100 只说明设备一直忙，**不代表它是瓶颈**（SSD 可以轻松跑满 util 还有余量）

### 压力累积（容器里唯一能用的容量指标）
```bash
cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
```
`full` 行表示"所有可运行任务都在等"。**它比瞬时使用率更能说明"已经没有任何空闲能力"**。

### 这一层的输出
一句话结论 + 是否继续往下：**机器有余量 → 进第 2 层；机器已饱和 → 先在系统层定位是谁、要不要扩容，同时继续往下找放大因素。**

## 阶段 2：实例层——数据库自己消耗了什么

### 连接与并发
```sql
SELECT count(*), state FROM pg_stat_activity GROUP BY state ORDER BY 2;
SELECT setting FROM pg_settings WHERE name = 'max_connections';
```
活跃连接数远小于 `max_connections` 却还是很慢，说明**瓶颈不在并发度，而在单条效率**。

### 缓存命中与 IO 结构
```sql
SELECT datname, blks_hit, blks_read,
       round(100.0 * blks_hit / nullif(blks_hit + blks_read, 0), 2) AS hit_pct,
       xact_commit, xact_rollback, deadlocks, conflicts, temp_files, temp_bytes
  FROM pg_stat_database WHERE datname = current_database();
```
- 命中率突然下降 → `shared_buffers` 装不下了，或出现了大量一次性扫描（报表、全表扫）
- **`temp_bytes` 快速增长 → 排序/哈希落盘**，说明 `work_mem` 不足或语句写法放大了中间结果
- `xact_rollback` 异常高 → 应用在大量回滚（约束冲突、超时重试），本身也消耗资源

### 写入侧的三个放大点
```sql
SELECT * FROM pg_stat_bgwriter;
SELECT * FROM pg_stat_checkpointer;      -- PG17+ 拆出的独立视图
SELECT * FROM pg_stat_archiver;
```
- `checkpoints_req` 远多于 `checkpoints_timed` → **checkpoint 是被"要求"触发的，根因通常是 `max_wal_size` 太小**，而不是 checkpoint 参数本身
- `buffers_backend_fsync` 非零 → 后端进程自己在做 fsync，说明写压力已超出后台写能力
- 归档失败会累积 `pg_wal` 并最终导致保护性停写

### autovacuum 与表级异常
```sql
SELECT relname, n_dead_tup, n_live_tup,
       last_autovacuum, last_autoanalyze
  FROM pg_stat_user_tables
 ORDER BY n_dead_tup DESC LIMIT 10;
```
死元组居高不下会同时导致两个后果：**表物理膨胀（多读页）和统计陈旧（计划变差）**。

### 锁
```sql
SELECT mode, count(*) FROM pg_locks GROUP BY mode ORDER BY 2 DESC;
SELECT pid, wait_event_type, wait_event, left(query, 80)
  FROM pg_stat_activity WHERE wait_event_type = 'Lock';
```
表级锁（`AccessExclusiveLock` 等）排队会形成"整表不可读"的放大效应，见第 4 层。

## 阶段 3：会话层——谁在占资源

```sql
SELECT state, wait_event_type, wait_event, count(*) AS sessions,
       round(max(now() - xact_start)::numeric, 1) AS max_xact_age_s
  FROM pg_stat_activity
 WHERE backend_type = 'client backend'
 GROUP BY 1, 2, 3
 ORDER BY sessions DESC;
```
三类会话的处理方向不同：

- **`active` 且数量多** → 真在算，看第 4 层的等待类型判断算得顺不顺
- **`idle in transaction`** → 占着快照不放。**它不消耗 CPU，却会钉住 xmin 地平线，让全库回收停滞**，还会长期持有锁。超过阈值必须处理，`idle_in_transaction_session_timeout` 就是为它准备的
- **`idle` 但数量极大** → 连接池配置问题，转连接与内存篇目

同时把最老的几个事务列出来（**长事务是很多"莫名变慢"的共同根因**）：
```sql
SELECT pid, now() - xact_start AS age, state, left(query, 100)
  FROM pg_stat_activity
 WHERE xact_start IS NOT NULL
 ORDER BY age DESC LIMIT 10;
```

## 阶段 4：等待层——瓶颈到底在等什么

这是整条流程里信息量最大的一层。把等待事件按类型聚合，**瓶颈会被自动归类**：

```sql
SELECT wait_event_type, wait_event, count(*)
  FROM pg_stat_activity WHERE state = 'active'
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

映射关系与含义：

| 等待类型 | 说明 | 下一步 |
|---|---|---|
| `IO`（如 `DataFileRead`） | 在读盘，缓存没兜住 | 回第 1 层看存储，或查是不是计划导致读太多 |
| `Lock`（如 `transactionid`、`relation`） | 被锁挡住 | 用 `pg_blocking_pids()` 找出阻塞源 |
| `LWLock`（如 `BufferContent`） | 内部争用 | 看是不是热点页争用、并发过高 |
| `Client`（如 `ClientRead`） | **在等客户端发数据/收结果** | 数据库不慢，是应用或网络 |
| `IPC` / `Timeout` | 等并行 worker 或主动等待 | 看并行度配置 |

**最容易误判的一条**：`Client:ClientRead` 占比高时，数据库侧看不出任何问题，因为它真的没在干活——时间花在等应用取结果上。**这类"慢"改数据库参数一定无效。**

## 阶段 5：综合知识库，给出结论并分岔

前四层的证据要拼成一句话，而不是并列罗列。拼法是问三个问题：

1. **瓶颈在哪一层**：系统资源 / 实例配置 / 会话结构 / 等待对象。
2. **是否有单一放大器**：一个慢 SQL、一个长事务、一个失效的 autovacuum、一个被占满的连接池——**多数"整库变慢"最终会收敛到一个具体对象**。
3. **处置的收益与代价**：扩容能解决吗？改参数要重启吗？限流会影响业务吗？

带着现象描述去知识库检索（如"整库变慢 IO 等待高""缓存命中率下降"），对照同类案例的判据与处置顺序，能显著缩短从证据到结论的距离。

最后分岔：

- **瓶颈由个别 SQL 主导**（`pg_stat_statements` 里少数语句占了大部分 `total_exec_time`，或等待集中在 `IO:DataFileRead` 且只来自几条语句）→ **进入 SQL 与执行计划诊断流程**。
- **瓶颈是全局资源或配置**（CPU 饱和、连接池耗尽、checkpoint 风暴、autovacuum 跟不上）→ 按第 2 层对应篇目处置，处置后重测阶段 0 定义的指标。
- **瓶颈在数据库之外**（`Client:ClientRead`、`st` 高、应用侧排队）→ 把结论交回应用侧或平台侧，**并给出证据**，不要用数据库参数去解释别人的问题。

## 一条纪律：改一项，测一次

整库诊断最常见的失败是"一次改了五个参数，变好了但不知道为什么好"。**收敛到单一假设、只改一个变量、用同一组指标复测**——这既是排障效率问题，也是能不能把经验写进知识库的前提。
