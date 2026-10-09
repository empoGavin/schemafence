> 语料来源：公开 PostgreSQL 通用知识整理 + 虚构案例，不含任何公司内部信息。

# 单实例故障诊断流程：先分类，再排查，最后给结论

## 流程总览：第一个动作是判断实例在不在

单实例故障最浪费时间的地方，是不分类就动手。看到"连不上"就去重启，重启完问题还在——而且现场没了，重启前的 `pg_stat_activity` 和日志缓冲全丢。

入口只有一个动作：**确认 postmaster 进程在不在**。

```bash
systemctl status postgresql-16          # 或： pg_ctl status -D $PGDATA
ps -ef | grep -w postgres | grep -v grep
```

三种结果对应三条不同的路，**不要串着走**：

| 观察到 | 判断 | 走哪条路 |
|---|---|---|
| postmaster 进程不在 | 实例未运行 | A 路：启动失败或已崩溃 |
| 进程在，客户端连不上 | 实例在，接入层出问题 | B 路：连接与认证 |
| 能连上，但写不进去或功能异常 | 实例在，运行态异常 | C 路：只读、磁盘、权限 |
| 能连、能写，只是整体变慢 | 不是故障，是性能问题 | 转性能诊断流程 |

每一条路都按同一个节奏走：**现象 → 日志 → 分层定位 → 知识库对照 → 结论**。中间任何一步拿到决定性证据，直接跳到最后一步，不必把流程走完。

## A 路：实例不在——先读日志的最后一行，而不是先重启

### 现象收集
先问清三件事：是**一直起不来**，还是**运行中突然消失**（这两者的根因分布完全不同）；有没有人做过重启、改参数、改权限、扩磁盘；业务侧是超时报错还是连接被拒。

### 日志收集
数据库日志位置由 `log_directory` 决定（相对 `$PGDATA` 或绝对路径）；若 `logging_collector = off`，日志进了 journald：

```bash
tail -100 $PGDATA/log/postgresql-*.log
journalctl -u postgresql-16 --since "1 hour ago" -p warning
```

同时看系统层——**这一层不查，会把它误诊成数据库自己崩了**：

```bash
dmesg -T | tail -80                      # oom-kill、I/O error、文件系统错误
dmesg -T | grep -i "blocked for more than"
```

### 分层定位
按日志最后一行的措辞分流：

- **`permission denied` / `could not open file`** → 数据目录属主或权限被人动过。确认目录属主是运行用户，且权限为 `0700`（`ls -ld $PGDATA`）。注意：**不要用 `chmod -R 777` 图省事**，那是把安全问题换成了另一个问题。
- **`address already in use`** → 端口被别的进程占了（可能有残留 postmaster，或别的实例）。`ss -lntp | grep 5432` 确认持有者是谁，再决定清理哪个。
- **`could not create lock file` / postmaster.pid 相关** → 先确认**没有存活进程**再处理 pid 文件：`pg_ctl status` 报 `no server running` 才可删。**pid 残留只是把排查带偏的表象**，删之前必须确认进程真的没了。
- **`invalid checkpoint record` / `could not locate a valid checkpoint` / `pg_control` 相关** → 控制文件或 WAL 已被破坏，进入恢复只能靠备份，不要再反复重启消耗剩余 WAL。
- **`unrecognized configuration parameter`** → 配置项拼写错误，或版本不支持该参数。用 `postgres -C 参数名 -D $PGDATA` 单独验证一个参数是否能被接受。

### 知识库对照
把日志最后一行加上关键措辞去知识库检索（如"启动失败 permission denied 数据目录"），对照同类案例的处置顺序。多数启动失败在"权限被改"和"端口被占"两类里。

### 结论
给出三样：**根因（可验证的一句话）**、**处置动作**、**验证指标**。例如"数据目录属主被批量改权限脚本改为 root，postmaster 无写权限；恢复属主为 postgres、权限回 0700，实例启动后 `pg_isready` 返回 accepting connections"。

## B 路：进程在但连不上——分应用侧和网络侧

### 现象收集
先区分范围：**所有客户端都连不上，还是只有某个应用连不上**。全都连不上，问题在实例或接入层；只有某一个应用，问题在那个应用的连接串、认证或防火墙策略。

### 日志收集
```bash
grep -E "FATAL|authentication failed|no pg_hba.conf entry|remaining connection slots" \
     $PGDATA/log/postgresql-*.log | tail -40
ss -lntp | grep 5432
```

### 分层定位
- **`no pg_hba.conf entry for host ...`** → 客户端来源不在 `pg_hba.conf` 允许范围。注意**匹配顺序是从上到下第一条命中生效**，未命中才拒绝；改完必须 `SELECT pg_reload_conf();` 或 `pg_ctl reload` 才生效。
- **`password authentication failed`** → 凭据问题。先排除密码错，再看认证方法是否被改（`scram-sha-256` 与老客户端不兼容是常见版本升级后遗症）。
- **`remaining connection slots are reserved` / `too many clients already`** → 连接数打满，转另一条线：按 `state` 分类连接，见"C 路"与连接池相关篇目。
- **日志里一条 FATAL 都没有，但客户端就是超时** → 问题多半不在数据库：查 `ss` 的 `Recv-Q`、查中间的网络设备与代理、查 `net.ipv4.tcp_max_syn_backlog` 与连接队列溢出（`netstat -s | grep -i listen`）。

### 知识库对照
"连接被拒""认证失败""连接数满"三类各有固定套路，检索现象描述可以直接命中。

### 结论
交付"拒绝发生在哪一层"的判断：是内核、pg_hba、认证、连接槽，还是应用侧。这一句决定了后面该改谁。

## C 路：能连但写不进——查磁盘与只读状态

### 现象收集
典型表述是"查询正常，写入报错"。先确认是全库所有表写不进，还是只有某张表/某个表空间。

### 日志收集与定位
```sql
-- 是否已进入只读（如 WAL 归档失败导致的保护性停写）
SELECT name, setting FROM pg_settings
 WHERE name IN ('default_transaction_read_only','archive_mode','archive_command');

-- 表空间与数据目录所在文件系统的用量，两个都要看
```
```bash
df -h $PGDATA; df -i $PGDATA        # inode 也要看：小文件极多时先耗尽 inode
ls -lt $PGDATA/pg_wal | head         # WAL 累积速度
```

分组判断：

- **`ERROR: could not extend file ... No space left on device`** → 磁盘满。**最先不能做的是删 `pg_wal` 里的文件**——那可能正在被复制或恢复使用，删了会把可恢复故障变成不可恢复。
- **`ERROR: cannot execute INSERT in a read-only transaction`** 且 `default_transaction_read_only` 为 off → 说明是归档或复制导致的保护性只读：查 `archive_command` 是否失败、复制槽是否卡住。**这是设计好的保护，不是故障本身**。
- **`ERROR: permission denied for table`** → 权限问题，不是故障，转权限模型篇目。
- **写入报 `could not write to file` 但磁盘有空间** → 查文件系统是否已挂载为只读（`mount | grep ro,`），这通常是存储层故障后的自我保护。

### 结论
明确区分"空间不足""保护性只读""权限问题""存储层只读"四种，四者的处置方向完全不重叠。

## 从流程走出来的结论长什么样

一个合格的故障结论包含四要素，缺一条就不能算收口：

1. **根因**：一句话，且能被某个命令验证——不要写"可能是磁盘压力大"，要写"`pg_wal` 所在文件系统使用率 100%，原因是 `archive_command` 连续失败七天"。
2. **处置**：改了什么，顺序是什么。
3. **验证**：哪个指标回到了什么范围。
4. **复发防线**：下次怎么提前发现（告警项、检查项、演练项）。

第 4 条最容易被省略，而它决定这次故障的价值——**同一类故障第二次发生时，排查时间应该从两小时压缩到十分钟**，靠的就是这条。
