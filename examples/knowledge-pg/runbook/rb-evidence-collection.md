> 语料来源：公开 PostgreSQL 通用知识整理 + 虚构案例，不含任何公司内部信息。

# 故障取证：现象、系统日志与数据库日志怎么采

## 为什么先定证据标准，而不是先定结论

故障现场最大的成本不是不会修，是**在信息不全的时候开始猜**。一旦开始猜，后面每一步都在为第一个猜测找证据，而不是在收集事实。

所以把"取证"单独拿出来当一步：在动任何参数、重启任何进程之前，先用固定格式把四类事实收齐——现象、系统层日志、数据库日志、变化点。四类都齐了再往下走，缺哪一类就先去补哪一类。

这个顺序还有一层现实考虑：**有些证据是不可再生的**。重启一次，`pg_stat_activity` 现场没了；reload 一次，参数被改过这件事就看不出来了；VACUUM 跑完，膨胀率回到正常。所以采集要在处置之前完成，而不是"先试试重启，不行再查"。

## 现象：把"慢"翻译成可检验的陈述

"数据库很慢"不是一个现象，是一个投诉。要把它加工成能被证伪的陈述，需要补齐五个维度：

| 维度 | 要问到的粒度 | 为什么需要 |
|---|---|---|
| 时间 | 开始时刻（精确到分钟）、持续多久、是否仍在持续 | 能去日志里对齐，也能判断是持续劣化还是单次尖峰 |
| 影响面 | 一个应用、一类操作，还是全部连不上 | 决定走实例级排查还是单语句排查 |
| 可复现性 | 稳定复现、偶发、还是只出现过一次 | 决定能不能用实验去验证假设 |
| 变化点 | 最近有没有发版、改参数、加索引、扩容、切换 | 八成以上的"突然变慢"能在这里找到起点 |
| 度量口径 | 是响应时间变长、吞吐下降，还是超时报错变多 | 三种现象的方向不同，指向的原因也不同 |

最后一条最容易被忽略。**响应时间变长和吞吐下降可以同时发生，也可以只发生一个**：锁等待会让两者同时恶化；而连接池耗尽可能表现为吞吐下降、单条响应时间却正常。先分清是哪一个，后面的取舍才不一样。

## 系统层日志：三个必看的地方

数据库慢的根因有相当比例不在数据库里，先把下面三处扫一遍再进库：

**内核环形缓冲区**——OOM killer 和 IO 错误只在这里留痕：

```bash
dmesg -T | tail -80
```

找这几类关键字：`oom-kill`（进程被内存压力杀掉）、`Out of memory`、`I/O error`、`blk_update_request`、`EXT4-fs error`、`XFS` 相关错误。**如果 postmaster 或某个 backend 被 OOM killer 杀过，数据库日志里往往只有"terminated by signal 9"，看不出原因**——这一条不查就会误诊成数据库自己崩了。

**阻塞超过 120 秒的任务**——磁盘或存储卡住时内核会记录：

```bash
dmesg -T | grep -i "blocked for more than"
journalctl -k --since "2 hours ago" | grep -i "hung_task\|blocked for"
```

出现 `blocked for more than 120 seconds` 基本可以断定是存储层问题，而不是 SQL 问题。

**资源历史曲线**——`sysstat` 装没装决定你能不能做时间回溯：

```bash
sar -u -f /var/log/sa/sa$(date +%d)     # CPU
sar -d -f /var/log/sa/sa$(date +%d)     # 磁盘
sar -r -f /var/log/sa/sa$(date +%d)     # 内存
```

没有历史数据时，退而求其次看压力累积指标（Linux 内核 PSI，可在容器内读）：

```bash
cat /proc/pressure/io /proc/pressure/memory /proc/pressure/cpu
```

`some` 行表示"至少有一个任务在等"，`full` 行表示"所有任务都在等"。**`full` 行的值持续上升，说明已经没有任何可用的空闲能力**，这比单点 CPU 使用率更有说服力。

如果是容器里跑的数据库，还要单独确认 cgroup 限制——数据库自己看到的 CPU/内存和容器被允许用的不是一回事：

```bash
cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max 2>/dev/null   # cgroup v2
cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null        # cgroup v1
```

## 数据库日志：先确认它记全了

出事之后才发现日志没配全，是最常见也最不该犯的错。日常就该确认这几个参数：

```sql
SELECT name, setting, source
  FROM pg_settings
 WHERE name IN ('log_destination','logging_collector','log_directory',
                'log_line_prefix','log_min_messages','log_min_error_statement',
                'log_min_duration_statement','log_checkpoints','log_connections',
                'log_disconnections','log_lock_waits','log_temp_files',
                'log_autovacuum_min_duration','log_replication_commands')
 ORDER BY name;
```

`source` 这一列很关键：它告诉你这个值是默认值、配置文件里写的，还是被 `ALTER SYSTEM` 改过。**诊断"某个参数是谁改的"只能靠它。**

`log_line_prefix` 直接决定日志可不可用。一个能支撑排查的前缀至少要有时间、PID、用户、库名：

```
log_line_prefix = '%m [%p] %q%u@%d/%a '
```

- `%m` 带毫秒的时间戳——没有它，多实例日志无法对齐，也无法和系统日志对齐
- `%p` 进程号——把一条错误和它前后的语句串起来靠这个
- `%u@%d/%a` 用户、数据库、应用名——区分是业务流量还是运维操作
- `%q` 用于会话内非会话进程，避免输出错位

推荐 `log_destination = 'csvlog'` 或 `'jsonlog'`：日志变成结构化字段，可以直接用 `COPY` 或程序解析，比正则匹配文本可靠得多。

当 SQL 确实很慢但仍能跑完时，让数据库主动把慢语句记下来，比事后翻 `pg_stat_statements` 更完整（后者只有统计量）：

```
log_min_duration_statement = '1s'      -- 先宽后窄：先 1s 抓全，定位后收紧到 200ms
log_lock_waits = on                    -- 记录等待超过 deadlock_timeout 的锁
log_temp_files = 0                     -- 记录排序/哈希落到临时文件的语句
log_checkpoints = on                   -- checkpoint 频率与写盘量
log_autovacuum_min_duration = '250ms'  -- autovacuum 的实际动作
```

日志文件的实际位置由 `log_directory` 决定（相对 `$PGDATA`，或写绝对路径）；若 `logging_collector = off`，日志进的是进程的 stderr，通常被 systemd 的 journald 接管：

```bash
journalctl -u postgresql-16 --since "2 hours ago" -p warning
```

## 读日志的三个纪律

**按时间倒着读，但按事件聚合。** 一次故障会产生成百上千行重复报错，逐行读会淹没在一模一样的信息里。先看错误级别的分布和时间跨度：

```bash
grep -oE "FATAL|PANIC|ERROR|WARNING" postgresql-*.log | sort | uniq -c
grep -c "deadlock detected" postgresql-*.log
```

**区分错误级别**：`ERROR` 是语句失败但会话继续；`FATAL` 是会话被终止；`PANIC` 是整个实例进入恢复。**看到 `PANIC` 就不要再往下猜业务逻辑了**，直接转到崩溃恢复这条线。

**留意崩溃时间点的三行**。恢复时数据库会写：

```
LOG:  database system was interrupted; last known up at ...
LOG:  database system was not properly shut down; automatic recovery in progress
LOG:  redo starts at ...
```

这三行给出了崩溃时刻与重做起点，是判断"丢了多久数据"的直接依据。

## 时间对齐：一次对不上，后面全白做

三个时钟要对齐检查：数据库服务器、应用服务器、日志收集系统。常见坑有两个：

- **时区**：数据库日志用本地时间，操作系统日志可能用 UTC。差 8 小时会把因果关系完全搞反。
- **NTP 漂移**：虚拟机挂起、宿主机时间调整都会让时钟跳变，日志里出现时间倒退。

对齐做法是先取一个绝对锚点，再让两侧日志围绕它展开：

```bash
date -u; timedatectl status | grep -i "synchronized\|NTP"
SELECT now(), pg_postmaster_start_time();
```

## 一份可复盘的证据包

采集的终点不是"我知道了原因"，是**这次采到的东西能让别人复核**。至少留下：

1. 现象陈述（上面五个维度填齐）
2. 关键日志片段（带原始时间戳，不要二次排版）
3. 当时的一次性快照：`pg_stat_activity`、`pg_locks`、`pg_stat_replication`、`pg_stat_database` 各留存一份
4. 关键参数与它们 `pg_settings.source`
5. 处置动作与动作前后的对比量

第 5 条最容易被略过。**处置记录不是给别人看的档案，它是下一次同类故障的检索线索**——同一个现象第二次出现时，能立刻想起上次改了什么，排查路径就短了一大截。
