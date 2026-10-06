#!/usr/bin/env python
"""Generate a large synthetic corpus for the store benchmark.

Why this exists
---------------
``examples/knowledge-dba`` is 14 notes / 40 chunks.  At that size the JSON and
pgvector runs differ by a few MB of RSS and a couple hundred ms of CPU, which
is the same order as the noise between two runs on the same machine.  The
question "which backend costs more CPU and memory" cannot be answered there,
so the answer is to grow the input rather than to argue about the difference.

This produces N notes of publicly-known PostgreSQL content, written in the
same shape as the hand-written corpus: a fictional business scenario carrying
a methodology.  Nothing here is company material.

    python scripts/make_scale_corpus.py --notes 400 --out examples/knowledge-scale

The text is generated from topic templates, so it is repetitive by design --
that is fine for a *resource* benchmark, where what matters is the number of
vectors and the width of each row, not retrieval quality.  Do not use it as a
retrieval-quality corpus: the question bank would have to be generated too,
and repeated phrasing inflates hit rates.

One thing to know before reading the results: at 1024 dimensions a float32
vector is 4 KB, so 10k chunks is roughly 40 MB of vectors.  To get the JSON
store into the hundreds of MB you need tens of thousands of chunks, which at
~40 chunks per note means a thousand notes.  ``--notes`` scales linearly;
``--dim`` is the other lever if you only care about memory per row.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER = "> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。\n"

# Each topic: title, the scenario it hangs on, the mechanism, the numbers a DBA
# would look at, and the action order.  Deliberately public knowledge.
TOPICS = [
    ("autovacuum 与膨胀治理", "订单表",
     "死元组占比超过阈值后顺序扫描要读大量无效页面，表体积与活元组数不成比例",
     ["SELECT relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables",
      "SELECT pg_size_pretty(pg_total_relation_size('orders'))",
      "SELECT last_autovacuum, autovacuum_count FROM pg_stat_user_tables"],
     ["先手动 VACUUM (VERBOSE, ANALYZE) 并重新 EXPLAIN 对比",
      "膨胀率过半且必须归还磁盘时才考虑 VACUUM FULL 或 pg_repack",
      "给该表单独设更激进的 autovacuum 阈值，并排查长事务与失效复制槽"]),
    ("连接池与 max_connections", "应用侧连接层",
     "每个后端进程占用固定内存并增加上下文切换，连接数超过 CPU 核数数倍后收益转负",
     ["SELECT count(*), state FROM pg_stat_activity GROUP BY state",
      "SHOW max_connections",
      "SELECT sum(numbackends) FROM pg_stat_database"],
     ["先用连接池把并发压到 CPU 核数附近",
      "再按 并发数 × 每连接内存 估算内存上限并留余量",
      "最后才谈调大 max_connections，且要同步调整内核共享内存参数"]),
    ("索引建了却不走", "查询计划",
     "统计信息过期、类型不匹配、隐式转换、函数包裹列、选择性太低都会让优化器放弃索引",
     ["EXPLAIN (ANALYZE, BUFFERS) <query>",
      "SELECT attname, n_distinct, correlation FROM pg_stats WHERE tablename='orders'",
      "对比 idx_scan 与 seq_scan"],
     ["先看估算行数与实际行数的偏离程度",
      "再确认列上的表达式是否与索引定义完全一致",
      "最后才考虑新建或重建索引，并同步 ANALYZE"]),
    ("锁等待与阻塞链", "事务并发",
     "一个长事务持有行锁或表锁会形成等待队列，后续请求全部挂在锁上而不是耗在计算上",
     ["SELECT pid, wait_event_type, wait_event FROM pg_stat_activity WHERE state <> 'idle'",
      "SELECT blocked.pid, blocking.pid FROM pg_locks blocked JOIN pg_locks blocking "
      "ON blocked.locktype = blocking.locktype AND blocked.relation = blocking.relation",
      "SELECT pg_blocking_pids(<pid>)"],
     ["先定位阻塞源而不是先杀会话",
      "判断该软取消（pg_cancel_backend）还是硬杀（pg_terminate_backend）",
      "给长事务加上超时与语句超时双保险"]),
    ("流复制与切换演练", "高可用",
     "主库写入通过 WAL 传到备库重放，延迟来自网络、备库 I/O 和长事务",
     ["SELECT client_addr, state, sync_state, replay_lag FROM pg_stat_replication",
      "SELECT pg_current_wal_lsn(), pg_last_wal_receive_lsn()",
      "SELECT slot_name, active, restart_lsn FROM pg_replication_slots"],
     ["先用 replay_lag 确认是否真有延迟而不是配置错误",
      "再区分网络带宽、备库磁盘和长事务三类成因",
      "切换演练必须在有流量的预发环境做，且要演练回切"]),
    ("备份与 PITR", "数据保护",
     "全量备份加连续 WAL 归档才能恢复到任意时间点，只有全量备份只能恢复到备份时刻",
     ["SELECT pg_current_wal_lsn()",
      "SELECT * FROM pg_stat_archiver",
      "备份前记录 SELECT pg_create_restore_point()"],
     ["先验证归档是否连续、是否存在断点",
      "恢复演练要真的恢复到指定时间点并校验业务数据",
      "备份保留策略要按恢复目标而不是按磁盘空间定"]),
    ("分区表策略", "大表治理",
     "分区把维护操作从全表级降到分区级，但分区数量过多会让计划时间与元数据膨胀",
     ["SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) "
      "FROM pg_stat_user_tables WHERE relname LIKE 'orders_%'",
      "SHOW enable_partition_pruning",
      "SELECT count(*) FROM pg_inherits"],
     ["按查询条件的时间粒度选分区键，避免跨分区扫描",
      "预建未来分区并自动清理过期分区",
      "分区数控制在百级以内，必要时用多级分区"]),
    ("慢查询定位方法", "性能诊断",
     "先定位再优化：从整体 QPS 与响应时间分布入手，再下沉到单条语句的计划与等待事件",
     ["SELECT query, calls, total_exec_time, mean_exec_time FROM pg_stat_statements "
      "ORDER BY total_exec_time DESC LIMIT 20",
      "SELECT queryid, query FROM pg_stat_activity WHERE state = 'active'",
      "EXPLAIN (ANALYZE, BUFFERS) <query>"],
     ["先看总耗时排序而不是平均耗时排序",
      "再看等待事件区分是 CPU、I/O 还是锁",
      "最后才改 SQL 或索引，改完必须回归对比"]),
    ("大版本升级与回滚", "版本演进",
     "逻辑复制或 pg_upgrade 两种路径的停机窗口与回滚代价完全不同",
     ["SELECT version()",
      "SELECT * FROM pg_replication_slots",
      "检查扩展对目标版本的兼容性"],
     ["先在副本上做一次完整演练并记录耗时",
      "升级前冻结 DDL 变更并停掉自动任务",
      "必须准备好回滚路径，且回滚本身也要演练过"]),
    ("最小权限模型", "安全",
     "按角色聚合权限而不是按人授予，读写分离且默认拒绝",
     ["SELECT rolname, rolsuper, rolcanlogin FROM pg_roles",
      "SELECT grantee, privilege_type FROM information_schema.role_table_grants",
      "SHOW default_transaction_read_only"],
     ["应用账号不用超级用户，且只给必要的库表权限",
      "只读账号直接用 default_transaction_read_only 兜底",
      "定期审计角色成员关系与过期账号"]),
    ("容量评估与告警", "容量规划",
     "按增长速率外推而不是看当前水位，磁盘、连接数、WAL 生成速率三条线都要有告警",
     ["SELECT pg_size_pretty(sum(pg_database_size(datname))) FROM pg_database",
      "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')",
      "SELECT count(*), max(now() - xact_start) FROM pg_stat_activity"],
     ["先看增长速率与剩余空间的天数而不是百分比",
      "为 WAL 单独留出空间，归档堵塞会撑爆磁盘",
      "预留一次全量重建索引的空间"]),
    ("子事务溢出", "极端场景",
     "每个子事务分配一个快照与 XID，长循环里的异常捕获会快速消耗资源并可能耗尽 inode",
     ["SELECT count(*) FROM pg_subtrans",
      "观察 pg_stat_activity 中长时间 holding 的语句",
      "检查文件系统 inode 使用率"],
     ["应用侧避免在长事务里做逐行异常捕获",
      "必要时把循环拆成批处理，让事务边界清晰",
      "出现告警先确认是查询卡住还是元数据资源耗尽"]),
    ("临时文件与 work_mem", "内存与落盘",
     "排序、哈希连接、聚合超出 work_mem 会落临时文件，磁盘 I/O 拖慢查询",
     ["SELECT query, temp_blks_written FROM pg_stat_statements "
      "ORDER BY temp_blks_written DESC LIMIT 10",
      "EXPLAIN (ANALYZE, BUFFERS) <query>",
      "SHOW work_mem"],
     ["先确认是否真的落盘而不是凭感觉调大 work_mem",
      "按单条语句的并发量估算 work_mem，全局调大很危险",
      "对个别重查询用 SET LOCAL 单独放大"]),
    ("检查点与刷脏", "写入性能",
     "检查点过密会造成写放大与周期性抖动，过疏会让崩溃恢复变慢",
     ["SHOW checkpoint_timeout",
      "SHOW max_wal_size",
      "SELECT * FROM pg_stat_bgwriter"],
     ["先区分是检查点抖动还是 autovacuum 抢 I/O",
      "用 max_wal_size 而不是 checkpoint_timeout 做主要调节",
      "监控 checkpoint_req 与 checkpoint_write_time 的比例"]),
    ("复制槽失效", "高可用隐患",
     "失效的复制槽会持续保留 WAL，长期不清理会撑满磁盘",
     ["SELECT slot_name, active, pg_size_pretty(pg_wal_lsn_diff("
      "pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots",
      "SELECT * FROM pg_stat_replication",
      "检查 pg_wal 目录增长"],
     ["先确认槽是否真的没人用再删除",
      "删除前确认对应备库已不需要补数据",
      "给槽的 WAL 保留量设告警"]),
    ("语句与事务超时", "稳定性兜底",
     "statement_timeout 防单条语句拖死，idle_in_transaction_session_timeout 防长事务占资源",
     ["SHOW statement_timeout",
      "SHOW idle_in_transaction_session_timeout",
      "SHOW lock_timeout"],
     ["先用监控找出需要设超时的场景而不是全局设死",
      "按角色或按库设置，关键批处理单独放宽",
      "超时后要有告警，静默超时比不设更危险"]),
    ("索引膨胀与重建", "索引维护",
     "频繁更新会使 B-tree 页面产生空洞，索引体积远超数据所需",
     ["SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) "
      "FROM pg_stat_user_indexes ORDER BY pg_relation_size(indexrelid) DESC",
      "SELECT * FROM pg_stat_all_indexes WHERE idx_scan = 0",
      "对比索引大小与表大小"],
     ["先删掉从未被扫描的索引",
      "再考虑 REINDEX CONCURRENTLY 而不是 REINDEX",
      "重建前确认磁盘有足够余量"]),
    ("统计信息与执行计划漂移", "计划稳定性",
     "采样不足或数据倾斜会让估算大幅偏离，计划从索引扫描翻成嵌套循环",
     ["SELECT attname, n_distinct, most_common_vals FROM pg_stats WHERE tablename='orders'",
      "SHOW default_statistics_target",
      "对比 EXPLAIN 估算行数与实际行数"],
     ["对倾斜列单独提高 statistics target",
      "用扩展统计信息处理多列相关性",
      "确实稳定的计划可以用计划提示兜底，但要记录原因"]),
    ("表空间与磁盘布局", "存储",
     "把热表、索引、WAL 分到不同设备可以分散 I/O，但会增加运维复杂度",
     ["SELECT spcname, pg_size_pretty(pg_tablespace_size(oid)) FROM pg_tablespace",
      "SELECT relname, pg_size_pretty(pg_relation_size(relid)) FROM pg_stat_user_tables",
      "查看数据目录与 WAL 目录的挂载点"],
     ["先量化冷热再谈分离，不要凭感觉分",
      "WAL 单独放设备收益最明显",
      "分离后备份与恢复流程要同步更新"]),
    ("逻辑复制与数据迁移", "跨版本跨库",
     "逻辑复制按行传输，适合大版本升级与部分表同步，但 DDL 不会自动同步",
     ["SELECT * FROM pg_stat_subscription",
      "SELECT slot_name, plugin FROM pg_replication_slots",
      "检查发布端与订阅端的表结构差异"],
     ["先做全量初始同步再做增量",
      "DDL 变更要两边手工同步并纳入发布流程",
      "切换前用校验查询比对行数与关键聚合"]),
    ("并行查询与资源约束", "执行策略",
     "并行度受 max_parallel_workers_per_gather 与表大小影响，但并发高时并行会互相抢资源",
     ["SHOW max_parallel_workers_per_gather",
      "EXPLAIN (ANALYZE) 观察 Gather 节点",
      "SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"],
     ["先确认并行是否真的带来收益而不是增加开销",
      "高并发场景考虑关闭并行换取整体吞吐",
      "对个别大查询用 SET LOCAL 单独打开"]),
    ("WAL 生成速率异常", "写入诊断",
     "批量导入、索引重建、VACUUM FULL 都会大量生成 WAL，归档跟不上就会堆积",
     ["SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')",
      "SELECT * FROM pg_stat_archiver",
      "SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir()"],
     ["先区分是正常批量操作还是异常写入",
      "归档堵塞优先扩容而不是提高归档并发",
      "长期看要按写入量预测 WAL 空间"]),
    ("会话与后台进程诊断", "可观测性",
     "pg_stat_activity 是所有诊断的入口，wait_event 告诉你进程在等什么",
     ["SELECT pid, usename, state, wait_event_type, wait_event, query "
      "FROM pg_stat_activity WHERE state <> 'idle'",
      "SELECT datname, xact_commit, xact_rollback, blks_read, blks_hit FROM pg_stat_database",
      "SELECT * FROM pg_stat_bgwriter"],
     ["先用 wait_event 区分等待类型再决定动作",
      "缓存命中率要结合 blks_read 绝对值看",
      "建立基线与告警，否则无法判断什么是异常"]),
    ("数据校验与一致性", "数据质量",
     "复制、迁移、归档恢复之后都要有可执行的校验方法，而不是靠抽样目测",
     ["SELECT count(*), sum(amount) FROM orders WHERE created_at >= <t>",
      "对比源库与目标库的校验查询结果",
      "检查序列与主键是否对齐"],
     ["校验查询要能命中索引，否则校验本身成为负担",
      "关键表做行数与聚合双重校验",
      "序列不同步是迁移后最常见的问题，要单独检查"]),
]


def note_text(topic, idx: int, rng: random.Random) -> str:
    title, scenario, mechanism, probes, actions = topic
    rows = rng.choice([120, 340, 880, 2100, 5400, 12000])
    pct = rng.choice([12, 23, 41, 58, 77, 91])
    return f"""{HEADER}
# {title}（案例 {idx:04d}）：{scenario}

## 现象

虚构场景：{scenario}的监控在某个工作日出现了明显偏离，
值班同事的第一反应是"先把参数调大"。本案例记录的是不这么做的判断路径。

结论：{mechanism}。在确认这一点之前，任何参数调整都只是在掩盖症状。

## 要看的数字

```sql
{chr(10).join(probes)}
```

以第 {idx:04d} 号案例的观测值为例：

- 观测行数 {rows}，其中异常占比 {pct}%
- 该指标在最近 7 天持续偏离基线
- 关联的另一个指标没有同步变化——这是区分成因的关键

## 处理顺序

1. {actions[0]}
2. {actions[1]}
3. {actions[2]}

顺序不能颠倒。先动后面两步通常会把真正的成因埋掉，
而且会让下一次出现同样现象时更难判断。

## 经验教训

- 看到指标异常先确认它是不是真的异常，基线比阈值更可靠
- 同一个现象至少有两个独立指标支撑时再下结论
- 处置动作必须可回退，且回退路径本身要演练过

## 常见误判

把 {mechanism} 误读成"资源不够"是最常见的误判。
加资源能缓解症状，但增长曲线不变的话，
下一次出问题只是时间问题，而且会更难定位。
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="generate a large synthetic corpus")
    ap.add_argument("--notes", type=int, default=400)
    ap.add_argument("--out", default=str(ROOT / "examples" / "knowledge-scale"))
    ap.add_argument("--seed", type=int, default=20261006)
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Clear previous output so a smaller --notes does not leave stale files that
    # the benchmark would happily ingest.
    for old in out.glob("*.md"):
        old.unlink()

    topics = TOPICS
    for i in range(args.notes):
        topic = topics[i % len(topics)]
        # Vary the body within a topic without changing its subject, so repeated
        # notes are not byte-identical.
        rng2 = random.Random(args.seed + i)
        name = f"scale-{i:04d}-{topic[0].split()[0]}.md"
        (out / name).write_text(note_text(topic, i, rng2), encoding="utf-8")
        if (i + 1) % 100 == 0:
            print(f"  wrote {i + 1} notes …")

    total = sum(f.stat().st_size for f in out.glob("*.md"))
    print(f"\n{args.notes} notes · {total / 1024 / 1024:.2f} MB → {out}")
    print("topics cycled:", len(topics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
