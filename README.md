# schemafence

> **Two halves: an agent that diagnoses a database from a DBA knowledge base, and a fence that stops it doing damage.**
>
> `schemafence` 由两半组成——**一个懂数据库的助手**（DBA 知识库 + 活库证据，回答"这张表为什么慢"），
> 和**一圈护栏**（每一条要落到库上的语句，先过七道闸门再执行）。

---

## Two halves, and one rule about how they meet

Most database AI tools ship one of the two and pretend it covers the other. They answer
diagnostic questions *and* hold a write-capable connection, with nothing in between.

|                    | **The agent** (knowledge side)                                   | **The fence** (constraint side)                          |
| ------------------ | ---------------------------------------------------------------- | -------------------------------------------------------- |
| Answers            | "orders 表为什么这么慢？" · "idle in transaction 有什么后果？"      | "这条语句允许执行吗？"                                     |
| How it decides     | retrieval + tool evidence + (optionally) a model                  | fixed rules — no model, no retrieval                      |
| Nature             | **probabilistic** — a bad answer is bad advice                    | **deterministic** — a bad decision is silent data damage   |
| Can it be tuned?   | yes: corpus, chunk/overlap/top-k, relevance floor, embedder, model | no. that is the point                                     |
| Its evidence       | [`eval/report-dba.md`](eval/report-dba.md) — 44 questions, hit rate per note | `demo.py` selftest — 13 guard + 10 router + 6 tool cases   |
| Where it lives     | `schemafence/agent.py`, `schemafence/knowledge.py`                | `schemafence/guard.py`                                    |

They meet in exactly **one** place: `Toolbox` in `schemafence/tools.py`. Retrieved passages
travel into the answer text and stop there — **nothing from the knowledge base ever enters
the execution path**, and the only thing that decides whether SQL runs is `guard()`.
That separation is why a mediocre retrieval score is a quality problem, not a safety problem.

```
   question ──▶ agent loop ──▶ Toolbox ──▶ guard() ──▶ database (read-only)
                    ▲                        │
                    │  answer text only      │  seven layers, every statement
              knowledge base  ◀── search_docs│
```

## Quick start

```bash
git clone https://github.com/empoGavin/schemafence
cd schemafence
python demo.py          # no database, no model, no API key — about 30 seconds
```

```
schemafence — the DBA agent, and the fence in front of the database
====================================================================
[check 1] schema resolution — which entity gets picked   (1)
[check 2] join-key sanity — rows silently dropped   (4)
[check 3] type & precision — values silently changed   (6)
[check 4] NULL semantics — three-valued logic traps   (1)
[check H] hygiene — comments and naming   (4)

[summary]
  16 finding(s): 5 high / 9 medium / 2 low
[guard] seven-layer selftest
  13 cases → all passed
[router] write intent vs practice question
  10 cases → all passed
[tools] four tools, no database attached
  ok   search_docs / offline hit          ok, hits>=1, floor 0
  ok   search_docs / floor honoured       ok, 0 hits + note
  ok   run_sql / no database              ok=False, says so
  ok   explain_sql / no database          ok=False, says so
  ok   get_table_stats / no database      ok=False, says so
  ok   get_table_stats / bad args         decision=bad-arguments
  6 cases → all passed
```

The `[tools]` block is a smoke test that exists because a real bug got past the
others: the relevance floor asked *"is an embedder attached?"* while `open_store`
attaches one on every path — so offline answers were filtered with an API-grade
floor of 0.45 against lexical scores of 0.089–0.285, and the agent told the user
"the note may not exist yet" about notes it had just ingested. **Presence of an
embedder is not the same as a semantic one**, and the test that missed it
constructed the store the way the product does not.

The agent half runs offline too — no database, no API key, no network. Here it is
answering the demo scenario's question with nothing but a corpus and a rule-based router:

```bash
python agent_cli.py --ingest examples/knowledge-dba   # chunk → embed → store
python agent_cli.py --ask "订单表查询走了顺序扫描，是不是该加个索引？"
python agent_cli.py --eval                            # top-k retrieval hit rate
```

```
[ask] 订单表查询走了顺序扫描，是不是该加个索引？
  · no signal in the question — fall back to the knowledge base

  ok 1. search_docs(query=订单表查询走了顺序扫描，是不是该加个索引？, k=5)
      → 5 passage(s)  [4 ms]

  driver: rules / no model   rounds: 1   7 ms

  From the knowledge base:
    · pg-bloat-seq-scan · 表膨胀导致的顺序扫描误诊：看着像缺索引，其实是死元组 (score 0.170) —
      虚构场景：订单表 `orders` 查询突然变慢，`EXPLAIN` 显示 `Seq Scan`，开发的第一反应是"加个 status 索引"…
    · pg-index-not-used · 索引建了却不走：六种常见原因与验证手段 / 原因六：参数化查询走了通用计划 (score 0.138)
    · pg-slow-query-method · 慢查询定位：从现象到 SQL 原文 / 一个合成案例：月末报表拖垮库 (score 0.134)
```

## Half one — the agent

`demo.py` audits a schema. `agent_cli.py` is the agent that *uses* one: it answers a
question by choosing between a knowledge base and the live catalogue, and every SQL
statement it produces still goes through `guard()`.

```bash
python agent_cli.py --ingest examples/knowledge-dba   # chunk → embed → store
python agent_cli.py --ask "订单表查询走了顺序扫描，是不是该加个索引？"
python agent_cli.py --eval                            # top-k retrieval hit rate
python agent_cli.py --eval --report eval/report-dba.md # eval + tuning grid, written down
python agent_cli.py --genq                            # draft eval questions from the corpus
python agent_cli.py --tune                            # chunk × overlap × top-k
```

And when part of the environment is missing, the tool says so rather than answering anyway:

```
[ask] PG 里表膨胀怎么治理？                       # no --db, no key, no network
  · the question is about the state of the data, not about practice
  · the question is about practice — check what we already know

  !! 1. get_table_stats()
      → not run: no database attached — start PostgreSQL and pass --db  [0 ms]
  ok 2. search_docs(query=PG 里表膨胀怎么治理？, k=5)
      → 5 passage(s)  [2 ms]

  driver: rules / no model   rounds: 1   5 ms

  (get_table_stats: not run: no database attached — start PostgreSQL and pass --db)
```

Note step 1: the tool does **not** silently return something plausible. It says what
it cannot reach, the step is marked `!!`, and the answer stays limited to what the
knowledge base can actually support. A missing tool is reported, never papered over.

With a database and a model attached, the shape is the same and the stakes are higher —
three calls, three jobs: *how* it ran, *why*, and *what to do about it*.

```
  ok 1. explain_sql(sql=EXPLAIN ANALYZE SELECT * FROM shop.orders;)
      → 4 plan line(s)  [6 ms]        # Seq Scan on orders (cost 0.00..37010.60, rows=336460)
  ok 2. get_table_stats(table=shop.orders)
      → 1 table(s)  [26 ms]           # 305.8 MB, ~10 rows, dead 3240081 (90.0%)
  ok 3. search_docs(query=orders 表查询慢，执行计划是全表扫描, k=5)
      → 5 passage(s)  [855 ms]        # pg-bloat-seq-scan · 死元组与顺序扫描

  driver: model / Qwen/Qwen3-8B   rounds: 3
```

The third call is the one worth watching. In the first live run the model answered a bloat
question correctly **without ever searching the corpus** — the reasoning frame came from
the system prompt, the numbers from `get_table_stats`, and the fixes from its own
parametric knowledge. Correct, but generic: it never mentioned that `VACUUM FULL` takes an
`ACCESS EXCLUSIVE` lock. Reusing a model's prior over a runbook is how an agent looks
smart while being useless, so searching the knowledge base is now a required step, and the
answer has to cite the passage or say plainly that none matched.

### The four tools

`search_docs`, `run_sql`, `explain_sql`, `get_table_stats`. All four are dispatched
through one door — `guard()` — and every call is appended to `agent_trace.jsonl` with
its decision, the layer it was decided at, and how long it took. **The loop is
hand-written on purpose:** when an interviewer asks how the agent chooses a tool and
what happens when one fails, the answer has to come from code you wrote.

Two of the failure modes it has to survive are worth naming, because both were found by
running it, not by reasoning about it:

- **A tool that fails identically forever.** A missing `doc_chunks` table made
  `search_docs` raise the same error four rounds in a row — each repeat paying for an
  embedding request — until the tool budget was gone. Calls are now memoised by
  (tool, arguments): a failed repeat is refused with "this exact call already failed,
  change approach", a *successful* repeat gets the cached payload back plus "you already
  have this, use it". A round made entirely of repeats trips a breaker that withholds the
  tools and forces a plain-text answer.
- **A model endpoint that goes quiet.** `Qwen3-8B` is a thinking model; with a ~2k-token
  system prompt the first call can exceed the read timeout. An unhandled `TimeoutError`
  used to kill the process and take the collected evidence with it. There is now
  retry-with-backoff (4xx is *not* retried — a 401 should not cost three attempts to
  report a bad key), a 300 s default timeout, and a degradation path that ends the run
  with the evidence assembled by hand, prefixed by what actually happened.

### A keyword is not intent

`删除` appears both in `帮我删掉 orders 这张表` (must be refused) and in
`怎么安全地删除大表的历史分区？` (a practice question, must be answered). The router
reads the **shape** of the sentence, not just the vocabulary: an imperative is a request
to act and is refused, a question is a request to explain and is answered from the
knowledge base with nothing executed.

The two mistakes are not symmetric. Letting a write request reach the tools costs one
refusal with no side effect — `guard()` is what actually stops a statement. Killing a
real question costs the user his answer, and no later gate can restore it. So the router
is deliberately generous, and the determinism lives in the one place that can enforce it.
Both behaviours are pinned by a selftest so that widening the keyword list cannot quietly
turn the assistant mute.

### Three independent axes

Not one "offline vs live" switch — each is chosen separately, and every combination works:

| axis | default | switched by | what it costs if you skip it |
| --- | --- | --- | --- |
| embedding | deterministic hashed lexical vector + corpus IDF | `SF_EMBED_API_KEY` (`--mode api`) | nothing is downloaded, no SDK: the vector is computed locally |
| store | JSON file under `.schemafence/` | `--db` → pgvector + HNSW ([`scripts/setup_rag.sql`](scripts/setup_rag.sql)) | stays a file on disk, still works |
| driver | rule-based router (the honest baseline) | `--llm openai` + `SF_LLM_API_KEY` | stays deterministic, still answers |

So "live" in this repository means **a real database is attached** (`--db`) — it says
nothing about API keys. `--db` with no key at all is a supported and useful
configuration: pgvector stores the vectors, and the vectors themselves still come from
the offline hashed embedding. Run it with no network and no account:

```
$ python agent_cli.py --ask "复制延迟看哪个指标？" --db postgresql://…/fence_demo
embedding : offline / hashed-lexical + corpus idf / 1024d
storage   : pgvector postgresql://…/fence_demo
driver    : rules
```

The only thing an API key buys you is a *different* embedding (and, for the driver, a
different router). It is never a prerequisite for the agent to run.

### The corpus, and why retrieval quality is measurable

Two corpora ship with the repo, and both are meant to be swapped for your own:

| Corpus                    | What it is                                                             | Notes   | Questions                      |
| ------------------------- | ---------------------------------------------------------------------- | ------- | ------------------------------ |
| `examples/knowledge/`     | 8 de-identified runbooks (the original pack)                            | 8       | `eval/questions.md` (16)        |
| `examples/knowledge-dba/` | synthetic notes — public PostgreSQL knowledge, fictional scenarios      | 14 / 40 chunks | `eval/questions-dba.md` (44) |

If your own notes are not allowed to leave the company, read
[`docs/synthetic-corpus.md`](docs/synthetic-corpus.md) first: it says what is safe to write
down, how to de-identify an incident, and carries a pre-publish checklist. Corporate RAG
usually fails on corpus compliance before it fails on retrieval.

`--eval` is the ruler, not a gate: 44 questions over 40 chunks, currently **97.7% at top-5**
(43/44) with the offline lexical embedder. The one remaining miss is a *word-form* failure,
not a chunking failure: the note says `50% 用量`, the question says `一半` — exactly where a
hashed lexical vector fails and a semantic one should not (see the report).

Growing the corpus also exposed a **label-ambiguity** problem, which is the more
transferable lesson: the new `pg-bloat-seq-scan` note answers question 9 as well as
`pg-vacuum-tuning` does, so a single-label harness scored a true hit as a miss and the
headline number dropped for a reason that had nothing to do with retrieval. Questions now
accept alternatives (`甲 / 乙`). **The first thing a growing corpus breaks is usually the
labels, not the retrieval.**

Two discipline notes the CLI prints for you rather than leaving to the reader:

- the hit rate is always printed next to the corpus size (100% over 16 chunks is a smoke
  test, not a result);
- `--eval` prints each question's **top-1 score** and names the floor to use — the lowest
  top-1 score among the questions that hit, minus a margin. That is how the relevance
  floor below was calibrated instead of guessed.

### What keeps irrelevant citations out

When a question has no counterpart in the corpus, top-k still returns k passages, and a
model told to cite something will cite the nearest neighbours — a real `idle in
transaction` question produced an answer full of subtransaction overflow and inode
exhaustion, both retrieved, both irrelevant. Four levers, cheapest first:

1. **A relevance floor in `search_docs`** — passages below it are dropped and reported
   (`dropped_below_floor`, `top_score`). Default 0.45 with API embeddings, 0 with the
   offline hash embedder, whose scores run an order of magnitude lower (0.089–0.285 on
   this corpus) and would lose real hits to any absolute threshold. `--min-score` /
   `SF_DOC_MIN_SCORE`.
2. **Citation discipline in the system prompt** — a note about subtransactions does not
   answer a question about idle transactions, and "no matching note exists" is a complete
   answer. Wrapping a near miss in a citation is *worse* than admitting the gap, because
   the citation makes it look verified.
3. **Calibration from the eval run** (above) rather than a hand-picked constant.
4. **Filling the gap** — `pg-idle-in-transaction.md` exists because the corpus, not the
   model, was the actual problem.

Not built yet, and worth knowing as the next tier: reranking, hybrid BM25 + vector
retrieval (exact terms like `idle in transaction` are where keywords beat embeddings), and
post-hoc citation auditing — a deterministic check that every cited source really came
from this retrieval round, which is the same idea as `guard()` applied to prose.

## Half two — the fence

### Why it exists

The industry spent two years learning an uncomfortable lesson about NL2SQL: **the hard part
is not the language model — it's the database.**

The most common failure is not a syntax error. It's *schema misinterpretation*: the LLM
picks the wrong table, joins on a lossy key, misreads a NULL-able column, or confuses two
similarly-named fields. The query **executes successfully** and returns a number that looks
perfectly reasonable.

**Nobody gets an error. The report is just wrong.**

`schemafence` puts a fence between the model and your data. Before a generated query
touches the database, it is checked against what the schema actually means — not just
whether it parses.

> SQL 不会报错，报表只是"错了"——没有人会收到任何告警。`schemafence` 就是为这种失败形态而建。

### What it checks

| # | Check                          | Catches                                                                                                                 |
| - | ------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| 1 | **Schema resolution**          | Near-duplicate tables and columns the model will confuse (`orders` / `orders_archive`)                                  |
| 2 | **Join-key sanity**            | Nullable key columns: the join that silently drops rows                                                                 |
| 3 | **Type & precision mismatch**  | Money in floating point, timestamps held as text, timestamps without a zone                                             |
| 4 | **NULL semantics**             | `NOT IN (SELECT …)` over a nullable column — returns empty, no error                                                    |
| 5 | **Result plausibility**        | Row counts wildly outside expectation (needs a live database)                                                           |
| 6 | **Runtime guardrails**         | Seven layers: L1 single statement · L2 read-only shape · L3 deny-listed keywords · L4 dangerous functions · L5 table whitelist · L6 forced LIMIT · L7 audit + session hardening |
| 7 | **Migration diff** *(planned)* | Same query, different behaviour after Oracle → PostgreSQL / domestic DB migration                                       |

Check 7 is the reason this project exists: **most migration defects are semantic, not
syntactic** — and no syntax converter will ever catch them.

Checks 1–4 and the guardrails (6) run today, with or without a database. Check 5 needs a
live connection and is partly wired up. Check 7 is the 30-day goal.

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

Using the fence as a library:

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

And a question that must be refused, not answered — the fence's answer, in prose:

```
[ask] 帮我删掉 orders 这张表
  · the question asks for a write — this agent may only read
  I cannot do that.  I only read: no INSERT, UPDATE, DELETE or DDL leaves this
  process.  If you need the data changed, that has to go through a change request
  against a write-capable role.
```

## How it works

```
                                 ┌─────────────────────────┐
            ┌──────────────┐     │      schemafence        │     ┌──────────┐
 question ─▶│ agent loop   │────▶│  schema check            │────▶│ Database │
            │ (rules / LLM │     │  join & type sanity      │     │ (read-   │
            │  tool calls) │     │  NULL semantics          │     │  only)   │
            └──────┬───────┘     │  plausibility bounds     │     └──────────┘
                   │             │  guard() L1–L7 + audit    │
       search_docs │             └─────────────────────────┘
                   ▼                ▲ retrieved text goes into the
          ┌──────────────────┐      │ answer, never into a statement
          │ knowledge layer  │──────┘
          │ chunk→embed→store│   JSON (offline) or pgvector (live)
          │ notes, runbooks, │   retrieves; never executes
          │ incident write-ups│
          └──────────────────┘
```

## Status

**Early — day 5 of a 7-day build.**

- [x] Project skeleton
- [x] Offline audit: checks 1–4 plus hygiene, straight from a DDL file
- [x] Seven-layer guardrail with a 13-case selftest, plus router and tool selftests (no database needed)
- [x] Live mode: reads the catalogue and `pg_stats` (measured NULL fractions)
- [x] Setup script for both `apt` and `dnf` families, plus two quickstarts ([Oracle Linux VM](docs/quickstart-oraclelinux-vm.md) · [Cloud Studio](docs/quickstart-cloudstudio.md))
- [x] Knowledge layer: chunk (heading-aware) → embed → store → retrieve, JSON or pgvector — *day 4*
- [x] Four tools + hand-written agent loop, rules offline and function calling with a key — *day 5*
- [x] Retrieval eval harness, tuning grid and a generated [`eval/report-dba.md`](eval/report-dba.md) — *day 4*
- [x] Agent hardening found by running it live: call memoisation, loop breaker, endpoint degradation, relevance floor, typed schema catalogue — *day 5*
- [ ] Migration diff (Oracle → PostgreSQL / domestic DB) — *30-day plan*

## Known limitations

A read-only fence is only useful if you know where it is thin.

- **The DDL reader is hand-written** and assumes one column definition per line.
  Fine for the bundled example; production should parse with `sqlglot` or
  `pg_query`. It is deliberately dependency-free, so `python demo.py` works on a
  fresh clone with no install.
- **The offline embedding is lexical, not semantic.** It is the baseline you can
  measure the real thing against, and it is why `--eval` always prints the corpus
  size next to the hit rate: 97.7% over 40 chunks is a smoke test, not a result.
  Switch to an API embedding with `SF_EMBED_API_KEY` and re-run `--eval`.
- **The retrieval scores are not comparable across embedders.** API-model similarities
  live around 0.5–0.7; the offline hash embedder scores 0.089–0.285 on the same corpus.
  That is why the relevance floor defaults to 0 offline and is calibrated from `--eval`
  before being used with a real embedding.
- **The eval harness is a ruler under revision.** Each question now accepts a set of
  sources (`甲 / 乙`), but it is still one rank against that set: a third note could
  legitimately answer the same question and the number would not know. Read the hit rate
  as a lower bound, and read the per-question table before the headline.
- **Corpora are searched one directory at a time.** IDF is computed over the corpus that
  was ingested, so mixing the two bundled packs in a single store changes the weights and
  measurably moves the hit rate (97.4% → 94.7% in an earlier run). Ingest one corpus.
- **Check 5 is not in the report yet.** Row estimates are printed, but nothing
  compares them against an expectation.
- **The planner is keyword-based on purpose.** It stands in for a model so the
  demo needs no API key. Nothing about the fence changes when a real model is
  plugged in — that is the point.
- **PostgreSQL only** for now (catalogue queries and `pg_stats`). Oracle is where
  the migration-diff work starts.

## Why I built this

I spent 21 years as an Oracle DBA (OCM), leading zero-downtime Oracle-to-distributed-DB
migrations across 20 regions. Every migration failure I've seen was *silent first*: the SQL
ran, the numbers looked plausible, and the damage showed up weeks later.

AI-generated SQL fails the same way, and so does AI-generated diagnosis: an answer that
reads well and cites nothing is the same failure mode as a query that returns a number
nobody questions. This project is that experience, turned into code — one half that gives
the model a memory of how these problems were actually solved, and one half that assumes
it will eventually be wrong anyway.

> 我做了 21 年 Oracle DBA（OCM），主导过 3 个产品、覆盖 20 个 Region 的零停机去 O 迁移。
> 见过的迁移事故几乎都是"先静默、后爆炸"。AI 生成的 SQL 正在用同样的方式失败——这个项目就是把那段经验写成代码。

## Contributing

Issues and PRs welcome — especially **real-world examples of plausible-but-wrong SQL**
(sanitized, please). They become test cases.

## License

[MIT](LICENSE)

---
