# Eval questions — PostgreSQL 14 Internals book (generated corpus)

The corpus under `examples/knowledge-pg-internals/` is **English** (the book's
own text), so these questions are asked in English too — the offline
hashed-lexical embedder matches on shared tokens, and a Chinese question
shares none with an English passage.

- Corpus: `examples/knowledge-pg-internals/` (13 chapters, 3 Part subdirectories)
- Source: *PostgreSQL 14 Internals*, Parts I & II, Egor Rogov,
  Postgres Professional 2022 — repaginated by `scripts/convert_pg_internals.py`
- Answers are keyed to the **file stem**, e.g. `pgi-isolation`
- Usage (`--ingest` 与 `--eval` 必须分两条命令):
  ```bash
  python agent_cli.py --corpus examples/knowledge-pg-internals \
                      --store .schemafence/pg-internals.json \
                      --ingest examples/knowledge-pg-internals
  python agent_cli.py --corpus examples/knowledge-pg-internals \
                      --store .schemafence/pg-internals.json \
                      --eval --eval-file eval/questions-pg-internals.md
  ```
- Judgement: a question counts as a hit if any of the top-k passages comes
  from the expected file. Two chapters that both answer it are written `a / b`.

| # | Question | Expected source |
|---|----------|-----------------|
| 1 | Which transaction isolation levels does PostgreSQL support and what anomalies does each allow? | pgi-isolation |
| 2 | What is a lost update and at which isolation level can it still happen? | pgi-isolation |
| 3 | How does Serializable isolation in PostgreSQL detect dangerous structures between transactions? | pgi-isolation |
| 4 | What do xmin and xmax in a tuple header mean and how do they decide visibility? | pgi-pages-and-tuples |
| 5 | How is a heap page laid out — page header, item pointers, tuples, special space? | pgi-pages-and-tuples |
| 6 | What happens to the old row version when a row is updated? | pgi-pages-and-tuples |
| 7 | Where does PostgreSQL keep the commit status of transactions (clog / pg_xact)? | pgi-pages-and-tuples |
| 8 | What is a virtual XID and why do read-only transactions get one? | pgi-pages-and-tuples |
| 9 | What is a snapshot and when is it taken under Read Committed versus Repeatable Read? | pgi-snapshots |
| 10 | What do the xmin, xmax and xip lists of a snapshot mean? | pgi-snapshots |
| 11 | Why can two snapshots taken in one transaction see different data? | pgi-snapshots |
| 12 | What is page pruning and which operations can trigger it? | pgi-page-pruning-and-hot-updates |
| 13 | What makes an update HOT and why does HOT avoid index updates? | pgi-page-pruning-and-hot-updates |
| 14 | What is the fillfactor trade-off on a frequently updated table? | pgi-page-pruning-and-hot-updates |
| 15 | How does autovacuum compute when a table needs vacuuming? | pgi-vacuum-and-autovacuum |
| 16 | What is the difference between VACUUM and VACUUM FULL? | pgi-vacuum-and-autovacuum / pgi-rebuilding-tables-and-indexes |
| 17 | What does the visibility map do and who maintains it? | pgi-vacuum-and-autovacuum / pgi-page-pruning-and-hot-updates |
| 18 | Why is freezing needed before transaction ID wraparound happens? | pgi-freezing |
| 19 | How do hint bits relate to freezing and why were hint bits introduced? | pgi-freezing / pgi-pages-and-tuples |
| 20 | How do VACUUM FULL, CLUSTER and REINDEX rebuild a table and its indexes? | pgi-rebuilding-tables-and-indexes |
| 21 | How is the buffer cache organized and how is a victim buffer chosen? | pgi-buffer-cache |
| 22 | What is the clock-sweep algorithm and what does buffer usage count mean? | pgi-buffer-cache |
| 23 | Why does a full scan use a small ring of buffers instead of the whole cache? | pgi-buffer-cache |
| 24 | Which background processes does the server run and what does each do? | pgi-processes-and-memory |
| 25 | What is the postmaster's role and how is shared memory allocated for the server processes? | pgi-processes-and-memory |
| 26 | How does a table map to files and forks on disk, and how big can one file be? | pgi-data-organization |
| 27 | What is TOAST and when does a value get stored out of line? | pgi-data-organization |
| 28 | What is the difference between a database, a schema and a tablespace? | pgi-data-organization |
| 29 | What message flow does the client-server protocol use to start a transaction? | pgi-clients-and-the-client-server-protocol |
| 30 | Why is the write-ahead log necessary for crash consistency? | pgi-write-ahead-log |
| 31 | What is an LSN and what is it used for? | pgi-write-ahead-log |
| 32 | What happens during a checkpoint and what does checkpoint_completion_target control? | pgi-write-ahead-log |
| 33 | Which wal_level values exist and what does each enable? | pgi-wal-modes |
| 34 | Why is full_page_writes needed and what does it cost? | pgi-wal-modes / pgi-write-ahead-log |
| 35 | How does wal_compression or unlogged logging trade performance for durability? | pgi-wal-modes |
