# schemafence

> **AI doesn't write wrong SQL by misunderstanding English. It writes wrong SQL by misreading your schema.**
>
> A constraint layer between LLMs and databases — catch plausible-but-wrong SQL *before* it runs.
>
> 在 LLM 和数据库之间加一层约束——在那些"看起来合理的错误 SQL"跑起来之前拦住它。

---

## Why this exists

The industry spent two years learning an uncomfortable lesson about NL2SQL: **the hard part is not the language model — it's the database.**

The most common failure is not a syntax error. It's *schema misinterpretation*: the LLM picks the wrong table, joins on a lossy key, misreads a NULL-able column, or confuses two similarly-named fields. The query **executes successfully** and returns a number that looks perfectly reasonable.

**Nobody gets an error. The report is just wrong.**

`schemafence` puts a fence between the model and your data. Before a generated query touches the database, it is checked against what the schema actually means — not just whether it parses.

> SQL 不会报错，报表只是"错了"——没有人会收到任何告警。`schemafence` 就是为这种失败形态而建。

## What it checks

| # | Check                          | Catches                                                                      |
| - | ------------------------------ | ---------------------------------------------------------------------------- |
| 1 | **Schema resolution**          | Tables/columns that don't exist or were "hallucinated"                       |
| 2 | **Join-key sanity**            | Lossy or wrong-key joins (e.g. joining on a nullable column)                 |
| 3 | **Type & precision mismatch**  | Implicit casts that silently truncate (money, timestamps, encodings)         |
| 4 | **NULL semantics**             | `NOT IN (SELECT …)`, `= NULL`, aggregates over NULL-heavy columns            |
| 5 | **Result plausibility**        | Row counts / cardinality wildly outside expectation                          |
| 6 | **Runtime guardrails**         | Read-only enforcement, statement whitelist, forced LIMIT, timeout, audit log |
| 7 | **Migration diff** *(planned)* | Same query, different behavior after Oracle → PostgreSQL/国产库 migration       |

Check 7 is the reason this project exists: **most migration defects are semantic, not syntactic** — and no syntax converter will ever catch them.

## Quick start

```bash
# Not on PyPI yet — install from source
git clone https://github.com/empoGavin/schemafence && cd schemafence && pip install -e .
```

```python
from schemafence import Fence, FenceConfig

fence = Fence(FenceConfig.from_dsn("postgresql://reader:***@prod-db/analytics"))

result = fence.check(sql, question="月活跃用户数")
if result.passed:
    rows = fence.execute(result.sql)   # 只读连接，强制 LIMIT
else:
    print(result.issues)               # 为什么这段 SQL 可能"看起来对，其实错"
```

## How it works

```
            ┌────────────┐      ┌─────────────────────────┐      ┌──────────┐
 question ─▶│ LLM / NL2SQL│─────▶│      schemafence        │─────▶│ Database │
            └────────────┘      │  schema check            │      │ (read-   │
                  ▲             │  join & type sanity      │      │  only)   │
                  │             │  NULL semantics          │      └──────────┘
            schema snapshot    │  plausibility bounds     │
            (pgvector store)   │  guardrails + audit log  │
                               └─────────────────────────┘
```

## Status

**Early / under active development.** The API will change. The checklist below is the plan.

- [x] Project skeleton
- [ ] Schema snapshot & pgvector store
- [ ] Check 1-4 (semantic checks)
- [ ] Guardrails & audit log
- [ ] CLI + docs
- [ ] Migration diff (Oracle → PG)

## Why I built this

I spent 21 years as an Oracle DBA (OCM), leading zero-downtime Oracle-to-distributed-DB migrations across 20 regions. Every migration failure I've seen was *silent first*: the SQL ran, the numbers looked plausible, and the damage showed up weeks later.

AI-generated SQL fails the same way. This project is that experience, turned into code.

> 我做了 21 年 Oracle DBA（OCM），主导过 3 个产品、覆盖 20 个 Region 的零停机去 O 迁移。见过的迁移事故几乎都是"先静默、后爆炸"。AI 生成的 SQL 正在用同样的方式失败——这个项目就是把那段经验写成代码。

## Contributing

Issues and PRs welcome — especially **real-world examples of plausible-but-wrong SQL** (sanitized, please). They become test cases.

## License

[MIT](LICENSE)

---
