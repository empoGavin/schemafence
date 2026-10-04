# 评测题库（检索召回）

规则：**每一题的"期望来源"必须是一篇真实存在的笔记**。改语料的时候同步改这张表，
否则召回率就没有意义。

- 用法：`python agent_cli.py --eval`（默认 top-5）
- 调参：`python agent_cli.py --tune`（切片 × 重叠 × top-k）
- 判定：返回的 top-k 里出现了"期望来源"，记为命中；命中位置记下来，用于分析排序质量

| # | 问题 | 期望来源 |
|---|------|---------|
| 1 | 表膨胀是怎么产生的，应该怎么治理？ | pg-bloat |
| 2 | VACUUM FULL 有什么风险，什么时候才该用？ | pg-bloat |
| 3 | 子事务过多会有什么后果？ | pg-subtransaction |
| 4 | 怎么找出把 xmin 钉住的那个会话？ | pg-subtransaction |
| 5 | 在事务里调用外部接口有什么风险？ | pg-subtransaction |
| 6 | 执行计划里估算行数和实际行数差很多说明什么？ | pg-execution-plan |
| 7 | 为什么有些查询用顺序扫描反而更快？ | pg-execution-plan |
| 8 | pgvector 的 HNSW 索引什么情况下会失效？ | pg-execution-plan |
| 9 | checkpoint 太频繁会有什么问题？ | pg-wal-checkpoint |
| 10 | 复制延迟应该怎么排查？ | pg-wal-checkpoint |
| 11 | 生产库半夜被锁卡死，十五分钟怎么定位？ | pg-lock-and-hang |
| 12 | 空闲事务的会话能不能直接杀掉？ | pg-lock-and-hang |
| 13 | 去 O 项目应该怎么分阶段推进？ | oracle-exit-playbook |
| 14 | 零停机切换有哪些关键手法？ | oracle-exit-playbook |
| 15 | 数据量从 104TB 压到 20TB 用了哪些手段？ | data-governance |
| 16 | 怎么判断数据治理是否真的有效？ | data-governance |

## 待补充（Day 4 你自己加）

不想从空白开始：`python agent_cli.py --genq` 会从语料生成 `eval/questions.draft.md`
（每篇至少 1 题、期望来源自动填好），你做三件事——删掉不会那样问的、改成真实问法、
补上真实场景题。详见手册 2.3 节。

把问题换成你真实被问过的、真实踩过的，命中率才有说服力。建议结构：
**3 题概念、3 题故障、3 题设计权衡、3 题本公司的具体场景（脱敏后）**，20–50 题足够。
