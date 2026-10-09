> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# 实例起不来：从启动日志的最后一行往回读

## 现象：一次例行重启之后，实例再没起来（虚构）

凌晨 02:40，虚构的订单库 `orders_prod` 在主机 `pg-node-01` 上做了一次例行重启，目的是加载新的 `shared_buffers`。`pg_ctl stop` 正常返回，但 `pg_ctl start` 之后 5432 端口始终没有监听。监控在 02:44 报"实例不可用"，应用侧报连接被拒绝。

值班的人第一反应是"上次大概没退干净，pid 残留了"，准备直接删掉 `$PGDATA/postmaster.pid` 再起。这个动作恰好是最不该先做的：它把一份本来能自证"到底为什么失败"的现场证据抹掉了，而且如果实例其实还活着，删 pid 会造成两个 postmaster 抢同一个数据目录。

## 先收集什么证据：启动日志的最后一行比 pid 文件值钱

实例起不来时，唯一不可再生的证据是"这一次启动"的日志。再启一次它就多一条，但"最新一次为什么失败"只在那最后几行里。所以顺序是：先读日志，再碰任何文件。

```bash
pg_ctl status -D /var/lib/pgsql/16/data
pg_ctl start -D /var/lib/pgsql/16/data -l /tmp/pgstart.log
tail -40 /tmp/pgstart.log
```

`pg_ctl status` 的退出码本身就是证据：文档写明，实例没在运行时返回 3，数据目录不可访问时返回 4（`pg_ctl` 参考页）。3 和 4 方向完全不同，先看这个数字能省掉一轮瞎猜。

系统层同时抓这几样：

```bash
ss -lntp | grep 5432
ls -ld /var/lib/pgsql/16/data
ls -l /var/lib/pgsql/16/data/postmaster.pid
dmesg -T | tail -40
```

配置文件的语法与拼写错误，在实例没起来的时候也能提前判读，靠的是 `pg_file_settings`：

```sql
SELECT sourcefile, sourceline, name, setting, applied, error
  FROM pg_file_settings WHERE error IS NOT NULL;
```

这个视图按行列出每个 `name = value` 条目来自哪个文件、第几行、能否生效、错在哪。文档特别提醒：它反映的是配置文件"当前内容"，不是服务器"上次实际应用的值"——诊断启动失败用它。

## 定位过程：六条岔路，为什么这次只剩"属主与权限"

第一步只做一件事：**读最后一行**。它决定走哪条岔路；从第一行读起会被历史日志带偏。

**岔路 A，socket 绑定失败。** 文档给的样例是：

```
LOG:  could not bind IPv4 address "127.0.0.1": Address already in use
HINT:  Is another postmaster already running on port 5432? If not,
 wait a few seconds and retry.
FATAL:  could not create any TCP/IP sockets
```

这时用 `ss -lntp | grep 5432` 找占用者。但有个必须分清的细节：**如果冒号后面的内核错误不是 `Address already in use` 而是 `Permission denied`，那就不是端口被占，而是端口号本身越权**。文档专门举了 `postgres -p 666` 的例子，报的同样是"是否已有 postmaster"的 HINT，真实原因却是低端口需要特权。按"端口占用"去治，方向就反了。

**岔路 B，pid 文件冲突。** 典型是上次实例被强杀（机器断电、`kill -9`、OOM killer），`postmaster.pid` 没清掉，本次启动撞上它：

```
FATAL:  lock file "postmaster.pid" already exists
HINT:  Is another postmaster (PID 45210) running in data directory ".../16/data"?
```

关键是**先确认那个 PID 是否还活着**，而不是直接删文件：

```bash
ps -p 45210 -o pid,stat,etime,cmd
```

文档对 `postmaster.pid` 的定义是"防止同一个数据目录被多个实例同时打开"（`Server Setup and Operation`）——它是一道保护。如果 45210 还在，删掉 pid 再 start 就会有两个 postmaster 抢同一个目录，那不是恢复，是制造损坏。

**岔路 C，属主与权限。** 这次真正落到的是这条。最后几行是：

```
FATAL:  data directory "/var/lib/pgsql/16/data" has wrong ownership
HINT:  The server must be started by the user that owns the data directory.
```

也可能是权限过宽：

```
FATAL:  data directory ".../16/data" has group or world access
DETAIL:  Permissions should be u=rwx (0700) or u=rwx,g=rx (0750).
```

**为什么前两条被排除、只剩这条**：`ss` 显示 5432 没有任何进程监听（A 不成立）；`ps -p 45210` 显示那个残留 PID 的进程早已不存在（B 不成立）；而 `ls -ld $PGDATA` 显示属主是 `root:root`、权限 `drwxrwxr-x`（0775），同时命中上面两条 FATAL。顺着变化点查变更系统，凌晨 02:35 有一个"主机安全基线"脚本对这个目录执行了 `chown root:root` 并把权限放宽到 0775，本意是"统一属主"，顺手把数据目录也改了。这就是根因。之所以 `stop` 显得"正常"，只是因为停实例不写数据目录；而启动第一件事就是校验属主与权限，于是直接 FATAL。

**岔路 D，配置项拼错。** 例如把 `wal_level` 写成 `wal_levl`，启动日志会直接报出无法识别的参数名；用上面那条 `pg_file_settings` 查询能定位到具体文件与行号。注意 `postgresql.auto.conf`（`ALTER SYSTEM` 写的）会覆盖 `postgresql.conf`，两边都要看。

**岔路 E，pg_control 损坏。** 若日志报 `could not read file "global/pg_control"`，或直接 `PANIC:  could not locate a valid checkpoint record`，问题已越过启动配置、进入存储一致性层面。文档说恢复时先读 `pg_control` 再据此读 WAL，而它小于一页、单页写，所以"理论上是弱点，实践中很少真出事"（`Reliability and the Write-Ahead Log`）；判它坏了之前，先排除磁盘层写失败。

**岔路 F，恢复配置缺失。** 从基础备份拉起来的实例，光有数据目录不够：必须存在 `recovery.signal` 或 `standby.signal` 才会进入恢复流程，两者都在时 `standby.signal` 优先（`Server Configuration`）。若缺信号文件，或信号文件在但 `restore_command` 指错位置，实例会在重放中途反复报找不到某个 WAL 段。一条纪律：**绝不能为了"先让它起来"就删 `recovery.signal`**——文档说恢复完成后服务器自己会删它，就是为了防止"误再次进入恢复"；你手工删，等于让它跳过尚未重放的 WAL 直接服务，是主动放弃数据。

## 结论与处置：先修属主与权限，pid 只是表象

根因：安全基线脚本改动了数据目录的属主与权限，PostgreSQL 启动时的属主/权限校验失败并拒绝启动；残留的 `postmaster.pid` 只是把排查带偏的表象。处置顺序：

1. 先确认没有活着的 postmaster（`ps`），再动手。
2. 把数据目录属主改回运行用户、权限收回 0700：

```bash
chown -R postgres:postgres /var/lib/pgsql/16/data
chmod 0700 /var/lib/pgsql/16/data
```

3. `ps` 确认原 pid 里的进程确实不存在后，才清理残留的 `postmaster.pid`。
4. 重新启动，核对端口监听与日志尾部的 `database system is ready to accept connections`。

验证指标：`ss -lntp` 看到 5432 监听；`SELECT pg_postmaster_start_time();` 返回的时间与本次启动一致；随后 10 分钟内 `pg_stat_activity` 无异常断连。

## 复盘要点

- **启动失败先读日志最后一行，再动文件**。pid 残留、端口占用、权限错误在日志里的措辞完全不同，先看最后一行就能分岔；先删 pid 或先 reboot 会把不可再生的证据抹掉。
- **`Address already in use` 和 `Permission denied` 共用同一句 HINT，根因却相反**。前者是占用，后者是端口号/权限，别被那句 HINT 带着走。
- **`postmaster.pid` 是保护不是垃圾**。删它之前必须用 `ps` 确认对应进程已死，否则可能起出两个实例、损坏数据目录。
- **数据目录的属主与权限是硬约束**，任何"统一属主""安全加固"的批量脚本都要把它排除在外；这类故障的隐蔽性在于主机层面看它"很干净"。
