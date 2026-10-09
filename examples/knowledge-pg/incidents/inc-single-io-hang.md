> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# IO 卡住：这时候改数据库参数是最没用的那一步

## 现象：查询集体变慢，等待事件全是 IO（虚构）

18:00 开始，虚构的订单库 `orders_prod`（主机 `pg-node-01`，数据放在一块网络块存储上）所有查询一起变慢，部分查询要几十秒才返回。现场第一眼是：

```sql
SELECT wait_event_type, wait_event, count(*)
  FROM pg_stat_activity
 WHERE wait_event_type IS NOT NULL
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

结果里 `IO` 类等待（`DataFileRead`、`DataFileWrite` 等）排在最前。运维群里同时冒出两个主意："把 checkpoint 调稀一点" 和 "重启一下数据库"。**在存储变慢/挂起的情况下，这两步一个无效、一个有害。**

## 先收集什么证据：先分清"谁在等谁"

要回答的核心问题是：**是数据库把存储压垮了，还是存储自己慢、数据库在等它？** 这两者的处置完全相反。

系统层，看单次 IO 的延迟（`await`）而不是只看吞吐：

```bash
iostat -x 1 10
cat /proc/pressure/io
vmstat 1 5
dmesg -T | tail -60
journalctl -k --since "30 min ago" | grep -i "hung_task\|blocked for more than"
```

`iostat` 里要看三件事：`await`（单次 IO 平均耗时）、`aqu-sz`（队列深度）、`%util`。**如果每次 IO 都很慢、但 IOPS 并不高（`r/s`、`w/s` 很低），那就是存储端延迟问题，不是数据库 IOPS 打满。** `dmesg` 里出现 `task ... blocked for more than 120 seconds`，是内核判定存储卡死（hung_task）的信号。

数据库层，用等待事件和 `pg_stat_io` 量化，注意 `pg_stat_io` 的时间列只有在 `track_io_timing` 打开时才非零：

```sql
SELECT backend_type, object, context, reads, writes,
       round(write_time::numeric, 0) AS write_ms,
       fsyncs, round(fsync_time::numeric, 0) AS fsync_ms
  FROM pg_stat_io
 ORDER BY write_time DESC;
```

```sql
SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time,
       checkpoint_sync_time, buffers_checkpoint, buffers_backend, buffers_backend_fsync
  FROM pg_stat_bgwriter;

SELECT name, setting FROM pg_settings
 WHERE name IN ('track_io_timing','checkpoint_timeout','max_wal_size',
                'checkpoint_flush_after','wal_writer_delay','bgwriter_lru_maxpages');
```

## 定位过程：把"数据库在等 IO"和"存储慢"分开

**第一步：确认是在等 IO。** `wait_event_type = 'IO'`、且 `pg_stat_io` 的 `write_time`／`fsync_time` 暴涨，说明数据库确实卡在 IO 调用上。但这只说明"数据库在等"，不说明"数据库造成了慢"。

**第二步：看存储延迟的量级。** `iostat` 显示 `await` 从平时的 2ms 涨到接近 800ms，而 `r/s`、`w/s` 反而偏低、`aqu-sz` 不高——**IO 数量不多，但每一次都极慢**。这是典型的存储端（网络块存储/阵列/链路）问题。如果是数据库自己压垮存储，应该看到的是 IOPS 或带宽被顶满，而不是"几个 IO 磨蹭半天"。

**第三步：看内核有没有判死。** `dmesg`／`journalctl -k` 里出现 `blocked for more than 120 seconds`，说明内核层已经观察到任务长时间阻塞在块设备上。到这一步可以断定：**问题在存储，数据库是受害者。**

**第四步：排掉其它方向。** 不是锁（`wait_event_type` 里 `Lock` 很少）；不是 CPU（`mpstat` 空闲）；不是内存不足（`dmesg` 里没有 OOM，`vmstat` 没有持续换页）。

**checkpoint 在这里是什么角色？** checkpoint 会把一段时间内的脏页集中刷盘，`checkpoint_completion_target` 会把它摊平，但仍有波峰（`Reliability and the Write-Ahead Log`）。存储正常时这没问题；存储抖动时，checkpoint 会把抖动放大成"周期性写入停顿"。**但它只是放大器，不是根因**：判据是——把 checkpoint 频率调低之后，抖动依旧存在，那就说明病在存储。所以本次不能靠调 checkpoint 解决。

**两个必须拦住的动作：**

- **不要改数据库参数。** `checkpoint_timeout`、`shared_buffers`、`bgwriter_lru_maxpages` 这些都作用在数据库这一侧，管不到存储端的单次 IO 延迟。改了不但无效，还会制造"改了也没用"的混乱，甚至掩盖真正的信号。
- **不要重启数据库。** 重启会强制走一次崩溃恢复，恢复期间要重放 WAL、重新读大量数据页，**IO 需求不降反升**，很可能把本已脆弱的存储彻底压垮，把"慢"升级成"起不来"。

## 结论与处置：先救存储，数据库这侧只做减负

根因：底层网络块存储出现抖动/链路降速，单次 IO 延迟飙升，数据库大量等待 IO，表现为整体查询变慢。

处置动作：

- 立刻转向存储侧：查云盘/阵列/链路的健康与延迟曲线，必要时切到备用存储或迁移节点。
- 数据库侧只做**减负**，不做调参：暂停非关键的批量/报表/备份任务；临时降低并发与并行度；避免此刻执行 `VACUUM`、`REINDEX` 等重 IO 操作。
- 保持数据库在线等待存储恢复，不要重启、不要频繁 checkpoint。
- 事后：确认 `track_io_timing` 常开，让 `pg_stat_io` 的时间维度可用；给存储延迟单独设告警。

验证指标：`iostat` 的 `await` 回落到毫秒级；`dmesg` 不再出现 `blocked for more than 120 seconds`；`pg_stat_activity` 里 IO 等待占比下降；慢查询数量回到正常。

## 复盘要点

- **先分清"数据库在等 IO"和"存储慢"**。看 `await` 而不是只看 IOPS：慢而少是存储问题，多而满是数据库压垮存储。
- **存储卡住时改数据库参数无效**。瓶颈在内核块层/存储端，不是 checkpoint、`shared_buffers` 能碰到的。
- **千万不要此时重启数据库**。它会触发崩溃恢复，让 IO 需求暴增，把慢变成起不来。
- **`hung_task` 与 `dmesg` 是判定存储卡死的关键证据**，务必和 `iostat` 一起看。
- **checkpoint 是放大器不是根因**。把频率调低后抖动仍在，就该把注意力转到存储，而不是继续调数据库参数。
