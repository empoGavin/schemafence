#!/usr/bin/env python3
"""Review somebody else's script against a rule pack.

    python -m schemafence.review --rules rules/table-hygiene.json --ddl script.sql
    python -m schemafence.review --rules rules/table-hygiene.json --verify
    python -m schemafence.review --vocabulary > vocabulary.json

Exit codes matter here, because this is meant to sit in a pipeline:

    0   read, and no rule fired
    1   read, and at least one rule fired
    2   could not review — a pack failed its own examples, or the file was
        read as nothing at all

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
        print(f"rules  : {pack['rulepack']:<24} "
              f"({len(pack['rules'])} rules, {len(rows)} examples verified)   "
              f"{path.name}")


def show_applied(report: RuleReport) -> None:
    heading("what each rule looked at")
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
                          "applies_to": ["ddl"], "rules": result["candidates"][:1]},
                         indent=2, ensure_ascii=False))
        return 0

    pack = {"rulepack": write_to.stem, "version": PACK_VERSION, "applies_to": ["ddl"],
            "rules": result["candidates"][:1]}
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schemafence.review",
        description="Review a DDL script against rule packs. Deterministic: no model, "
                    "no database, no network.")
    parser.add_argument("--rules", action="append", default=None,
                        help="rule pack JSON (repeatable); default rules/table-hygiene.json")
    parser.add_argument("--ddl", default=None,
                        help="the script to review; - reads standard input")
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

    paths = [Path(p) for p in (args.rules or [str(DEFAULT_RULE_DIR / "table-hygiene.json")])]
    if not args.ddl and not args.verify:
        parser.print_help()
        print("\nnothing to do: pass --ddl, --verify or --vocabulary")
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

    if args.ddl == "-":
        text = sys.stdin.read()
        source = "standard input"
    else:
        source_path = Path(args.ddl)
        if not source_path.exists():
            print(f"\nscript not found: {source_path}")
            return 2
        text = source_path.read_text(encoding="utf-8")
        source = source_path.name

    print(f"script : {source}")
    report = review_ddl([pack for _path, pack, _rows in loaded], text)

    if report.unread:
        print(f"unread : {len(report.unread)} statement(s) no reader claimed")
    print(f"read   : {report.statements} statement(s) → {report.tables} table(s), "
          f"{report.columns} column(s), {report.changes} change action(s)")

    show_applied(report)
    show_findings(report)

    if report.notes:
        heading("[notes]")
        for note in report.notes:
            print(f"  · {note}")

    heading("[verdict]")
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
