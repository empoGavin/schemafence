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
from .ddl import Schema, parse_ddl
from .facts import (CHANGE_KINDS, FACT_MODELS, FIELDS, TARGETS, is_built,
                    models_for, rows_for, subject_of)

PACK_VERSION = 1

# The vocabulary is not defined here.  It is whatever the registered fact
# providers offer (see ``facts.py``), so the engine and the export cannot
# disagree about what a rule may name.

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
    "`applies_to` says which facts the pack may be applied to. A pack that asks",
    "for the catalogue is refused over a script, because a script knows only the",
    "indexes it creates and the catalogue knows the ones already on the table.",
    "In title, detail and fix, {field} is replaced by that field on the row that",
    "failed — use it to name the other row, as {duplicate_of} does.",
    "Read this and write the rule, or write only the title, the examples and the",
    "severity and let the evaluator search for the facts that fit: --infer does",
    "that offline, and both routes end at load_rulepack's example gate.",
]

# `{duplicate_of}`, not `{anything}`: a detail that quotes a JSON fragment keeps
# working, and a misspelled field is still refused by name.
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")



class RulePackError(ValueError):
    """A rule pack that cannot be loaded, with the reason a person can act on."""


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


def _render(text: str, row: dict) -> str:
    """Fill ``{field}`` from the row the rule fired on.

    "This index is a duplicate" is a finding somebody has to go and chase; the
    same sentence with the name of the other index is one they can act on.  A
    field with no value renders as nothing, and an unknown name never reaches
    here — ``validate_rulepack`` refused it by name at load.
    """
    def value(match: re.Match) -> str:
        found = row.get(match.group(1))
        if isinstance(found, (list, tuple)):
            return ", ".join(_as_text(item) for item in found)
        return _as_text(found)

    return PLACEHOLDER.sub(value, text)


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
        _require(value in FACT_MODELS,
                 f"`applies_to: {value}` — no such fact model; known: "
                 f"{', '.join(FACT_MODELS)}")
        _require(is_built(value), f"`applies_to: {value}` — {FACT_MODELS[value]}")
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
        _require(target in TARGETS(),
                 f"{where}: target is one of {', '.join(TARGETS())}")
        for key in ("title", "detail", "fix"):
            _require(isinstance(rule.get(key), str) and rule[key].strip(),
                     f"{where}: `{key}` is required")
            for name in PLACEHOLDER.findall(rule[key]):
                _require(name in FIELDS()[target],
                         f"{where}: `{key}` fills {{{name}}} from the row, but this "
                         f"target does not provide a field called `{name}`")
        offered = set(models_for(target))
        _require(bool(offered & set(applies)),
                 f"{where}: target `{target}` is read from "
                 f"{', '.join(sorted(offered))}, but the pack declares "
                 f"applies_to {applies}")

        select = rule.get("select", {})
        _require(isinstance(select, dict), f"{where}: `select` is an object")
        for key, value in select.items():
            _selector_ok(key, value, FIELDS()[target], where)
        if target == "change":
            # A kind the reader never produces selects no row at all, and a rule
            # that selects no row is silent — which reads exactly like a pass.
            # Refusing the pack by name is the only way that stays visible.
            named: list[str] = []
            if "kind" in select:
                named.append(select["kind"])
            for key in ("kind_in", "kind_not_in"):
                named += list(select.get(key) or [])
            unknown = [kind for kind in named if kind not in CHANGE_KINDS]
            _require(not unknown,
                     f"{where}: `select` names change kind {unknown}, which the reader "
                     f"never produces; known kinds: {', '.join(CHANGE_KINDS)}")

        has_assert, forbids = "assert" in rule, bool(rule.get("forbid"))
        _require(has_assert != forbids,
                 f"{where}: give either `assert` or `forbid`, not both and not neither")
        if has_assert:
            _require(isinstance(rule["assert"], list) and rule["assert"],
                     f"{where}: `assert` is a non-empty list")
            probe = {name: "" for name in FIELDS()[target]}
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

    exact = find_overlaps(pack)["exact"]
    _require(not exact, f"`{pack['rulepack']}` has a rule written twice: "
                        + "; ".join(exact))
    return pack


def _run_one_rule(rule: dict, schema: Schema) -> tuple[list[Finding], list[dict], int]:
    target = rule["target"]
    rows = rows_for(target, schema)
    fields = FIELDS()[target]
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
                check=rule["id"], severity=rule["severity"],
                title=_render(rule["title"], row),
                subject=subject_of(target, row),
                detail=_render(rule["detail"], row), fix=_render(rule["fix"], row)))
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
# Overlap — the same rule twice, one rule inside another, two rules that disagree
# --------------------------------------------------------------------------- #
def rule_signature(rule: dict) -> str:
    """What a rule means, with its wording removed.

    A pack grows by copying a rule and editing it.  Two rules with one
    signature are one rule, and applying both reports every subject twice, so
    this is what catches the copy nobody finished editing.
    """
    select = json.dumps(rule.get("select") or {}, sort_keys=True, ensure_ascii=False)
    if rule.get("forbid"):
        return f"{rule['target']} | {select} | forbid"
    assertions = sorted(json.dumps(a, sort_keys=True, ensure_ascii=False)
                        for a in rule.get("assert", []))
    return f"{rule['target']} | {select} | {' & '.join(assertions)}"


def _same_assertions(one: dict, other: dict) -> bool:
    def canon(rule):
        return sorted(json.dumps(a, sort_keys=True, ensure_ascii=False)
                      for a in rule.get("assert", []))
    return canon(one) == canon(other)


def _select_is_subset(one: dict, other: dict) -> bool:
    """True when every constraint in `one` is also in `other`.

    Selection keys are ANDed, so the rule with fewer constraints selects more
    rows and reports more findings.  The narrower rule can only add wording.
    """
    one, other = one or {}, other or {}
    return all(k in other and other[k] == v for k, v in one.items())


def _disagreement(one: dict, other: dict) -> str | None:
    """The first pair of assertions that cannot both hold on one row."""
    for x in one.get("assert", []):
        for y in other.get("assert", []):
            if x.get("field") != y.get("field"):
                continue
            pair = (x.get("op"), y.get("op"))
            if pair in (("present", "absent"), ("absent", "present")):
                return f"`{x['field']}` is required present by one and absent by the other"
            if pair in (("matches", "not_matches"), ("not_matches", "matches")) \
                    and x.get("value") == y.get("value"):
                return f"`{x['field']}` must match and must not match {x.get('value')!r}"
    return None


def find_overlaps(pack: dict) -> dict[str, list[str]]:
    """Rules that repeat, contain, or contradict one another.

    Returned rather than raised, because two of the three are worth reading
    rather than worth refusing: a rule may legitimately sit inside a wider one
    when its wording is the point.  `load_rulepack` refuses only `exact`.
    """
    rules = pack["rules"]
    exact: list[str] = []
    redundant: list[str] = []
    conflicting: list[str] = []
    owner: dict[str, str] = {}

    for rule in rules:
        signature = rule_signature(rule)
        if signature in owner:
            exact.append(f"`{rule['id']}` says what `{owner[signature]}` already says "
                         f"(same target, same selection, same assertion) — applying both "
                         f"would report every subject twice")
        else:
            owner[signature] = rule["id"]

    for index, one in enumerate(rules):
        for other in rules[index + 1:]:
            if one.get("target") != other.get("target"):
                continue
            if one.get("forbid") or other.get("forbid"):
                continue
            select_one, select_other = one.get("select") or {}, other.get("select") or {}
            if select_one != select_other and _same_assertions(one, other):
                if _select_is_subset(select_one, select_other):
                    redundant.append(f"`{other['id']}` sits inside `{one['id']}` — every "
                                     f"row it selects is selected by `{one['id']}` too, "
                                     f"with the same assertion, so it adds wording only")
                elif _select_is_subset(select_other, select_one):
                    redundant.append(f"`{one['id']}` sits inside `{other['id']}` — every "
                                     f"row it selects is selected by `{other['id']}` too, "
                                     f"with the same assertion, so it adds wording only")
            if select_one == select_other:
                why = _disagreement(one, other)
                if why:
                    conflicting.append(f"`{one['id']}` and `{other['id']}` select the same "
                                       f"rows and disagree: {why}")
    return {"exact": exact, "redundant": redundant, "conflicting": conflicting}


def cross_pack_notes(packs: list[dict]) -> list[str]:
    """Two packs that define one rule id, or one rule.

    Applied together they are a single review, and a finding carries only the
    rule id, so an id used twice makes two different rules indistinguishable
    in the output.
    """
    notes: list[str] = []
    id_owner: dict[str, str] = {}
    signature_owner: dict[str, tuple[str, str]] = {}
    for pack in packs:
        name = pack["rulepack"]
        for rule in pack["rules"]:
            if rule["id"] in id_owner and id_owner[rule["id"]] != name:
                notes.append(f"rule id `{rule['id']}` is defined by both "
                             f"`{id_owner[rule['id']]}` and `{name}` — a finding names "
                             f"only the id, so the two cannot be told apart")
            else:
                id_owner.setdefault(rule["id"], name)
            signature = rule_signature(rule)
            if signature in signature_owner and signature_owner[signature][0] != name:
                other_pack, other_id = signature_owner[signature]
                notes.append(f"`{name}.{rule['id']}` says what "
                             f"`{other_pack}.{other_id}` says — the same rule is in two "
                             f"packs, so a subject would be reported twice")
            else:
                signature_owner.setdefault(signature, (name, rule["id"]))
    return notes


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
    unapplied: list[str] = field(default_factory=list)
    #           packs whose declared fact model is not the one these facts came
    #           from.  Not the same as blind: the file was read fine, the rules
    #           were not applied to it, and neither is a pass.
    statements: int = 0
    tables: int = 0
    columns: int = 0
    changes: int = 0
    indexes: int = 0
    origin: str = "ddl"
    unread: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def asserted(self) -> int:
        """Rules that found at least one row to judge.

        A rule that matched no row asserted nothing about the script, and an
        empty set is not a pass.  The count is here rather than in the printer
        because the verdict and the exit code both depend on it, and they must
        agree with each other.
        """
        return sum(1 for row in self.applied if row["matched"])

    @property
    def idle(self) -> int:
        """Rules that named an empty set of rows, so nothing was checked."""
        return len(self.applied) - self.asserted


# What to do when a pack asks for facts this run did not read.  A refusal that
# does not say which command would answer it is just an error message.
ORIGIN_ADVICE = {
    "catalog": ("these facts came from the catalog, which has no statement list — a "
                "rule over change actions has nothing to read here"),
    "ddl": ("these facts came from a script, which knows only the statements it "
            "contains. What a table already holds is in the catalog: read it with "
            "`--db <dsn>`"),
    "schema": ("these facts are a schema on its own, with no statement list"),
}


def run_rulepack(pack: dict, schema: Schema) -> RuleReport:
    """Apply one pack to one parsed script.  Pure: no model, no network, no clock."""
    report = RuleReport(pack_id=pack["rulepack"], pack_version=pack["version"],
                        packs=[pack["rulepack"]], origin=schema.origin)
    models = pack.get("applies_to", ["ddl"])
    if schema.origin not in models:
        report.unapplied.append(pack["rulepack"])
        report.notes.append(
            f"`{pack['rulepack']}` WAS NOT APPLIED — it declares applies_to "
            f"{models} and these facts were read from `{schema.origin}`: "
            + ORIGIN_ADVICE.get(schema.origin, ""))
        return report

    report.statements = schema.statements
    report.tables = len(schema.tables)
    report.columns = schema.column_count
    report.changes = len(schema.changes)
    report.indexes = sum(len(t.indexes) for t in schema.tables)
    report.unread = list(schema.unread)

    for position, head, why in schema.unread:
        report.notes.append(f"statement {position} was not read ({why}): {head}")

    overlaps = find_overlaps(pack)
    report.notes.extend(overlaps["redundant"])
    report.notes.extend(overlaps["conflicting"])

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
        merged.unapplied.extend(report.unapplied)
        merged.unread.extend(report.unread)
    # The statement and table counts are not summed here: they describe the
    # facts, not the packs applied to them, and only the caller that read the
    # facts knows them.  ``review_schema`` fills them in.
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


def review_schema(packs: list[dict], schema: Schema) -> RuleReport:
    """Apply every pack to one already-read Schema.

    The half ``review_ddl`` reuses.  A caller that read the facts itself — from
    a live catalogue, say — has no reason to re-parse anything.

    The counts come from the schema rather than from the reports, so a pack
    that was refused for asking the wrong fact model does not make a file that
    was read look like a file that was empty.
    """
    merged = merge_reports([run_rulepack(pack, schema) for pack in packs])
    merged.statements = schema.statements
    merged.tables = len(schema.tables)
    merged.columns = schema.column_count
    merged.changes = len(schema.changes)
    merged.indexes = sum(len(t.indexes) for t in schema.tables)
    merged.origin = schema.origin
    merged.notes.extend(cross_pack_notes(packs))
    return merged


def review_ddl(packs: list[dict], text: str) -> RuleReport:
    """Parse a script once, apply every pack.  The whole review path."""
    return review_schema(packs, parse_ddl(text))


def vocabulary() -> dict:
    """The closed vocabulary, as the prompt you hand to a model.

    A rule is generated from this and nothing else, so the export cannot drift
    from the evaluator: both are built from the same module.
    """
    return {
        "rulepack_version": PACK_VERSION,
        "targets": FIELDS(),
        "fact_models": {m: (r or "built") for m, r in FACT_MODELS.items()},
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
        "applies_to": [model for model, reason in FACT_MODELS.items() if reason is None],
        "finding_text": {
            "note": "title, detail and fix are shown with the row that failed",
            "path": {"{field}": "that field on the failing row, e.g. {duplicate_of}"},
        },
        "rule_pack": {
            "rulepack": "a name, e.g. table-hygiene",
            "version": PACK_VERSION,
            "applies_to": ["ddl", "catalog — which facts this pack may be applied to; "
                           "a pack is refused, not silently applied, over another"],
            "rules": [{
                "id": "lowercase-with-dashes, unique in the file",
                "severity": "high | medium | low",
                "target": " | ".join(TARGETS()),
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
# Inference — the rules that would separate the examples you supplied
# --------------------------------------------------------------------------- #
def _example_rows(target: str, script: str) -> list[dict]:
    return rows_for(target, parse_ddl(script))


def _candidate_assertions(target: str, examples: list[list[dict]],
                          hints: dict | None = None) -> list[dict]:
    """Every assertion the closed vocabulary can make, for one target.

    Nothing here invents a value: ``present`` and ``absent`` need none, the
    numeric bounds are the values your own examples contain, and a pattern only
    appears if you supplied it under ``hints.matches``.  A synthesised regex
    would be a guess dressed as a rule.
    """
    out: list[dict] = []
    for name, kind in FIELDS()[target].items():
        out.append({"field": name, "op": "present"})
        out.append({"field": name, "op": "absent"})
        if kind == "int":
            values = sorted({row.get(name) for rows in examples for row in rows
                             if isinstance(row.get(name), int)})
            for value in values:
                out.append({"field": name, "op": "gte", "value": value})
                out.append({"field": name, "op": "lte", "value": value})
    for field_name, pattern in ((hints or {}).get("matches") or {}).items():
        out.append({"field": field_name, "op": "matches", "value": pattern})
        out.append({"field": field_name, "op": "not_matches", "value": pattern})
    return out


def _candidate_selections(target: str) -> list[dict]:
    out: list[dict] = [{}]
    for name, kind in FIELDS()[target].items():
        if kind == "bool":
            out.append({name: True})
            out.append({name: False})
        else:
            out.append({f"{name}_present": True})
            out.append({f"{name}_absent": True})
    return out


def _fires_on(target: str, rows: list[dict], select: dict, assertion: dict) -> int:
    where = "inference"
    return sum(1 for row in rows
               if row_matches(row, select, FIELDS()[target], where)
               and not assertion_holds(assertion, row, where))


def _rank(target: str, select: dict, assertion: dict) -> tuple:
    """Prefer the plainest rule that fits.

    Plainest first means no selector, then a boolean fact over a number, then
    the earliest operator — with one correction: an operator is scored against
    the field it is applied to before it is scored by position.  ``absent`` on
    a number is a way of writing "zero", and a rule that says
    ``duplicate_count absent`` is a rule somebody has to read twice.
    """
    kind = FIELDS()[target].get(assertion["field"], "str")
    op = assertion["op"]
    fits = op in ("lte", "gte", "equals", "not_equals") if kind == "int" \
        else op in ("present", "absent")
    return (len(select),
            0 if fits else 1,
            0 if kind == "bool" else (1 if kind == "int" else 2),
            ASSERT_OPS.index(op),
            assertion["field"])


def rule_from_examples(request: dict, select: dict, assertion: dict) -> dict:
    """A loadable rule: the description you wrote, the facts that fit, your examples."""
    rule = {
        "id": request.get("id") or "unnamed-rule",
        "severity": request.get("severity", "medium"),
        "target": request["target"],
        "title": request.get("title", ""),
        "detail": request.get("detail", ""),
        "fix": request.get("fix", ""),
        "assert": [assertion],
        "examples": {"fires": list(request["examples"]["fires"]),
                     "silent": list(request["examples"]["silent"])},
    }
    if select:
        rule["select"] = select
    return rule


def infer_rule(request: dict, limit: int = 5) -> dict:
    """The rules that would separate the examples you supplied.

    The evaluator used backwards: enumerate the closed vocabulary, keep what
    fits every label, and report what nearly fits when nothing does.  No model
    runs here, and that is the point — a rule you can generate *and* apply is
    one this evaluator can already express.

    A candidate is offered only when it fires on every ``fires`` example and
    stays silent on every ``silent`` one.  That is the bar ``load_rulepack``
    will hold it to anyway, so anything returned here loads as it stands: the
    examples travel with the rule.

    When nothing fits, the near misses matter more than the answer.  They name
    the examples the closest rule cannot separate, which is how "this needs a
    fact the reader does not collect yet" gets said out loud instead of
    arriving as a rule that quietly never fires.
    """
    target = request.get("target")
    if target not in TARGETS():
        return {"error": f"target `{target}` — choose one of {', '.join(TARGETS())}"}
    models = request.get("applies_to") or ["ddl"]
    if not isinstance(models, list) or not models:
        return {"error": "`applies_to` is a non-empty list of fact models"}
    for model in models:
        if model not in FACT_MODELS:
            return {"error": f"`applies_to: {model}` — no such fact model; known: "
                             f"{', '.join(FACT_MODELS)}"}
        if not is_built(model):
            return {"error": f"`applies_to: {model}` — {FACT_MODELS[model]}"}
    examples = request.get("examples") or {}
    for label in ("fires", "silent"):
        scripts = examples.get(label)
        if not isinstance(scripts, list) or not scripts:
            return {"error": f"`examples.{label}` is a non-empty list of DDL scripts"}
        for script in scripts:
            if not isinstance(script, str) or not script.strip():
                return {"error": f"`examples.{label}` holds DDL as text"}

    parsed = {label: [_example_rows(target, script) for script in examples[label]]
              for label in ("fires", "silent")}
    every = parsed["fires"] + parsed["silent"]
    assertions = _candidate_assertions(target, every, request.get("hints"))
    selections = _candidate_selections(target)

    fitting, near = [], []
    for select in selections:
        for assertion in assertions:
            got = [label for label, want_fire in (("fires", True), ("silent", False))
                   for rows in parsed[label]
                   if (_fires_on(target, rows, select, assertion) > 0) is not want_fire]
            wrong = len(got)
            total = len(every)
            if wrong == 0:
                fitting.append((_rank(target, select, assertion), select, assertion))
            else:
                near.append((wrong / total, _rank(target, select, assertion),
                             select, assertion, total - wrong, total))

    fitting.sort(key=lambda item: item[0])
    candidates = [rule_from_examples(request, select, assertion)
                  for _key, select, assertion in fitting[:limit]]

    near.sort(key=lambda item: (item[0], item[1]))
    misses = [{"select": select, "assert": [assertion],
               "separated": f"{right} of {total} example(s)",
               "wrong": wrong} for _r, _k, select, assertion, right, total, wrong in
              [(n[0], n[1], n[2], n[3], n[4], n[5], n[5] - n[4]) for n in near[:limit]]]

    result: dict = {
        "target": target,
        "applies_to": models,
        "examples": {label: len(examples[label]) for label in ("fires", "silent")},
        "candidates": candidates,
        "near_misses": misses,
        "searched": len(selections) * len(assertions),
    }
    if not candidates:
        result["note"] = (
            "the examples the near misses cannot separate are the ones that need a "
            "fact the reader does not collect yet. That is a change to the fact "
            "provider, not to the rule file.")
    return result


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


def _specimen(*, target="table", select=None, forbid=None, fires, silent, rid="t",
              detail="d") -> dict:
    rule = {"id": rid, "severity": "high", "target": target, "title": "t",
            "detail": detail, "fix": "f",
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
         "3 packs", shipped)

    def refused_on_unknown_placeholder():
        try:
            load_rulepack(_pack([_specimen(fires=["CREATE TABLE t (a INT);"],
                                           silent=["CREATE TABLE t (a INT);"],
                                           detail="says {nope}")]))
        except RulePackError as exc:
            return str(exc)[:46], "does not provide a field called `nope`" in str(exc)
        return "loaded", False

    case("a `{field}` that this target does not provide is refused", "no field named",
         refused_on_unknown_placeholder)

    def placeholder_is_filled_from_the_row():
        pack = load_rulepack(_pack([
            {"id": "column-needs-comment", "severity": "low", "target": "column",
             "title": "no column comment", "detail": "{table}.{name} has none",
             "fix": "f",
             "assert": [{"field": "comment", "op": "present"}],
             "examples": {"fires": ["CREATE TABLE t (a INT);"],
                          "silent": ["CREATE TABLE t (a INT);"
                                     "COMMENT ON COLUMN t.a IS 'x';"]}},
        ]))
        report = review_ddl([pack], "CREATE TABLE t (a INT);")
        return (report.findings[0].detail if report.findings else "(none)",
                len(report.findings) == 1
                and report.findings[0].detail == "t.a has none")

    case("a finding fills `{field}` from the row it fired on", "t.a has none",
         placeholder_is_filled_from_the_row)

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
        # not "does the message contain a particular phrase" but "does the
        # refusal carry the reason registered for that model, verbatim"
        reason = FACT_MODELS["sql"]
        try:
            load_rulepack(_pack([_specimen(fires=["CREATE TABLE t (a INT);"],
                                           silent=["CREATE TABLE t (a INT);"])],
                                applies_to=["sql"]))
        except RulePackError as exc:
            return "refused", "applies_to: sql" in str(exc) and reason in str(exc)
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
                set(vocab["targets"]) == set(TARGETS())
                and set(vocab["targets"]["table"]) == set(FIELDS()["table"]))

    case("the exported vocabulary matches the evaluator it is exported from",
         "3 targets", vocabulary_export_is_consistent)

    return rows, all_passed
