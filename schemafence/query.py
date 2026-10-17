#!/usr/bin/env python3
"""Review one statement before anybody runs it.

    python -m schemafence.query --sql query.sql --schema schema.sql
    python -m schemafence.query --sql - --db "$PG_DSN" < query.sql

Written for the statement a language model produced: it came out of something
that has never seen the schema, and it is valid SQL, so the database will
answer it.  Two questions, in this order, because the second one only matters
if the first one passes:

  * will it run?  ``guard()`` answers allow / rewrite / block, and the
    statement it hands back is the one to execute.  A rewritten statement is
    shown, not silently substituted.
  * if it runs, can the number be wrong?  ``check_sql()`` reads the statement
    against the schema.  This is the part a database cannot tell you: both
    halves are valid on their own.

The schema is never defaulted.  Judging a production statement against the
bundled example schema would report "clean" for every table the example does
not have, which is the one output this project must never print.

Exit codes:

    0   it will run, and no check fired
    1   it will not run, or at least one check fired
    2   could not review — no schema given, the file was unreadable, the
        statement was empty, or it named a table this run did not read

2 covers "named a table this run did not read" on purpose.  A query that
points at a table nobody loaded is not a clean query; it is a query nobody
checked.  The one case that does exit 0 with nothing asserted is a statement
that names no table at all: nothing in it depends on the schema, so there was
nothing to get wrong.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checks import SEVERITY_RANK
from .checks_sql import SqlReview, check_sql
from .ddl import Schema, parse_ddl
from .guard import _sanitize, guard

WIDTH = 70


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def heading(text: str) -> None:
    print()
    print(text)
    rule()


def _read_sql(value: str) -> tuple[str, str]:
    """(text, problem).  ``-`` reads standard input, as `review --script` does."""
    if value == "-":
        try:
            return sys.stdin.read(), ""
        except OSError as exc:                        # noqa: BLE001 - reported
            return "", f"cannot read standard input: {type(exc).__name__}: {exc}"
    path = Path(value)
    if not path.is_file():
        return "", f"no such file: {value}"
    try:
        return path.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"cannot read it: {type(exc).__name__}: {exc}"


def _load_schema(path: str | None, dsn: str | None) -> tuple[Schema | None, str, str]:
    """(schema, the line to print, why not).  One source, named by the caller."""
    if dsn:
        from .review import read_catalog           # one reader for the catalogue
        schema, why = read_catalog(dsn)
        if schema is None:
            return None, "", why
        return schema, (f"the live catalog — {len(schema.tables)} table(s), "
                        f"{schema.column_count} column(s), read-only"), ""

    file = Path(path or "")
    if not file.is_file():
        return None, "", f"no such file: {path}"
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, "", f"cannot read the schema: {type(exc).__name__}: {exc}"
    schema = parse_ddl(text)
    if not schema.tables:
        return None, "", (f"{file.name} defines no table, so there is nothing to read "
                          f"the statement against")
    return schema, (f"{file.name} ({len(schema.tables)} table(s), "
                    f"{schema.column_count} column(s))"), ""


def show_guard(result, max_rows: int) -> None:
    """The verdict from the fence, and the statement it hands back."""
    if result.ok and result.rewritten:
        print(f"guard  : ALLOWED (rewritten) — it was going to take more rows than "
              f"{max_rows}, so a LIMIT was set")
    elif result.ok:
        print("guard  : ALLOWED")
    else:
        print(f"guard  : BLOCKED at {result.layer} — {result.reason}")
        return
    print("         run this, not the statement as it was written:")
    for line in result.sql.strip().splitlines():
        print(f"           {line.strip()}")
    if result.session_sql:
        print("         session, before it:")
        for statement in result.session_sql:
            print(f"           {statement}")


def show_review(review: SqlReview) -> None:
    if review.findings:
        heading(f"[findings] {len(review.findings)}")
        for finding in review.findings:
            print(f"  {finding.severity.upper():<7}{finding.subject}")
            print(f"         check : {finding.check}")
            print(f"         what  : {finding.title}")
            print(f"         why   : {finding.detail}")
            print(f"         fix   : {finding.fix}")
            print()
    if review.notes:
        heading("[notes]")
        for note in review.notes:
            print(f"  · {note}")


def verdict(review: SqlReview, ok: bool, layer: str, max_rows: int) -> int:
    """The exit code and the line that belongs with it, from one place."""
    heading("[verdict]")
    if not ok:
        print(f"  does not run — guard blocked it at {layer}.")
        print("  nothing was executed, and nothing in this report says it would have")
        print("  been correct. The exit code is 1: there is something to fix.")
        return 1
    if review.unresolved:
        names = ", ".join(review.unresolved)
        print(f"  not reviewed — {len(review.unresolved)} table(s) named in the statement "
              f"are not in the schema this run read: {names}")
        print("  what fired above is real, but the rest of the statement was never")
        print("  checked. Pass the whole schema with --schema, or read the live catalog")
        print("  with --db, and run it again. The exit code is 2 for that reason.")
        return 2
    if review.findings:
        worst = min(SEVERITY_RANK.get(f.severity, 9) for f in review.findings)
        name = [n for n, rank in SEVERITY_RANK.items() if rank == worst][0]
        print(f"  {len(review.findings)} finding(s), worst {name} — it will run, and the "
              f"number it returns")
        print("  can be wrong while the database reports no error. A person reads this "
              "and decides.")
        return 1
    if not review.references:
        print("  clean — this statement names no table, so nothing in it depends on the")
        print("  schema it was passed. There was nothing here to get wrong.")
        return 0
    print(f"  clean — the statement will run, and no check fired against "
          f"{len(review.references)} table(s).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schemafence.query",
        description="Review one statement before it runs: will it run, and can the "
                    "number it returns be wrong? Deterministic: no model, no network.")
    parser.add_argument("--sql", required=True, metavar="PATH|-",
                        help="the statement to review; - reads standard input")
    parser.add_argument("--schema", default=None, metavar="DDL",
                        help="a DDL file describing the tables the statement reads")
    parser.add_argument("--db", default=None, metavar="DSN",
                        help="read the tables from a live PostgreSQL catalog, read-only")
    parser.add_argument("--max-rows", type=int, default=100, metavar="N",
                        help="the row cap the guard applies (default 100)")
    args = parser.parse_args(argv)

    print("schemafence query — one statement, and the schema it will run against")
    rule("=")

    if not args.schema and not args.db:
        print("\nno schema was given, so there is nothing to read the statement against.")
        print("  --schema <file.sql>   a DDL file with the tables this statement reads")
        print("  --db <dsn>            the live catalog, read-only")
        print("\njudging a statement against a schema that is not the one it will run on")
        print("would report every missing table as clean, which is the one answer this")
        print("command must not give. Nothing was reviewed.")
        return 2

    text, problem = _read_sql(args.sql)
    if problem:
        print(f"\n{problem}")
        return 2
    if not _sanitize(text or "").strip():
        print("\nnothing to read — once comments and literals are removed, the "
              "statement is empty.")
        print("  a review of nothing is not a clean review, so the exit code is 2.")
        return 2

    schema, described, why_not = _load_schema(args.schema, args.db)
    if schema is None:
        print(f"\n{why_not}")
        print("  nothing was read and the statement was not reviewed.")
        return 2

    print(f"sql    : {args.sql}")
    print(f"schema : {described}")

    result = guard(text, max_rows=args.max_rows)
    show_guard(result, args.max_rows)

    review = check_sql(schema, text)
    show_review(review)

    return verdict(review, result.ok, result.layer, args.max_rows)


if __name__ == "__main__":
    raise SystemExit(main())
