> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# CPU 打满：先把 CPU 拆开，再决定动哪里

## 现象：CPU 长时间贴在 100%（虚构）

虚构的订单库 `orders_prod` 跑在一台 4 核的云主机 `pg-node-01` 上。某个下午开始，CPU 使用率长时间贴着 95% 以上，查询整体变慢。运维群里已经出现两种声音："加 CPU" 和 "调大 work_mem"。这两种都可能没用——CPU 打满至少有三种截然不同的成因，先分层才能选对方向。

## 先收集什么证据：先把 CPU 拆成用户态、内核态、等待

系统层，目标是把一颗 CPU 的时间拆开，看它到底花在哪：

```bash
top -b -n1 -H | head -30
pidstat -u 1 5
vmstat 1 5
mpstat -P ALL 1 3
cat /proc/pressure/cpu
```

读法：`mpstat`／`vmstat` 里 `us` 高说明在算，`sy` 高说明系统调用与上下文切换多，`wa` 高其实是 IO 等待（那是另一条线），`steal` 高说明被虚拟化邻居抢走了——**此时加 CPU 也没用，要找宿主**。`vmstat` 的 `r` 列（可运行队列长度）比使用率更能反映"排队程度"。

数据库层，先看"谁在占 CPU"以及并行的规模：

```sql
SELECT count(*), state, wait_event_type
  FROM pg_stat_activity GROUP BY state, wait_event_type ORDER BY 1 DESC;

SELECT backend_type, count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC;

SELECT left(query, 70) AS query, count(*) AS workers
  FROM pg_stat_activity
 WHERE backend_type = 'parallel worker'
 GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

再按累计耗时找"吃 CPU 的语句"，注意联看 `rows` 与 `calls` 判断是否计划劣化：

```sql
SELECT calls, rows,
       round(total_exec_time::numeric, 0) AS total_ms,
       round(mean_exec_time::numeric, 1)  AS mean_ms,
       left(query, 70) AS query
  FROM pg_stat_statements
 ORDER BY total_exec_time DESC LIMIT 15;

SELECT name, setting FROM pg_settings
 WHERE name IN ('max_parallel_workers','max_parallel_workers_per_gather',
                'max_worker_processes','max_connections','work_mem');
```

**CPU 一直跑满时，从哪一层开始拆**：第一步永远是把 CPU 拆成用户态、内核态、IO 等待、以及被虚拟化层偷走的部分（`us / sy / wa / st`）。因为"跑满"这个说法本身不指向任何原因——内核态高是上下文切换或系统调用密集，IO 等待高说明 CPU 在等盘、此时加 CPU 无效，`st` 高则说明是宿主机层面被抢走了、数据库侧怎么调都没用。**先分层，再决定动哪里。**

## 定位过程：三层归因，再排两个假设

**第一层：CPU 花在算还是花在切换？** 本次读数：`us` 高、`sy` 中等、`wa` 低、`steal` 为 0。于是先排除两件事——不是 IO 瓶颈（`wa` 低），也不是被虚拟化邻居抢（`steal` 0）。方向锁定在"查询真的在 CPU 上算"。

**第二层：是不是并行查询太多。** 数 `backend_type = 'parallel worker'` 的会话，发现某条聚合查询每个执行实例拉起 7～8 个 worker，同时有 6 个这样的实例在跑——几十个进程抢 4 个核，越抢越慢。这里有一个反直觉结论：**并行不是免费的，它把 CPU 压力成倍放大**，而且文档明确说：请求的 worker 数量"可能实际拿不到"，因为受 `max_worker_processes` 与 `max_parallel_workers` 全局限制（`Server Configuration`）。所以"设了并行度 8"不等于"真有 8 个核在为你干活"，它更可能是十几条查询互相抢占、集体变慢。在高并发 OLTP 上，把 `max_parallel_workers_per_gather` 调小反而常常提升整体吞吐。

**第三层：是不是计划劣化导致重复扫描。** 在 `pg_stat_statements` 里，那条聚合查询 `calls` 不高但 `total_exec_time` 很高，且 `rows`（实际返回/处理行数）远大于预期——典型的统计信息过期后估算失准，退化成大表顺序扫描。用 `EXPLAIN (ANALYZE, BUFFERS)` 复核，计划里出现对 3 亿行 `orders` 表的并行顺序扫描、且缓冲区命中率低，坐实这条。

**再排除两个容易走错的方向：**

- **"CPU 高 = 查询重"不一定成立。** 还要看连接数与 CPU 的关系：如果 `sy` 占比高、单条查询耗时其实正常、只是连接数远超核数，那瓶颈是"连接太多"。几百个连接轮流跑小查询，每次都建快照、切换上下文、抢 `ProcArray` 一类的轻量锁，`sy` 与 CPU 一起上去，但每条查询都不慢。这时**减少连接／上连接池比加 CPU 有效**。本次 `sy` 不算高、单条查询确实很慢，所以不是这条。
- **"加 CPU 就能好"不成立。** 4 核上已经几十个并行 worker 在抢，核数翻倍只是让抢的人更多；真正的动作是降低并行度和修计划。

## 结论与处置：修计划劣化，同时把并行度压下来

根因：一次数据倾斜后相关表的统计信息未及时更新，导致一条按租户聚合的查询计划劣化，退化成对大表的并行顺序扫描；叠加偏高的 `max_parallel_workers_per_gather`，多个并发实例同时拉起大量并行 worker，把 4 核 CPU 打满。

处置动作：

- 对相关表执行 `ANALYZE`，必要时提高目标列的统计目标，让估算回到准确区间。
- 评估补索引（如 `(tenant_id, created_at)` 这类覆盖过滤与排序的复合索引），把顺序扫描换成索引扫描。
- 下调 `max_parallel_workers_per_gather`（例如 8 → 2），把并行留给真正的分析型查询；同时给报表类查询设 `statement_timeout`，或迁到只读实例，避免与在线业务抢核。
- 若现场判断是"连接太多"那一条，则改上连接池、压低连接数，而不是加 CPU。

验证指标：`mpstat` 的 `us` 回落、`r` 队列缩短；`parallel worker` 会话数下降；该查询在 `pg_stat_statements` 里的 `total_exec_time` 大幅下降；`pg_stat_database` 的 `blks_read` 回落（扫描行数减少）。

## 复盘要点

- **CPU 打满先分层**：`us`／`sy`／`wa`／`steal` 分别指向计算、切换、IO、宿主机竞争，方向完全不同。
- **并行是把双刃剑**。它加速单条大查询，却在并发下把 CPU 压力放大；`max_parallel_workers_per_gather` 在高并发 OLTP 上应保守。
- **计划劣化会伪装成"CPU 故障"**。看 `pg_stat_statements` 的 `rows` 与 `calls`、再用 `EXPLAIN (ANALYZE, BUFFERS)` 复核，别只看耗时。
- **高 CPU 有时是连接太多**。`sy` 高、单条查询不慢时，减少连接比加核更对症。
- **加 CPU 之前先问问 `steal`**。云主机/容器被邻居抢时，加核解决不了问题。
