# Roadmap

What is built, what is next, and in what order. The two sections at the end
say what this project deliberately will not do, because that is the part that
keeps it from turning into a general-purpose SQL linter.

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

Five things were asked for that the tables above do not yet cover, and two more
came out of reviewing the code against them. Each one reads and reports. None of
them executes, so none of them changes what this project is.

| # | Asked for | Lands in | Missing today |
| --- | --- | --- | --- |
| **R1** | a table with no primary key is a finding | `checks.py` | `ddl.py` records inline and table-level `PRIMARY KEY` already, for S1. The rule itself is not written. |
| **R2** | one concept, one standard: naming, type, unit, enum | `checks.py`, then tier 4 | Today only check H (`Inconsistent naming for the same concept`, missing comments) and check 1 (near-duplicate names). |
| **R3** | a row count far outside expectation | `check 5`, then tier 2 | `check 5` answers one question and needs a live connection. Tier 2 is its finished form. |
| **R4** | a review rule is a description and its examples, not code | `checks.py`, and the rule file below | Every review rule is a Python function. `ddl.py` already carries the facts three of them need. |
| **R5** | the B-class errors, not only the A class | `checks_sql.py`, then tier 3 | S2, S4, S5, S6, and the counterexample tier. |
| **R6** | a change script is judged by its actions, not by the schema it leaves behind | `ddl.py`, `checks.py` | `ddl.py` reads `CREATE TABLE` and nothing else, so an `ALTER` script parses as zero tables, reports zero findings and exits zero. |
| **R7** | the fence's rule set is configurable | `guard.py`, and the file below | The seven layers are code. |

R3 keeps the read-only rule. `pg_class.reltuples` and `pg_stats` are read; a
live connection is not a write connection, which is why the `--db` axis has
always been independent of the key axes.

R6 carries a requirement that outranks the feature: **when the reader recognises
nothing, it has to say so.** A review that silently returns nothing is worse
than no review, because zero findings reads as a pass. The discipline already
holds on both sides of it. `checks_sql.py` abstains with a note instead of
guessing, and `tests/qa_probe.py` turns a raising selftest into a FAIL line
rather than letting the crash swallow the summary.

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

### R4 — the review rule set

A review rule is a description plus the examples that prove it, in one file. The
rule is a declaration over the parsed schema, not Python:

```yaml
id: column-comment-cites-standard
severity: medium
scope: column
assert:
  comment_matches: "(?i)标准\\s*[:：]\\s*[A-Z]{2,}-\\d+"
message: A column comment has to name the data standard it implements.
fix: Add 标准：<ID> to the column comment.
examples:
  - silent: 'COMMENT ON COLUMN shop.orders.paid_at IS ''支付时间 标准：TS-004'''
  - fires:  'COMMENT ON COLUMN shop.orders.paid_at IS ''支付时间'''
```

The examples are not a prompt for a model. They are the acceptance test: a rule
that cannot reproduce every label in its own file does not get committed, and
the labels are the same bar the checks above are held to, one case that fires
and one near miss that stays silent. That bar is why configurable does not decay
into approximate.

The vocabulary stays closed on purpose: a selector over tables, columns or
constraints, and assertions of presence, type, nullability, pattern, enumeration
and reference. No loops, no user-defined functions, no code in the file. A rule
set that can express anything cannot be reviewed by a person, and a rule set
nobody can read is not evidence.

Authoring may use a model. Execution never does: the model runs while the rule is
being written, its proposal is shown as a diff, and the review path calls only
the deterministic evaluator. This is the separation the two halves already rest
on, one level up, because a rule is where a probabilistic step gets frozen into a
deterministic one.

The facts have to exist before the rules over them do, and most of them already
do. `ddl.py` records primary keys, `UNIQUE` constraints, table comments and
column comments, which is why R1 and R2 are nearer than they look: what is
missing is the rule, not the reader. Change actions are the exception, which is
why R6 comes first.

The five checks that already exist stay Python. The rule file layer is for what
gets added, starting with R1 and R2; moving the old ones across is mechanical and
can wait until the vocabulary stops moving.

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
