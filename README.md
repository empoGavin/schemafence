# schemafence

[![qa](https://github.com/empoGavin/schemafence/actions/workflows/qa.yml/badge.svg)](https://github.com/empoGavin/schemafence/actions/workflows/qa.yml)

> An agent that answers database questions from a DBA knowledge base and diagnoses issues on a live instance, and a fence that every statement must pass through before it reaches the database.
>
> 三层：先查知识库，再看活库的真实样子（不是别人描述的样子），最后才决定这条语句放不放行。

## Two halves, and one rule about how they meet

Most database AI tools ship one of these and let it stand in for the other: a diagnostic
answer and a write-capable connection in the same process with nothing in between.

|                | **The agent** (knowledge side)                      | **The fence** (constraint side)                          |
| -------------- | --------------------------------------------------- | -------------------------------------------------------- |
| Answers        | "orders 表为什么这么慢？" · "idle in transaction 有什么后果？" | "这条语句允许执行吗？"                                             |
| Decides by     | retrieval, tool evidence, optionally a model        | fixed rules, no model and no retrieval                   |
| Nature         | probabilistic: a wrong answer is bad advice          | deterministic: a wrong decision damages data silently    |
| Tuning         | corpus, chunk/overlap/top-k, floor, embedder, model | not possible, by design                                  |
| Evidence       | [`eval/report-dba.md`](eval/report-dba.md) — 44 questions, hit rate per note | `demo.py` selftest — 13 guard + 10 router + 7 tool + 12 sql-check cases; [`docs/bench.md`](docs/bench.md) — JSON vs pgvector at 40 and 4000 chunks |
| Lives in       | `schemafence/agent.py`, `schemafence/knowledge.py`  | `schemafence/guard.py`                                   |

They meet in one place: `Toolbox` in `schemafence/tools.py`. Retrieved passages go into
the answer text and no further, and `guard()` alone decides whether a statement runs — so
a mediocre retrieval score is a quality problem, not a safety one.

## Quick start

```bash
git clone https://github.com/empoGavin/schemafence && cd schemafence
python demo.py          # no database, no model, no API key, about 30 seconds
```

```
schemafence — the DBA agent, and the fence in front of the database
====================================================================
source : sample_schema.sql      tables : 7      columns : 33

[check 1] schema resolution — which entity gets picked   (1)
[check 2] join-key sanity — rows silently dropped   (4)
[check 3] type & precision — values silently changed   (6)
[check 4] NULL semantics — three-valued logic traps   (1)
[check H] hygiene — comments and naming   (4)
[summary]  16 finding(s): 5 high / 9 medium / 2 low
```

Each finding prints a why and a fix; four selftest blocks close the run — 13 guard, 10 router,
7 tools, 12 sql-checks, each ending `N cases → all passed`. The `[tools]` one catches a bug the
other tests cannot see: the relevance floor asked whether an embedder was attached, and
`open_store` attaches one even offline, so the floor of 0.45 dropped every lexical hit (0.089–0.285).

## Half one — the agent

`demo.py` audits a schema. `agent_cli.py` answers questions against one, choosing
between the knowledge base and the live catalogue.

```bash
python agent_cli.py --ingest examples/knowledge-dba   # chunk → embed → store
python agent_cli.py --ask "订单表查询走了顺序扫描，是不是该加个索引？"
python agent_cli.py --eval --report eval/report-dba.md # hit rate + tuning grid
```

**Four tools** — `search_docs`, `run_sql`, `explain_sql`, `get_table_stats` — all through
one door, `guard()`, and every call appended to `agent_trace.jsonl` with its decision, its
layer and its duration. The loop is hand-written rather than delegated to a framework, so
tool selection and failure handling stay explainable line by line. Two bugs were only found
by running it live: a tool that failed identically every round until the budget was gone
(calls are now memoised, with a breaker that forces a plain-text answer), and a model
endpoint that went quiet (retry with backoff, 4xx not retried, and a degradation path).

**A keyword is not intent.** `删除` appears in `帮我删掉 orders 这张表`, which has to be
refused, and in `怎么安全地删除大表的历史分区？`, which has to be answered. The router
reads the shape of the sentence as well as the vocabulary: a write request that reaches the
tools is stopped by `guard()` with no side effect, while a real question that gets refused is
simply lost. So the router errs toward answering and the determinism stays in the layer that
can enforce it.

**Three independent axes**, not one "offline vs live" switch:

| axis      | default                             | switched by                       | if you leave it out                        |
| --------- | ----------------------------------- | --------------------------------- | ------------------------------------------ |
| embedding | hashed lexical vector + corpus IDF  | `SF_EMBED_API_KEY`                | nothing is downloaded and no SDK is needed  |
| store     | JSON file under `.schemafence/`      | `--db` → pgvector + HNSW          | stays a file on disk, still works          |
| driver    | rule-based router                   | `--llm openai` + `SF_LLM_API_KEY` | stays deterministic, still answers         |

"Live" here means a real database is attached (`--db`), and it says nothing about API
keys: `--db` with no key is supported and useful, and both quickstarts run that way.

### The corpus, and why retrieval quality is measurable

Five corpora ship with the repo — de-identified runbooks, synthetic DBA notes, an operations
pack, the PostgreSQL 16 manual and *PostgreSQL 14 Internals* — all meant to be replaced with
your own. **Corpus shape decides retrieval quality more than size does:** the hand-written pack
scores 100% top-5 over 50 questions while the full manual scores 82.5% over 40, and merging
them costs the runbook questions 30 points while costing the syntax questions nothing. Sizes,
the measured cost of merging (96.8% → 74.6% over 201 questions, 89.7% at `--k 20`) and the
pgvector ingest shapes are in [`docs/pg-corpus.md`](docs/pg-corpus.md); rules for notes that
cannot leave a company are in [`docs/synthetic-corpus.md`](docs/synthetic-corpus.md). `--eval`
is a measuring stick, not a gate: 44 questions over 40 chunks come out at **97.7% top-5**
offline, with the relevance floor named rather than guessed.

## Half two — the fence

### Why it exists

NL2SQL has had a few years now, and the hard part turned out to be the database rather than
the language model. The common failure is not a syntax error but a misread schema: the wrong
table, a lossy join key, a nullable column read as plain, two similarly named fields confused.
The query runs and returns a plausible number — nothing raises an error, the report is wrong.

> SQL 不会报错，报表只是"错了"——没有人会收到任何告警。

### What it checks

| #   | Check                             | Catches                                                                                                                                                                         |
| --- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Schema resolution**             | Near-duplicate tables and columns the model will confuse (`orders` / `orders_archive`)                                                                                          |
| 2   | **Join-key sanity**               | Nullable key columns: the join that silently drops rows                                                                                                                         |
| 3   | **Type & precision mismatch**     | Money in floating point, timestamps held as text, timestamps without a zone                                                                                                     |
| 4   | **NULL semantics**                | `NOT IN (SELECT …)` over a nullable column — returns empty, no error                                                                                                            |
| 5   | **Result plausibility**           | Row counts wildly outside expectation (needs a live database)                                                                                                                   |
| S   | **SQL-side semantics** *(S1, S3)* | A measure summed after a one-to-many join has multiplied it; a `LEFT JOIN` cancelled by a `WHERE` on the right table                                                            |
| 6   | **Runtime guardrails**            | Seven layers: L1 single statement · L2 read-only shape · L3 deny-listed keywords · L4 dangerous functions · L5 table whitelist · L6 forced LIMIT · L7 audit + session hardening |

Checks 1–4, S and the guardrails run with or without a database. Check 5 needs a live
connection and is partly wired up. Check 7, the migration diff, is not built yet. It is the
reason the project exists, and a different story from this one.

Check S is the one worth explaining. `checks.py` reads the schema and never sees a statement;
`guard.py` reads the statement and never sees a schema. A fan-out and a cancelled `LEFT JOIN`
are valid on both sides and only visible where the two meet, so neither can go into the layer
that already exists. The new module reads both, reports, and never decides whether anything
runs — its rules, its abstentions and the tiers above it are in
[`docs/roadmap.md`](docs/roadmap.md). As a library:

```python
from schemafence import parse_ddl
from schemafence.checks_sql import check_sql
from schemafence.guard import guard
schema = parse_ddl(open("examples/sample_schema.sql").read())
sql = "SELECT SUM(o.amount) FROM shop.orders o JOIN shop.refunds r ON o.id = r.order_id"
print([f.subject for f in check_sql(schema, sql).findings])   # ['SUM(o.amount)  x  shop.refunds']
print(guard("SELECT * FROM shop.orders; DROP TABLE shop.users").layer)   # L1
```

With a live database (optional): `bash scripts/setup_pg.sh` covers the `apt` and `dnf` families,
then `python demo.py --db postgresql://…/fence_demo` — step by step in the
[Oracle Linux 10 VM](docs/quickstart-oraclelinux-vm.md) or
[Cloud Studio](docs/quickstart-cloudstudio.md) quickstart. A question the agent refuses:

```
[ask] 帮我删掉 orders 这张表
  · the question asks for a write — this agent may only read
  I cannot do that.  I only read: no INSERT, UPDATE, DELETE or DDL leaves this process.  If
  you need the data changed, that has to go through a change request against a write-capable role.
```

## Status

**Early. The offline path runs end to end; live mode is partly wired up.** Done: checks 1–4 and
hygiene, the seven-layer guardrail and its selftests, live catalogue and `pg_stats` reads, the
knowledge layer on JSON or pgvector, the eval harness, and 97 offline QA cases. Not done: the
migration diff.

## Known limitations

- **Both readers are hand-written, and both stop where they stop.** The DDL reader assumes
  one column definition per line; the SQL-side checks read a single query level and abstain,
  with a note, on anything nested, unparsable or ambiguous. Production should parse with
  `sqlglot` or `pg_query`; it is dependency-free on purpose, so `python demo.py` runs on a
  fresh clone with no install.
- **The offline embedding is lexical, not semantic**, and its scores are not comparable
  with an API model's (0.089–0.285 against 0.5–0.7). 97.7% over 40 chunks proves the
  plumbing works, not that retrieval is good.
- **The eval harness is a ruler under revision.** Each question accepts a set of sources (`甲 / 乙`),
  but it is still one rank against that set, so read the per-question table before the headline.
- **Corpora are searched one directory at a time.** IDF is computed over the corpus that was
  ingested, so mixing two packs in one store moves the hit rate silently. Check 5 is not in
  the report yet, the planner is keyword-based on purpose, and the live path is PostgreSQL only.

## Why I built this

I spent 21 years as an Oracle DBA (OCM), leading zero-downtime Oracle-to-distributed-DB
migrations across 20 regions. Every migration failure I have seen was silent first: the SQL
ran, the numbers looked plausible, and the damage surfaced weeks later.

AI-generated SQL fails the same way, and so does AI-generated diagnosis. An answer that reads
well and cites nothing is the same failure mode as a query that returns a number nobody checks.
What I can bring here is both readings at once: what the model is actually doing, and what the
database will actually do with it. So the project is laid out in the order I would work, and in a
form a model can use: recall how the problem was solved before, look at the instance instead of a
description of it, and only then let a statement through. That last step is the one I trust a
model with least, which is why it is written as fixed rules.

## Contributing

Issues and PRs welcome — especially **real-world examples of plausible-but-wrong SQL**
(sanitized, please). They become test cases; use the [issue template](.github/ISSUE_TEMPLATE/wrong-sql.md).

## License

[MIT](LICENSE)
