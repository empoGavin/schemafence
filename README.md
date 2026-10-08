# schemafence

> an agent that answers database questions from a DBA knowledge base and diagnoses issues on a live instance, and a fence that every statement must pass through before it reaches the database.
>
> 一个懂数据库的助手（知识库 + 活库诊断，比如回答"这张表为什么慢"），  
> 外加一圈护栏（每一条要落到库上的语句，先过七道闸门再执行）。

---

## Two halves, and one rule about how they meet

The knowledge side and the constraint side are deliberately separate. Most database AI  
tools ship one of them and let it stand in for the other: a diagnostic answer and a  
write-capable connection end up in the same process with nothing in between.

|                | **The agent** (knowledge side)                                               | **The fence** (constraint side)                          |
| -------------- | ---------------------------------------------------------------------------- | -------------------------------------------------------- |
| Answers        | "orders 表为什么这么慢？" · "idle in transaction 有什么后果？"                             | "这条语句允许执行吗？"                                             |
| How it decides | retrieval, tool evidence, optionally a model                                 | fixed rules, no model and no retrieval                   |
| Nature         | probabilistic: a wrong answer is bad advice                                  | deterministic: a wrong decision damages data silently    |
| Tuning it      | corpus, chunk/overlap/top-k, relevance floor, embedder, model                | not possible, by design                                  |
| Its evidence   | [`eval/report-dba.md`](eval/report-dba.md) — 44 questions, hit rate per note | `demo.py` selftest — 13 guard + 10 router + 6 tool cases |
| Where it lives | `schemafence/agent.py`, `schemafence/knowledge.py`                           | `schemafence/guard.py`                                   |

They meet in one place: `Toolbox` in `schemafence/tools.py`. Retrieved passages go  
into the answer text and no further. Nothing from the knowledge base reaches the  
execution path, and `guard()` is the only thing that decides whether a statement  
runs. That is why a mediocre retrieval score is a quality problem and not a safety  
one.

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
python demo.py          # no database, no model, no API key, about 30 seconds
```

```
schemafence — the DBA agent, and the fence in front of the database
====================================================================
source : sample_schema.sql
tables : 7      columns : 33
mode   : offline (no database, no model, no API key)

[check 1] schema resolution — which entity gets picked   (1)
[check 2] join-key sanity — rows silently dropped   (4)
[check 3] type & precision — values silently changed   (6)
[check 4] NULL semantics — three-valued logic traps   (1)
[check H] hygiene — comments and naming   (4)
                                        # finding detail trimmed; see --verbose
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
  ok   run_sql / blocked call             decision=blocked, layer=L2

  these cases exist because of a bug the other tests missed: the
  relevance floor asked whether an embedder was attached, and the CLI
  attaches one even offline, so an API-grade floor of 0.45 filtered
  out every lexical hit (their scores run 0.089-0.285).
  7 cases → all passed
```

The `[tools]` block was added after a bug the other tests could not see. The relevance  
floor asked whether an embedder was attached, and `open_store` attaches one on every  
path, offline included. Offline answers were therefore filtered with the API floor of  
0.45 against lexical scores of 0.089–0.285: every passage was dropped, and the agent  
told the user "the note may not exist yet" about notes it had just ingested. The unit  
test that missed this built a store with `embedder=None`, which is not how the CLI  
wires it.

The agent half runs offline too, with no database, no API key and no network. Here it  
is answering the demo scenario's question with nothing but a corpus and a rule-based  
router:

```bash
python agent_cli.py --ingest examples/knowledge-dba   # chunk → embed → store
python agent_cli.py --ask "订单表查询走了顺序扫描，是不是该加个索引？"
python agent_cli.py --eval                            # top-k retrieval hit rate
```

```
[ask] 订单表查询走了顺序扫描，是不是该加个索引？
  · no signal in the question — fall back to the knowledge base

  ok 1. search_docs(query=订单表查询走了顺序扫描，是不是该加个索引？, k=5)
      → 5 passage(s)  [3 ms]

  driver: rules / no model   rounds: 1   6 ms

  From the knowledge base:
    · pg-bloat-seq-scan · 表膨胀导致的顺序扫描误诊：看着像缺索引，其实是死元组 (score 0.170) —
      表膨胀导致的顺序扫描误诊：看着像缺索引，其实是死元组。 一次典型的误诊路径。
      虚构场景：订单表 `orders` 查询突然变慢，`EXPLAIN` 显示 `Seq Scan`，
      开发的第一反应是"加个 status 索引"。 但翻 `pg_stat_user_tables` 发现：
      - 表 300 MB，`n_live
    · pg-index-not-used · 索引建了却不走：六种常见原因与验证手段 / 原因六：参数化查询走了通用计划 (score 0.138) —
      原因六：参数化查询走了通用计划。 同一个 SQL 用不同参数反复执行时，
      PG 可能从自定义计划切到通用计划，而通用计划对某些参数值恰好很糟…
    · pg-slow-query-method · 慢查询定位：从现象到 SQL 原文 / 一个合成案例：月末报表拖垮库 (score 0.134) —
      一个合成案例：月末报表拖垮库。 某零售订单系统（虚构）在月末出现整体响应变慢…
```

The CLI prints up to 200 characters per passage; entries above end where I cut  
them for width, not where the corpus does. Note the shape of the top hit: the  
chunk text carries its own heading (`表膨胀导致的顺序扫描误诊…` repeated), because  
headings are inlined into the chunk at ingest time — without that, a question  
phrased like a heading would not match the chunk that the heading belongs to.

## Half one — the agent

`demo.py` audits a schema. `agent_cli.py` is the agent that uses one: it answers a  
question by choosing between the knowledge base and the live catalogue, and every SQL  
statement it produces still goes through `guard()`.

```bash
python agent_cli.py --ingest examples/knowledge-dba   # chunk → embed → store
python agent_cli.py --ask "订单表查询走了顺序扫描，是不是该加个索引？"
python agent_cli.py --eval                            # top-k retrieval hit rate
python agent_cli.py --eval --report eval/report-dba.md # eval + tuning grid, written down
python agent_cli.py --genq                            # draft eval questions from the corpus
python agent_cli.py --tune                            # chunk × overlap × top-k
```

When part of the environment is missing, the tool says so instead of answering anyway:

```
[ask] PG 里表膨胀怎么治理？                       # no --db, no key, no network
  · the question is about the state of the data
  · the question is about practice — check what we already know

  !! 1. get_table_stats()
      → not run: no database attached — start PostgreSQL and pass --db  [0 ms]
  ok 2. search_docs(query=PG 里表膨胀怎么治理？, k=5)
      → 5 passage(s)  [3 ms]

  driver: rules / no model   rounds: 1   8 ms

  (get_table_stats: not run: no database attached — start PostgreSQL and pass --db)
  From the knowledge base: …                       # passage list trimmed here
```

Step 1 is the rule in miniature. With nothing to query, the tool does not return  
something plausible; it reports what it cannot reach, the step is marked `!!`, and the  
answer stays inside what the corpus can support.

With a database and a model attached, the same three steps happen in a different order:  
read the plan, read the table's stats, search the corpus.

```
  ok 1. explain_sql(sql=EXPLAIN ANALYZE SELECT * FROM shop.orders;)
      → 4 plan line(s)  [6 ms]        # Seq Scan on orders (cost 0.00..37010.60, rows=336460)
  ok 2. get_table_stats(table=shop.orders)
      → 1 table(s)  [26 ms]           # 305.8 MB, ~10 rows, dead 3240081 (90.0%)
  ok 3. search_docs(query=orders 表查询慢，执行计划是全表扫描, k=5)
      → 5 passage(s)  [855 ms]        # pg-bloat-seq-scan · 死元组与顺序扫描

  driver: model / Qwen/Qwen3-8B   rounds: 3
```

The third call is the one that changed the design. In the first live run the model  
answered the bloat question correctly without searching the corpus at all. The reasoning  
frame came from the system prompt, the numbers from `get_table_stats`, and the fixes  
from its own training data. The answer was right but generic, and it never mentioned  
that `VACUUM FULL` takes an `ACCESS EXCLUSIVE` lock. Searching the knowledge base is now  
a required step, and the answer has to cite the passage it used or say plainly that none  
matched.

### The four tools

`search_docs`, `run_sql`, `explain_sql`, `get_table_stats`. All four go through one door,  
`guard()`, and every call is appended to `agent_trace.jsonl` with its decision, the layer  
it was decided at, and its duration. The loop is hand-written rather than delegated to a  
framework, so that tool selection and failure handling stay explainable line by line.

Two failures were found by running this against a live database, not by reading the code.

The first was a tool that failed identically every time. A missing `doc_chunks` table made  
`search_docs` raise the same error four rounds in a row, each repeat paying for an  
embedding request, until the tool budget was gone. Calls are now memoised by  
(tool, arguments): a failed repeat is refused with a note that the identical call already  
failed, and a successful repeat gets its cached payload back with a note to move on. A  
round made entirely of repeats trips a breaker that withholds the tools and forces a  
plain-text answer.

The second was a model endpoint that went quiet. `Qwen3-8B` is a thinking model, and with  
a ~2k-token system prompt its first call can exceed the read timeout. An unhandled  
`TimeoutError` used to kill the process and take the collected evidence with it. There is  
now retry with backoff (4xx is not retried — a 401 should not cost three attempts to  
report a bad key), a 300 s default timeout, and a degradation path that ends the run with  
the evidence assembled by hand and a line saying what happened.


### A keyword is not intent

`删除` appears both in `帮我删掉 orders 这张表`, which has to be refused, and in  
`怎么安全地删除大表的历史分区？`, which has to be answered. The router reads the shape of  
the sentence as well as the vocabulary: an imperative is a request to act and is refused;  
a question is a request to explain and is answered from the knowledge base with nothing  
executed.

The two mistakes cost different things, so they are not weighed the same. A write request  
that reaches the tools is stopped by `guard()` with no side effect. A real question that  
gets refused is simply lost, and no later gate brings it back. The router therefore errs  
toward answering, and the determinism stays in the layer that can enforce it. Both  
behaviours are pinned by a selftest, so widening the keyword list cannot quietly make the  
assistant mute.

### Three independent axes

Three separate choices, not one "offline vs live" switch. Every combination works:

| axis      | default                                          | switched by                                                                 | if you leave it out                                                        |
| --------- | ------------------------------------------------ | --------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| embedding | deterministic hashed lexical vector + corpus IDF | `SF_EMBED_API_KEY` (`--mode api`)                                           | nothing is downloaded and no SDK is needed: the vector is computed locally |
| store     | JSON file under `.schemafence/`                  | `--db` → pgvector + HNSW ([`scripts/setup_rag.sql`](scripts/setup_rag.sql)) | stays a file on disk, still works                                          |
| driver    | rule-based router (the honest baseline)          | `--llm openai` + `SF_LLM_API_KEY`                                           | stays deterministic, still answers                                         |

"Live" in this repository means a real database is attached (`--db`). It says nothing  
about API keys. `--db` with no key at all is a supported and useful configuration:  
pgvector stores the vectors, and the vectors themselves still come from the offline  
hashed embedding. Run it with no network and no account:

```
$ python agent_cli.py --ask "复制延迟看哪个指标？" --db postgresql://…/fence_demo
embedding : offline / hashed-lexical + corpus idf / 1024d
storage   : pgvector postgresql://…/fence_demo
driver    : rules
```

An API key changes which embedding is used and which router drives the loop. It is not a  
prerequisite for anything here to run.

### The corpus, and why retrieval quality is measurable

Two corpora ship with the repo, and both are meant to be replaced with your own:

| Corpus                    | What it is                                                         | Notes          | Questions                    |
| ------------------------- | ------------------------------------------------------------------ | -------------- | ---------------------------- |
| `examples/knowledge/`     | 8 de-identified runbooks (the original pack)                       | 8              | `eval/questions.md` (16)     |
| `examples/knowledge-dba/` | synthetic notes — public PostgreSQL knowledge, fictional scenarios | 14 / 40 chunks | `eval/questions-dba.md` (44) |

If your own notes are not allowed to leave the company, read  
[`docs/synthetic-corpus.md`](docs/synthetic-corpus.md) first. It covers what is safe to  
write down, how to de-identify an incident, and it ends with a checklist to run before  
publishing. In most corporate RAG projects the corpus rules fail before the retrieval  
does.

`--eval` is a measuring stick, not a pass/fail gate. 44 questions over 40 chunks come out  
at **97.7% top-5** (43/44) with the offline lexical embedder. The single miss is a  
word-form problem rather than a chunking problem: the note says `50% 用量` and the  
question says `一半`. That is exactly where a hashed lexical vector fails and a semantic  
one should not (see the report).

Growing the corpus then exposed a labelling problem, which turned out to be the more  
useful lesson. The new `pg-bloat-seq-scan` note answers question 9 as well as  
`pg-vacuum-tuning` does, so a single-label harness scored a true hit as a miss and the  
headline number fell for a reason that had nothing to do with retrieval. Questions now  
accept a set of sources (`甲 / 乙`). Adding notes tends to break the labels before it  
breaks the retrieval.

Two things the CLI prints so that you do not have to remember them:

- the hit rate always appears next to the corpus size (100% over 16 chunks proves nothing);
- `--eval` prints each question's **top-1 score** and names the floor to use — the lowest  
  top-1 score among the questions that hit, minus a margin. That is how the relevance  
  floor below was calibrated instead of guessed.

### What keeps irrelevant citations out

When the corpus has no answer, top-k still returns k passages, and a model that was told  
to cite something will cite the nearest neighbours. A real question about  
`idle in transaction` came back full of subtransaction overflow and inode exhaustion: both  
retrieved, neither relevant. Four things keep that out, cheapest first:

1. **A relevance floor in `search_docs`.** Passages below it are dropped and reported  
   (`dropped_below_floor`, `top_score`). The default is 0.45 with API embeddings and 0 with  
   the offline hash embedder, whose scores run an order of magnitude lower (0.089–0.285 on  
   this corpus) and would lose real hits to any absolute threshold. `--min-score` /  
   `SF_DOC_MIN_SCORE`.
2. **Citation discipline in the system prompt.** A note about subtransactions does not  
   answer a question about idle transactions, and "no matching note exists" is a complete  
   answer. Wrapping a near miss in a citation is worse than admitting the gap, because the  
   citation makes it look verified.
3. **Calibration from the eval run**, above, rather than a constant picked by hand.
4. **Filling the gap.** `pg-idle-in-transaction.md` exists because the corpus, not the  
   model, was the actual problem.

Not built yet, and the next tier: reranking, hybrid BM25 + vector retrieval (exact terms  
like `idle in transaction` are where keywords beat embeddings), and post-hoc citation  
auditing — a deterministic check that every cited source really came from this retrieval  
round, which is `guard()` applied to prose.

## Half two — the fence

### Why it exists

NL2SQL has had a few years now, and the hard part turned out to be the database rather  
than the language model.

The most common failure is not a syntax error but a misread schema: the model picks the  
wrong table, joins on a lossy key, misreads a nullable column, or confuses two similarly  
named fields. The query runs, and it returns a number that looks entirely reasonable.  
Nothing raises an error; the report is simply wrong.

`schemafence` sits between the model and the data. Before a generated query reaches the  
database it is checked against what the schema actually means, not only against whether it  
parses.

> SQL 不会报错，报表只是"错了"——没有人会收到任何告警。`schemafence` 就是为这种失败形态而建。

### What it checks

| # | Check                          | Catches                                                                                                                                                                         |
| - | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **Schema resolution**          | Near-duplicate tables and columns the model will confuse (`orders` / `orders_archive`)                                                                                          |
| 2 | **Join-key sanity**            | Nullable key columns: the join that silently drops rows                                                                                                                         |
| 3 | **Type & precision mismatch**  | Money in floating point, timestamps held as text, timestamps without a zone                                                                                                     |
| 4 | **NULL semantics**             | `NOT IN (SELECT …)` over a nullable column — returns empty, no error                                                                                                            |
| 5 | **Result plausibility**        | Row counts wildly outside expectation (needs a live database)                                                                                                                   |
| 6 | **Runtime guardrails**         | Seven layers: L1 single statement · L2 read-only shape · L3 deny-listed keywords · L4 dangerous functions · L5 table whitelist · L6 forced LIMIT · L7 audit + session hardening |
| 7 | **Migration diff** *(planned)* | Same query, different behaviour after Oracle → PostgreSQL / domestic DB migration                                                                                               |

Check 7 is the reason the project exists. Most migration defects are semantic rather than  
syntactic, and no syntax converter will catch them.

Checks 1–4 and the guardrails (6) run today, with or without a database. Check 5 needs a  
live connection and is partly wired up. Check 7 is not built yet.

The whole pipeline, from question to verdict:

```bash
python demo.py --ask "total order amount for the last week?"
#   question : total order amount for the last week?
#   #1  shop.orders                score 6
#   #2  shop.orders_archive        score 6
#   #3  shop.refunds               score 4
#   note : 3 candidate tables scored close together — the model picks one, and
#          nothing in the database stops it picking the wrong one
#   guard: ALLOWED (rewritten)   →  LIMIT 100 added, session hardened
```

A question with one obvious answer behaves the other way:

```bash
python demo.py --ask "哪个表存了退款信息？"
#   #1  shop.refunds               score 12      ← one clear candidate, nothing to catch
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


A question the agent has to refuse rather than answer:

```
[ask] 帮我删掉 orders 这张表
  · the question asks for a write — this agent may only read

  driver: rules / no model   rounds: 1   0 ms

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

**Early. The offline path runs end to end; live mode is partly wired up.**

- [x] Project skeleton
- [x] Offline audit: checks 1–4 plus hygiene, straight from a DDL file
- [x] Seven-layer guardrail with a 13-case selftest, plus router and tool selftests (no database needed)
- [x] Live mode: reads the catalogue and `pg_stats` (measured NULL fractions)
- [x] Setup script for both `apt` and `dnf` families, plus two quickstarts ([Oracle Linux VM](docs/quickstart-oraclelinux-vm.md) · [Cloud Studio](docs/quickstart-cloudstudio.md))
- [x] Knowledge layer: chunk (heading-aware) → embed → store → retrieve, JSON or pgvector
- [x] Four tools + hand-written agent loop, rules offline and function calling with a key
- [x] Retrieval eval harness, tuning grid and a generated [`eval/report-dba.md`](eval/report-dba.md)
- [x] Offline QA suite: 84 cases over the guard, router, retrieval, CLI and the layer artefact ([`tests/`](tests/))
- [x] Agent hardening found by running it live: call memoisation, loop breaker, endpoint degradation, relevance floor, typed schema catalogue
- [ ] Migration diff (Oracle → PostgreSQL / domestic DB)

## Known limitations

It helps to know where the fence is thin.

- **The DDL reader is hand-written** and assumes one column definition per line.  
  Fine for the bundled example; production should parse with `sqlglot` or  
  `pg_query`. It is deliberately dependency-free, so `python demo.py` works on a  
  fresh clone with no install.
- **The offline embedding is a lexical bag of words, not a semantic one.** It is the  
  baseline the real thing gets measured against, and it is why `--eval` always prints  
  the corpus size next to the hit rate: 97.7% over 40 chunks proves the plumbing works,  
  not that retrieval is good. Switch to an API embedding with `SF_EMBED_API_KEY` and  
  re-run `--eval`.
- **Retrieval scores are not comparable across embedders.** API-model similarities live  
  around 0.5–0.7; the offline hash embedder scores 0.089–0.285 on the same corpus. That is  
  why the relevance floor defaults to 0 offline and is calibrated from `--eval` before  
  being used with a real embedding.
- **The eval harness is a ruler under revision.** Each question now accepts a set of  
  sources (`甲 / 乙`), but it is still one rank against that set: a third note could  
  legitimately answer the same question and the number would not know. Read the hit rate  
  as a lower bound, and read the per-question table before the headline.
- **Corpora are searched one directory at a time.** IDF is computed over the corpus that  
  was ingested, so mixing the two bundled packs in a single store changes the weights and  
  measurably moves the hit rate (97.4% → 94.7% in an earlier run). Ingest one corpus.
- **Check 5 is not in the report yet.** Row estimates are printed, but nothing compares  
  them against an expectation.
- **The planner is keyword-based on purpose.** It stands in for a model so the demo needs  
  no API key. Nothing about the fence changes when a real model is plugged in.
- **PostgreSQL only** for now (catalogue queries and `pg_stats`). Oracle is where the  
  migration-diff work starts.

## Why I built this

I spent 21 years as an Oracle DBA (OCM), leading zero-downtime Oracle-to-distributed-DB  
migrations across 20 regions. Every migration failure I have seen was silent first: the SQL  
ran, the numbers looked plausible, and the damage surfaced weeks later.

AI-generated SQL fails the same way, and so does AI-generated diagnosis. An answer that  
reads well and cites nothing is the same failure mode as a query that returns a number  
nobody checks. This project is that experience turned into code: one half that remembers  
how these problems were solved before, and one half that assumes the model will be wrong  
eventually.

> 我做了 21 年 Oracle DBA（OCM），主导过 3 个产品、覆盖 20 个 Region 的零停机去 O 迁移。  
> 见过的迁移事故几乎都是"先静默、后爆炸"。AI 生成的 SQL 正在用同样的方式失败——这个项目就是把那段经验写成代码。

## Contributing

Issues and PRs welcome — especially **real-world examples of plausible-but-wrong SQL**  
(sanitized, please). They become test cases.

## License

[MIT](LICENSE)

---
