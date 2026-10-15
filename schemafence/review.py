#!/usr/bin/env python3
"""Review somebody else's script against a rule pack.

    python -m schemafence.review --rules rules/table-hygiene.json --ddl script.sql
    python -m schemafence.review --rules rules/table-hygiene.json --verify
    python -m schemafence.review --vocabulary > vocabulary.json

A rule can also ask for facts a script does not have.  "No table may carry two
indexes on the same columns, in the same order" is about what the table
*already* holds, and a script only knows the indexes it creates:

    python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN"
    python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN" \
                                 --ddl release.sql

The second form reads the catalogue and then the indexes the script adds, so
the question it answers is the one a reviewer actually has: after this
migration, will the table carry two indexes on the same key?

Exit codes matter here, because this is meant to sit in a pipeline:

    0   read, and no rule fired
    1   read, and at least one rule fired
    2   could not review — a pack failed its own examples, a pack asked for
        facts this run did not read, or the file was read as nothing at all

2 is the point of the third code.  A review that understood nothing must not
be able to print "0 finding(s)" and exit 0, because that reads as approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checks import SEVERITY_RANK
from .facts import FACT_MODELS
from .rulepack import (DEFAULT_RULE_DIR, infer_rule, load_rulepack, PACK_VERSION,
                       RulePackError, RuleReport, review_ddl, verify_rulepack,
                       vocabulary)

WIDTH = 70


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def heading(text: str) -> None:
    print()
    print(text)
    rule()


def show_identity(packs: list[tuple[Path, dict, list]]) -> None:
    for path, pack, rows in packs:
        count = len(pack["rules"])
        print(f"rules  : {pack['rulepack']:<24} "
              f"({count} rule{'s' if count != 1 else ''}, {len(rows)} examples verified)   "
              f"{path.name}")


def show_applied(report: RuleReport) -> None:
    heading("what each rule looked at")
    if not report.applied:
        print("  no rule was applied. The packs are listed above and the notes")
        print("  below say why; nothing here cleared anything.")
        return
    print(f"  {'rule':<44}{'target':<8}{'matched':>7}{'fired':>7}")
    for row in report.applied:
        mark = "" if row["matched"] else "   (nothing matched)"
        print(f"  {row['id']:<44}{row['target']:<8}{row['matched']:>7}"
              f"{row['findings']:>7}{mark}")
    print()
    print("  a rule that matched nothing asserted nothing. That is visible here")
    print("  rather than folded into a total, because zero is not a pass.")


def show_findings(report: RuleReport) -> None:
    if not report.findings:
        return
    heading(f"[findings] {len(report.findings)}")
    ordered = sorted(report.findings,
                     key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.check, f.subject))
    for finding in ordered:
        print(f"  {finding.severity.upper():<7}{finding.subject}")
        print(f"         rule : {finding.check}")
        print(f"         what : {finding.title}")
        print(f"         why  : {finding.detail}")
        print(f"         fix  : {finding.fix}")
        print()


def show_inference(result: dict, request_path: Path, write_to: Path | None) -> int:
    applies_to = result.get("applies_to") or ["ddl"]
    print(f"request: {request_path.name}  →  target {result['target']}, "
          f"{result['examples']['fires']} fires, {result['examples']['silent']} silent")
    print(f"searched: {result['searched']} selection/assertion pair(s) over the "
          f"closed vocabulary")
    print()
    if not result["candidates"]:
        print("  no rule over the current facts separates these examples.")
    for index, rule in enumerate(result["candidates"], 1):
        print(f"  {index}. assert {json.dumps(rule['assert'], ensure_ascii=False)}")
        print(f"     select {json.dumps(rule.get('select', {}), ensure_ascii=False)}")
    if result["near_misses"]:
        print()
        print("  closest rules that do not separate everything:")
        for miss in result["near_misses"]:
            print(f"    · {json.dumps(miss['assert'], ensure_ascii=False)}")
            if miss.get("select"):
                print(f"      selected from "
                      f"{json.dumps(miss['select'], ensure_ascii=False)}")
            print(f"      separates {miss['separated']}")
    if result.get("note"):
        print()
        for line in _wrap(result["note"]):
            print(f"  {line}")

    if write_to is None:
        if not result["candidates"]:
            return 2                      # an empty pack on stdout is an invitation
        print()
        print(json.dumps({"rulepack": request_path.stem, "version": PACK_VERSION,
                          "applies_to": applies_to, "rules": result["candidates"][:1]},
                         indent=2, ensure_ascii=False))
        return 0

    pack = {"rulepack": write_to.stem, "version": PACK_VERSION,
            "applies_to": applies_to, "rules": result["candidates"][:1]}
    try:
        load_rulepack(pack, verify=True)          # the gate, before anything is written
    except RulePackError as exc:
        print(f"\nthe candidate did not pass the example gate: {exc}")
        return 2
    write_to.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print()
    print(f"  written and verified: {write_to}")
    print(f"  use it now:  python -m schemafence.review --rules {write_to} --ddl script.sql")
    return 0


def _wrap(text: str, width: int = 64) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])


def read_catalog(dsn: str):
    """The catalogue, read-only.  Returns (schema, note) or (None, why not)."""
    from . import snapshot

    try:
        conn = snapshot.connect(dsn)
    except Exception as exc:                          # noqa: BLE001 - reported, not raised
        return None, f"cannot reach the catalog: {type(exc).__name__}: {exc}"
    try:
        return snapshot.read_schema(conn), None
    except Exception as exc:                          # noqa: BLE001 - a query, not a crash
        return None, f"the catalog could not be read: {type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _default_packs(from_catalog: bool) -> list[Path]:
    """The packs to apply when none were named.

    A script and a catalog are not reviewed by the same rules, so the default
    follows the facts rather than a fixed filename: a pack that declares
    ``applies_to: ["catalog"]`` is the one worth running when the facts came
    from a database, and vice versa.  A file that will not parse is included
    rather than skipped, so the loader gets to name the problem.
    """
    if not from_catalog:
        return [DEFAULT_RULE_DIR / "table-hygiene.json"]
    chosen = []
    for path in sorted(DEFAULT_RULE_DIR.glob("*.json")):
        try:
            declared = json.loads(path.read_text(encoding="utf-8")).get("applies_to")
        except (OSError, json.JSONDecodeError):
            declared = None
        if declared is None or "catalog" in declared:
            chosen.append(path)
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schemafence.review",
        description="Review a DDL script against rule packs. Deterministic: no model, "
                    "no network. With --db, reads a live catalog read-only.")
    parser.add_argument("--rules", action="append", default=None,
                        help="rule pack JSON (repeatable). Default: table-hygiene.json for a "
                             "script, every pack that declares the catalog model for --db")
    parser.add_argument("--ddl", default=None,
                        help="the script to review; - reads standard input")
    parser.add_argument("--db", default=None, metavar="DSN",
                        help="read the facts from a live PostgreSQL catalog, read-only. "
                             "With --ddl, the indexes that script creates are added on "
                             "top, so a pack can ask about the state after the migration")
    parser.add_argument("--facts", action="store_true",
                        help="list the registered fact models and targets, and stop")
    parser.add_argument("--verify", action="store_true",
                        help="run each pack's own examples and stop")
    parser.add_argument("--vocabulary", action="store_true",
                        help="print the closed fact/selector/assert vocabulary, which is "
                             "what you give a model when you want a rule written")
    parser.add_argument("--infer", default=None, metavar="REQUEST.json",
                        help="search the closed vocabulary for the rules that separate "
                             "the examples in this request file, offline")
    parser.add_argument("--limit", type=int, default=5, metavar="N",
                        help="with --infer: how many candidates to show (default 5)")
    parser.add_argument("--write", default=None, metavar="PACK.json",
                        help="with --infer: write the first candidate as a pack, after "
                             "it passes the example gate")
    args = parser.parse_args(argv)

    if args.vocabulary:
        print(json.dumps(vocabulary(), indent=2, ensure_ascii=False))
        return 0

    if args.facts:
        from .facts import FIELDS, models_for
        for model, reason in FACT_MODELS.items():
            print(f"model  {model:<10}{'built' if reason is None else reason}")
        print()
        for target, fields in FIELDS().items():
            print(f"target {target:<10}{len(fields)} field(s), read from "
                  f"{', '.join(models_for(target))}")
        return 0

    if args.infer:
        print("schemafence infer — from your examples, the rule that separates them")
        rule("=")
        try:
            request = json.loads(Path(args.infer).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read the request: {exc}")
            return 2
        result = infer_rule(request, limit=args.limit)
        if "error" in result:
            print(f"refused: {result['error']}")
            return 2
        return show_inference(result, Path(args.infer),
                              Path(args.write) if args.write else None)

    paths = [Path(p) for p in args.rules] if args.rules else _default_packs(bool(args.db))
    if not paths:
        print(f"no pack in `{DEFAULT_RULE_DIR.name}/` declares the catalog model, which is "
              f"what `--db` supplies — nothing to apply")
        return 2
    if not args.ddl and not args.verify and not args.db:
        parser.print_help()
        print("\nnothing to do: pass --ddl, --db, --verify or --vocabulary")
        return 2

    print("schemafence review — a rule pack, and a script somebody else wrote")
    rule("=")

    loaded: list[tuple[Path, dict, list]] = []
    for path in paths:
        try:
            pack = load_rulepack(path, verify=True)
        except (RulePackError, OSError) as exc:
            print(f"refused: {path} — {exc}")
            print("\nthe file was not applied. Fix the pack, or fix the reader it needs.")
            return 2
        rows, _ = verify_rulepack(pack)
        loaded.append((path, pack, rows))
    show_identity(loaded)

    if args.verify:
        print()
        for path, pack, rows in loaded:
            print(f"  {pack['rulepack']}")
            for label, expectation, passed, detail in rows:
                print(f"    {'ok  ' if passed else 'FAIL'} {label:<46}{expectation}")
            print()
        failed = [label for _p, _k, rows in loaded
                  for label, _e, passed, _d in rows if not passed]
        print(f"  {sum(len(r) for _p, _k, r in loaded)} example(s) → "
              f"{'all passed' if not failed else 'FAILURES: ' + ', '.join(failed)}")
        return 0 if not failed else 2

    packs = [pack for _path, pack, _rows in loaded]
    text = ""
    if args.ddl == "-":
        text = sys.stdin.read()
    elif args.ddl:
        source_path = Path(args.ddl)
        if not source_path.exists():
            print(f"\nscript not found: {source_path}")
            return 2
        text = source_path.read_text(encoding="utf-8")

    from .rulepack import review_schema

    if args.db:
        schema, why_not = read_catalog(args.db)
        if schema is None:
            print(f"\n{why_not}")
            print("  nothing was read and no rule was applied.  Reading a catalog is a "
                  "read: it does not need the write path, and it does not touch one.")
            return 2
        print(f"facts  : the live catalog — {len(schema.tables)} table(s), "
              f"{sum(len(t.indexes) for t in schema.tables)} index(es), read-only")
        if text.strip():
            from .ddl import parse_ddl
            from .snapshot import apply_script
            applied = apply_script(schema, parse_ddl(text))
            print(f"script : {args.ddl} → {applied.indexes} index(es) it creates were "
                  f"added to those facts")
            if applied.new_tables:
                print(f"         {len(applied.new_tables)} table(s) it creates are not in "
                      f"the catalog, so their indexes were compared with nothing: "
                      + ", ".join(applied.new_tables))
        elif args.ddl:
            print("script : standard input → no statements were read from it")
        report = review_schema(packs, schema)
    else:
        print(f"script : {source_path.name if args.ddl else '—'}")
        report = review_ddl(packs, text)

    if report.unread and report.origin != "catalog":
        print(f"unread : {len(report.unread)} statement(s) no reader claimed")
    if report.origin == "catalog":
        print(f"read   : {report.tables} table(s), {report.columns} column(s), "
              f"{report.indexes} index(es)")
    else:
        print(f"read   : {report.statements} statement(s) → {report.tables} table(s), "
              f"{report.columns} column(s), {report.changes} change action(s)")

    show_applied(report)
    show_findings(report)

    if report.notes:
        heading("[notes]")
        for note in report.notes:
            print(f"  · {note}")

    heading("[verdict]")
    if report.unapplied:
        print(f"  could not review with {len(report.unapplied)} pack(s) not applied: "
              + ", ".join(f"`{name}`" for name in report.unapplied))
        print("  a pack that was not applied has not cleared anything. Each note above")
        print("  names the facts it asked for, and the command that would supply them.")
        return 2
    if report.blind:
        print("  could not review — nothing in this file was understood.")
        print("  this is not a pass, and the exit code is 2 for that reason.")
        return 2
    if report.findings:
        top = min(SEVERITY_RANK.get(f.severity, 9) for f in report.findings)
        worst = [name for name, rank in SEVERITY_RANK.items() if rank == top][0]
        print(f"  {len(report.findings)} finding(s), worst {worst} — the script does not "
              f"pass these rules.")
        print("  a person reads this and decides. This layer reviews; it never executes.")
        return 1
    print("  no rule fired. Every rule named a non-empty set of rows to assert on —")
    print("  read the matched column above to see what was actually checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
