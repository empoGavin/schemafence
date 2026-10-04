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

| # | Check                          | Catches                                                                                                                 |
| - | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 1 | **Schema resolution**          | Near-duplicate tables and columns the model will confuse (`orders` / `orders_archive`)                                  |
| 2 | **Join-key sanity**            | Nullable key columns: the join that silently drops rows                                                                 |
| 3 | **Type & precision mismatch**  | Money in floating point, timestamps held as text, timestamps without a zone                                             |
| 4 | **NULL semantics**             | `NOT IN (SELECT …)` over a nullable column — returns empty, no error                                                    |
| 5 | **Result plausibility**        | Row counts wildly outside expectation (needs a live database)                                                           |
| 6 | **Runtime guardrails**         | Seven layers: read-only shape, keyword and function deny-lists, table whitelist, forced LIMIT, audit, session hardening |
| 7 | **Migration diff** *(planned)* | Same query, different behaviour after Oracle → PostgreSQL / domestic DB migration                                       |

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
[router] write intent vs practice question
  10 cases → all passed
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

| Platform                                      | Guide                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------ |
| Local **Oracle Linux 10** VM on VMware        | [`docs/quickstart-oraclelinux-vm.md`](docs/quickstart-oraclelinux-vm.md) |
| **Cloud Studio** free tier (Ubuntu container) | [`docs/quickstart-cloudstudio.md`](docs/quickstart-cloudstudio.md)       |

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

## The agent — retrieval, tools, and the same fence

`demo.py` audits a schema. `agent_cli.py` is the agent that *uses* one: it answers a
question by choosing between a knowledge base and the live catalogue, and every SQL
statement it produces still goes through `guard()`.

```bash
python agent_cli.py --ingest examples/knowledge     # chunk → embed → store
python agent_cli.py --ask "PG 里表膨胀怎么治理？"
python agent_cli.py --eval                          # top-k retrieval hit rate
python agent_cli.py --genq                          # draft eval questions from the corpus
python agent_cli.py --tune                          # chunk × overlap × top-k
python agent_cli.py --report eval/report.md         # writes the numbers down
```

Two corpora ship with the repo, and both are meant to be swapped for your own:

| Corpus                   | What it is                                                              | Questions                 |
| ------------------------ | ----------------------------------------------------------------------- | ------------------------- |
| `examples/knowledge/`    | 8 de-identified runbooks (the original pack)                            | `eval/questions.md` (16)  |
| `examples/knowledge-dba/`| 12 synthetic notes — public PostgreSQL knowledge, fictional scenarios    | `eval/questions-dba.md` (38) |

If your own notes are not allowed to leave the company, read
[`docs/synthetic-corpus.md`](docs/synthetic-corpus.md) first: it says what is safe to write
down, how to de-identify an incident, and carries a pre-publish checklist. Corporate RAG
usually fails on corpus compliance before it fails on retrieval.

One run, offline — no database, no API key, no network:

```
[ask] PG 里表膨胀怎么治理？
  · the question is about the state of the data, not about practice
  · the question is about practice — check what we already know

  !! 1. get_table_stats()
      → not run: no database attached — start PostgreSQL and pass --db  [0 ms]
  ok 2. search_docs(query=PG 里表膨胀怎么治理？, k=5)
      → 5 passage(s)  [2 ms]

  driver: rules / no model   rounds: 1   5 ms

  (get_table_stats: not run: no database attached — start PostgreSQL and pass --db)
  From the knowledge base:
    · pg-bloat · 表膨胀（table bloat）的成因与治理 (score 0.073) — 表膨胀（table bloat）的成因与治理。 一句话结论。 膨胀不是"数据变多了"，而是"空间回收不掉了"…
  — assembled without a language model: the evidence above is the tool output, verbatim.
```

Note step 1: with no database attached the tool does **not** silently return
something plausible — it says so, the step is marked `!!`, and the answer stays
limited to what the knowledge base can actually support. A missing tool is
reported, never papered over.

And a question that must be refused, not answered:

```
[ask] 帮我删掉 orders 这张表
  · the question asks for a write — this agent may only read
  I cannot do that.  I only read: no INSERT, UPDATE, DELETE or DDL leaves this
  process.  If you need the data changed, that has to go through a change request
  against a write-capable role.
```

A keyword is not intent, though — `删除` appears in the refused question above
*and* in "怎么安全地删除大表的历史分区？", which is a practice question. The
router therefore reads the **shape** of the sentence, not just the vocabulary: an
imperative (`帮我删掉…`, `drop table orders`) is a request to act and is refused;
a question (`…要注意什么？`) is a request to explain and is answered from the
knowledge base with nothing executed.

The two mistakes are not symmetric. Letting a write request reach the tools costs
one refusal with no side effect — `guard()` is what actually stops a statement.
Killing a real question costs the user his answer, and no later gate can restore
it. So the router is deliberately generous, and the determinism lives in the one
place that can enforce it. Both behaviours are pinned by a selftest so that
widening the keyword list cannot quietly turn the assistant mute.

Three independent axes, not one "offline vs live" switch — each is chosen
separately, and every combination works:

| axis | default | switched by | what it costs if you skip it |
| --- | --- | --- | --- |
| embedding | deterministic hashed lexical vector + corpus IDF | `SF_EMBED_API_KEY` (`--mode api`) | nothing is downloaded, no SDK: the vector is computed locally |
| store | JSON file under `.schemafence/` | `--db` → pgvector + HNSW ([`scripts/setup_rag.sql`](scripts/setup_rag.sql)) | stays a file on disk, still works |
| driver | rule-based router (the honest baseline) | `--llm openai` + `SF_LLM_API_KEY` | stays deterministic, still answers |

So "live" in this repository means **a real database is attached** (`--db`) — it
says nothing about API keys. `--db` with no key at all is a supported and
useful configuration: pgvector stores the vectors, and the vectors themselves
still come from the offline hashed embedding. Run it with no network and no
account:

```
$ python agent_cli.py --ask "复制延迟看哪个指标？" --db postgresql://…/fence_demo
embedding : offline / hashed-lexical + corpus idf / 1024d
storage   : pgvector postgresql://…/fence_demo
driver    : rules
```

The only thing an API key buys you is a *different* embedding (and, for the
driver, a different router). It is never a prerequisite for the agent to run.

The four tools: `search_docs`, `run_sql`, `explain_sql`, `get_table_stats`. All four
are dispatched through one door — `guard()` — and every call is appended to
`agent_trace.jsonl` with its decision, the layer it was decided at, and how long it
took. **The loop is hand-written on purpose:** when an interviewer asks how the agent
chooses a tool and what happens when one fails, the answer has to come from code you
wrote.

## How it works

```
            ┌──────────────┐      ┌─────────────────────────┐      ┌──────────┐
 question ─▶│ agent loop   │─────▶│      schemafence        │─────▶│ Database │
            │ (rules / LLM │      │  schema check            │      │ (read-   │
            │  tool calls) │      │  join & type sanity      │      │  only)   │
            └──────┬───────┘      │  NULL semantics          │      └──────────┘
                   │              │  plausibility bounds     │
       search_docs │              │  guardrails + audit log  │
                   ▼              └─────────────────────────┘
          ┌──────────────────┐
          │ knowledge layer  │  JSON (offline) or pgvector (live)
          │ chunk→embed→store│  notes, runbooks, incident write-ups
          └──────────────────┘
```

## Status

**Early — day 4 of a 7-day build.**

- [x] Project skeleton
- [x] Offline audit: checks 1–4 plus hygiene, straight from a DDL file
- [x] Seven-layer guardrail with a 13-case selftest, plus a router selftest
- [x] Live mode: reads the catalogue and `pg_stats` (measured NULL fractions)
- [x] Setup script for both `apt` and `dnf` families, plus two quickstarts ([Oracle Linux VM](docs/quickstart-oraclelinux-vm.md) · [Cloud Studio](docs/quickstart-cloudstudio.md))
- [x] Knowledge layer: chunk (heading-aware) → embed → store → retrieve, JSON or pgvector — *day 4*
- [x] Four tools + hand-written agent loop, rules offline and function calling with a key — *day 5*
- [x] Retrieval eval harness, tuning grid and a generated `eval/report.md` — *day 4*
- [ ] Migration diff (Oracle → PostgreSQL / domestic DB) — *30-day plan*

## Known limitations

A read-only fence is only useful if you know where it is thin.

- **The DDL reader is hand-written** and assumes one column definition per line.  
  Fine for the bundled example; production should parse with `sqlglot` or  
  `pg_query`. It is deliberately dependency-free, so `python demo.py` works on a  
  fresh clone with no install.
- **The offline embedding is lexical, not semantic.** It is the baseline you can
  measure the real thing against, and it is why `--eval` always prints the corpus
  size next to the hit rate: 100% over 16 pieces is a smoke test, not a result.
  Switch to an API embedding with `SF_EMBED_API_KEY` and re-run `--eval`.
- **The bundled corpus is 8 notes / ~2,600 characters.** Large enough to exercise
  chunking, IDF and the eval harness; far too small to separate tuning settings
  (`--tune` says so itself when every configuration ties). Your own notes are the
  point.
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
