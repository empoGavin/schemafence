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
| 1 | **Schema resolution**          | Near-duplicate tables and columns the model will confuse (`orders` / `orders_archive`) |
| 2 | **Join-key sanity**            | Nullable key columns: the join that silently drops rows                      |
| 3 | **Type & precision mismatch**  | Money in floating point, timestamps held as text, timestamps without a zone  |
| 4 | **NULL semantics**             | `NOT IN (SELECT …)` over a nullable column — returns empty, no error         |
| 5 | **Result plausibility**        | Row counts wildly outside expectation (needs a live database)                |
| 6 | **Runtime guardrails**         | Seven layers: read-only shape, keyword and function deny-lists, table whitelist, forced LIMIT, audit, session hardening |
| 7 | **Migration diff** *(planned)* | Same query, different behaviour after Oracle → PostgreSQL / domestic DB migration |

Check 7 is the reason this project exists: **most migration defects are semantic, not syntactic** — and no syntax converter will ever catch them.

Checks 1–4 and the guardrails (6) run today, with or without a database. Check 5 needs a live connection and is partly wired up. Check 7 is the 30-day goal.

## Quick start

```bash
git clone https://github.com/empoGavin/schemafence
cd schemafence
python demo.py          # no database, no model, no API key — about 30 seconds
```

```
schemafence — the constraint layer between LLMs and databases
====================================================================
source : sample_schema.sql
tables : 7      columns : 33
mode   : offline (no database, no model, no API key)

[check 1] schema resolution — which entity gets picked   (1)
  HIGH   shop.orders  vs  shop.orders_archive
         why : Two tables differ only by name (same entity, one is the archive/copy)…
[check 2] join-key sanity — rows silently dropped   (4)
[check 3] type & precision — values silently changed   (6)
[check 4] NULL semantics — three-valued logic traps   (1)
[check H] hygiene — comments and naming   (4)

  16 finding(s): 5 high / 9 medium / 2 low
[guard] seven-layer selftest
  13 cases → all passed
```

See the whole pipeline — question, table choice, generated SQL, verdict:

```bash
python demo.py --ask "total order amount for the last week?"
#   #1  shop.orders                score 6
#   #2  shop.orders_archive        score 6
#   note : 2 candidate tables scored close together — the model picks one,
#          and nothing in the database stops it picking the wrong one
#   guard: ALLOWED
```

A question with one obvious answer behaves the other way:

```bash
python demo.py --ask "哪个表存了退款信息？"
#   #1  shop.refunds               score 12      ← no ambiguity, nothing to catch
```

With a live database (optional — pick your platform):

```bash
bash scripts/setup_pg.sh
pip install -r requirements.txt
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

`setup_pg.sh` handles both package families: `apt` (Debian/Ubuntu) and `dnf`
(RHEL, Oracle Linux, Rocky, AlmaLinux). Step-by-step guides:

| Platform | Guide |
| -------- | ----- |
| Local **Oracle Linux 10** VM on VMware | [`docs/quickstart-oraclelinux-vm.md`](docs/quickstart-oraclelinux-vm.md) |
| **Cloud Studio** free tier (Ubuntu container) | [`docs/quickstart-cloudstudio.md`](docs/quickstart-cloudstudio.md) |

Using it as a library:

```python
from schemafence import parse_ddl, analyze
from schemafence.guard import guard

schema = parse_ddl(open("examples/sample_schema.sql").read())
for finding in analyze(schema):
    print(finding.severity, finding.subject)

verdict = guard("SELECT * FROM shop.orders; DROP TABLE shop.users")
print(verdict.ok, verdict.layer, verdict.reason)
# False L1 multiple statements in one call are not allowed
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

**Early — day 3 of a 7-day build.**

- [x] Project skeleton
- [x] Offline audit: checks 1–4 plus hygiene, straight from a DDL file
- [x] Seven-layer guardrail with a 13-case selftest
- [x] Live mode: reads the catalogue and `pg_stats` (measured NULL fractions)
- [x] Setup script for both `apt` and `dnf` families, plus two quickstarts ([Oracle Linux VM](docs/quickstart-oraclelinux-vm.md) · [Cloud Studio](docs/quickstart-cloudstudio.md))
- [ ] Schema snapshot in pgvector, real embeddings — *day 4*
- [ ] Tool-calling agent over a real model — *day 5*
- [ ] Guardrails wired into the execution path — *day 6*
- [ ] Migration diff (Oracle → PostgreSQL / domestic DB) — *30-day plan*

## Known limitations

A read-only fence is only useful if you know where it is thin.

- **The DDL reader is hand-written** and assumes one column definition per line.
  Fine for the bundled example; production should parse with `sqlglot` or
  `pg_query`. It is deliberately dependency-free, so `python demo.py` works on a
  fresh clone with no install.
- **No embeddings yet.** The pgvector store lands on day 4; today the snapshot is
  read straight from the catalogue.
- **Check 5 is not in the report yet.** Row estimates are printed, but nothing
  compares them against an expectation.
- **The planner is keyword-based on purpose.** It stands in for a model so the
  demo needs no API key. Nothing about the fence changes when a real model is
  plugged in — that is the point.
- **PostgreSQL only** for now (catalogue queries and `pg_stats`). Oracle is where
  the migration-diff work starts.

## Why I built this

I spent 21 years as an Oracle DBA (OCM), leading zero-downtime Oracle-to-distributed-DB migrations across 20 regions. Every migration failure I've seen was *silent first*: the SQL ran, the numbers looked plausible, and the damage showed up weeks later.

AI-generated SQL fails the same way. This project is that experience, turned into code.

> 我做了 21 年 Oracle DBA（OCM），主导过 3 个产品、覆盖 20 个 Region 的零停机去 O 迁移。见过的迁移事故几乎都是"先静默、后爆炸"。AI 生成的 SQL 正在用同样的方式失败——这个项目就是把那段经验写成代码。

## Contributing

Issues and PRs welcome — especially **real-world examples of plausible-but-wrong SQL** (sanitized, please). They become test cases.

## License

[MIT](LICENSE)

---
