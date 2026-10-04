> 语料来源：公开技术文章与 PostgreSQL 源码核对 + 脱敏后的生产案例（无表名、无系统标识、无内部信息）。

# 自旋锁争用：old_snapshot_threshold 把 CPU 烧在页剪枝上

## 先分清：自旋锁和 LWLock 的可观测性完全不同

这是排查这类问题的第一个认知门槛：

- **LWLock** 会出现在 `pg_stat_activity.wait_event` 里，能直接看出来
- **自旋锁**（`s_lock` → `perform_spin_delay`）是用户态忙等 + 短退避，
  **`pg_stat_activity` 里看不到任何等待事件**，数据库监控面板上一切正常

所以现象是"CPU 很高、等待事件看起来正常、`pg_stat_statements` 里也没有异常慢的 SQL"。
这种情况只能靠操作系统层面的采样工具：`perf top` / `perf record`。

## 机制：页剪枝路径上的两次取锁

PG 12 的 `heap_page_prune_opt()`（页剪枝）里，对**普通用户表**会调用
`TransactionIdLimitedForOldSnapshots()`；只有系统目录和逻辑解码可见的关系才跳过。
也就是说，**每一次"尝试剪枝"都要走这个函数**——包括读操作触发的回表路径
（`index_fetch_heap` → `heap_page_prune_opt`），不是只有写入才会中招。

进入该函数的条件是：

```
TransactionIdIsNormal(recentXmin) && old_snapshot_threshold >= 0 && RelationAllowsEarlyPruning(relation)
```

只要参数不是 `-1`，函数内部就会去拿 `oldSnapshotControl` 上的自旋锁
（PG 12 是 `mutex_latest_xmin` 与 `mutex_threshold` 两把），
同一份快照路径上的 `MaintainOldSnapshotTimeMapping()` 还会再拿一次，
并且可能去抢 `OldSnapshotTimeMapLock` 这把 LWLock。
**高并发下所有 backend 都在抢同一份全局结构，CPU 就全烧在自旋上。**

另一个放大器：`RelationAllowsEarlyPruning()` 会走到 `RelationHasUnloggedIndex()`，
而它每次都调 `RelationGetIndexList()` **重新构造一份索引列表**再逐个查有没有 unlogged 索引。
所以**索引越多的表，开销越大**——社区实测一个 30 个索引的表，扫描耗时被抬高到 2.5 倍。
（PG 13 缓存了这个结果。）

## 60 秒定位：perf 里看到这个函数名就是它

```bash
perf top -p $(pgrep -f "postgres.*-D" | head -1)
# 或采样后看报告
perf record -F 999 -g -p <backend_pid> -- sleep 30 && perf report --stdio
```

要在采样里看到的关键符号：

```
s_lock / perform_spin_delay                          ← 自旋锁本体（PG 用户态自旋）
_raw_spin_lock / native_queued_spin_lock_slowpath    ← 内核侧的自旋锁符号
TransactionIdLimitedForOldSnapshots                  ← 页剪枝里的取锁点
GetSnapshotCurrentTimestamp
SetOldSnapshotThresholdTimestamp
MaintainOldSnapshotTimeMapping                       ← 快照获取路径里的取锁点
```

注意在性能工具里，**spin lock（自旋锁）常以两种形态出现**：PG 自己的用户态自旋
（`s_lock`、`perform_spin_delay`）和内核侧的锁函数
（`_raw_spin_lock`、`native_queued_spin_lock_slowpath`）。
无论看到哪一种，含义都是"CPU 花在了抢锁上，而不是在干活"。

公开案例里的采样占比很典型：`heap_page_prune_opt` 占 24%，
其中一半以上是 `TransactionIdLimitedForOldSnapshots` 内部的 `s_lock`；
另一半来自 `GetTransactionSnapshot → GetSnapshotData` 路径上的同类自旋。
**两个热点指向同一个全局结构，这就是定位到该参数的依据。**

然后用一条 SQL 确认参数：

```sql
SELECT name, setting, unit, source, pending_restart
  FROM pg_settings WHERE name = 'old_snapshot_threshold';
```

## 案例：会话数上来之后 CPU 被打满

**现象**：PG 12 实例，用户会话数一上来，服务器 CPU 迅速升高；
业务侧表现为整体变慢，但没有锁等待，也没有单条 SQL 明显变慢。

**定位**：用 `perf top` 观察，**自旋锁相关占比高达 87%–90%**，
调用链指向 `heap_page_prune_opt` → `TransactionIdLimitedForOldSnapshots` → `s_lock` → `perform_spin_delay`。
核对参数发现 `old_snapshot_threshold` 被设成了 10（即 10 分钟），
而默认值是 `-1`（禁用）——**这是一次"非默认参数被打开"引入的性能故障**。
公开实测数据也吻合：开启该参数后 10 个并发 CPU 就翻倍，50 并发把 CPU 打满，
而不开启时 400 并发才到 90%，差距接近 8 倍。

**处置**：**取消该参数的显式设置**，恢复默认值 `-1`（禁用）后，CPU 回落。

**复盘三点**：
1. 参数是从哪里来的？**升级、迁移、上云之后新实例的参数与老库不一致**是这类问题的常见来源
   （公开案例就是"升级后的库打开了这个参数，升级前的老库没有"）。
2. 该参数**只能在服务启动时设置**，所以任何调整都必然伴随一次实例重启——
   观测到的"恢复"里可能混有重启本身的效应，结论要谨慎。
3. **非默认参数要有台账**：谁改的、为什么改、有没有对应的性能验证。

## 处置：取消显式设置，让参数回到默认的 `-1`

这个案例的处置动作是**取消 `old_snapshot_threshold` 的显式设置**——配置文件里不再写这一行，
参数回落到默认值，CPU 随后回落。这里有两个必须说清的点。

**第一，"取消设置"为什么就等于"禁用"。**
`postgresql.conf` 里该参数的默认值本来就是 `-1`（通常是注释状态），
而**只有 `-1` 才表示禁用**这个特性，等价于把快照年龄上限设为无穷。
"取消显式设置"与"显式写成 `-1`"效果完全等价，都是回到禁用态；
反过来，**只要显式写下任何一个非 -1 的值**（`10`、`1min`、甚至 `0`），
页剪枝路径上的取锁就会被打开。
**所以判断依据不是"值看起来大不大"，而是"这一行是否偏离了默认"。**

**第二，`0` 不是"关闭"的意思，别把它当开关。**
官方文档明确说 `0` 和 `1min` 这类小值"**因为偶尔对测试有用**才允许存在"，
没有"关闭"的语义。对照 PG 12 源码也能印证：`== 0` 的分支是
**在获取 `mutex_latest_xmin` 自旋锁之后**才判断的，分支内还会调用
`SetOldSnapshotThresholdTimestamp()` 再取一次 `mutex_threshold` 锁；
`MaintainOldSnapshotTimeMapping()` 里的 `== 0` 提前返回同样发生在取锁之后。
**所以"设成 0"并不会让这条路径绕开自旋锁。**
之所以容易记混，是因为在别的参数语境里 `0` 常表示"关闭 / 不限制"
（很多 timeout、limit 类参数都是这个约定），而这里 `0` 是"快照年龄阈值为 0 分钟"，
含义正好相反——这是官方文档专门提醒过的一个坑。

**可验证的证据**：改完之后查 `pg_settings.source`，它应该回到 `default`：

```sql
SELECT name, setting, source, pending_restart
  FROM pg_settings WHERE name = 'old_snapshot_threshold';
-- 取消显式设置之后：setting = -1, source = default
-- 出问题的时候：    setting = 10, source = configuration file
```

`source` 字段会告诉你这个值来自"内置默认值"还是"配置文件"。
这正是排查**参数漂移**最直接的手段：升级、迁移、上云之后逐个比对 `source`，
凡是 `source` 不是 `default`、又与业务基线不一致的项，都值得单独问一句
"谁改的、为什么改、有没有做过性能验证"。

**一句话讲法（面试用）**：
"那台库的 `old_snapshot_threshold` 被显式设成了 10 分钟，导致页剪枝路径上两个热点抢同一把自旋锁，
CPU 被烧到 90%；**取消这个显式设置、回到默认的 -1（禁用）之后 CPU 就回落了**。
用 `pg_settings.source` 就能定位这类参数漂移，不用靠猜。"

## 版本差异

| 版本 | 行为 |
|---|---|
| PG 12 及更早 | 页剪枝路径上的取锁开销最重，高并发下可把 CPU 打满，**问题主要影响这一档** |
| PG 13–16 | PG 13 的"快照可扩展性"补丁集修改了 `heap_page_prune_opt` 对该参数的处理方式——**只在"否则无法修剪"时才应用该限制**，使其明显更便宜、冲突更少；同时索引列表结果被缓存。公开实测在 15.x 上开启该参数已基本无可观测损耗 |
| PG 17 及以后 | 该参数**已被移除**（理由是长期存在的正确性与性能问题），不再存在这个隐患 |

所以如果还在 PG 12 上遇到它，除了关参数，更根本的动作是**规划版本升级**。

## 预防清单

1. **非默认参数台账**：升级、迁移、上云后逐项比对 `pg_settings` 的 `source` 字段，
   找出与基线不一致的项——很多"性能故障"其实是参数漂移
2. **压测时做参数对照**：同一版本、同一压力，开关关键参数各跑一遍，
   把 CPU 和吞吐差异记下来（公开实测就是这个方法做出的 8 倍差距）
3. **CPU 高但等待事件正常时，直接上 perf**，不要在 `pg_stat_activity` 里找答案
4. 记住这条判据：**自旋锁争用是"全实例级"的**——
   症状与并发数强相关、与单条 SQL 无关，且随会话数上升呈非线性恶化

## 参考来源

- PostgreSQL 12 文档，`old_snapshot_threshold`（-1 才禁用；0 与 1min 属于"测试用小值"） https://www.postgresql.org/docs/12/runtime-config-resource.html
- PostgreSQL 17 发行说明：移除 `old_snapshot_threshold`（提交 f691f5b8，Thomas Munro） https://www.postgresql.org/docs/17/release-17.html
- Thomas Munro 在 pgsql-hackers 报告 `heap_page_prune_opt → TransactionIdLimitedForOldSnapshots` 热点与索引数量的放大效应 https://www.postgresql.org/message-id/flat/CA+hUKGKT8oTkp5jw_U4p0S-7UG9zsvtw_M47Y285bER6a2gD+g@mail.gmail.com
- Andres Freund 关于快照可扩展性补丁集修改 `old_snapshot_threshold` 应用时机的说明（v13） https://postgresql.org/message-id/20200407121503.zltbpqmdesurflnm%40alap3.anarazel.de
- 《恼人的自旋锁》与续篇：升级后开启该参数导致 CPU 翻倍的压测对照 https://blog.itpub.net/69978437/viewspace-3034668
- dba.stackexchange：开启该参数后 pgbench TPS 从 85 万降到 22 万，perf 显示 `s_lock` 分布 https://dba.stackexchange.com/questions/332449/
