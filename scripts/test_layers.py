#!/usr/bin/env python
"""Layer-by-layer tests: the seven static checks and the seven runtime gates.

Two different sevens live in this project and they are easy to confuse:

  checks.py  seven findings about a *schema*   (1 schema resolution, 2 join
             keys, 3 types, 4 NULL semantics, 5 result plausibility, H hygiene)
             — advice.  A wrong finding costs a reader five minutes.

  guard.py   seven gates in front of a *statement*  (L1 single statement,
             L2 read-only shape, L3 keywords, L4 functions, L5 whitelist,
             L6 forced limit, L7 audit + session hardening)
             — enforcement.  A wrong decision costs data.

Both are tested here, each layer with a positive case (must pass through) and
negative cases (must be stopped, at the layer that owns the rule, with a
reason a human can act on).  Layer attribution matters: a DROP stopped at L2
because it does not start with SELECT is not the same bug as a DROP stopped
at L3 because of the keyword list, and only the second one fires inside a
CTE.

Two of the guard's layers cannot be a string check and are enforced by the
database instead (a SELECT-only role, statement_timeout).  `--show-env`
prints the SQL and psql commands that verify them; `--db` runs the live part.

    python scripts/test_layers.py                     # everything offline
    python scripts/test_layers.py --suite guard
    python scripts/test_layers.py --suite checks --db postgresql://...
    python scripts/test_layers.py --show-env          # env prep + live cmds
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from bench_util import md_table, write_json  # noqa: E402
from schemafence.checks import analyze  # noqa: E402
from schemafence.ddl import parse_ddl  # noqa: E402
from schemafence.guard import guard  # noqa: E402
from schemafence.tools import Toolbox  # noqa: E402

# --------------------------------------------------------------------------- #
# the guard: seven runtime layers
# --------------------------------------------------------------------------- #

GUARD_CASES: list[dict] = [
    # ---- L1: one statement per call
    dict(id="L1-1", layer="L1", name="empty statement is refused",
         sql="", expect="blocked"),
    dict(id="L1-2", layer="L1", name="whitespace only is refused",
         sql="   \n  ", expect="blocked"),
    dict(id="L1-3", layer="L1", name="comment-only input is refused",
         sql="-- nothing to run here", expect="blocked"),
    dict(id="L1-4", layer="L1", name="two statements in one call are refused",
         sql="SELECT 1; DROP TABLE shop.users", expect="blocked"),
    dict(id="L1-5", layer="L1", name="one statement with a trailing semicolon passes",
         sql="SELECT 1;", expect="allowed"),

    # ---- L2: read-only statement shape
    dict(id="L2-1", layer="L2", name="DROP never starts a read-only statement",
         sql="DROP TABLE shop.orders", expect="blocked", reason_contains="read-only"),
    dict(id="L2-2", layer="L2", name="UPDATE never starts a read-only statement",
         sql="UPDATE shop.orders SET amount = 0", expect="blocked", reason_contains="read-only"),
    dict(id="L2-3", layer="L2", name="SELECT … FOR UPDATE is a locking read",
         sql="SELECT * FROM shop.orders FOR UPDATE", expect="blocked", reason_contains="locking"),
    dict(id="L2-4", layer="L2", name="SELECT … FOR NO KEY UPDATE is a locking read",
         sql="SELECT * FROM shop.orders FOR NO KEY UPDATE", expect="blocked", reason_contains="locking"),
    dict(id="L2-5", layer="L2", name="SELECT … FOR SHARE is a locking read",
         sql="SELECT * FROM shop.orders FOR SHARE", expect="blocked", reason_contains="locking"),
    dict(id="L2-6", layer="L2", name="SELECT … FOR KEY SHARE is a locking read",
         sql="SELECT * FROM shop.orders FOR KEY SHARE", expect="blocked", reason_contains="locking"),
    dict(id="L2-7", layer="L2", name="WITH … SELECT is read-only",
         sql="WITH recent AS (SELECT 1 AS x) SELECT * FROM recent", expect="allowed"),
    dict(id="L2-8", layer="L2", name="TABLE is shorthand for SELECT *",
         sql="TABLE shop.orders", expect="allowed"),
    dict(id="L2-9", layer="L2", name="VALUES is a read-only statement",
         sql="VALUES (1), (2)", expect="allowed"),
    dict(id="L2-10", layer="L2", name="EXPLAIN is read-only",
         sql="EXPLAIN SELECT * FROM shop.orders", expect="allowed"),

    # ---- L3: forbidden keywords, inside a statement whose prefix is allowed
    dict(id="L3-1", layer="L3", name="UPDATE hidden in a CTE is caught",
         sql="WITH x AS (UPDATE shop.orders SET amount = 0 RETURNING id) SELECT * FROM x",
         expect="blocked", reason_contains="UPDATE"),
    dict(id="L3-2", layer="L3", name="DELETE hidden in a CTE is caught",
         sql="WITH t AS (DELETE FROM shop.orders RETURNING id) SELECT * FROM t",
         expect="blocked", reason_contains="DELETE"),
    dict(id="L3-3", layer="L3", name="SELECT … INTO materialises a table",
         sql="SELECT * FROM shop.orders INTO archived_orders",
         expect="blocked", reason_contains="INTO"),
    dict(id="L3-4", layer="L3", name="CREATE hidden in a CTE is caught",
         sql="WITH c AS (CREATE TABLE shop.tmp_orders AS SELECT 1) SELECT 1",
         expect="blocked", reason_contains="CREATE"),
    dict(id="L3-5", layer="L3", name="TRUNCATE inside a comment is not a statement",
         sql="SELECT 1 /* TRUNCATE TABLE shop.orders */", expect="allowed"),
    dict(id="L3-6", layer="L3", name="a banned word inside a string literal is data",
         sql="SELECT 'DELETE FROM shop.orders' AS example", expect="allowed"),

    # ---- L4: dangerous functions
    dict(id="L4-1", layer="L4", name="pg_sleep is refused",
         sql="SELECT pg_sleep(10)", expect="blocked", reason_contains="pg_sleep"),
    dict(id="L4-2", layer="L4", name="pg_read_file is refused",
         sql="SELECT pg_read_file('/etc/passwd')", expect="blocked",
         reason_contains="pg_read_file"),
    dict(id="L4-3", layer="L4", name="nextval is a write to a sequence",
         sql="SELECT nextval('shop.orders_id_seq')", expect="blocked",
         reason_contains="nextval"),
    dict(id="L4-4", layer="L4", name="pg_terminate_backend is refused",
         sql="SELECT pg_terminate_backend(12345)", expect="blocked",
         reason_contains="pg_terminate_backend"),
    dict(id="L4-5", layer="L4", name="set_config can change session behaviour",
         sql="SELECT set_config('statement_timeout', '0', false)", expect="blocked",
         reason_contains="set_config"),
    dict(id="L4-6", layer="L4", name="pg_advisory_lock takes a global lock",
         sql="SELECT pg_advisory_lock(42)", expect="blocked",
         reason_contains="pg_advisory_lock"),
    dict(id="L4-7", layer="L4", name="current_setting is refused too (conservative)",
         sql="SELECT current_setting('search_path')", expect="blocked",
         reason_contains="current_setting", known_false_positive=True),
    dict(id="L4-8", layer="L4", name="a harmless pg_ function passes",
         sql="SELECT pg_size_pretty(1024)", expect="allowed"),

    # ---- L5: table whitelist (per deployment)
    dict(id="L5-1", layer="L5", name="whitelisted table passes",
         sql="SELECT * FROM shop.orders", expect="allowed",
         whitelist=["shop.orders"]),
    dict(id="L5-2", layer="L5", name="table outside the whitelist is refused",
         sql="SELECT * FROM shop.users", expect="blocked", whitelist=["shop.orders"],
         reason_contains="whitelist"),
    dict(id="L5-3", layer="L5", name="an unqualified table is resolved to the whitelist too",
         sql="SELECT * FROM orders_archive", expect="blocked", whitelist=["orders"],
         reason_contains="whitelist"),
    dict(id="L5-4", layer="L5", name="a JOIN cannot smuggle a second table in",
         sql=("SELECT o.id FROM shop.orders o JOIN shop.users u"
              " ON u.id = o.customer_id"),
         expect="blocked", whitelist=["shop.orders"], reason_contains="shop.users"),
    dict(id="L5-5", layer="L5", name="no whitelist = the layer is off",
         sql="SELECT * FROM shop.users", expect="allowed", whitelist=[]),

    # ---- L6: forced row limit
    dict(id="L6-1", layer="L6", name="a missing LIMIT is added",
         sql="SELECT * FROM shop.orders", expect="allowed",
         assert_sql_contains="LIMIT 100", assert_rewritten=True),
    dict(id="L6-2", layer="L6", name="the limit follows --max-rows",
         sql="SELECT * FROM shop.orders", expect="allowed", max_rows=25,
         assert_sql_contains="LIMIT 25", assert_rewritten=True),
    dict(id="L6-3", layer="L6", name="an explicit LIMIT is left alone",
         sql="SELECT * FROM shop.orders LIMIT 5", expect="allowed",
         assert_not_rewritten=True, assert_sql_not_contains="LIMIT 100"),
    dict(id="L6-4", layer="L6", name="'limit 3' inside a literal is not a LIMIT",
         sql="SELECT 'limit 3' AS marker FROM shop.orders", expect="allowed",
         assert_rewritten=True, assert_sql_contains="LIMIT 100"),
    dict(id="L6-5", layer="L6", name="lowercase limit is recognised",
         sql="select * from shop.orders limit 7", expect="allowed",
         assert_not_rewritten=True),
]

LIVE_ENV_SQL = """\
-- L7, database side: a role that physically cannot write (layer 7a)
CREATE ROLE agent_ro LOGIN PASSWORD 'ro_only';
GRANT CONNECT ON DATABASE fence_demo TO agent_ro;
GRANT USAGE  ON SCHEMA public TO agent_ro;
GRANT USAGE  ON SCHEMA shop   TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop   TO agent_ro;
"""

LIVE_VERIFY_CMDS = [
    # 7a: the role cannot write, whatever the string checker said
    'psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" '
    '-c "DELETE FROM shop.orders WHERE 1=1"',
    '# expect: ERROR: permission denied for table orders',
    # 7b: the instance honours the timeout the Toolbox sets
    'psql "postgresql://postgres@localhost:5432/fence_demo" '
    '-c "SET statement_timeout = \'10s\'; SELECT pg_sleep(20)"',
    '# expect: ERROR: canceling statement due to statement timeout  (~10s)',
    # 7b, through the product: the Toolbox issues SET on its own connection
    # before every query.  The deterministic half (guard() returns exactly
    # those three statements, the Toolbox executes them first) is covered
    # offline by case L7-1 and by run_tool_selftest(); what needs a live
    # instance is only that PostgreSQL accepts them, which the psql pair
    # above and below shows.
    'psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" '
    '-c "SHOW statement_timeout" -c "SHOW default_transaction_read_only"',
    '# expect: the role connects; the defaults are the server\'s, not the agent\'s',
    # 7c: the audit trail the guard writes
    'tail -n 5 agent_trace.jsonl',
    '# expect: one JSON line per tool call with tool/decision/layer/ok/ms, and a',
    '#         blocked write shows decision=blocked with the layer that stopped it',
]


def run_guard_suite(trace_dir: Path) -> list[dict]:
    rows: list[dict] = []

    for case in GUARD_CASES:
        verdict = guard(case["sql"], max_rows=case.get("max_rows", 100),
                        table_whitelist=case.get("whitelist"))
        got = "allowed" if verdict.ok else "blocked"
        problems = []

        if got != case["expect"]:
            problems.append(f"expected {case['expect']}, got {got} "
                            f"({verdict.layer}: {verdict.reason})")
        if case["expect"] == "blocked":
            if verdict.layer != case["layer"]:
                problems.append(f"stopped at {verdict.layer}, expected {case['layer']}")
            want = case.get("reason_contains")
            if want and want.lower() not in verdict.reason.lower():
                problems.append(f"reason does not mention {want!r}: {verdict.reason!r}")
        else:
            if case.get("assert_rewritten") and not verdict.rewritten:
                problems.append("expected the statement to be rewritten with a LIMIT")
            if case.get("assert_not_rewritten") and verdict.rewritten:
                problems.append("expected the statement to be left alone")
            frag = case.get("assert_sql_contains")
            if frag and frag not in verdict.sql:
                problems.append(f"rewritten SQL lacks {frag!r}")
            frag = case.get("assert_sql_not_contains")
            if frag and frag in verdict.sql:
                problems.append(f"rewritten SQL should not contain {frag!r}")

        rows.append({"suite": "guard", "id": case["id"], "layer": case["layer"],
                     "name": case["name"], "sql": case["sql"],
                     "expected": case["expect"],
                     "actual": got if not problems else f"{got} · {'; '.join(problems)}",
                     "layer_reported": verdict.layer, "reason": verdict.reason,
                     "known_false_positive": case.get("known_false_positive", False),
                     "passed": not problems,
                     "problems": problems})

    # ---- L7: audit record and session hardening (no database needed) -------
    def l7(case_id: str, name: str, ok: bool, detail: str) -> None:
        rows.append({"suite": "guard", "id": case_id, "layer": "L7", "name": name,
                     "sql": "(toolbox call)", "expected": "recorded",
                     "actual": detail, "passed": ok, "problems": [] if ok else [detail]})

    verdict = guard("SELECT 1", timeout_ms=2500)
    wanted = {"SET default_transaction_read_only = on": True,
              "SET statement_timeout = '2500ms'": True,
              "SET idle_in_transaction_session_timeout = '30s'": True}
    missing = [s for s in wanted if s not in verdict.session_sql]
    l7("L7-1", "session hardening statements are returned for the caller",
       not missing, "all three present" if not missing else f"missing {missing}")

    blocked = guard("DROP TABLE shop.orders")
    audit_ok = (blocked.audit.get("decision") == "blocked"
                and blocked.audit.get("layer") == "L2"
                and isinstance(blocked.audit.get("ms"), (int, float)))
    l7("L7-2", "a blocked decision carries an audit record",
       audit_ok, json.dumps(blocked.audit, ensure_ascii=False))

    allowed = guard("SELECT * FROM shop.orders")
    audit_ok = (allowed.audit.get("decision") == "allowed"
                and allowed.audit.get("tables") == ["shop.orders"]
                and allowed.audit.get("rewritten") is True)
    l7("L7-3", "an allowed decision records tables and rewriting",
       audit_ok, json.dumps(allowed.audit, ensure_ascii=False))

    trace = trace_dir / "layer_trace.jsonl"
    trace.unlink(missing_ok=True)
    box = Toolbox(trace_path=trace)
    box.call("run_sql", {"sql": "SELECT 1"})                 # allowed, no database
    box.call("run_sql", {"sql": "DROP TABLE shop.orders"})   # blocked at L2
    box.call("nonsense_tool", {})                            # unknown tool
    lines = [json.loads(l) for l in trace.read_text(encoding="utf-8").splitlines() if l.strip()]
    shaped = (len(lines) == 3
              and lines[1]["decision"] == "blocked" and lines[1]["layer"] == "L2"
              and lines[2]["decision"] == "unknown-tool"
              and all("ms" in l and "tool" in l for l in lines))
    l7("L7-4", "every tool call lands in agent_trace.jsonl",
       shaped, f"{len(lines)} line(s): "
               + ", ".join(f"{l['tool']}/{l['decision']}" for l in lines))

    guard_inside_tool = box.call("run_sql", {"sql": "SELECT * FROM shop.orders FOR UPDATE"})
    l7("L7-5", "the tools share the guard's door",
       (not guard_inside_tool.ok) and guard_inside_tool.layer == "L2",
       f"{guard_inside_tool.brief()}")

    return rows


# --------------------------------------------------------------------------- #
# the checks: seven findings about a schema
# --------------------------------------------------------------------------- #

CLEAN_COMMENTS = """\
COMMENT ON TABLE shop.%%s IS 'described for the reader';
COMMENT ON COLUMN shop.%s.%s IS 'described too';
"""

CHECKS_CASES: list[dict] = [
    # ---- check 1: schema resolution
    dict(id="C1-1", check="1", expect=True,
         name="orders vs orders_archive is an archive pair",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2)
);
CREATE TABLE shop.orders_archive (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2)
);"""),
    dict(id="C1-2", check="1", expect=True,
         name="column names one character apart",
         ddl="""CREATE TABLE shop.payments (
  id BIGINT NOT NULL,
  customer_uid BIGINT,
  customer_id  BIGINT
);"""),
    dict(id="C1-3", check="1", expect=False,
         name="two unrelated tables raise nothing",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2)
);
CREATE TABLE shop.customers (
  id BIGINT NOT NULL,
  full_name TEXT
);"""),

    # ---- check 2: join keys
    dict(id="C2-1", check="2", expect=True, severity="high",
         name="nullable FK used in joins",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  customer_id BIGINT REFERENCES shop.customers(id)
);"""),
    dict(id="C2-2", check="2", expect=True, severity="medium",
         name="nullable key column with no FK at all",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  campaign_id BIGINT
);"""),
    dict(id="C2-3", check="2", expect=False,
         name="a NOT NULL key is not a silent-drop risk",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  customer_id BIGINT NOT NULL
);"""),

    # ---- check 3: types and precision
    dict(id="C3-1", check="3", expect=True, severity="high",
         name="money in floating point",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount DOUBLE PRECISION
);"""),
    dict(id="C3-2", check="3", expect=True, severity="high",
         name="timestamp stored as text",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  created_at TEXT
);"""),
    dict(id="C3-3", check="3", expect=True, severity="medium",
         name="timestamp without time zone",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  created_at TIMESTAMP
);"""),
    dict(id="C3-4", check="3", expect=False,
         name="NUMERIC money and TIMESTAMPTZ raise nothing",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2),
  created_at TIMESTAMPTZ
);"""),

    # ---- check 4: NULL semantics
    dict(id="C4-1", check="4", expect=True,
         name="set-like table with a nullable key",
         ddl="""CREATE TABLE shop.blacklist (
  id BIGINT NOT NULL,
  user_id BIGINT
);"""),
    dict(id="C4-2", check="4", expect=False,
         name="the same table with NOT NULL is safe from NOT IN",
         ddl="""CREATE TABLE shop.blacklist (
  id BIGINT NOT NULL,
  user_id BIGINT NOT NULL
);"""),

    # ---- hygiene
    dict(id="H-1", check="H", expect=True,
         name="table with no comment",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2)
);"""),
    dict(id="H-2", check="H", expect=True,
         name="table described, columns not",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  amount NUMERIC(12,2)
);
COMMENT ON TABLE shop.orders IS 'orders, live table';"""),
    dict(id="H-3", check="H", expect=True,
         name="two spellings for the same idea",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  created_at TIMESTAMPTZ
);
CREATE TABLE shop.shipments (
  id BIGINT NOT NULL,
  ctime TIMESTAMPTZ
);
COMMENT ON TABLE shop.orders IS 'orders';
COMMENT ON COLUMN shop.orders.id IS 'pk';
COMMENT ON COLUMN shop.orders.created_at IS 'when placed';
COMMENT ON TABLE shop.shipments IS 'shipments';
COMMENT ON COLUMN shop.shipments.id IS 'pk';
COMMENT ON COLUMN shop.shipments.ctime IS 'when created';"""),
    dict(id="H-4", check="H", expect=False,
         name="described table with one time convention",
         ddl="""CREATE TABLE shop.orders (
  id BIGINT NOT NULL,
  created_at TIMESTAMPTZ
);
COMMENT ON TABLE shop.orders IS 'orders, live table';
COMMENT ON COLUMN shop.orders.id IS 'primary key';
COMMENT ON COLUMN shop.orders.created_at IS 'when the order was placed';"""),
]


def run_checks_suite(dsn: str | None) -> list[dict]:
    rows: list[dict] = []
    for case in CHECKS_CASES:
        schema = parse_ddl(case["ddl"])
        findings = [f for f in analyze(schema) if f.check == case["check"]]
        found = bool(findings)
        problems = []
        if found != case["expect"]:
            problems.append(f"expected {'a finding' if case['expect'] else 'no finding'}, "
                            f"got {len(findings)}")
        want = case.get("severity")
        if want and found and not any(f.severity == want for f in findings):
            problems.append(f"expected severity {want}, got "
                            f"{sorted({f.severity for f in findings})}")
        rows.append({"suite": "checks", "id": case["id"], "layer": f"check {case['check']}",
                     "name": case["name"], "expected": case["expect"],
                     "actual": (findings[0].title + f" ({findings[0].severity})")
                     if findings else "nothing reported",
                     "passed": not problems, "problems": problems})

    # check 5 is a live check: prove it is not claimed offline, then run it live
    sample = parse_ddl(Path(ROOT / "examples" / "sample_schema.sql").read_text(encoding="utf-8"))
    offline_ids = {f.check for f in analyze(sample)}
    rows.append({"suite": "checks", "id": "C5-1", "layer": "check 5",
                 "name": "check 5 is not faked from a DDL file",
                 "expected": "absent offline", "actual": f"checks seen: {sorted(offline_ids)}",
                 "passed": "5" not in offline_ids, "problems": []})

    if dsn:
        try:
            from schemafence import snapshot
            conn = snapshot.connect(dsn)
            with conn:
                fractions = snapshot.null_fractions(conn)
                estimates = snapshot.row_estimates(conn)
            detail = (f"{len(fractions)} nullable column(s) with measured fractions, "
                      f"{len(estimates)} table estimate(s)")
            rows.append({"suite": "checks", "id": "C5-2", "layer": "check 5",
                         "name": "check 5 reads pg_stats on a live database",
                         "expected": "rows returned", "actual": detail,
                         "passed": bool(fractions or estimates), "problems": []})
        except Exception as exc:  # noqa: BLE001
            rows.append({"suite": "checks", "id": "C5-2", "layer": "check 5",
                         "name": "check 5 reads pg_stats on a live database",
                         "expected": "rows returned",
                         "actual": f"{type(exc).__name__}: {exc}",
                         "passed": False, "problems": [str(exc)]})
    else:
        rows.append({"suite": "checks", "id": "C5-2", "layer": "check 5",
                     "name": "check 5 reads pg_stats on a live database",
                     "expected": "rows returned",
                     "actual": "skipped — pass --db to run it",
                     "passed": True, "problems": [], "skipped": True})
    return rows


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def print_suite(rows: list[dict], title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 60 - len(title)))
    for row in rows:
        mark = "PASS" if row["passed"] else ("SKIP" if row.get("skipped") else "FAIL")
        print(f"  {mark}  {row['id']:<6} {row['layer']:<8} {row['name'][:58]}")
        if not row["passed"]:
            print(f"        expected : {row['expected']}")
            print(f"        actual   : {row['actual']}")
        elif row["suite"] == "checks" and row.get("actual"):
            print(f"        → {row['actual'][:100]}")


def merge_into_existing(path: Path, suites: dict) -> dict:
    """Fold this run's suites into whatever the file already holds.

    Necessary because the two live halves need different flags — ``--suite
    guard`` needs no database, ``--suite checks --db`` does — so the run is
    normally two commands, and both default to the same output path.  Without
    this, the second command silently replaced the first: the run printed
    44/44 and the file next to it held nothing but the 18 checks, which is
    precisely backwards from what an artefact is for.

    A suite this run did not execute keeps its previous rows.  A suite it did
    execute replaces them, so re-running one half cannot leave a stale copy.
    """
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous = None  # a corrupt file is not worth failing the run over

    merged = dict((previous or {}).get("suites") or {})
    merged.update({k: v for k, v in suites.items() if v})

    fresh = [r for rows in merged.values() for r in rows]
    failed = sum(1 for r in fresh if not r["passed"])
    skipped = sum(1 for r in fresh if r.get("skipped"))
    return {
        "suites": merged,
        "passed": len(fresh) - failed - skipped,
        "failed": failed,
        "skipped": skipped,
        "ran_this_invocation": [k for k, v in suites.items() if v],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="seven checks + seven guard layers")
    parser.add_argument("--suite", default="all", choices=["guard", "checks", "all"])
    parser.add_argument("--db", default=None, help="DSN, to run the live half of check 5")
    parser.add_argument("--show-env", action="store_true",
                        help="print the SQL and psql commands the live layers need")
    parser.add_argument("--out", default=str(ROOT / "bench" / "layers.json"))
    args = parser.parse_args(argv)

    if args.show_env:
        print("environment for the database-side layers (L7a/L7b, check 5)")
        print("─" * 64)
        print(LIVE_ENV_SQL)
        print("verification:")
        for line in LIVE_VERIFY_CMDS:
            print(f"  {line}")
        print()
        print("the guard's own layers need no environment:")
        print("  python scripts/test_layers.py --suite guard")
        return 0

    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        if args.suite in ("guard", "all"):
            rows += run_guard_suite(Path(tmp))
        if args.suite in ("checks", "all"):
            rows += run_checks_suite(args.db)

    guard_rows = [r for r in rows if r["suite"] == "guard"]
    check_rows = [r for r in rows if r["suite"] == "checks"]
    if guard_rows:
        print_suite(guard_rows, "runtime gates (guard.py)")
    if check_rows:
        print_suite(check_rows, "schema checks (checks.py)")

    failed = [r for r in rows if not r["passed"]]
    skipped = [r for r in rows if r.get("skipped")]
    print()
    print(f"{len(rows) - len(failed) - len(skipped)}/{len(rows) - len(skipped)} cases passed"
          + (f", {len(skipped)} skipped (needs --db)" if skipped else ""))
    if failed:
        print(f"{len(failed)} FAILED: " + ", ".join(r["id"] for r in failed))

    written = Path(args.out)
    payload = merge_into_existing(written, {"guard": guard_rows, "checks": check_rows})
    missing = [s for s in ("guard", "checks")
               if not (payload["suites"].get(s) or [])]
    write_json(written, payload)
    print(f"json: {written.relative_to(ROOT) if written.is_relative_to(ROOT) else written}")
    if missing:
        print(f"  note: no {', '.join(missing)} rows in this file — that suite has "
              f"not been run against this --out yet")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
