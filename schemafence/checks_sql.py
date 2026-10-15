"""SQL-side semantic checks: the statement and the schema, read together.

``checks.py`` reads a schema and never sees a statement.  ``guard.py`` reads a
statement and never sees a schema.  Both are right about what they look at, and
that is exactly why neither of them can see the mistakes below: the query is
valid on its own, the schema is valid on its own, and the number that comes back
is wrong.

  S1  fan-out          a measure summed after a one-to-many join has multiplied
  S2  DISTINCT on a measure   two real 100s collapse into one 100
  S3  LEFT JOIN + WHERE       the right table's condition turns it into an inner join
  S4  time boundaries         BETWEEN and 23:59:59 take in an extra day
  S5  mixed time columns      created_at where the definition says paid_at
  S6  mixed aggregation level order-level and line-level columns summed together

S1 and S3 are implemented; the rest are described in ``docs/roadmap.md``.
Nothing here guesses.  Anything this reader cannot resolve becomes a note:
an unparsed structure, a column that belongs to two tables, a nested query.
A check that abstains is worth more than a check that is sometimes wrong,
because ``guard()`` is the only thing that decides whether a statement runs,
and it must stay deterministic.

That boundary is the point of the module.  These checks report; they do not
block.  A finding says "this number will be wrong", which is advice.  Whether
the statement executes is decided by ``guard()`` and by nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .checks import SEVERITY_RANK, Finding
from .ddl import Schema, Table, parse_ddl
from .guard import _sanitize

AGGREGATES = ("SUM", "AVG")

NOT_AN_ALIAS = {
    "on", "where", "group", "order", "having", "limit", "offset", "union", "window",
    "join", "inner", "left", "right", "full", "outer", "cross", "natural", "as",
    "using", "and", "or", "set", "values", "select", "returning", "fetch", "lateral",
}

_AGG = re.compile(r"\b(SUM|AVG)\s*\(", re.IGNORECASE)
_FROM_JOIN = re.compile(
    r"\b(FROM|JOIN)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"
    r"(?:\s+(?:AS\s+)?([A-Za-z_][\w$]*))?",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"\b(WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION|WINDOW)\b",
    re.IGNORECASE,
)
_EQUALITY = re.compile(
    r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)"
)
_USING = re.compile(r"\bUSING\s*\(([^)]*)\)", re.IGNORECASE)
_LEFT_JOIN = re.compile(r"\bLEFT\s+(?:OUTER\s+)?JOIN\b", re.IGNORECASE)
_NESTED = re.compile(r"\(\s*(?:SELECT|WITH)\b", re.IGNORECASE)
_QUALIFIED = re.compile(r"^([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)$")
_PLAIN = re.compile(r"^[A-Za-z_]\w*$")


@dataclass
class SqlReview:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    """One table reference in the FROM / JOIN chain, with what we learned about it."""

    table: Table | None
    name: str
    alias: str
    kind: str                                  # FROM | JOIN
    start: int
    key_columns: list[str] = field(default_factory=list)
    multiplying: list[str] = field(default_factory=list)


def _depth_map(text: str) -> list[int]:
    """Paren depth before each character.

    The checks read one query level.  Depth is how they know which level a
    keyword is on, so a join inside a subquery cannot be mistaken for one in
    the statement being asked about.
    """
    depth, out = 0, []
    for ch in text:
        if ch == "(":
            out.append(depth)
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(depth)
        else:
            out.append(depth)
    return out


def _paren_body(text: str, open_idx: int) -> str | None:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1: i]
    return None


def _clause_body(clean: str, depth: list[int], keyword: str) -> str:
    """Text of one clause, from after its keyword to the next clause at depth 0."""
    found = [m for m in _CLAUSE.finditer(clean) if depth[m.start()] == 0]
    for i, m in enumerate(found):
        if not m.group(1).upper().startswith(keyword):
            continue
        stop = found[i + 1].start() if i + 1 < len(found) else len(clean)
        return clean[m.end(): stop]
    return ""


def _read_chain(schema: Schema, clean: str, depth: list[int],
                review: SqlReview) -> list[_Entry]:
    """Read FROM / JOIN into a chain, with each join's key columns resolved.

    A join key that is not unique on the joined side is a multiplying join:
    every row on the other side now appears as many times as it has matches.
    """
    entries: list[_Entry] = []
    for m in _FROM_JOIN.finditer(clean):
        if depth[m.start()] != 0:
            continue
        kind, raw_name, alias = m.group(1).upper(), m.group(2), m.group(3)
        if alias and alias.lower() in NOT_AN_ALIAS:
            alias = None
        entries.append(_Entry(
            table=schema.table(raw_name),
            name=raw_name,
            alias=(alias or raw_name.split(".")[-1]).lower(),
            kind="FROM" if kind == "FROM" else "JOIN",
            start=m.start(),
        ))
    if not entries:
        return entries

    for i, entry in enumerate(entries):
        stop = len(clean)
        for m in _CLAUSE.finditer(clean):
            if depth[m.start()] == 0 and m.start() > entry.start:
                stop = m.start()
                break
        if i + 1 < len(entries) and entries[i + 1].start < stop:
            stop = entries[i + 1].start
        segment = clean[entry.start: stop]

        keys: list[str] = []
        for eq in _EQUALITY.finditer(segment):
            left_alias, left_col, right_alias, right_col = (g.lower() for g in eq.groups())
            if left_alias == entry.alias:
                keys.append(left_col)
            elif right_alias == entry.alias:
                keys.append(right_col)
        if not keys:
            using = _USING.search(segment)
            if using:
                keys = [p.strip().strip('"').lower()
                        for p in using.group(1).split(",") if p.strip()]
        entry.key_columns = sorted(set(keys))

        if entry.table is None:
            review.notes.append(
                f"{entry.name} is not in the schema this run read — its join keys "
                f"cannot be checked")
        elif entry.kind == "JOIN" and entry.key_columns:
            entry.multiplying = [k for k in entry.key_columns
                                 if not entry.table.is_unique(k)]
    return entries


def _check_fanout(review: SqlReview, chain: list[_Entry], clean: str,
                  depth: list[int]) -> None:
    multiplying = [e for e in chain if e.multiplying]
    if not multiplying:
        return

    group_body = _clause_body(clean, depth, "GROUP")
    group_words = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", group_body)}

    for m in _AGG.finditer(clean):
        if depth[m.start()] != 0:
            continue
        body = _paren_body(clean, m.end() - 1)
        if body is None:
            review.notes.append(f"{m.group(1).upper()} call is not closed — skipped")
            continue
        body = body.strip()
        if body.lower().startswith("distinct") or re.search(r"[*,]", body):
            continue                       # SUM(DISTINCT …) is S2; COUNT(*) is not a measure
        owner = None
        if _QUALIFIED.match(body):
            alias, col = (g.lower() for g in _QUALIFIED.match(body).groups())
            owner = next((e for e in chain if e.alias == alias), None)
            if owner is None:
                review.notes.append(
                    f"{m.group(1).upper()}({body}) names an alias that is not in this "
                    f"query level — left alone")
                continue
        elif _PLAIN.match(body):
            col = body
            owners = [e for e in chain if e.table is not None and e.table.column(col)]
            if len(owners) != 1:
                review.notes.append(
                    f"{m.group(1).upper()}({col}) belongs to {len(owners)} tables in this "
                    f"query — left alone")
                continue
            owner = owners[0]
        else:
            continue                       # an expression over several columns
        if owner.table is None:
            continue

        for child in multiplying:
            if child is owner:
                continue                   # the measure is on the side that multiplies
            if group_body and all(k.lower() in group_words for k in child.key_columns):
                review.notes.append(
                    f"{m.group(1).upper()}({body}) is aggregated at the grain of "
                    f"{child.name} already (GROUP BY its join key) — nothing is multiplied")
                continue
            review.findings.append(Finding(
                check="S1",
                severity="high",
                title="Measure multiplied by a one-to-many join",
                subject=f"{m.group(1).upper()}({body})  x  {child.name}",
                detail=(
                    f"`{m.group(1).upper()}({body})` is summed over rows the join to "
                    f"`{child.name}` has already multiplied. `{child.name}."
                    f"{child.multiplying[0]}` is neither the primary key nor unique, so one "
                    f"row of `{owner.table.qualified}` becomes as many rows as it has "
                    f"matches there. Three line items turn one 100 into 300. The query runs "
                    f"and nothing raises an error."),
                fix=(
                    f"Aggregate `{owner.table.qualified}` before the join (a CTE or a "
                    f"subquery at that grain), or sum `{child.name}`'s own measure instead."),
            ))


def _check_left_join_where(review: SqlReview, chain: list[_Entry], clean: str,
                           depth: list[int]) -> None:
    where_body = _clause_body(clean, depth, "WHERE")
    if not where_body:
        return

    joined: dict[str, str] = {}
    for m in _LEFT_JOIN.finditer(clean):
        if depth[m.start()] != 0:
            continue
        after = [e for e in chain if e.kind == "JOIN" and e.start >= m.start()]
        if after:
            joined.setdefault(after[0].alias, after[0].name)

    for alias in sorted(joined):
        offender = None
        for hit in re.finditer(rf"\b{re.escape(alias)}\s*\.\s*([A-Za-z_]\w*)", where_body):
            tail = where_body[hit.end(): hit.end() + 16]
            if not re.match(r"^\s+IS\s+(NOT\s+)?NULL\b", tail, re.IGNORECASE):
                offender = hit
                break                     # IS NULL is the idiom, not the mistake
        if offender is None:
            continue
        col = offender.group(1)
        review.findings.append(Finding(
            check="S3",
            severity="high",
            title="A LEFT JOIN cancelled by the WHERE clause",
            subject=f"{joined[alias]}.{col}  tested in WHERE",
            detail=(
                f"`{joined[alias]}` is joined with LEFT JOIN, then `{alias}.{col}` is tested "
                f"in WHERE. Rows with no match hold NULL there, the test evaluates to NULL, "
                f"and the row is dropped — the LEFT JOIN behaves exactly like an INNER JOIN, "
                f"and the unmatched rows are simply absent from the total."),
            fix=(
                "Move the condition into the JOIN's ON clause. If the unmatched rows are what "
                f"you want, say so with `{alias}.{col} IS NULL`."),
        ))


def check_sql(schema: Schema, sql: str) -> SqlReview:
    """Read one statement against one schema.  Reports; never decides execution."""
    review = SqlReview()
    clean = _sanitize(sql or "")
    if not clean:
        review.notes.append("nothing to read — the statement is empty once comments "
                            "and literals are removed")
        return review
    depth = _depth_map(clean)

    if _NESTED.search(clean):
        review.notes.append(
            "the statement has a nested query — S1 and S3 read a single query level "
            "and leave the nested ones alone")

    chain = _read_chain(schema, clean, depth, review)
    if chain:
        _check_fanout(review, chain, clean, depth)
        _check_left_join_where(review, chain, clean, depth)

    seen, unique = set(), []
    for finding in review.findings:
        key = (finding.check, finding.subject)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 9), f.check, f.subject))
    review.findings = unique
    return review


# --------------------------------------------------------------------------- #
# The selftest, and the sample it runs on
# --------------------------------------------------------------------------- #
SQL_SAMPLE_DDL = """
CREATE TABLE shop.orders (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT,
  status       SMALLINT      NOT NULL,
  paid_amount  NUMERIC(12,2) NOT NULL,
  paid_at      TIMESTAMPTZ   NOT NULL,
  created_at   TIMESTAMPTZ   NOT NULL
);

CREATE TABLE shop.order_items (
  id          BIGSERIAL PRIMARY KEY,
  order_id    BIGINT        NOT NULL,
  sku         TEXT          NOT NULL,
  qty         INT           NOT NULL,
  line_amount NUMERIC(12,2) NOT NULL
);

CREATE TABLE shop.refunds (
  id            BIGSERIAL PRIMARY KEY,
  order_id      BIGINT        NOT NULL,
  refund_amount NUMERIC(12,2) NOT NULL,
  status        TEXT          NOT NULL,
  refunded_at   TIMESTAMPTZ   NOT NULL
);

CREATE TABLE shop.order_audit (
  order_id BIGINT PRIMARY KEY,
  note     TEXT
);

CREATE TABLE shop.subscription (
  order_id BIGINT NOT NULL UNIQUE,
  plan     TEXT   NOT NULL
);
"""

# (label, statement, expected check codes)
SQL_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("fan-out: a measure from the one side, summed across a 1:N join",
     "SELECT SUM(o.paid_amount) FROM shop.orders o "
     "JOIN shop.order_items oi ON o.id = oi.order_id",
     ("S1",)),
    ("LEFT JOIN filtered in WHERE: the outer rows are gone",
     "SELECT o.id, r.refund_amount FROM shop.orders o "
     "LEFT JOIN shop.refunds r ON o.id = r.order_id WHERE r.refund_amount > 0",
     ("S3",)),
    ("the anti-join idiom: IS NULL is what a LEFT JOIN is for",
     "SELECT o.id FROM shop.orders o "
     "LEFT JOIN shop.refunds r ON o.id = r.order_id WHERE r.id IS NULL",
     ()),
    ("the same S3 shape, with GROUP BY after the WHERE clause",
     "SELECT o.id FROM shop.orders o "
     "LEFT JOIN shop.refunds r ON o.id = r.order_id "
     "WHERE r.refund_amount > 0 GROUP BY o.id",
     ("S3",)),
    ("a WHERE clause that is not the last one: ORDER BY follows",
     "SELECT o.id FROM shop.orders o WHERE o.status = 'paid' ORDER BY o.id",
     ()),
    ("a WHERE clause that is not the last one: LIMIT follows",
     "SELECT o.id FROM shop.orders o WHERE o.status = 'paid' LIMIT 10",
     ()),
    ("a measure from the side that multiplies: nothing is inflated",
     "SELECT SUM(oi.line_amount) FROM shop.orders o "
     "JOIN shop.order_items oi ON o.id = oi.order_id",
     ()),
    ("already at the child's grain: GROUP BY its join key",
     "SELECT SUM(o.paid_amount) FROM shop.orders o "
     "JOIN shop.order_items oi ON o.id = oi.order_id GROUP BY oi.order_id",
     ()),
    ("a join key that is the primary key: one row per order",
     "SELECT SUM(o.paid_amount) FROM shop.orders o "
     "JOIN shop.order_audit a ON o.id = a.order_id",
     ()),
    ("a join key under a UNIQUE constraint: also one row per order",
     "SELECT SUM(o.paid_amount) FROM shop.orders o "
     "JOIN shop.subscription s ON o.id = s.order_id",
     ()),
    ("LEFT JOIN with the condition in ON, not WHERE",
     "SELECT o.id FROM shop.orders o "
     "LEFT JOIN shop.refunds r ON o.id = r.order_id AND r.refund_amount > 0",
     ()),
    ("a nested query: outside what this reader parses",
     "SELECT t.amt FROM (SELECT SUM(o.paid_amount) AS amt FROM shop.orders o "
     "JOIN shop.order_items oi ON o.id = oi.order_id) t",
     ()),
]


def run_sql_selftest() -> tuple[list[tuple[str, str, bool]], bool]:
    """Return (rows, all_passed) where each row is (label, expectation, passed)."""
    schema = parse_ddl(SQL_SAMPLE_DDL)
    rows: list[tuple[str, str, bool]] = []
    all_passed = True
    for label, sql, expected in SQL_CASES:
        got = tuple(sorted({f.check for f in check_sql(schema, sql).findings}))
        passed = got == tuple(sorted(expected))
        all_passed = all_passed and passed
        found = ", ".join(got) if got else "nothing"
        rows.append((label, found, passed))
    return rows, all_passed
