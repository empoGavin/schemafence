"""The checks.

Each finding answers three questions a DBA would ask:
  1. what is wrong in the schema        (title / subject)
  2. what will an LLM get wrong because of it   (detail)
  3. what to change                      (fix)

Check numbering follows the README:
  1  schema resolution      wrong or near-duplicate entities get picked
  2  join-key sanity        a join silently drops rows
  3  type & precision       implicit casts that quietly lose data
  4  NULL semantics         three-valued logic traps
  5  result plausibility    needs a live database (see snapshot.py)
  H  hygiene                comments and naming consistency
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ddl import FLOAT_TYPES, TEXT_LIKE, TIMESTAMP_NO_TZ, Column, Schema, Table

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

ARCHIVE_SUFFIXES = (
    "_archive", "_archived", "_bak", "_backup", "_history", "_hist",
    "_old", "_temp", "_tmp", "_copy", "_v2", "_2023", "_2024", "_2025",
)

SET_LIKE_TABLES = (
    "blacklist", "blocklist", "banned", "ban_list", "denylist", "deny_list",
    "exclude", "exclusion", "allowlist", "whitelist", "filter_list",
)


@dataclass
class Finding:
    check: str          # "1".."5" or "H"
    severity: str       # high | medium | low
    title: str
    subject: str
    detail: str
    fix: str

    def render(self) -> str:
        tag = self.severity.upper().ljust(6)
        return (f"  {tag} {self.subject}\n"
                f"         why : {self.detail}\n"
                f"         fix : {self.fix}")


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- check 1
def check_schema_resolution(schema: Schema) -> list[Finding]:
    out: list[Finding] = []
    names = [(t.name.lower(), t) for t in schema.tables]

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, ta = names[i]
            b, tb = names[j]
            if a == b:
                continue
            reason = None
            if (a.startswith(b) and a.endswith(ARCHIVE_SUFFIXES)) or \
               (b.startswith(a) and b.endswith(ARCHIVE_SUFFIXES)):
                reason = "same entity, one is the archive/copy"
            elif min(len(a), len(b)) >= 6 and _lev(a, b) <= 2:
                reason = "names differ by only one or two characters"
            if reason:
                out.append(Finding(
                    check="1", severity="high", title="Near-duplicate table names",
                    subject=f"{ta.qualified}  vs  {tb.qualified}",
                    detail=(f"Two tables differ only by name ({reason}). Asked for "
                            f"\"recent orders\", a model can pick the archive table and "
                            f"return stale data. The query succeeds; nothing raises an error."),
                    fix=("State in a table comment which one is live and which is archived, "
                         "and consider renaming so the difference is unmistakable "
                         "(orders_current / orders_archived)."),
                ))

    for t in schema.tables:
        cols = [(c.name.lower(), c) for c in t.columns]
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, ca = cols[i]
                b, cb = cols[j]
                if min(len(a), len(b)) >= 6 and _lev(a, b) == 1:
                    out.append(Finding(
                        check="1", severity="medium", title="Near-duplicate column names",
                        subject=f"{t.qualified}.{ca.name}  vs  {t.qualified}.{cb.name}",
                        detail=("Column names one character apart, in the same table. "
                                "A model picking the wrong one still gets a valid query."),
                        fix="Rename one of them, or document both in comments.",
                    ))
    return out


# ---------------------------------------------------------------- check 2
def check_join_keys(schema: Schema) -> list[Finding]:
    out: list[Finding] = []
    for t in schema.tables:
        for c in t.columns:
            if not c.looks_like_key or not c.nullable or c.name in t.primary_key:
                continue
            has_fk = bool(c.references)
            out.append(Finding(
                check="2",
                severity="high" if has_fk else "medium",
                title="Nullable key column used in joins",
                subject=f"{t.qualified}.{c.name}",
                detail=(f"`{c.name}` is nullable"
                        + (f" and references {c.references}" if has_fk else " and has no FK constraint")
                        + ". An inner join on it silently drops every row where it is NULL — "
                          "totals come out low and no error is raised."),
                fix=("Document what a NULL means here (e.g. guest checkout), and make sure any "
                     "agent-generated join is checked for nullability before it runs."),
            ))
    return out


# ---------------------------------------------------------------- check 3
def check_types(schema: Schema) -> list[Finding]:
    out: list[Finding] = []
    for t, c in schema.all_columns:
        bt = c.base_type
        if c.looks_like_money and bt in FLOAT_TYPES:
            out.append(Finding(
                check="3", severity="high", title="Money stored as floating point",
                subject=f"{t.qualified}.{c.name}  ({c.type})",
                detail=("Binary floating point cannot represent decimal money exactly. "
                        "A generated `WHERE amount = 0.1` or a SUM used for reconciliation "
                        "drifts, and the difference only shows up in an audit weeks later."),
                fix="Move to NUMERIC(12,2) (or store minor units as BIGINT).",
            ))
        elif c.looks_like_time and bt in TEXT_LIKE:
            out.append(Finding(
                check="3", severity="high", title="Timestamp stored as text",
                subject=f"{t.qualified}.{c.name}  ({c.type})",
                detail=("A time column held as text sorts and compares as a string. "
                        "`BETWEEN '2026-01-01' AND '2026-01-31'` looks right and silently "
                        "returns the wrong interval."),
                fix="Migrate to TIMESTAMPTZ; until then, any query on it must cast and be flagged.",
            ))
        elif c.looks_like_time and bt in TIMESTAMP_NO_TZ:
            out.append(Finding(
                check="3", severity="medium", title="Timestamp without time zone",
                subject=f"{t.qualified}.{c.name}  ({c.type})",
                detail=("No offset is stored, so \"last 7 days\" means different things to the "
                        "application and to a reporting query. Aggregations across regions drift."),
                fix="Prefer TIMESTAMPTZ for anything compared to now().",
            ))
    return out


# ---------------------------------------------------------------- check 4
def check_null_semantics(schema: Schema) -> list[Finding]:
    out: list[Finding] = []
    for t in schema.tables:
        if not any(k in t.name.lower() for k in SET_LIKE_TABLES):
            continue
        for c in t.columns:
            if c.nullable and c.looks_like_key:
                out.append(Finding(
                    check="4", severity="high", title="Anti-join over a nullable column",
                    subject=f"{t.qualified}.{c.name}",
                    detail=(f"`{t.qualified}` looks like a set used in anti-joins, and its "
                            f"`{c.name}` is nullable. One NULL row makes "
                            f"`NOT IN (SELECT {c.name} FROM {t.qualified})` return an empty "
                            "result for every question — the most common silent failure in SQL."),
                    fix=("Use NOT EXISTS instead of NOT IN, and keep this nullable column on the "
                         "list of things the fence checks before executing generated SQL."),
                ))
    return out


# ---------------------------------------------------------------- hygiene
def _time_role(name: str) -> str | None:
    n = name.lower()
    if re.match(r"^(gmt_)?(create|created|creation)", n) or n in ("ctime", "created"):
        return "created"
    if re.match(r"^(gmt_)?(update|updated|modify|modified|last_modif)", n):
        return "updated"
    if re.match(r"^(is_)?(delete|deleted|del|removed)", n):
        return "deleted"
    return None


def check_hygiene(schema: Schema) -> list[Finding]:
    out: list[Finding] = []

    for t in schema.tables:
        if not t.comment:
            out.append(Finding(
                check="H", severity="medium", title="Table has no comment",
                subject=f"{t.qualified}",
                detail=(f"{len(t.columns)} columns and no table comment. For a model this table "
                        "is a bag of names — this is where \"which table should I use?\" goes wrong."),
                fix="One line stating what the table holds and whether it is live or archived.",
            ))
        elif t.commented_columns == 0:
            out.append(Finding(
                check="H", severity="low", title="No column comments",
                subject=f"{t.qualified}",
                detail=("The table is described but its columns are not, so coded values and "
                        "nullable fields stay invisible to any generated query."),
                fix="At least comment the status/type columns and any nullable key.",
            ))

    roles: dict[str, set[str]] = {}
    for t, c in schema.all_columns:
        role = _time_role(c.name)
        if role:
            roles.setdefault(role, set()).add(c.name)

    for role, names in roles.items():
        if len(names) > 1:
            joined = ", ".join(sorted(names))
            out.append(Finding(
                check="H", severity="medium", title="Inconsistent naming for the same concept",
                subject=f"{role} time: {joined}",
                detail=("The same idea is spelled several ways across tables. A model has to "
                        "guess which convention applies where, and guesses wrong silently."),
                fix="Pick one convention and document it in the project README.",
            ))
    return out


def analyze(schema: Schema) -> list[Finding]:
    """Run every offline check. Check 5 needs a live database — see snapshot.py."""
    findings: list[Finding] = []
    findings += check_schema_resolution(schema)
    findings += check_join_keys(schema)
    findings += check_types(schema)
    findings += check_null_semantics(schema)
    findings += check_hygiene(schema)
    findings.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.check, f.subject))
    return findings


def summarize(findings: list[Finding]) -> dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    counts["total"] = len(findings)
    return counts
