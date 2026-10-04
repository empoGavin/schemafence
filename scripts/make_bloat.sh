#!/usr/bin/env bash
# make_bloat.sh — deliberately bloat shop.orders so the live demo has a real
# "why is this slow" story instead of a made-up one.
#
# mode = bloat (default):
#   1. turns autovacuum OFF for shop.orders, so cleanup cannot race the demo
#   2. prints baseline stats + plan
#   3. bulk-inserts ROWS orders            (default 100000)
#   4. runs PASSES full-table UPDATEs      (default 5) — every pass leaves a
#      dead tuple behind: 5 passes x 100k rows = ~500k dead tuples
#   5. deletes every 10th row
#   6. prints after-stats, the plan and a timed full scan
#
# mode = cleanup:
#   re-enables autovacuum, VACUUM (ANALYZE), shows reclaimed stats.
#   Plain VACUUM returns space to the free map but does NOT shrink the file —
#   that needs VACUUM FULL at the price of an ACCESS EXCLUSIVE lock.  That
#   trade-off is exactly what the knowledge notes (pg-vacuum-tuning) discuss.
#
# usage:
#   DSN="postgresql://postgres:pgvec123@localhost:5432/fence_demo"
#   ./scripts/make_bloat.sh "$DSN"            # bloat
#   ROWS=200000 PASSES=8 ./scripts/make_bloat.sh "$DSN"
#   ./scripts/make_bloat.sh "$DSN" cleanup
#
# demo tip: the agent's explain_sql rewrites SELECT * to add LIMIT 10, and a
# LIMIT plan stops after 10 rows, so its cost barely moves.  Ask something
# that forces a full pass (count / sum / group) — or just read the timed
# count(*) this script prints before and after.
set -euo pipefail

DSN="${1:?usage: make_bloat.sh <dsn> [bloat|cleanup]}"
MODE="${2:-bloat}"

case "$MODE" in
  bloat)
    ROWS="${ROWS:-100000}"
    PASSES="${PASSES:-5}"
    psql "$DSN" -v ON_ERROR_STOP=1 <<SQL
\set rows ${ROWS}
\set passes ${PASSES}
\echo '== 1) autovacuum off for shop.orders (it must not clean up mid-demo) =='
ALTER TABLE shop.orders SET (autovacuum_enabled = off);

\echo '== 2) baseline: live/dead tuples, heap size, plan =='
SELECT n_live_tup, n_dead_tup,
       pg_size_pretty(pg_relation_size('shop.orders')) AS heap_size
  FROM pg_stat_user_tables
 WHERE schemaname = 'shop' AND relname = 'orders';
EXPLAIN (COSTS ON) SELECT * FROM shop.orders;
\timing on
SELECT count(*), round(sum(amount)::numeric, 2) FROM shop.orders;
\timing off

\echo '== 3) bulk insert' :rows 'rows =='
INSERT INTO shop.orders (user_id, status, amount, currency, create_time)
SELECT g % 1000,
       CASE WHEN g % 3 = 0 THEN 30 ELSE 20 END,
       round((g % 500)::numeric, 2)::double precision,
       'CNY',
       now() - (g || ' minutes')::interval
  FROM generate_series(1, :rows) AS g;

\echo '== 4) churn:' :passes 'full-table UPDATE passes -> dead tuples =='
DO \$\$
BEGIN
  FOR i IN 1..:passes LOOP
    UPDATE shop.orders
       SET status = CASE WHEN status = 20 THEN 30 ELSE 20 END;
    RAISE NOTICE 'churn pass % done', i;
  END LOOP;
END
\$\$;

\echo '== 5) delete every 10th row =='
DELETE FROM shop.orders WHERE id % 10 = 0;

\echo '== 6) after: watch n_dead_tup, heap_size, plan cost, scan time =='
SELECT n_live_tup, n_dead_tup, n_tup_upd,
       pg_size_pretty(pg_relation_size('shop.orders')) AS heap_size
  FROM pg_stat_user_tables
 WHERE schemaname = 'shop' AND relname = 'orders';
EXPLAIN (COSTS ON) SELECT count(*) FROM shop.orders;
\timing on
SELECT count(*), round(sum(amount)::numeric, 2) FROM shop.orders;
\timing off

\echo
\echo 'Now ask the agent again (use a full-pass question so the plan shows it):'
\echo "  python agent_cli.py --llm openai --db ${DSN} --ask \"统计一下 orders 表的订单总数和总金额\""
\echo 'When done:  ./scripts/make_bloat.sh '"${DSN}"' cleanup'
SQL
    ;;
  cleanup)
    psql "$DSN" -v ON_ERROR_STOP=1 <<'SQL'
\echo '== re-enable autovacuum + VACUUM (ANALYZE) =='
ALTER TABLE shop.orders SET (autovacuum_enabled = on);
VACUUM (ANALYZE) shop.orders;
SELECT n_live_tup, n_dead_tup,
       pg_size_pretty(pg_relation_size('shop.orders')) AS heap_size
  FROM pg_stat_user_tables
 WHERE schemaname = 'shop' AND relname = 'orders';
\echo
\echo 'note: plain VACUUM reclaims dead tuples but the file does not shrink.'
\echo '      VACUUM FULL shop.orders; would shrink it — at the price of an'
\echo '      ACCESS EXCLUSIVE lock on the table.  That trade-off is what the'
\echo '      knowledge notes (pg-vacuum-tuning) are about.'
SQL
    ;;
  *)
    echo "unknown mode: $MODE (use bloat or cleanup)" >&2
    exit 2
    ;;
esac
