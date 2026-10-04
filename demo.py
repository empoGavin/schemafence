#!/usr/bin/env python3
"""schemafence demo — a schema audit in 30 seconds.

    python demo.py                     # offline: read the example DDL, no database
    python demo.py --ask "最近一周的订单金额合计是多少？"
    python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo

Offline mode needs nothing but Python 3.10+.  That is intentional: the
fence is deterministic, so it does not need a model, a key, or a network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemafence import analyze, parse_ddl, summarize            # noqa: E402
from schemafence.agent import run_route_selftest                 # noqa: E402
from schemafence.guard import guard, run_selftest                # noqa: E402
from schemafence.planner import plan                             # noqa: E402

WIDTH = 68
CHECK_TITLES = {
    "1": "schema resolution — which entity gets picked",
    "2": "join-key sanity — rows silently dropped",
    "3": "type & precision — values silently changed",
    "4": "NULL semantics — three-valued logic traps",
    "H": "hygiene — comments and naming",
}


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def heading(text: str) -> None:
    print()
    print(text)
    rule()


def show_findings(findings) -> None:
    grouped: dict[str, list] = {}
    for finding in findings:
        grouped.setdefault(finding.check, []).append(finding)

    for check in sorted(grouped, key=lambda c: (c == "H", c)):
        items = grouped[check]
        heading(f"[check {check}] {CHECK_TITLES.get(check, '')}   ({len(items)})")
        for item in items:
            print(item.render())
            print()


def show_plan(question: str, schema) -> None:
    heading("[planner] natural language → candidate tables → SQL")
    print(f"  question : {question}")
    result = plan(question, schema)
    if not result.candidates:
        print("  no candidate table found")
        return
    for rank, cand in enumerate(result.candidates[:3], 1):
        print(f"  #{rank}  {cand.table.qualified:<26} score {cand.score}")
    for note in result.notes:
        print(f"  note : {note}")

    if not result.sql:
        return
    print()
    print("  proposed SQL")
    print(f"    {result.sql}")

    verdict = guard(result.sql)
    print()
    if verdict.ok:
        print(f"  guard: ALLOWED{' (rewritten)' if verdict.rewritten else ''}")
        if verdict.rewritten:
            print("    " + verdict.sql.replace("\n", "\n    "))
        if verdict.tables:
            print(f"    tables : {', '.join(verdict.tables)}")
        print(f"    session: {'; '.join(verdict.session_sql)}")
    else:
        print(f"  guard: BLOCKED at {verdict.layer} — {verdict.reason}")


def show_guard_selftest() -> bool:
    heading("[guard] seven-layer selftest")
    rows, passed_all = run_selftest()
    for sql, expected, passed in rows:
        mark = "ok  " if passed else "FAIL"
        verdict = "allow" if expected else "block"
        print(f"  {mark} expect {verdict:<5}  {sql}")
    print()
    print(f"  {len(rows)} cases → {'all passed' if passed_all else 'FAILURES present'}")
    return passed_all


def show_route_selftest() -> bool:
    heading("[router] write intent vs practice question")
    rows, passed_all = run_route_selftest()
    for question, expected, passed in rows:
        mark = "ok  " if passed else "FAIL"
        verdict = "refuse" if expected else "answer"
        print(f"  {mark} expect {verdict:<6}  {question}")
    print()
    print("  a write keyword is not intent: the imperative is refused, the")
    print("  question is answered from the knowledge base with nothing executed.")
    print(f"  {len(rows)} cases → {'all passed' if passed_all else 'FAILURES present'}")
    return passed_all


def show_live(dsn: str, schema) -> None:
    from schemafence import snapshot

    heading("[live] connected to a real database")
    try:
        conn = snapshot.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - demo-friendly message
        print(f"  could not connect: {exc}")
        print("  tip: run scripts/setup_pg.sh first, or start docker compose up -d")
        return

    with conn:
        live = snapshot.read_schema(conn)
        print(f"  tables read from the catalogue : {len(live.tables)}")
        print(f"  columns                        : {live.column_count}")

        estimates = snapshot.row_estimates(conn)
        print()
        print("  row estimates (from pg_class.reltuples, no COUNT(*))")
        for (ns, name), rows in sorted(estimates.items()):
            if name in {t.name for t in live.tables}:
                print(f"    {ns}.{name:<24} ~{rows}")

        fractions = snapshot.null_fractions(conn)
        if not fractions:
            print()
            print("  no column statistics yet — run ANALYZE on the demo schema")
        else:
            print()
            print("  measured NULL fractions (pg_stats) — the join-loss evidence")
            for ns, table, column, frac, _nd in fractions[:10]:
                print(f"    {ns}.{table}.{column:<20} {frac * 100:5.1f}% NULL")

    live_findings = analyze(live)
    counts = summarize(live_findings)
    print()
    print(f"  same checks, live data → {counts['total']} finding(s) "
          f"({counts['high']} high, {counts['medium']} medium, {counts['low']} low)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="Audit a schema for the misreadings that make AI-generated SQL "
                    "return plausible-but-wrong answers.",
    )
    parser.add_argument("--source", default=str(ROOT / "examples" / "sample_schema.sql"),
                        help="DDL file to analyse (offline mode)")
    parser.add_argument("--ask", default=None, help="ask a question, see the plan and the verdict")
    parser.add_argument("--db", default=None, help="PostgreSQL DSN; enables live mode")
    parser.add_argument("--fail-on", default="never",
                        choices=["never", "low", "medium", "high"],
                        help="exit non-zero if a finding at this level or above exists")
    args = parser.parse_args(argv)

    print("schemafence — the constraint layer between LLMs and databases")
    rule("=")

    source = Path(args.source)
    if not source.exists():
        print(f"source not found: {source}")
        return 2

    schema = parse_ddl(source.read_text(encoding="utf-8"))
    print(f"source : {source.name}")
    print(f"tables : {len(schema.tables)}      columns : {schema.column_count}")
    print("mode   : offline (no database, no model, no API key)")

    findings = analyze(schema)
    show_findings(findings)

    counts = summarize(findings)
    heading("[summary]")
    print(f"  {counts['total']} finding(s): {counts['high']} high / "
          f"{counts['medium']} medium / {counts['low']} low")
    print("  every one of them produces a query that runs successfully and returns")
    print("  a wrong number.  None of them raises an error.")

    guard_ok = show_guard_selftest()
    route_ok = show_route_selftest()

    if args.ask:
        show_plan(args.ask, schema)

    if args.db:
        show_live(args.db, schema)

    print()
    rule("=")
    print("next: docs/quickstart-oraclelinux-vm.md (or -cloudstudio) · "
          "examples/sample_schema.sql to try your own")

    thresholds = {"never": None, "low": 100, "medium": 1, "high": 0}
    limit = thresholds[args.fail_on]
    if limit is not None:
        order = {"high": 0, "medium": 1, "low": 2}
        if any(order[f.severity] <= limit for f in findings):
            return 1
    if not guard_ok:
        return 1
    if not route_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
