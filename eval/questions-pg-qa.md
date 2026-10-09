# Eval questions — coverage ("how do I write X"), not diagnosis

These are questions the **diagnostic** corpus cannot answer at all. The
hand-written pack (`examples/knowledge-pg/`) is 31 runbooks about *what to do
when something is wrong* — it has no `ON CONFLICT`, no window-function syntax,
no `tsquery`. So these questions measure the other half of the job: **coverage**.

That is the trade-off the merged corpus is meant to buy. Run this file against
three corpora and the shape of the trade-off becomes visible:

- `examples/knowledge-pg/` — expect 0/16: the diagnosis pack has no syntactical answers.
- `examples/knowledge-pg-official/` — expect high: the manual is what answers these.
- `examples/knowledge-merged/` — the question: does adding 31 runbooks cost anything here?

**Parser constraint (applies to every file in this directory)**: `load_questions`
collects *any* line starting with `|` that has three or more cells, skipping only
a header row whose first cell is literally `#`. So an explanatory table placed
above the question table is silently counted as extra questions and inflates the
denominator. Use bullet lists for prose, and keep the only table in the file the
one that holds questions.

- Answers are keyed to the **file stem** of the English corpus, e.g.
  `pg16-chapter-6-data-manipulation`.
- **Language caveat**: these are asked in English on purpose. The offline
  hashed-lexical embedder matches shared tokens, and a Chinese question shares
  none with an English passage. Offline, you must ask in the corpus's language;
  with `--mode api` (real embeddings) Chinese questions work against English
  text. This is a property of the embedder, not of the corpus.
- Usage:
  ```bash
  python agent_cli.py --corpus examples/knowledge-merged \
                      --store .schemafence/merged.json \
                      --eval --eval-file eval/questions-pg-qa.md --k 20
  ```

| # | Question | Expected source |
| --- | --- | --- |
| 1 | How do I write an INSERT ... ON CONFLICT that updates the row instead of failing? | pg16-chapter-6-data-manipulation |
| 2 | How do I write a recursive WITH query (recursive CTE)? | pg16-chapter-7-queries |
| 3 | How do I write a LATERAL subquery in the FROM clause? | pg16-chapter-7-queries |
| 4 | How do I define an identity column with GENERATED ALWAYS AS IDENTITY? | pg16-chapter-5-data-definition |
| 5 | How do I create a foreign key constraint, and what does ON DELETE CASCADE do? | pg16-chapter-5-data-definition |
| 6 | Which operators and functions can query jsonb values (-> ->> jsonb_path_query)? | pg16-chapter-9-functions-and-operators |
| 7 | How do I create a partial index and an expression index? | pg16-chapter-11-indexes |
| 8 | How do I create a covering index with INCLUDE so a query can use an index-only scan? | pg16-chapter-11-indexes |
| 9 | How does full-text search work — tsvector, tsquery, ts_rank? | pg16-chapter-12-full-text-search |
| 10 | How do I write a window function call using the OVER clause? | pg16-chapter-4-sql-syntax |
| 11 | How do I read EXPLAIN ANALYZE output, and what do the cost estimates mean? | pg16-chapter-14-performance-tips |
| 12 | Which configuration settings control parallel query? | pg16-chapter-15-parallel-query |
| 13 | How do I write a PL/pgSQL function that contains a loop? | pg16-chapter-43-pl-pgsql-sql-procedural-language |
| 14 | How do I create a trigger together with its trigger function? | pg16-chapter-39-triggers |
| 15 | What built-in data types exist for UUID, arrays and ranges? | pg16-chapter-8-data-types |
| 16 | How do I write a CREATE TABLE with column defaults and constraints? | pg16-chapter-5-data-definition |
