> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 崩溃恢复跑了 40 分钟：先分清它在重放还是在等 WAL

## 现象：断电重启后，实例长时间停在恢复里（虚构）

机柜掉电（UPS 没覆盖到那一排），虚构的订单库 `orders_prod` 在主机 `pg-node-01` 上被非正常关机。来电后拉起实例，日志进入恢复，然后卡在那里快 40 分钟：

```
LOG:  database system was interrupted; last known up at 2026-05-12 03:11:07 CST
LOG:  database system was not properly shut down; automatic recovery in progress
LOG:  redo starts at 0/1A2B3C48
```

监控看不到实例，应用连不上，值班判断"恢复卡死了"，打算 `kill` 掉那个 startup 进程再重来。这一步会让计时器归零，还可能把一次正常恢复变成反复失败。

## 先收集什么证据：恢复期你连不进库，别指望 SQL 现场

这是崩溃恢复最容易踩的认知坑：**主库的崩溃恢复（不是 standby 的 archive recovery）期间，数据库不接受连接**，`pg_stat_activity` 你根本查不到。所以证据只有两类：日志、以及操作系统层。

日志层，关注这几行的位置与间距：

```
LOG:  redo starts at 0/1A2B3C48
...
LOG:  redo done at 0/2F4A11B0
LOG:  last completed transaction was at log time 2026-05-12 03:11:06.981553+08
LOG:  database system is ready to accept connections
```

系统层，要能区分"它在读盘"还是"它在空转"：

```bash
ps -o pid,etime,cmd -C postgres | grep startup
iostat -x 1 10
dmesg -T | tail -40
free -m
```

`ps` 那一行会显示成 `postgres: startup recovering 000000010000000000000001` 的样子，能直接看到它当前在哪个 WAL 段。再看控制文件，它记录了 checkpoint 位置：

```bash
pg_controldata -D /var/lib/pgsql/16/data
```

`pg_controldata` 会打印 "Latest checkpoint location" 一类的控制文件字段（`pg_controldata` 参考页），用它来估算还要重放多远，比盲等靠谱。

## 定位过程：一个二分法——在重放，还是在等

**第一刀：看设备是否有读 IO。** 跑着 `iostat -x 1` 盯数据盘：如果 `%util` 高、`r/s` 持续有值，说明 startup 进程真的在读 WAL 并重放；如果设备近乎空闲、CPU 也接近 0，那它不是慢，是**卡住或停下来等某样东西**。这两条路后面的处置完全相反。

**如果在重放，只是慢，那要问"为什么这次要重放的这么多"。** 重放量 ≈ 从 `redo starts at` 到 WAL 末端之间的距离，而它由"最后一次 checkpoint 有多旧"决定。按顺序查三个参数及其来源：

```sql
SELECT name, setting, source, sourcefile, sourceline
  FROM pg_settings
 WHERE name IN ('checkpoint_timeout','max_wal_size','min_wal_size',
                'checkpoint_completion_target','full_page_writes');
```

本次的答案很典型：为了压住白天的 checkpoint 写抖动，有人把 `checkpoint_timeout` 从 5 分钟提到 30 分钟、`max_wal_size` 从 1GB 提到 8GB，`source` 显示来自 `postgresql.auto.conf`。这确实让白天的写更平滑，代价就是 checkpoint 间隔变长、崩溃后要重放的 WAL 成倍增长。再加上 `full_page_writes` 默认为 on：每次 checkpoint 之后，每个数据页的首次修改都会把**整页**记进 WAL，重放量又放大一截（`Reliability and the Write-Ahead Log`）。所以"慢"是被配置预期出来的，不是坏。

**第二刀：如果设备空闲，就要判是"正常收尾"还是"真的失败"。** 正常情况下，重放到 WAL 记录末端会看到：

```
LOG:  invalid record length at 0/2F4A11B0: wanted 24, got 0
LOG:  redo done at 0/2F4A11B0
```

`invalid record length ... got 0` 看着像错误，其实只是"到这里没有下一条记录了"的正常结束标志。反过来，如果日志出现 `could not open file "pg_wal/00000001000000000000001B"`、`PANIC` 或 `could not locate a valid checkpoint record`，那才是恢复失败：前者说明缺 WAL 段（主库崩溃恢复不依赖归档，段本该在 `pg_wal` 里），后者说明 checkpoint 记录本身不可读。到这一步才轮到"从备份恢复"进入选项。

**两个必须排除的误判**：

- "卡在某个 LSN 不动"多半是误判。启动过程本身有进度日志：`log_startup_progress_interval` 默认 10 秒，会让 startup 进程为长时间未完成的启动操作（同步数据目录、重置 unlogged 关系、重放等）周期性打点；若它被改成 0 关掉了，就只能靠 `iostat` 和 `pg_controldata` 的字段判断——总之别凭"日志没动"下结论。
- "重启一下也许更快"是错的。`kill` 掉 startup 进程等于把已经重放的部分作废，下次启动要从头再来；反复在中途杀掉，可能因为不断崩溃重来而永远到不了末端。

## 结论与处置：这次是正常恢复，只是 checkpoint 间隔被调得太长

根因：为压 checkpoint 写抖动，把 `checkpoint_timeout`、`max_wal_size` 调大且 `full_page_writes=on`，导致崩溃后需要重放的 WAL 量显著增加；恢复过程本身正常，只是耗时长。

处置动作：

- 先停手，用 `iostat` 确认 startup 进程在持续推进，耐心等它跑完，不要 `kill`。
- 恢复完成后记录这次耗时，并把两个参数折中，在"写抖动"与"恢复时间"之间取平衡，例如 `checkpoint_timeout` 回到 15 分钟、`max_wal_size` 回到 4GB；`checkpoint_completion_target` 保持默认 0.9 附近，让写尽量摊平。
- 打开 `log_checkpoints = on`，让每次 checkpoint 的写量与耗时可见。

验证指标：再次跑 `pg_controldata` 看到 "Latest checkpoint location" 已经前进；日志出现 `database system is ready to accept connections`；实例可连接后 `pg_postmaster_start_time()` 与本次启动一致；把本次恢复耗时与 `redo done - redo start` 的距离一起记进值班手册，用于下次估算等待时间。

## 复盘要点

- **恢复慢不等于数据丢**。先量"还要重放多远"，再决定要不要动用备份；多数情况它只是需要时间。
- **崩溃恢复期连不进库，SQL 视图帮不上忙**。判活靠日志加 `iostat`、`pg_controldata`，不要因为查不到 `pg_stat_activity` 就以为实例死了。
- **`invalid record length ... got 0` 是正常收尾，不是错误**；把"缺 WAL 段 / PANIC / 找不到有效 checkpoint"当成真正的失败信号。
- **不要 kill startup 进程、不要中途重启**。那会把进度清零，甚至因为反复失败而永远恢复不完。
- **checkpoint 相关参数是"写性能"和"恢复时间"的跷跷板**。调大到舒服以后，务必把"崩溃恢复会长"这件事写进预期，别等真出事才想起。
