# Eval questions — PostgreSQL 16 official manual (generated corpus)

The corpus under `examples/knowledge-pg-official/` is **English**, extracted from
the official manual, so these questions are asked in English too. That is not a
style choice: the offline hashed-lexical embedder matches on shared tokens, and
a Chinese question shares none with an English passage.

- Corpus: `examples/knowledge-pg-official/` (98 chapters, 8 Part subdirectories)
- Answers are keyed to the **file stem**, e.g. `pg16-chapter-19-server-setup-and-operation`
- Usage:
  ```bash
  python agent_cli.py --corpus examples/knowledge-pg-official \
                      --store .schemafence/pg-official.json \
                      --ingest examples/knowledge-pg-official \
                      --eval --eval-file eval/questions-pg-official.md
  ```
- Judgement: a question counts as a hit if any of the top-k passages comes from
  the expected file. Two adjacent chapters that both answer the question are
  written as `a / b`.

| # | Question | Expected source |
|---|----------|-----------------|
| 1 | What index types does PostgreSQL support? | pg16-chapter-11-indexes |
| 2 | Which multicolumn and partial indexes should I use? | pg16-chapter-11-indexes |
| 3 | What are the transaction isolation levels and what anomalies do they allow? | pg16-chapter-13-concurrency-control |
| 4 | How does explicit locking work and what are the table lock modes? | pg16-chapter-13-concurrency-control |
| 5 | How do I analyze a query's performance with EXPLAIN? | pg16-chapter-14-performance-tips |
| 6 | When can parallel query be used? | pg16-chapter-15-parallel-query |
| 7 | What configuration parameters control resource consumption? | pg16-chapter-20-server-configuration |
| 8 | How do I configure error reporting and logging? | pg16-chapter-20-server-configuration |
| 9 | Which settings control the write-ahead log? | pg16-chapter-20-server-configuration / pg16-chapter-30-reliability-and-the-write-ahead-log |
| 10 | Which authentication methods can be configured in pg_hba.conf? | pg16-chapter-21-client-authentication |
| 11 | How do database roles and role attributes work? | pg16-chapter-22-database-roles |
| 12 | How do I create and manage a database? | pg16-chapter-23-managing-databases |
| 13 | What routine database maintenance tasks are needed? | pg16-chapter-25-routine-database-maintenance-tasks |
| 14 | What is the difference between pg_dump and a file system level backup? | pg16-chapter-26-backup-and-restore |
| 15 | How do I set up a streaming replication standby server? | pg16-chapter-27-high-availability-load-balancing-and-replication |
| 16 | What options exist for high availability and load balancing? | pg16-chapter-27-high-availability-load-balancing-and-replication |
| 17 | How do I monitor database activity with the statistics views? | pg16-chapter-28-monitoring-database-activity |
| 18 | How can I view locks currently held? | pg16-chapter-28-monitoring-database-activity / pg16-chapter-13-concurrency-control |
| 19 | How do I determine disk usage of the database? | pg16-chapter-29-monitoring-disk-usage |
| 20 | What happens when the disk becomes full? | pg16-chapter-29-monitoring-disk-usage |
| 21 | How does write-ahead logging guarantee reliability? | pg16-chapter-30-reliability-and-the-write-ahead-log |
| 22 | What is the difference between logical and physical replication? | pg16-chapter-31-logical-replication / pg16-chapter-27-high-availability-load-balancing-and-replication |
| 23 | When should JIT compilation be used? | pg16-chapter-32-just-in-time-compilation-jit |
| 24 | What is the path of a query through the system? | pg16-chapter-52-overview-of-postgresql-internals |
| 25 | How does PostgreSQL process a transaction? | pg16-chapter-74-transaction-processing |
| 26 | Which system catalogs does PostgreSQL provide? | pg16-chapter-53-system-catalogs |
| 27 | Which system views are available for administration? | pg16-chapter-54-system-views |
| 28 | What is genetic query optimization and when is it triggered? | pg16-chapter-62-genetic-query-optimizer |
| 29 | How does the planner use statistics to estimate rows? | pg16-chapter-76-how-the-planner-uses-statistics |
| 30 | How are B-tree indexes implemented internally? | pg16-chapter-67-b-tree-indexes |
| 31 | What are GIN indexes used for? | pg16-chapter-70-gin-indexes |
| 32 | When is a BRIN index useful? | pg16-chapter-71-brin-indexes |
| 33 | How is a table's data physically stored on disk? | pg16-chapter-73-database-physical-storage |
| 34 | What are the limits of PostgreSQL such as maximum table size? | pg16-appendix-k-postgresql-limits |
| 35 | How are SQLSTATE error codes structured? | pg16-appendix-a-postgresql-error-codes |
| 36 | What does the CREATE INDEX command accept? | pg16-sql-commands |
| 37 | What command line options does pg_basebackup take? | pg16-postgresql-server-applications |
| 38 | Which server applications ship with PostgreSQL? | pg16-postgresql-server-applications |
| 39 | How do I install PostgreSQL from source code? | pg16-chapter-17-installation-from-source-code |
| 40 | How do I shut down and start the database server? | pg16-chapter-19-server-setup-and-operation |

## Notes

Two things these questions deliberately cover:

- **The corpus is a manual, not a runbook.** It answers "what is the parameter
  called / what does the view contain", not "my standby is lagging, what now".
  That second kind of question is what `eval/questions-pg.md` (the hand-written
  corpus) measures. Keeping the two rulers separate is the point: they measure
  two different corpora, and mixing them into one store dilutes IDF for both.
- **Ingesting the whole manual is not required.** The output is split by Part,
  so `--ingest examples/knowledge-pg-official/part-iii-server-administration`
  searches only the server administration chapters. Narrower corpora make IDF
  more discriminating; use the whole tree when you need coverage.
