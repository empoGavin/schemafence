# 评测题库草稿（--genq 自动生成，改完再并入 questions.md）

- 语料：examples/knowledge（8 篇笔记）

## 这份草稿怎么用（三步，预计 10 分钟）

1. **删**：任何你不会那样问的题，直接删掉——评测题必须是真实问法，
   不是模板句。留下你觉得"这是我真会问的"的那些。
2. **改**：把模板腔改成你平时说话的问法（"表膨胀应该怎么处理？"
   → "PG 里表膨胀怎么治？"），问法越像你真实提问，命中率越可信。
3. **补**：加上模板生成不了的三类题——真实故障、真实权衡、
   本公司具体场景（脱敏）。这三类最接近真实提问。

> 题库的作用是**量尺**，不是**门槛**：它不需要覆盖你未来会问的一切，
> 它只需要稳定——下次改切片、换嵌入、加语料之后，同一套题重跑，
> 看命中率是涨是跌。这正是回归测试的思路。

## 草稿（每篇笔记至少 1 题，先保覆盖、再抠问法）

| # | 问题 | 期望来源 |
|---|------|---------|
| 1 | 数据治理应该怎么处理？ | data-governance |
| 2 | 分布式数据库出了问题怎么排查？ | distributed-db-troubleshooting |
| 3 | 分布式数据库做设计决策时要权衡什么？ | distributed-db-troubleshooting |
| 4 | 去 O 项目应该分几步推进？ | oracle-exit-playbook |
| 5 | 去 O 项目做设计决策时要权衡什么？ | oracle-exit-playbook |
| 6 | 表膨胀是怎么产生的？ | pg-bloat |
| 7 | 表膨胀应该怎么处理？ | pg-bloat |
| 8 | 读懂 PostgreSQL 执行计划是怎么产生的？ | pg-execution-plan |
| 9 | 生产库半夜卡死出了问题怎么排查？ | pg-lock-and-hang |
| 10 | 怎么判断生产库半夜卡死做得好不好？ | pg-lock-and-hang |
| 11 | 子事务、空闲事务与长事务是怎么产生的？ | pg-subtransaction |
| 12 | 子事务、空闲事务与长事务出了问题怎么排查？ | pg-subtransaction |
| 13 | WAL、checkpoint 与复制延迟出了问题怎么排查？ | pg-wal-checkpoint |

## 改完之后

```bash
python agent_cli.py --eval-file eval/questions.draft.md --eval   # 先试跑
# 满意后把表粘进 eval/questions.md（或直接 --eval-file 指向草稿文件）
```
