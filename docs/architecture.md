# Architecture: which file does what

Read this when you want to change something. It answers *which file do I open*, and which parts are
deliberately not wired to each other. To run the thing instead, see [`quickstart.md`](quickstart.md).

The project is an **AI × DBA** one and it has three parts: **review** (a script or a statement,
reported on and never run), a **diagnostic agent** (reads an instance, draws a conclusion, remembers
what it saw), and **the fence** (the one place that decides). The tables below say which file each
part lives in — including the parts that are not written yet, because "where would this go" is a
question this file exists to answer.

## Entry points

| Your job | Command | Path through the code |
| --- | --- | --- |
| Review a script somebody wrote | `python -m schemafence.review --rules rules/*.json --ddl colleague.sql` | `review.py` → `ddl.py` → `facts.py` → `rulepack.py` |
| Review the same script against what the live tables already carry | `python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN" --ddl release.sql` | `snapshot.py` → `facts.py` → `rulepack.py` |
| Answer a question your runbook has the answer to | `python agent_cli.py --ask "…"` | `planner.py` → `knowledge.py` → `tools.py` → `guard.py` |
| Diagnose a live instance | `python demo.py --db "$PG_DSN"` | `snapshot.py` → `checks.py` |
| Run everything offline, no database and no key | `python demo.py` | all of the above except the live reads |
| The regression gate | `python tests/qa_probe.py` | 133 cases |
| Check a query for the number it gets wrong | **no command yet** — R11 | would be `checks_sql.py` |
| Hand the gate a statement of your own | **no command yet** — R15 | would be `guard.py` |
| Run a diagnosis, and keep what it found | **no command yet** — R12 | would be `snapshot.py` → `checks.py` → a store of past runs |

**Three gaps worth naming.** The statement checks (S1 and S3, "a measure summed after a one-to-many
join", "a `LEFT JOIN` cancelled by a `WHERE`") have **no command-line entry** — there is no `--sql`.
`demo.py` runs them as a selftest and the library exposes `check_sql`, but you cannot hand it your
own statement and get a report back. That is R11. The gate itself has no front door: it is a library
function, so a statement *you* wrote can only reach it from Python. That is R15. And the diagnostic
half of the agent — one pass that walks a set of checks over an instance, says what they add up to,
and keeps the result — has no code at all yet; both things it would run on exist, the live read in
`snapshot.py` and the schema checks in `checks.py`. That is R12.

## The modules

Everything under `schemafence/` is a library; the two scripts at the repo root are front doors.

| File | What it does |
| --- | --- |
| `ddl.py` | Reads a change script: which tables and columns it ends up leaving behind, which indexes and keys it creates, and what actions it takes to get there. Every spelling of the same thing (a `PRIMARY KEY` on the column, in the table body, as a named constraint, added later by `ALTER TABLE`, or by `CREATE UNIQUE INDEX`) collapses into **one** recorded fact. |
| `snapshot.py` | Reads the same facts out of a running PostgreSQL: tables, columns and indexes from `pg_catalog`, row estimates from `pg_class.reltuples`, NULL fractions from `pg_stats`. `build_schema()` is a pure function so the parsing can be tested offline; `apply_script()` carries the indexes a script under review adds onto the catalog facts. |
| `facts.py` | The fact registry: which fields a rule may select, for each of the fact models (`ddl`, `schema`, `catalog`), and the providers that produce the rows. `--vocabulary` and `--facts` are both generated from here, so the vocabulary a rule is written against cannot drift from the evaluator that runs it. |
| `rulepack.py` | A review rule as a **file**: a selection over facts, an assertion, and the examples that prove it. Verifies a pack against its own examples at load time and refuses it otherwise. Owns `review_ddl()` and `review_schema()` (the half both review paths share) and `--infer`. |
| `checks.py` | The schema checks — resolution, join keys, types and precision, NULL semantics, hygiene. Reads a schema only. |
| `checks_sql.py` | The statement checks (S1, S3). Reads a query **and** a schema, which is why it is a separate module: a fan-out and a cancelled `LEFT JOIN` are valid on both sides and only visible where the two meet. Reports; never decides. |
| `guard.py` | The seven layers that decide whether a statement may run: one statement, read-only shape, deny-listed keywords, dangerous functions, table whitelist, forced `LIMIT`, audit and session hardening. This is the one place where the answer is allow or block. |
| `knowledge.py` | Chunk → embed → store → retrieve. Offline embedder is a hashed lexical vector with corpus IDF; API mode is any OpenAI-compatible endpoint. Two stores: a JSON file, or pgvector with an HNSW index. |
| `planner.py` | Question → candidate tables → SQL, by keyword. No model. Deliberately not clever: it is the thing that makes the offline path work at all. |
| `agent.py` | The loop: pick a tool, keep the evidence, answer. Hand-written rather than delegated to a framework. |
| `tools.py` | The four tools — `search_docs`, `run_sql`, `explain_sql`, `get_table_stats` — and `Toolbox`, the single place the knowledge side and the execution side meet. |
| `review.py` | The command line for reviewing a script, plus `--verify`, `--vocabulary`, `--facts`, `--infer`, `--write`. Exit code 0 clean / 1 rule fired / 2 could not review. |
| `__init__.py` | The public surface, listed with signatures. |

Front doors and harness:

| File | What it does |
| --- | --- |
| `demo.py` | Audits a schema and runs the selftests. Offline by default, `--db` to add the live read. |
| `agent_cli.py` | `--ingest`, `--ask`, `--eval`, `--tune`, `--genq`, `--report`. |
| `tests/qa_probe.py` | 133 offline cases, including the merge gates that keep a rule pack from growing by accident, and the doc checks that keep a quoted number honest. |
| `scripts/` | Corpus converters, the pgvector ingest, the benchmark trio and its report generator, `setup_pg.sh`, `test_layers.py`. |
| `rules/` | Three shipped rule packs: `table-hygiene`, `change-review`, `index-hygiene`. |

## Where the facts come from

There are three sources, and they are not interchangeable. Getting this wrong is the easiest way to
add a rule that quietly never applies.

- **A script is a fact source.** Deterministic, offline, parsed by `ddl.py`.
- **A live instance is the same facts read by another reader.** `snapshot.py` reads `pg_catalog`
  read-only; the `--db` axis never writes anything.
- **A corpus is a list of spellings, not a runtime input.** Every way of writing an index or a key
  that the PostgreSQL manual documents became a test case in `tests/qa_probe.py`. Documentation
  produced **tests**; it never touches the execution path, because retrieval is probabilistic and
  this layer is not allowed to be.

A rule declares which facts it needs in its first lines:

```json
{ "applies_to": ["catalog"], … }
```

A pack whose `applies_to` does not match the facts of this run is **not applied** — it is reported as
`WAS NOT APPLIED`, with the command that would supply the missing facts, and the exit code becomes
**2**. A rule that could not be evaluated has cleared nothing; saying "0 finding(s)" over it would be
the same lie as passing a file nobody understood.

## Three parts, and how they touch

|  | **Review** — reports | **Diagnose** — reads and concludes | **The fence** — decides |
| --- | --- | --- | --- |
| Reads | a schema, a script, or a statement plus a schema | an instance, read-only, plus the runbook | the statement that is about to run |
| Input may contain writes | yes — it runs nothing | no — one read-only axis | no — read-only only, DDL refused outright |
| Hands back | findings, each with a why and a fix | a report, a conclusion, and a memory of the run | allow or block, and the layer that stopped it |
| Changed by | rules are files under `rules/` | the criteria are notes, cases and procedures; the checks they run on are rules | thresholds are configuration; the decision logic is not |
| Lives in | `checks.py`, `checks_sql.py`, `rules/*.json` | `knowledge.py`, `planner.py`, `agent.py`, `tools.py`, `snapshot.py` | `guard.py` |
| Built today | checks 1–4, S1 and S3, 9 rules | the live read and the schema checks only | three `Toolbox` calls |

A change script therefore never reaches the gate: `guard()` refuses DDL by construction, so the
script goes to the reviewer, the report goes to a person, and the person decides.

The knowledge side and the execution side meet in one place, `Toolbox` in `tools.py`: retrieved
text goes into the answer and no further. A mediocre retrieval score is a quality problem, not a
safety one.

## Changing one thing

| You want to | Change |
| --- | --- |
| Add a review rule | a new file under `rules/`, or `--infer` from a description and two examples. No code change. |
| Add a **fact** a rule can select | `facts.py` — register it on the model, and teach the reader that produces it (`ddl.py` or `snapshot.py`) to fill it in. |
| Add a schema check | `checks.py`, plus a case in `tests/qa_probe.py`. |
| Add a statement check | `checks_sql.py`. It reports, so it does not belong in `guard.py`. |
| Change whether a statement may run | `guard.py`. This is the one thing a rule pack cannot touch. |
| Make every live read pass that gate | `snapshot.py`'s five catalog reads, plus the two defects in `guard.py` that stop two of them passing at all and would silently cut the other three (R13). |
| Let a person hand the gate a statement | a front door on `guard.py` (R15). The decision stays where it is; the front door is a wrapper around it. |
| Add a check over a **result** rather than a statement | a new module beside `checks_sql.py`. It reads a result and the expectation it is judged against, reports, and never decides — the same rule the rest of this layer is held to. The offline half needs no database; the live half would go through the gate. |
| Add a diagnostic pass, or a memory of past ones | a new module. It would read through `snapshot.py` and reuse `checks.py`, and its conclusions would be kept as a store of past runs (R12). |
| Change the answer quality | the corpus, the chunking, `--k`, or the relevance floor — not the code path. |
| Add a fact model (e.g. rules over a statement rather than a schema) | `facts.py`, and a reader that produces those rows. `FACT_MODELS` names what exists; `"sql"` is the model not yet built. |

## The three axes

One switch is not enough to describe how it runs. These are independent:

| axis | default | switched by | if you leave it out |
| --- | --- | --- | --- |
| embedding | hashed lexical vector + corpus IDF | `SF_EMBED_API_KEY` | nothing is downloaded and no SDK is needed |
| store | JSON file under `.schemafence/` | `--db` → pgvector + HNSW | stays a file on disk, still works |
| driver | rule-based planner | `--llm openai` + `SF_LLM_API_KEY` | stays deterministic, still answers |

`--db` says a real database is attached. It says nothing about API keys: `--db` with no key is a
supported configuration, and both walkthroughs in the quickstart run that way.

## Where the AI is, and where the DBA is

The model appears in two places, and neither of them decides. It can drive the tool loop in
`agent.py` when `--llm openai` is set, and it can be handed the exported vocabulary to draft a rule
while a person is authoring one. Everything that decides — the checks, the rule evaluator, the
fence — is a function whose output is reproduced exactly with no model in the room. That split is
not a limitation of this project; it is the project.
