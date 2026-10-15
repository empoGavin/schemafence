"""The rule pack: a review rule as a file, not as a Python function.

Why this exists
---------------
Every review rule used to be a function in ``checks.py``.  Adding one meant
editing the engine, so the person who knew the rule and the person who could
ship it were never the same person.  A rule pack moves the rule out of the
code and into a file that can be reviewed, versioned and diffed.

What makes a rule *immediately* usable
--------------------------------------
A rule applies the moment it is written, because there is nothing between the
file and the verdict except this module.  That holds only while a rule is a
*declaration over facts the engine already reads*.  So the fact vocabulary is
closed and lives here, in one place, and ``vocabulary()`` prints it — that
export is what you hand to a model when you want a rule written.  The day a
rule needs a fact that is not in the list, the change is in the reader, not in
the file, and no amount of prompting helps.

The division of labour that follows from that:

  * authoring may use a model — describe the rule, supply examples
  * execution never does — ``run_rulepack`` is a pure function of
    ``(pack, Schema)``: no network, no model, no database, no clock
  * the file is the gate between them, and it is committed

An example is not a prompt
--------------------------
Each rule carries ``examples``: scripts that must fire it and scripts that must
stay silent.  ``load_rulepack`` runs them and refuses the file if any label is
wrong.  A rule that cannot reproduce its own examples does not load, so a model
cannot talk its way past the bar the hand-written checks are already held to —
one case that fires and one near miss that stays silent.

Zero findings is not a pass
---------------------------
Two ways this module refuses to look green:

  * a rule whose selector matches nothing says so, by name, in ``notes``
  * a script where no reader recognised anything sets ``blind``, and the CLI
    exits non-zero, because "0 finding(s)" printed over an unread file reads as
    approval and is the one output that must be impossible

That second one is R6 in ``docs/roadmap.md``, and it is why this layer came
after the change-action reader rather than before it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .checks import SEVERITY_RANK, Finding
from .ddl import CHANGE_KINDS, Schema, parse_ddl

PACK_VERSION = 1

TARGETS = ("table", "column", "change")

# The fact vocabulary.  A rule may name these and only these, which is what
# keeps "configurable" from turning into "a second, worse SQL dialect".
TABLE_FIELDS: dict[str, str] = {
    "name": "str",
    "schema": "str",
    "qualified": "str",
    "comment": "str|null",
    "column_count": "int",
    "primary_key_columns": "list[str]",
    "has_primary_key": "bool",
    "unique_key_count": "int",
    "commented_columns": "int",
    "uncommented_columns": "int",
    "created_by_change": "bool",     # a CREATE TABLE in this script
    "touched_by_change": "bool",     # named by at least one change action
    "change_count": "int",
}
COLUMN_FIELDS: dict[str, str] = {
    "name": "str",
    "table": "str",
    "qualified": "str",
    "type": "str",
    "base_type": "str",
    "nullable": "bool",
    "has_default": "bool",
    "default": "str|null",
    "references": "str|null",
    "comment": "str|null",
    "is_primary": "bool",
    "is_unique": "bool",
    "looks_like_key": "bool",
    "looks_like_money": "bool",
    "looks_like_time": "bool",
    "added_by_change": "bool",       # an ALTER TABLE ADD COLUMN in this script
    "added_by": "str|null",
}
CHANGE_FIELDS: dict[str, str] = {
    "kind": "str",
    "table": "str",
    "column": "str|null",
    "subject": "str",
    "detail": "str",
    "statement": "str",
    "position": "int",
}
FIELDS = {"table": TABLE_FIELDS, "column": COLUMN_FIELDS, "change": CHANGE_FIELDS}

# Longest suffix first: _matches_all must be read before _matches.
SELECT_OPS = ("_not_matches", "_matches_all", "_not_in", "_matches", "_present",
              "_absent", "_in")
ASSERT_OPS = ("present", "absent", "equals", "not_equals", "in", "not_in",
              "matches", "not_matches", "matches_all", "matches_none", "gte", "lte")

AUTHORING_CONTRACT = [
    "A rule pack is one JSON file. Nothing in it executes; it is read and applied.",
    "A rule names a target, selects rows, and says what those rows must satisfy.",
    "Findings are the selected rows that fail. That is the whole model.",
    "\"forbid\": true replaces the assertion: every selected row is a finding.",
    "Every rule carries examples. fires: at least one finding. silent: none.",
    "load_rulepack runs the examples and refuses the file if any label is wrong.",
    "Use only the fields and operators listed here. An unknown name is refused",
    "at load time with the field and the rule named, not ignored at runtime.",
    "Because the vocabulary is closed, a rule that needs a new field is a change",
    "to the reader, not to the file: say so instead of inventing a field name.",
]


class RulePackError(ValueError):
    """A rule pack that cannot be loaded, with the reason a person can act on."""


# --------------------------------------------------------------------------- #
# Facts — the rows a rule selects from
# --------------------------------------------------------------------------- #
def _change_counts(schema: Schema) -> tuple[dict[str, int], set[str]]:
    counts: dict[str, int] = {}
    touched: set[str] = set()
    for change in schema.changes:
        if not change.table:
            continue
        counts[change.table] = counts.get(change.table, 0) + 1
        touched.add(change.table)
    return counts, touched


def table_rows(schema: Schema) -> list[dict]:
    counts, touched = _change_counts(schema)
    rows = []
    for t in schema.tables:
        key = t.qualified.lower()
        rows.append({
            "name": t.name,
            "schema": t.schema,
            "qualified": key,
            "comment": t.comment,
            "column_count": len(t.columns),
            "primary_key_columns": list(t.primary_key),
            "has_primary_key": bool(t.primary_key),
            "unique_key_count": len(t.unique_keys),
            "commented_columns": t.commented_columns,
            "uncommented_columns": len(t.columns) - t.commented_columns,
            "created_by_change": t.created_by is not None,
            "touched_by_change": key in touched,
            "change_count": counts.get(key, 0),
        })
    return rows


def column_rows(schema: Schema) -> list[dict]:
    _, touched = _change_counts(schema)
    rows = []
    for t in schema.tables:
        key = t.qualified.lower()
        for c in t.columns:
            rows.append({
                "name": c.name,
                "table": key,
                "qualified": f"{key}.{c.name.lower()}",
                "type": c.type,
                "base_type": c.base_type,
                "nullable": c.nullable,
                "has_default": c.default is not None,
                "default": c.default,
                "references": c.references,
                "comment": c.comment,
                "is_primary": c.name.lower() in {k.lower() for k in t.primary_key},
                "is_unique": t.is_unique(c.name),
                "looks_like_key": c.looks_like_key,
                "looks_like_money": c.looks_like_money,
                "looks_like_time": c.looks_like_time,
                "added_by_change": c.added_by is not None,
                "added_by": c.added_by,
                "_touched": key in touched,
            })
    return rows


def change_rows(schema: Schema) -> list[dict]:
    return [{
        "kind": c.kind,
        "table": c.table,
        "column": c.column,
        "subject": c.subject,
        "detail": c.detail,
        "statement": c.statement,
        "position": c.position,
    } for c in schema.changes]


def facts(schema: Schema, target: str) -> list[dict]:
    return {"table": table_rows, "column": column_rows, "change": change_rows}[target](schema)


def row_subject(row: dict, target: str) -> str:
    return str(row.get("subject") or row.get("qualified") or row.get("name") or target)


# --------------------------------------------------------------------------- #
# The two evaluators — a selector, and an assertion over a selected row
# --------------------------------------------------------------------------- #
def _truthy(value) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str) or isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


def _as_text(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _split_key(key: str) -> tuple[str, str | None]:
    for op in SELECT_OPS:
        if key.endswith(op):
            return key[: -len(op)], op
    return key, None


def _selector_ok(key: str, value, fields: dict[str, str], where: str) -> None:
    name, op = _split_key(key)
    if name not in fields:
        raise RulePackError(f"{where}: `{key}` — no field named `{name}` for this target; "
                            f"known fields: {', '.join(sorted(fields))}")
    if op is None:
        if isinstance(value, list):
            raise RulePackError(f"{where}: `{key}` compares to a list — use `{name}_in`")
        return
    if op in ("_present", "_absent"):
        if not isinstance(value, bool):
            raise RulePackError(f"{where}: `{key}` takes true or false")
        return
    if op in ("_in", "_not_in"):
        if not isinstance(value, list):
            raise RulePackError(f"{where}: `{key}` takes a list")
        return
    patterns = value if op == "_matches_all" else [value]
    if not isinstance(patterns, list):
        raise RulePackError(f"{where}: `{key}` takes a list of patterns")
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise RulePackError(f"{where}: `{key}` takes a regex as text")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RulePackError(f"{where}: `{key}` is not a valid regex ({exc})") from exc


def row_matches(row: dict, select: dict, fields: dict[str, str], where: str) -> bool:
    for key, value in (select or {}).items():
        _selector_ok(key, value, fields, where)
        name, op = _split_key(key)
        actual = row.get(name)
        if op is None:
            if value is None:
                if actual is not None:
                    return False
            elif actual != value:
                return False
        elif op == "_present":
            if _truthy(actual) is not value:
                return False
        elif op == "_absent":
            if _truthy(actual) is value:
                return False
        elif op == "_in":
            if actual not in value:
                return False
        elif op == "_not_in":
            if actual in value:
                return False
        elif op == "_matches":
            if not re.search(value, _as_text(actual)):
                return False
        elif op == "_not_matches":
            if re.search(value, _as_text(actual)):
                return False
        elif op == "_matches_all":
            if not all(re.search(p, _as_text(actual)) for p in value):
                return False
    return True


def assertion_holds(assertion: dict, row: dict, where: str) -> bool:
    if not isinstance(assertion, dict):
        raise RulePackError(f"{where}: an assertion is an object with `field` and `op`")
    op = assertion.get("op")
    if op not in ASSERT_OPS:
        raise RulePackError(f"{where}: unknown assertion op `{op}`; known ops: "
                            f"{', '.join(ASSERT_OPS)}")
    field_name = assertion.get("field")
    if field_name not in row:
        raise RulePackError(f"{where}: assertion names `{field_name}`, which this target "
                            f"does not provide")
    actual = row.get(field_name)
    value = assertion.get("value")
    text = _as_text(actual)

    if op == "present":
        return _truthy(actual)
    if op == "absent":
        return not _truthy(actual)
    if op == "equals":
        return actual == value
    if op == "not_equals":
        return actual != value
    if op == "in":
        return actual in (value or [])
    if op == "not_in":
        return actual not in (value or [])
    if op == "matches":
        return bool(re.search(_as_text(value), text))
    if op == "not_matches":
        return not re.search(_as_text(value), text)
    if op == "matches_all":
        patterns = value or []
        return bool(patterns) and all(re.search(_as_text(p), text) for p in patterns)
    if op == "matches_none":
        return all(not re.search(_as_text(p), text) for p in (value or []))
    if op in ("gte", "lte"):
        try:
            left, right = float(actual), float(value)
        except (TypeError, ValueError):
            raise RulePackError(f"{where}: `{op}` needs numbers, got "
                                f"{actual!r} and {value!r}") from None
        return left >= right if op == "gte" else left <= right
    return False                                                     # pragma: no cover


# --------------------------------------------------------------------------- #
# Loading, and the example gate
# --------------------------------------------------------------------------- #
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RulePackError(message)


def validate_rulepack(pack: dict) -> dict:
    """Check a pack against the closed vocabulary.  Returns it unchanged."""
    _require(isinstance(pack, dict), "a rule pack is a JSON object")
    _require(pack.get("version") == PACK_VERSION,
             f"version must be {PACK_VERSION}, got {pack.get('version')!r}")
    _require(isinstance(pack.get("rulepack"), str) and pack["rulepack"],
             "the pack needs a `rulepack` name")
    applies = pack.get("applies_to", ["ddl"])
    _require(isinstance(applies, list) and applies, "`applies_to` is a non-empty list")
    for value in applies:
        _require(value in ("ddl", "schema"),
                 f"`applies_to: {value}` — the only fact model built is the DDL one "
                 f"(ddl, schema); a SQL-side rule needs a SQL fact model, which is "
                 f"not built yet")
    rules = pack.get("rules")
    _require(isinstance(rules, list) and rules, "`rules` is a non-empty list")

    seen: set[str] = set()
    for rule in rules:
        _require(isinstance(rule, dict), "every rule is an object")
        rid = rule.get("id")
        _require(isinstance(rid, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", rid or ""),
                 f"rule id `{rid}` — lowercase letters, digits and dashes")
        _require(rid not in seen, f"rule id `{rid}` appears twice")
        seen.add(rid)
        where = f"rule `{rid}`"

        _require(rule.get("severity") in ("high", "medium", "low"),
                 f"{where}: severity is high, medium or low")
        target = rule.get("target")
        _require(target in TARGETS, f"{where}: target is one of {', '.join(TARGETS)}")
        for key in ("title", "detail", "fix"):
            _require(isinstance(rule.get(key), str) and rule[key].strip(),
                     f"{where}: `{key}` is required")
        if target == "change":
            _require("ddl" in applies,
                     f"{where}: targets change actions, so the pack needs "
                     f"`applies_to: [\"ddl\"]`")

        select = rule.get("select", {})
        _require(isinstance(select, dict), f"{where}: `select` is an object")
        for key, value in select.items():
            _selector_ok(key, value, FIELDS[target], where)

        has_assert, forbids = "assert" in rule, bool(rule.get("forbid"))
        _require(has_assert != forbids,
                 f"{where}: give either `assert` or `forbid`, not both and not neither")
        if has_assert:
            _require(isinstance(rule["assert"], list) and rule["assert"],
                     f"{where}: `assert` is a non-empty list")
            probe = {name: "" for name in FIELDS[target]}
            for assertion in rule["assert"]:
                _require(isinstance(assertion, dict), f"{where}: an assertion is an object")
                field_name = assertion.get("field")
                _require(isinstance(field_name, str),
                         f"{where}: every assertion names a `field`")
                assertion_holds({"field": field_name, "op": "absent"}, probe, where)
        if target == "change" and not forbids:
            _require(False, f"{where}: a rule over change actions is written with "
                            f"`forbid` — an action list is a list of things not to do")

        examples = rule.get("examples")
        _require(isinstance(examples, dict), f"{where}: `examples` is required")
        for label in ("fires", "silent"):
            scripts = examples.get(label)
            _require(isinstance(scripts, list) and scripts,
                     f"{where}: `examples.{label}` is a non-empty list of DDL scripts")
            for script in scripts:
                _require(isinstance(script, str) and script.strip(),
                         f"{where}: `examples.{label}` holds DDL as text")
    return pack


def _run_one_rule(rule: dict, schema: Schema) -> tuple[list[Finding], list[dict], int]:
    target = rule["target"]
    rows = facts(schema, target)
    fields = FIELDS[target]
    where = f"rule `{rule['id']}`"
    findings: list[Finding] = []
    matched = 0

    for row in rows:
        if not row_matches(row, rule.get("select"), fields, where):
            continue
        matched += 1
        fails = bool(rule.get("forbid"))
        if not fails:
            fails = not all(assertion_holds(a, row, where) for a in rule["assert"])
        if fails:
            findings.append(Finding(
                check=rule["id"], severity=rule["severity"], title=rule["title"],
                subject=row_subject(row, target),
                detail=rule["detail"], fix=rule["fix"]))
    return findings, rows, matched


def verify_rulepack(pack: dict) -> tuple[list[tuple[str, str, bool, str]], bool]:
    """Run every rule's own examples.  (rows, all_passed); a row is one label."""
    validate_rulepack(pack)
    rows: list[tuple[str, str, bool, str]] = []
    all_passed = True
    for rule in pack["rules"]:
        for label, expect_findings in (("fires", True), ("silent", False)):
            for index, script in enumerate(rule["examples"][label], 1):
                schema = parse_ddl(script)
                findings, _rows, _matched = _run_one_rule(rule, schema)
                fired = bool(findings)
                passed = fired is expect_findings
                all_passed = all_passed and passed
                rows.append((f"{rule['id']}.{label}[{index}]",
                             f"{len(findings)} finding(s)", passed,
                             _excerpt(script) if passed else
                             f"expected {'a finding' if expect_findings else 'silence'}, "
                             f"got {len(findings)} finding(s)"))
    return rows, all_passed


def _excerpt(text: str, limit: int = 58) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def load_rulepack(source, verify: bool = True) -> dict:
    """Read a pack from a path, text or dict; verify its examples; return it."""
    if isinstance(source, dict):
        pack = source
    else:
        text = source if isinstance(source, str) and source.lstrip().startswith("{") \
            else Path(source).read_text(encoding="utf-8")
        try:
            pack = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RulePackError(f"not valid JSON: {exc}") from exc
    validate_rulepack(pack)
    if verify:
        rows, passed = verify_rulepack(pack)
        if not passed:
            bad = ", ".join(row[0] for row in rows if not row[2])
            raise RulePackError(
                f"`{pack['rulepack']}` failed its own examples: {bad} — a rule that "
                f"cannot reproduce every label in its file is not applied")
    return pack


def load_pack_file(path) -> dict:
    return load_rulepack(path, verify=True)


# --------------------------------------------------------------------------- #
# The review
# --------------------------------------------------------------------------- #
@dataclass
class RuleReport:
    pack_id: str = ""
    pack_version: int = PACK_VERSION
    packs: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)
    #        {id, title, target, matched, findings}
    blind: bool = False
    statements: int = 0
    tables: int = 0
    columns: int = 0
    changes: int = 0
    unread: list[tuple[int, str, str]] = field(default_factory=list)


def run_rulepack(pack: dict, schema: Schema) -> RuleReport:
    """Apply one pack to one parsed script.  Pure: no model, no network, no clock."""
    report = RuleReport(pack_id=pack["rulepack"], pack_version=pack["version"],
                        packs=[pack["rulepack"]])
    report.statements = schema.statements
    report.tables = len(schema.tables)
    report.columns = schema.column_count
    report.changes = len(schema.changes)
    report.unread = list(schema.unread)

    for position, head, why in schema.unread:
        report.notes.append(f"statement {position} was not read ({why}): {head}")

    partial = [t.qualified for t in schema.tables if t.created_by is None]
    if partial:
        report.notes.append(
            f"{len(partial)} table(s) are altered but not created here, so their "
            f"definitions are incomplete: {', '.join(sorted(partial))}. A rule aimed at a "
            f"whole table reads an incomplete row for these — scope it with "
            f"`created_by_change` if it needs the full definition")

    if not schema.recognised:
        report.blind = True
        report.notes.append(
            f"NOTHING RECOGNISED — {schema.statements} statement(s) read and none of "
            f"them understood. Zero findings over an unread file is not a pass")

    for rule in pack["rules"]:
        findings, rows, matched = _run_one_rule(rule, schema)
        report.findings.extend(findings)
        report.applied.append({"id": rule["id"], "title": rule["title"],
                               "target": rule["target"], "matched": matched,
                               "findings": len(findings)})
        if matched == 0:
            report.notes.append(
                f"rule `{rule['id']}` matched 0 of {len(rows)} {rule['target']} row(s) "
                f"— it asserted nothing here, which is not the same as a pass")

    seen, unique = set(), []
    for finding in report.findings:
        key = (finding.check, finding.subject)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.check, f.subject))
    report.findings = unique

    if "ddl" in pack.get("applies_to", []) and not schema.changes:
        report.notes.append(
            "no change actions in this file — it was read as a schema snapshot, so a "
            "rule over change actions had nothing to look at")
    return report


def merge_reports(reports: list[RuleReport]) -> RuleReport:
    """One verdict from several packs, which is how a review gate is used."""
    merged = RuleReport(packs=[r.pack_id for r in reports])
    seen_notes: set[str] = set()
    for report in reports:
        merged.pack_id = merged.pack_id or report.pack_id
        merged.findings.extend(report.findings)
        for note in report.notes:
            if note in seen_notes:      # two packs reading the same script see the same
                continue                # unparsed statement; say it once
            seen_notes.add(note)
            merged.notes.append(f"{report.pack_id}: {note}")
        merged.applied.extend(dict(row, pack=report.pack_id) for row in report.applied)
        merged.blind = merged.blind or report.blind
        merged.unread.extend(report.unread)
    merged.statements = reports[0].statements if reports else 0
    merged.tables = reports[0].tables if reports else 0
    merged.columns = reports[0].columns if reports else 0
    merged.changes = reports[0].changes if reports else 0
    seen, unique = set(), []
    for finding in merged.findings:
        key = (finding.check, finding.subject)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.check, f.subject))
    merged.findings = unique
    return merged


def review_ddl(packs: list[dict], text: str) -> RuleReport:
    """Parse a script once, apply every pack.  The whole review path."""
    schema = parse_ddl(text)
    return merge_reports([run_rulepack(pack, schema) for pack in packs])


def vocabulary() -> dict:
    """The closed vocabulary, as the prompt you hand to a model.

    A rule is generated from this and nothing else, so the export cannot drift
    from the evaluator: both are built from the same module.
    """
    return {
        "rulepack_version": PACK_VERSION,
        "targets": {target: FIELDS[target] for target in TARGETS},
        "selector": {
            "note": "every key in `select` must hold, so they are ANDed",
            "path": {
                "<field>": "equals. null means the value is null",
                "<field>_in": "the value is one of a list",
                "<field>_not_in": "the value is none of a list",
                "<field>_matches": "a regex, searched, not anchored",
                "<field>_not_matches": "the regex does not match",
                "<field>_matches_all": "every regex in the list matches",
                "<field>_present": "true / false — non-empty, not null",
                "<field>_absent": "the inverse of _present",
            },
        },
        "assertion_ops": list(ASSERT_OPS),
        "severities": ["high", "medium", "low"],
        "change_kinds": list(CHANGE_KINDS),
        "applies_to": ["ddl", "schema"],
        "rule_pack": {
            "rulepack": "a name, e.g. table-hygiene",
            "version": PACK_VERSION,
            "applies_to": ["ddl"],
            "rules": [{
                "id": "lowercase-with-dashes, unique in the file",
                "severity": "high | medium | low",
                "target": "table | column | change",
                "title": "one line, shown next to the row",
                "detail": "why it is wrong, in the words a DBA would use",
                "fix": "what to change",
                "select": {"<field>": "..."},
                "assert": [{"field": "<field>", "op": "present|matches|matches_all|...",
                            "value": "..."}],
                "forbid": "instead of `assert`: every selected row is a finding",
                "examples": {
                    "fires": ["a script that must produce at least one finding"],
                    "silent": ["a near miss that must produce none"],
                },
            }],
        },
        "authoring_contract": AUTHORING_CONTRACT,
    }


# --------------------------------------------------------------------------- #
# The selftest
# --------------------------------------------------------------------------- #
DEFAULT_RULE_DIR = Path(__file__).resolve().parents[1] / "rules"

_CLEAN_SCRIPT = """
CREATE TABLE shop.orders (
  id        BIGSERIAL PRIMARY KEY,
  paid_at   TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE shop.orders IS '订单主表 用途:支付与退款事实 在线';
COMMENT ON COLUMN shop.orders.id IS '主键 标准:TS-004';
COMMENT ON COLUMN shop.orders.paid_at IS '支付时间 标准:TS-004';
"""


def _pack(rules: list[dict], **extra) -> dict:
    return dict({"rulepack": "selftest", "version": PACK_VERSION,
                 "applies_to": ["ddl"], "rules": rules}, **extra)


def _specimen(*, target="table", select=None, forbid=None, fires, silent, rid="t") -> dict:
    rule = {"id": rid, "severity": "high", "target": target, "title": "t",
            "detail": "d", "fix": "f",
            "examples": {"fires": fires, "silent": silent}}
    if select is not None:
        rule["select"] = select
    if forbid:
        rule["forbid"] = True
    else:
        rule["assert"] = [{"field": "comment", "op": "present"}]
    return rule


def run_rulepack_selftest() -> tuple[list[tuple[str, str, bool, str]], bool]:
    """Offline cases for the rule pack layer.  (rows, all_passed)."""
    rows: list[tuple[str, str, bool, str]] = []
    all_passed = True

    def case(label: str, expect: str, run) -> None:
        nonlocal all_passed
        try:
            got, passed_flag = run()
        except Exception as exc:                      # noqa: BLE001 - a case, not a crash
            rows.append((label, "raised", False, f"{type(exc).__name__}: {exc}"))
            all_passed = False
            return
        rows.append((label, expect, passed_flag, got))
        all_passed = all_passed and passed_flag

    def shipped():
        packs = sorted(DEFAULT_RULE_DIR.glob("*.json"))
        ids = []
        for path in packs:
            pack = load_rulepack(path, verify=True)
            ids.append(f"{path.name}:{len(pack['rules'])}")
        return " ".join(ids), bool(packs)

    case("the shipped packs load, and every one of their examples holds",
         "2 packs", shipped)

    def refused_when_a_silent_example_fires():
        pack = _pack([_specimen(fires=["CREATE TABLE t (a INT);"],
                                silent=["CREATE TABLE t (a INT);"])])
        try:
            load_rulepack(pack, verify=True)
        except RulePackError:
            return "refused", True
        return "loaded", False

    case("a pack whose `silent` example fires is refused", "refused",
         refused_when_a_silent_example_fires)

    def refused_when_a_fires_example_is_silent():
        pack = _pack([_specimen(fires=['COMMENT ON TABLE t IS \'x\';'],
                                silent=["CREATE TABLE t (a INT);"])])
        try:
            load_rulepack(pack, verify=True)
        except RulePackError:
            return "refused", True
        return "loaded", False

    case("a pack whose `fires` example stays silent is refused", "refused",
         refused_when_a_fires_example_is_silent)

    def refused_on_unknown_field():
        try:
            load_rulepack(_pack([_specimen(select={"cmment": "x"},
                                           fires=["CREATE TABLE t (a INT);"],
                                           silent=["CREATE TABLE t (a INT);"])]))
        except RulePackError as exc:
            return str(exc)[:44], "no field named `cmment`" in str(exc)
        return "loaded", False

    case("a misspelled field is refused at load, by name", "no field named",
         refused_on_unknown_field)

    def refused_on_sql_fact_model():
        try:
            load_rulepack(_pack([_specimen(fires=["CREATE TABLE t (a INT);"],
                                           silent=["CREATE TABLE t (a INT);"])],
                                applies_to=["sql"]))
        except RulePackError as exc:
            return "refused", "not built yet" in str(exc)
        return "loaded", False

    case("a pack that asks for the SQL fact model is refused, and says why",
         "refused", refused_on_sql_fact_model)

    def refused_on_duplicate_id():
        try:
            load_rulepack(_pack([
                _specimen(rid="same", fires=["CREATE TABLE t (a INT);"],
                          silent=["CREATE TABLE t (a INT);"
                                  "COMMENT ON TABLE t IS 'x';"]),
                _specimen(rid="same", fires=["CREATE TABLE t (a INT);"],
                          silent=["CREATE TABLE t (a INT);"
                                  "COMMENT ON TABLE t IS 'x';"]),
            ]))
        except RulePackError as exc:
            return "refused", "twice" in str(exc)
        return "loaded", False

    case("a duplicate rule id is refused", "refused", refused_on_duplicate_id)

    def clean_script_is_quiet():
        pack = load_rulepack(_pack([
            {"id": "table-needs-comment", "severity": "medium", "target": "table",
             "title": "no table comment", "detail": "d", "fix": "f",
             "assert": [{"field": "comment", "op": "present"}],
             "examples": {"fires": ["CREATE TABLE t (a INT);"],
                          "silent": ["CREATE TABLE t (a INT);"
                                     "COMMENT ON TABLE t IS 'x';"]}},
            {"id": "column-needs-comment", "severity": "low", "target": "column",
             "title": "no column comment", "detail": "d", "fix": "f",
             "assert": [{"field": "comment", "op": "present"}],
             "examples": {"fires": ["CREATE TABLE t (a INT);"],
                          "silent": ["CREATE TABLE t (a INT);"
                                     "COMMENT ON COLUMN t.a IS 'x';"]}},
        ]))
        report = review_ddl([pack], _CLEAN_SCRIPT)
        return f"{len(report.findings)} finding(s)", not report.findings

    case("a script that satisfies every rule reports nothing", "0 finding(s)",
         clean_script_is_quiet)

    def a_rule_that_matched_nothing_says_so():
        pack = load_rulepack(_pack([
            {"id": "only-archived-tables", "severity": "low", "target": "table",
             "title": "archived table without a comment", "detail": "d", "fix": "f",
             "select": {"name_matches": "_archive$"},
             "assert": [{"field": "comment", "op": "present"}],
             "examples": {"fires": ["CREATE TABLE t_archive (a INT);"],
                          "silent": ["CREATE TABLE t_archive (a INT);"
                                     "COMMENT ON TABLE t_archive IS 'x';"]}},
        ]))
        report = review_ddl([pack], "CREATE TABLE shop.orders (id INT);")
        said = any("matched 0 of" in n for n in report.notes)
        return (f"{len(report.findings)} finding(s), noted={said}",
                not report.findings and said)

    case("a rule whose selector matched nothing says so instead of passing",
         "noted", a_rule_that_matched_nothing_says_so)

    def an_unread_file_is_blind():
        pack = load_rulepack(_pack([
            {"id": "table-needs-comment", "severity": "medium", "target": "table",
             "title": "no table comment", "detail": "d", "fix": "f",
             "assert": [{"field": "comment", "op": "present"}],
             "examples": {"fires": ["CREATE TABLE t (a INT);"],
                          "silent": ["CREATE TABLE t (a INT);"
                                     "COMMENT ON TABLE t IS 'x';"]}},
        ]))
        report = review_ddl([pack], "\\copy shop.orders from 'x.csv'\nVACUUM FULL;\n")
        return (f"blind={report.blind} unread={len(report.unread)}",
                report.blind and not report.findings and bool(report.unread))

    case("a script no reader recognised is marked blind, not clean", "blind=True",
         an_unread_file_is_blind)

    def forbid_fires_on_a_drop():
        pack = load_rulepack(_pack([
            {"id": "no-drop-column", "severity": "high", "target": "change",
             "title": "a column is dropped", "detail": "d", "fix": "f",
             "forbid": True, "select": {"kind": "drop_column"},
             "examples": {"fires": ["ALTER TABLE t DROP COLUMN a;"],
                          "silent": ["ALTER TABLE t ADD COLUMN a INT;"]}},
        ]))
        report = review_ddl([pack], "ALTER TABLE shop.orders DROP COLUMN channel;")
        return (f"{len(report.findings)} finding(s)",
                len(report.findings) == 1
                and report.findings[0].subject == "shop.orders.channel")

    case("a forbid rule over change actions fires on the action, not the schema",
         "1 finding(s)", forbid_fires_on_a_drop)

    def a_change_script_is_read_as_actions():
        schema = parse_ddl("ALTER TABLE shop.orders ADD COLUMN channel TEXT;")
        return (f"{len(schema.changes)} change(s), {len(schema.tables)} table(s)",
                len(schema.changes) == 1 and len(schema.tables) == 1
                and schema.tables[0].created_by is None)

    case("an ALTER-only script reads as one change, not as zero tables",
         "1 change(s)", a_change_script_is_read_as_actions)

    def matches_all_needs_every_pattern():
        pack = load_rulepack(_pack([
            {"id": "comment-states-role", "severity": "medium", "target": "table",
             "title": "comment does not state the role", "detail": "d", "fix": "f",
             "select": {"comment_present": True},
             "assert": [{"field": "comment", "op": "matches_all",
                         "value": ["用途", "(在线|归档)"]}],
             "examples": {
                 "fires": ["CREATE TABLE t (a INT);"
                           "COMMENT ON TABLE t IS '订单表 用途:事实表';"],
                 "silent": ["CREATE TABLE t (a INT);"
                            "COMMENT ON TABLE t IS '订单表 用途:事实表 在线';"]}},
        ]))
        report = review_ddl([pack], "CREATE TABLE t (a INT);"
                                    "COMMENT ON TABLE t IS '订单表 在线';")
        return (f"{len(report.findings)} finding(s)", len(report.findings) == 1)

    case("`matches_all` requires every pattern, so a partial comment fires",
         "1 finding(s)", matches_all_needs_every_pattern)

    def vocabulary_export_is_consistent():
        vocab = vocabulary()
        return (f"{len(vocab['targets'])} targets, {len(vocab['assertion_ops'])} ops",
                set(vocab["targets"]) == set(TARGETS)
                and set(vocab["targets"]["table"]) == set(TABLE_FIELDS))

    case("the exported vocabulary matches the evaluator it is exported from",
         "3 targets", vocabulary_export_is_consistent)

    return rows, all_passed
