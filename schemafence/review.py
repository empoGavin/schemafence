#!/usr/bin/env python3
"""Review somebody else's script against a rule pack.

    python -m schemafence.review --rules rules/table-hygiene.json --script script.sql
    python -m schemafence.review --script sql/2026-10/            # a folder, one row per file
    python -m schemafence.review --rules rules/table-hygiene.json --verify
    python -m schemafence.review --vocabulary > vocabulary.json

A script is not always DDL.  A release also holds UPDATE, DELETE, INSERT and
TRUNCATE, and the reader turns each into an action of its own kind, with
`has_where` telling a filtered statement from one that touches every row.

A rule can also ask for facts a script does not have.  "No table may carry two
indexes on the same columns, in the same order" is about what the table
*already* holds, and a script only knows the indexes it creates:

    python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN"
    python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN" \
                                 --script release.sql

The second form reads the catalogue and then the indexes the script adds, so
the question it answers is the one a reviewer actually has: after this
migration, will the table carry two indexes on the same key?

Exit codes matter here, because this is meant to sit in a pipeline:

    0   read, and no rule fired
    1   read, and at least one rule fired
    2   could not review — a pack failed its own examples, a pack asked for
        facts this run did not read, the file was read as nothing at all, or
        no rule found a single row to assert on

2 is the point of the third code.  A review that understood nothing must not
be able to print "0 finding(s)" and exit 0, because that reads as approval.
The same holds one level down: a pack whose every rule named an empty set of
rows checked nothing, and that is not a pass either.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
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
    print(f"  use it now:  python -m schemafence.review --rules {write_to} --script script.sql")
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


# --------------------------------------------------------------------------- #
# What to review, and what a run of it came to
# --------------------------------------------------------------------------- #
@dataclass
class _Target:
    """One thing to review: a file, standard input, or a path that is not there."""

    label: str
    path: Path | None = None
    problem: str = ""


def _expand(values: list[str]) -> list[_Target]:
    """Turn the paths somebody typed into the files to review, in order.

    A folder is every ``*.sql`` under it, recursively, sorted: a migration
    folder is the shape a review is usually asked for, and a shell loop around
    this command loses both the summary and the exit code.  A path that is not
    there, or a folder with no script in it, becomes a target that could not be
    reviewed rather than a traceback — the rest of the batch is still reviewed,
    and the exit code says one input was never looked at.
    """
    targets: list[_Target] = []
    for value in values:
        if value == "-":
            targets.append(_Target("standard input"))
            continue
        path = Path(value)
        if path.is_dir():
            found = sorted(path.rglob("*.sql"))
            if not found:
                targets.append(_Target(str(path), None, f"no *.sql file under {path}"))
            targets += [_Target(str(name.relative_to(path)), name) for name in found]
        elif path.is_file():
            targets.append(_Target(str(path), path))
        else:
            targets.append(_Target(str(path), None,
                                   f"no such file or directory: {value}"))
    return targets


def _text_of(target: _Target, cache: dict) -> tuple[str, str]:
    """(text, problem).  Standard input is read once, however often it is named."""
    if target.path is None:
        if "stdin" not in cache:
            cache["stdin"] = sys.stdin.read()
        return cache["stdin"], ""
    try:
        return target.path.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"cannot read it: {type(exc).__name__}: {exc}"


def _default_packs(model: str) -> list[Path]:
    """Every pack in `rules/` that declares this fact model, in filename order.

    A script and a catalog are not reviewed by the same rules, so the default
    follows the facts rather than a fixed filename: a pack that declares
    ``applies_to: ["ddl"]`` is among the ones worth running over a script, and
    a pack that needs a catalog is not, because it would be refused and the
    refusal would look like a problem with the script.  A file that will not
    parse is included rather than skipped, so the loader gets to name it.
    """
    chosen = []
    for path in sorted(DEFAULT_RULE_DIR.glob("*.json")):
        try:
            declared = json.loads(path.read_text(encoding="utf-8")).get("applies_to")
        except (OSError, json.JSONDecodeError):
            declared = None
        if declared is None or model in declared:
            chosen.append(path)
    return chosen


def _worst(report: RuleReport) -> str:
    top = min(SEVERITY_RANK.get(f.severity, 9) for f in report.findings)
    return [name for name, rank in SEVERITY_RANK.items() if rank == top][0]


def _outcome(report: RuleReport) -> tuple[int, str]:
    """The exit code and the one line that belongs with it.

    Both come out of here so that a row in the summary and the verdict of a
    single file can never disagree.  Two means nothing was cleared: a pack that
    was never applied, a file nobody understood, or rules that between them
    found no row to judge.  A rule with an empty subject set has not passed
    anything, and saying so is the whole point of the third code.
    """
    if report.unapplied:
        return 2, f"not reviewed, {len(report.unapplied)} pack(s) not applied"
    if report.blind:
        return 2, "not reviewed, nothing in it was understood"
    if report.findings:
        return 1, f"{len(report.findings)} finding(s), worst {_worst(report)}"
    if not report.applied:
        return 2, "not reviewed, no rule was applied"
    if report.asserted == 0:
        return 2, f"nothing asserted, all {report.idle} rule(s) empty"
    return 0, f"clean, {report.asserted} of {len(report.applied)} rule(s) asserted"


def _read_line(report: RuleReport) -> str:
    if report.origin == "catalog":
        return (f"read   : {report.tables} table(s), {report.columns} column(s), "
                f"{report.indexes} index(es)")
    return (f"read   : {report.statements} statement(s) → {report.tables} table(s), "
            f"{report.columns} column(s), {report.changes} change action(s)")


def _show_tail(report: RuleReport) -> int:
    """Everything after the `script :` line, for one file.  Returns the exit code."""
    if report.unread and report.origin != "catalog":
        print(f"unread : {len(report.unread)} statement(s) no reader claimed")
    print(_read_line(report))

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
        print(f"  {len(report.findings)} finding(s), worst {_worst(report)} — the script does "
              f"not pass these rules.")
        print("  a person reads this and decides. This layer reviews; it never executes.")
        return 1
    if report.asserted == 0:
        print(f"  nothing was asserted. All {report.idle} rule(s) named an empty set of rows,")
        print("  so nothing about this file was checked. That is not a pass, and the exit")
        print("  code is 2 for that reason.")
        return 2
    if report.idle:
        print(f"  no rule fired. {report.asserted} of {len(report.applied)} rule(s) named at "
              f"least one row to assert on;")
        print(f"  the other {report.idle} named an empty set, which cleared nothing — see the "
              f"matched column above.")
    else:
        print(f"  no rule fired. Every one of the {report.asserted} rule(s) named a non-empty")
        print("  set of rows to assert on — read the matched column above to see what was checked.")
    return 0


def _show_summary(results: list[tuple[_Target, RuleReport | None, str]]) -> list[int]:
    rows = []
    for target, report, problem in results:
        if report is None:
            rows.append((target.label, "-", "-", "-", "-", problem))
            continue
        _code, line = _outcome(report)
        rows.append((target.label, str(report.statements), str(report.tables),
                     str(report.changes), str(len(report.findings)), line))
    head = ("file", "stmt", "tab", "chg", "find")
    widths = [max(len(row[i]) for row in rows + [head]) for i in range(len(head))]
    heading("what each file came to")
    print("  " + "  ".join(f"{head[i]:<{widths[i]}}" if i == 0 else f"{head[i]:>{widths[i]}}"
                           for i in range(len(head))) + "  result")
    for row in rows:
        cells = "  ".join(f"{row[i]:<{widths[i]}}" if i == 0 else f"{row[i]:>{widths[i]}}"
                          for i in range(len(head)))
        print(f"  {cells}  {row[-1]}")
    return [_outcome(report)[0] if report is not None else 2
            for _t, report, _p in results]


def _show_batch(results: list[tuple[_Target, RuleReport | None, str]]) -> int:
    codes = _show_summary(results)

    # Detail for the files a person has to act on.  A file that was only
    # notes — every rule empty, nothing to say — is already one row above, and
    # repeating its notes per file buries the findings that matter.
    for target, report, _problem in results:
        if report is None or not (report.findings or report.unapplied):
            continue
        heading(f"[{target.label}]")
        show_findings(report)
        if report.unapplied and report.notes:
            print("  notes")
            for note in report.notes:
                print(f"   · {note}")

    if 2 in codes:
        code = 2
    elif 1 in codes:
        code = 1
    else:
        code = 0
    clean = sum(1 for c in codes if c == 0)
    fired = sum(1 for c in codes if c == 1)
    unread = sum(1 for c in codes if c == 2)

    heading("[verdict]")
    print(f"  {len(results)} file(s): {clean} clean, {fired} with findings, "
          f"{unread} not reviewable")
    if code == 2:
        print("  exit code 2 — at least one file was not reviewed, or nothing in it")
        print("  was asserted on. Read its row above; that file has not passed.")
    elif code == 1:
        print("  exit code 1 — at least one rule fired. A person reads this and decides.")
    else:
        print("  exit code 0 — every file was read and no rule fired.")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schemafence.review",
        description="Review a script against rule packs. Deterministic: no model, "
                    "no network. With --db, reads a live catalog read-only.")
    parser.add_argument("--rules", action="append", default=None,
                        help="rule pack JSON (repeatable). Default: every pack whose "
                             "applies_to names the model these facts come from — ddl for a "
                             "script, catalog for --db")
    parser.add_argument("--script", "--ddl", dest="script", action="append", default=None,
                        metavar="PATH",
                        help="a script, or a folder of *.sql scripts (repeatable); - reads "
                             "standard input. `--ddl` is the older name for this flag")
    parser.add_argument("--db", default=None, metavar="DSN",
                        help="read the facts from a live PostgreSQL catalog, read-only. "
                             "With one script, the indexes that script creates are added on "
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

    paths = [Path(p) for p in args.rules] if args.rules \
        else _default_packs("catalog" if args.db else "ddl")
    if not paths:
        print(f"no pack in `{DEFAULT_RULE_DIR.name}/` declares the model `--db` supplies — "
              f"nothing to apply")
        return 2

    targets = _expand(args.script or [])
    if not targets and not args.verify and not args.db:
        parser.print_help()
        print("\nnothing to do: pass --script, --db, --verify or --vocabulary")
        return 2
    if args.db and len(targets) > 1:
        print(f"--db adds one script's indexes on top of the catalogue, so it takes one "
              f"script; {len(targets)} were given.")
        print("  review the batch without --db, or run the files one at a time.")
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
        if targets:
            text, problem = _text_of(targets[0], {})
            if problem:
                print(f"\n{problem}")
                return 2
            if text.strip():
                from .ddl import parse_ddl
                from .snapshot import apply_script
                applied = apply_script(schema, parse_ddl(text))
                print(f"script : {targets[0].label} → {applied.indexes} index(es) it creates "
                      f"were added to those facts")
                if applied.new_tables:
                    print(f"         {len(applied.new_tables)} table(s) it creates are not in "
                          f"the catalog, so their indexes were compared with nothing: "
                          + ", ".join(applied.new_tables))
            else:
                print(f"script : {targets[0].label} → no statements were read from it")
        return _show_tail(review_schema(packs, schema))

    results: list[tuple[_Target, RuleReport | None, str]] = []
    cache: dict = {}
    for target in targets:
        if target.problem:
            results.append((target, None, target.problem))
            continue
        text, problem = _text_of(target, cache)
        if problem:
            results.append((target, None, problem))
            continue
        results.append((target, review_ddl(packs, text), ""))

    if len(results) == 1 and results[0][1] is not None:
        print(f"script : {results[0][0].label}")
        return _show_tail(results[0][1])
    if len(results) == 1:
        print(f"\n{results[0][2]}")
        return 2
    return _show_batch(results)


if __name__ == "__main__":
    raise SystemExit(main())
