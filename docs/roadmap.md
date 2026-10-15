# Roadmap

What is built, what is next, and in what order. Read it as the DBA work it lets
somebody hand over: a change script read before the window opens, a query read
for the number it will get wrong, a question answered from the runbook. The two
sections at the end say what this project deliberately will not do, because that
is the part that keeps it from turning into a general-purpose SQL linter.

## The gap this roadmap fills

`checks.py` reads a schema and never sees a statement. `guard.py` reads a
statement and never sees a schema. Each is right about what it looks at,
which is why neither can see this class of mistake:

| What reads | What it eats | What it can see |
| --- | --- | --- |
| `checks.py` | the DDL only | structural defects. No statement at all. |
| `guard.py` | the SQL text only | statement shape, keywords, functions, table names, LIMIT. No semantics. |
| `checks_sql.py` | **both** | errors that live where the two meet |

A query can be valid, the schema can be valid, and the number that comes back
can still be wrong. `LEFT JOIN order_items` is fine. `SUM(amount)` is fine.
`SUM(amount)` after that join is not, and nothing in the database says so.

## S1–S6: the checks, and where they stand

The two marked done live in `schemafence/checks_sql.py`. All of them are
deterministic functions of `(Schema, SQL)`, and none of them needs a database,
a model, or a network.

| # | Check | What it catches | Status |
| --- | --- | --- | --- |
| **S1** | fan-out | a measure summed after a one-to-many join has multiplied. Three line items turn one 100 into 300. | done |
| **S3** | `LEFT JOIN` cancelled by `WHERE` | a condition on the right table in `WHERE` drops every unmatched row, so the outer join behaves like an inner one. | done |
| S2 | `SUM(DISTINCT …)` | do not reach for `DISTINCT` to undo a fan-out: two legitimate 100s collapse into one 100. | next |
| S4 | time boundaries | `BETWEEN '2026-01-01' AND '2026-01-31'`, and `<= '… 23:59:59'`, both take in the wrong interval. | next |
| S5 | mixed time columns | `created_at` where the definition means `paid_at`. Two orders move in or out of the month. | planned |
| S6 | mixed aggregation level | order-level and line-level columns summed together without converging the grain first. | planned |

Two rules hold for every check added here:

1. **A structure the reader cannot resolve produces a note, never a finding.**
   An unparsed join, a column that belongs to two tables, a nested query — all
   of them abstain and say so. A check that guesses is worse than a check that
   abstains, because the whole point of this half is that its output can be
   trusted without reading it.
2. **Nothing here decides whether a statement runs.** These checks report.
   `guard()` is still the only thing that allows or blocks.

The fan-out check reads uniqueness off the schema: a join key that is a primary
key or carries a `UNIQUE` constraint joins one row to one row and cannot
multiply. That is why `ddl.py` records inline `PRIMARY KEY` and `UNIQUE`
declarations, not just table-level ones.

## The four tiers above it

Ordered by what can be pinned down without a database, which is also the order
in which they become testable.

### 1. Static checks — done, extending

`checks_sql.py`, above. Extending it means adding S2, S4, S5, S6 at the same
quality bar: one selftest case that fires, and one near miss that must stay
silent.

### 2. Step-wise reconciliation — needs a database

Split a candidate query into its CTE chain and execute it step by step, printing
three columns per step: rows, distinct target keys, and measure total. A number
that moves between steps is where the query went wrong, and it is a far shorter
search than reading the SQL.

This is `check 5` in its finished form. Today `check 5` only asks whether a row
count is wildly outside expectation; this asks whether rows and money are
conserved at every step.

Values are all generated. Nothing here reads a real customer table.

### 3. Counterexample runs — the highest-value tier

Turn "does the answer change when the business numbers did not" into code. The
criterion is invariance, not equality against a known good value:

| Change on the data side | Expected | If it fails, the query was |
| --- | --- | --- |
| add one more line item to an order, same total | unchanged | double counting the join |
| add one legitimate order | moves by exactly that order | dropping rows via `DISTINCT` |
| split one 30 refund into 10 + 20 | unchanged | deduplicating by amount |
| add a refund dated after the cut-off | unchanged | wrong boundary |
| add a refund that is still pending | unchanged | not filtering on status |

This is metamorphic testing: it needs no hand-derived expected value, only a
before and an after. Every change runs inside a transaction that is rolled back,
so the read-only constraint is not broken — the verification gets the same
safety boundary as the queries it verifies.

### 4. A metric definition layer — the most expensive, and the most valuable

Everything above checks the query. This one checks the definition the query is
supposed to be implementing, because most of these bugs are a definition
disagreement that no query can settle:

```yaml
net_sales:
  grain: order
  population: paid_at in [month_start, next_month_start)
  measure: SUM(paid_amount)
  deduct: refunds where status = 'SUCCESS' and refunded_at < next_month_start
  exclude: [pending, failed]
  timezone: Asia/Shanghai
  boundary: half-open
```

Compiled against a query, that file can reject `created_at` where it says
`paid_at`, `BETWEEN` where it says half-open, and `SUM(DISTINCT …)` where it
says `SUM`. It is also the only part of this that would version and regression
test a definition when it changes.

## Requested, and where each one lands

Six things were asked for that the tables above do not yet cover, and three more
came out of reviewing the code against them. Each one reads and reports. None of
them executes, so none of them changes what this project is.

| # | Asked for | Lands in | State |
| --- | --- | --- | --- |
| **R1** | a table with no primary key is a finding | a rule pack | **done** — `rules/table-hygiene.json`, rule `table-must-have-primary-key` |
| **R2** | one concept, one standard: naming, type, unit, enum | a rule pack, then tier 4 | partly. The comment rules shipped; naming, unit and enum still need rules, and today only check H and check 1 read naming. |
| **R3** | a row count far outside expectation | `check 5`, then tier 2 | `check 5` answers one question and needs a live connection. Tier 2 is its finished form. |
| **R4** | a review rule is a description and its examples, not code | `schemafence/rulepack.py`, `schemafence/facts.py`, `rules/*.json` | **done** — the fact registry, the vocabulary, `--infer` and the overlap checks are all below |
| **R5** | the B-class errors, not only the A class | `checks_sql.py`, then tier 3 | S2, S4, S5, S6, and the counterexample tier. |
| **R6** | a change script is judged by its actions, not by the schema it leaves behind | `ddl.py`, rule packs | **done** — `ALTER`, `DROP`, `CREATE INDEX`, `COMMENT ON`, DML and session statements are read as actions; every statement no reader claimed is listed by position; a change kind the vocabulary does not declare is reported rather than silently produced; and a file that yields nothing at all exits 2. The same pass collapsed the four spellings of one primary key into one fact, which is what let the rule cover all of them |
| **R7** | the fence's rule set is configurable | `guard.py` | The seven layers are code. |
| **R8** | a rule over the statement, not only over the schema | a SQL fact model | `checks_sql.py` has the facts it needs (the FROM chain, join keys, uniqueness) but does not expose them as rows, so every rule written so far reads DDL. |
| **R9** | a rule that needs what the database already holds | a `catalog` fact model, `pg_index` | **done** — `rules/index-hygiene.json` refuses a script and asks for `--db`; the row shape is held identical to the script reader's by `tests/qa_probe.py` M1. The query itself has not been run against a server yet. |
| **R10** | the schema audit says when it read nothing, not only the reviewer | `demo.py`, `checks.py` | not done — checks 1 to H are functions of a schema, so a file with no tables in it yields `0 finding(s)` and exit 0 with nothing said. A script holding only `\copy … from 'x.csv'` and `VACUUM FULL;` reproduces it, and the same file through `python -m schemafence.review` exits 2. |

R3 keeps the read-only rule. `pg_class.reltuples` and `pg_stats` are read; a
live connection is not a write connection, which is why the `--db` axis has
always been independent of the key axes.

R6 carries a requirement that outranks the feature: **when the reader recognises
nothing, it has to say so.** A review that silently returns nothing is worse
than no review, because zero findings reads as a pass. The discipline already
holds on both sides of it. `checks_sql.py` abstains with a note instead of
guessing, and `tests/qa_probe.py` turns a raising selftest into a FAIL line
rather than letting the crash swallow the summary.

It holds in the reviewer, and it does not hold in the schema audit yet, which is
R10. Checks 1 to H are functions of a schema; a script with no tables in it is a
file they have nothing to say about, and "nothing to say about it" has to be a
different sentence from "clean".

## A rule set has to be a file

R4 and R7 are the two requests with a constraint attached, because being
deterministic is the whole point of that half of the project. They need
different answers, and they share one rule: **what is configurable is an
artefact that is committed**, not a value that is looked up.

### R7 — the fence's rule set

| | |
| --- | --- |
| May be configured | the table whitelist, the row cap behind `LIMIT 100`, the forbidden function list, the allowed schemas |
| Configured with care | switches for the seven layers themselves. Turning off L3 is the same as allowing a write inside a CTE. |
| Never configured | the decision logic |

Every load writes the rule set it applied into the audit record. That is what
makes "who changed the fence" answerable.

The rule set may not be fetched from a database or a service. A fence that
depends on a row somebody can update is not a fence.

### R4 — the review rule set, as built

A rule is a declaration over facts the engine already reads. Nothing in the file
executes. There are four targets with a closed field list each, and this is the
whole model:

**a rule selects rows, and says what those rows must satisfy. The findings are
the selected rows that fail.**

| Target | Fields |
| --- | --- |
| `table` | name, schema, qualified, comment, column_count, primary_key_columns, has_primary_key, unique_key_count, index_count, commented_columns, uncommented_columns, created_by_change, touched_by_change, change_count |
| `column` | name, table, qualified, type, base_type, nullable, has_default, default, references, comment, is_primary, is_unique, looks_like_key, looks_like_money, looks_like_time, added_by_change, added_by |
| `index` | name, table, qualified, columns, columns_text, column_count, unique, primary, from_constraint, method, expression, partial, predicate, comparable, created_here, duplicate_count, duplicate_of |
| `change` | kind, table, column, subject, detail, statement, position |

Selectors, all ANDed: `field` (equals, and `null` means is null), `_in`,
`_not_in`, `_matches`, `_not_matches`, `_matches_all`, `_present`, `_absent`.
Assertions: `present`, `absent`, `equals`, `not_equals`, `in`, `not_in`,
`matches`, `not_matches`, `matches_all`, `matches_none`, `gte`, `lte`. A rule
over change actions takes `forbid` instead of an assertion — every selected row
is a finding, which is how "this script drops a table" is written.

`title`, `detail` and `fix` may name a field of the failing row as `{field}`,
which is how a finding can say *which* other index it collides with.

```json
{
  "id": "column-comment-cites-standard",
  "severity": "medium",
  "target": "column",
  "title": "Column comment does not cite the data standard",
  "detail": "A comment that cites a standard can be checked against it.",
  "fix": "Add 标准：<ID> to the column comment.",
  "select": { "comment_present": true },
  "assert": [{ "field": "comment", "op": "matches",
               "value": "(?i)(标准|standard)\\s*[:：]\\s*[A-Z]{2,6}-\\d{1,4}" }],
  "examples": {
    "fires": ["CREATE TABLE t (a TIMESTAMPTZ); COMMENT ON COLUMN t.a IS '支付时间';"],
    "silent": ["CREATE TABLE t (a TIMESTAMPTZ); COMMENT ON COLUMN t.a IS '支付时间 标准:TS-004';"]
  }
}
```

**The examples are not a prompt for a model. They are the acceptance test.** A
rule that cannot reproduce every label in its own file is refused at load, by
name, and is not applied — the same bar the hand-written checks are held to, one
case that fires and one near miss that stays silent. The three packs in `rules/`
ship with 29 examples between them, and any file is refused if one is wrong.

The authoring loop, which is the reason "immediately usable" holds at all:

```bash
python -m schemafence.review --vocabulary > vocabulary.json   # the closed vocabulary
# hand that file, the rule in words, and two examples to a model
python -m schemafence.review --rules your-pack.json --verify  # refuses it if a label is wrong
python -m schemafence.review --rules your-pack.json --ddl colleague.sql
```

The model runs while the rule is being written and never afterwards: the review
path calls only the evaluator. That is the separation the two halves already rest
on, one level up, because a rule is where a probabilistic step gets frozen into a
deterministic one. `--vocabulary` is printed by the module that evaluates, so the
instructions and the evaluator cannot drift apart.

**The file is JSON, not YAML**, and the reason is the constraint above: this repo
installs nothing, and JSON is the one strict format in the standard library.
YAML's implicit typing (`no` becomes False, `1.0` becomes a float, a tab breaks
the file) is a poor fit for something that decides pass or fail.

Four rules keep the vocabulary from decaying into approximate:

1. **A rule may not need a fact the engine does not already read.** The day it
   does, the change is in the reader, not in the file — and that stays visible,
   because the field name is refused at load instead of quietly never matching.
   R8 is that boundary: a rule over the statement waits for an SQL fact model.
2. **A rule that matched nothing says so, by name.** No matched rows means the
   rule asserted nothing, which is not the same as a pass. The report prints
   matched and fired per rule so the difference stays visible.
3. **A script no reader understood exits 2, not 0.** `NOTHING RECOGNISED` with no
   findings is not a clean review, and that exit code is why a pipeline cannot
   read it as one.
4. **An action list is still not an execution path.** A rule that forbids
   `drop_column` reports it. Nothing in this layer runs a script.

What changed between the first draft of this section and the built version is
worth recording. The format moved from YAML to JSON. The fact list grew a third
target once `ddl.py` learned to read change actions. And both rules that need a
whole table were narrowed to `created_by_change: true` — the instructive one. An
`ALTER`-only script names tables whose definitions the reader has never seen, so
a check for a primary key fires on a table whose key simply is not in the file,
and the finding is about the script rather than about the schema. The fact had to
exist first; the report now names the tables it can only see in part instead of
guessing about them.

The five checks that already exist stay Python. The rule file layer is for what
gets added — moving the old ones across is mechanical and can wait until the
vocabulary stops moving.

### What can be configured, and what cannot

Three files, three different answers, because the three of them do different things.

| Where | Reports, or decides | A rule file may change | A rule file may never change |
| --- | --- | --- | --- |
| `checks.py` | reports | any rule over the schema | the facts it reads |
| `checks_sql.py` | reports | nothing yet — R8 | the facts it reads |
| `guard.py` | **decides** | the table whitelist, the row cap, the forbidden functions | the seven layers |

The gap between the first two rows and the third is not one of effort. A reviewer
configured too loosely reports less; a fence configured too loosely lets a write
through. So R7 is parameters rather than rules, and it shares this layer's loader
without sharing its evaluator — the same shape as the two halves of the project,
one level down.

### The fact layer is a registry

`rulepack.py` does not know what DDL is. It asks `schemafence/facts.py` for rows
with named fields, and a provider declares three things: the fields it offers, the
fact models it can be read from, and how to turn one into rows. Four providers are
registered — `table`, `column`, `index`, `change` — and R8 is a fifth.

This is the whole reason a rule can be added without touching the engine, and it is
also why the vocabulary cannot drift from the evaluator: `--vocabulary` and
`--facts` are both built from the registry that applies the rules.

### Where the facts come from, and what may not

A fact has one shape and three ways to arrive. That is the answer to "add a rule
about primary keys and it should find every way somebody writes one":

| Source | Supplies | Why it is safe |
| --- | --- | --- |
| the script under review | tables, columns, keys, indexes, change actions | a parser, deterministic and offline |
| the live catalog | the same fields, read from `pg_catalog` | a read; the `--db` axis has never been the write axis |
| the corpus | **the list of spellings**, as test cases | documentation read once by a person, then compiled into `tests/qa_probe.py` |

The third row is the one worth stating plainly. Retrieval is probabilistic and the
review layer is not allowed to be, so the manual is not consulted at review time: it
is where the list of spellings came from, and the list became cases the reader has
to pass. Four spellings of a primary key, five of an index, and the assembler's own
question — "is this index already there?" — answered from the catalog, because a
script cannot know what the table holds.

Which means `applies_to` is enforced rather than documented. A pack that declares
`["catalog"]` and is handed a script is **not applied**: the run says so, says which
command would supply the facts, and exits 2. A rule that could not be evaluated has
not cleared anything, and that has to be visible in the same place a pass is.

### Inference, and what it refuses to do

`--infer` enumerates the closed vocabulary, keeps what fires on every `fires`
example and stays silent on every `silent` one, and reports the near misses when
nothing fits. The evaluator used backwards, offline, with no model in the loop.

It does not synthesise a value it was not given. `present` and `absent` need none,
numeric bounds come from the examples themselves, and a pattern appears only if the
request supplied it under `hints.matches`. A regex invented from two samples would
be a guess wearing a rule's clothing, and this layer exists so that a person can
read a rule and check it.

What it is good for is the case that started this: a requirement that can be
written several ways. Hand it four spellings of one primary key and it comes back
with `has_primary_key present` — but only because the reader had already collapsed
them into one fact. So the search is also a test of the fact layer. Examples the
vocabulary cannot separate are facts the reader does not collect yet, which is why
that case exits 2 and names the near misses instead of proposing a rule that would
never fire.

### Overlap, so a pack does not grow by accident

A pack grows by copying a rule and editing it, so the loader looks for what the copy
did not change. One signature in two rules is refused outright. A rule that sits
inside a wider one with the same assertion is reported as adding wording only. Two
rules that select the same rows and disagree are reported as contradicting. Across
packs, the same rule id in two of them is reported too, because a finding carries
the id and nothing else — two rules behind one id cannot be told apart in the
output.

## Prior art

The four error patterns in S1–S4 are a well-known class, and they were named
before this repository existed. 何志华, *AI 写的 SQL 能跑通，为什么结果还是错的？*
(2026-10-09), sets out the same failure from the semantics side: the query
executes, the number looks reasonable, and nothing raises an error. That article
stops at a process — confirm the definition, reconcile intermediate results,
test with counterexamples. This repository turns the parts of that process that
can be made deterministic into checks that run offline, with no key and no
database.

No data or SQL text from that article is used here. The sample schema in
`checks_sql.py` was written for this repository; what is shared is the shape of
the mistake, which is industry-wide.

## What this will not do

- **No write path.** Executing a change is not a deeper version of this project,
  it is the failure the fence exists to prevent. Reviewing a change that a human
  wrote is in scope, and that is `checks.py` and `checks_sql.py`.
- **Not a general SQL linter.** What separates this from one is determinism, the
  separation between the two halves, and evidence that can be checked. Six accurate
  checks beat fifty approximate ones.
- **No model in the semantic path.** The moment a check needs a model to decide
  whether a `SUM` is right, it inherits the failure mode it was built to catch.
- **No third-party dependency on the main path.** `python demo.py` has to work
  on a fresh clone with nothing installed. Anything that needs a parser library
  belongs behind a flag, not in the default path.
- **No Oracle migration diff yet.** The README says the same in one line: it is not
  built, and it is a different story from this one. It happens after the tiers above
  are solid.
