"""A very small DDL reader.

Why not a real SQL parser?  Because the offline demo must run on a fresh
clone with *zero* pip installs (``python demo.py``), and the subset we need
is small: CREATE TABLE, column definitions, table/column COMMENTs, and the
change actions a migration script is made of.

Reading a script happens in two passes over the same text:

  * the schema it leaves behind — tables, columns, keys, comments
  * the actions it takes to get there — :class:`Change`, one per statement

The second pass is what lets a review say "this adds a column with no
comment" instead of "zero tables, zero findings, exit zero".  A script that
reads as nothing must say so, so :class:`Schema` also carries the statement
count and the list of statements no reader claimed.

Known limits — see README "Known limitations":
  * assumes one column definition per line (true of the bundled example)
  * does not understand CREATE TABLE ... AS SELECT, inheritance or domains
  * a dollar-quoted body is copied whole and its contents are not parsed
  * a production version should parse with sqlglot or pg_query
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FLOAT_TYPES = {"real", "float", "float4", "float8", "double precision"}
TIMESTAMP_NO_TZ = {"timestamp", "timestamp without time zone"}
TEXT_LIKE = {"text", "char", "character", "character varying", "varchar", "bpchar"}

_CONSTRAINT_KEYWORDS = re.compile(
    r"\b(NOT\s+NULL|NULL|DEFAULT|PRIMARY\s+KEY|UNIQUE|CHECK|REFERENCES"
    r"|GENERATED|COLLATE|CONSTRAINT)\b",
    re.IGNORECASE,
)
_TABLE_CONSTRAINT = re.compile(
    r"^(PRIMARY\s+KEY|UNIQUE|CHECK|CONSTRAINT|FOREIGN\s+KEY|EXCLUDE|LIKE)\b",
    re.IGNORECASE,
)
_REFERENCE = re.compile(r"REFERENCES\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.IGNORECASE)
_DEFAULT = re.compile(
    r"\bDEFAULT\s+(.+?)(?=\b(?:NOT\s+NULL|NULL|PRIMARY\s+KEY|UNIQUE|CHECK"
    r"|REFERENCES|GENERATED|COLLATE|CONSTRAINT)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_DEF = re.compile(r'^"?([A-Za-z_][\w$]*)"?\s+(.*)$', re.DOTALL)
_CREATE_TABLE = re.compile(
    r"CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMP(?:ORARY)?\s+|UNLOGGED\s+)*TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s*\(",
    re.IGNORECASE,
)
_COMMENT_ON = re.compile(
    r"COMMENT\s+ON\s+(TABLE|COLUMN)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){0,2})"
    r"\s+IS\s+'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    references: str | None = None
    comment: str | None = None
    primary: bool = False
    unique: bool = False
    added_by: str | None = None        # the ALTER TABLE that added it, if any

    @property
    def base_type(self) -> str:
        return re.sub(r"\s+", " ", self.type).strip().lower()

    @property
    def looks_like_key(self) -> bool:
        return self.name.lower() == "id" or self.name.lower().endswith("_id")

    @property
    def looks_like_money(self) -> bool:
        n = self.name.lower()
        return any(w in n for w in ("amount", "price", "balance", "fee",
                                    "cost", "salary", "total", "money", "charge"))

    @property
    def looks_like_time(self) -> bool:
        n = self.name.lower()
        return (n.endswith(("_at", "_time", "_date", "_ts"))
                or n.startswith(("created", "updated", "deleted", "occurred", "gmt_")))


@dataclass
class Table:
    name: str
    schema: str = ""
    columns: list[Column] = field(default_factory=list)
    comment: str | None = None
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    created_by: str | None = None      # None when the table is only reached by ALTER

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> Column | None:
        key = name.lower()
        return next((c for c in self.columns if c.name.lower() == key), None)

    def is_unique(self, name: str) -> bool:
        """One row per value of this column, from a PK or a UNIQUE constraint.

        The SQL-side checks ask this about a join key.  When the answer is no,
        joining the table multiplies the rows on the other side, and an
        aggregate over them counts the same row more than once.
        """
        key = name.split(".")[-1].lower()
        if key in {c.lower() for c in self.primary_key}:
            return True
        col = self.column(key)
        if col is not None and col.unique:
            return True
        return any(len(k) == 1 and k[0].lower() == key for k in self.unique_keys)

    @property
    def commented_columns(self) -> int:
        return sum(1 for c in self.columns if c.comment)


CHANGE_KINDS = (
    "create_table", "add_column", "drop_column", "alter_column", "add_constraint",
    "drop_constraint", "create_index", "drop_index", "drop_table", "rename",
    "comment", "dml", "grant", "session", "other_ddl",
)


@dataclass
class Change:
    """One statement of a script, read as an action rather than as a result.

    A review of a migration reads the action, because the schema the script
    leaves behind is not what the person deploying it has to approve.  Adding
    a nullable column with no default is fine; adding it NOT NULL to a table
    with rows is not.  Both leave the same schema.
    """

    kind: str                          # one of CHANGE_KINDS
    table: str = ""                    # qualified, lowercase, "" when not applicable
    column: str | None = None
    detail: str = ""                   # what the statement does, in one line
    statement: str = ""                # the statement as written, whitespace collapsed
    position: int = 0                  # 1-based position in the script

    @property
    def subject(self) -> str:
        if self.column:
            return f"{self.table}.{self.column}" if self.table else self.column
        return self.table or self.kind


@dataclass
class Schema:
    tables: list[Table] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    statements: int = 0
    unread: list[tuple[int, str, str]] = field(default_factory=list)
    #                            position, leading keyword, why nothing was read

    def table(self, name: str) -> Table | None:
        key = name.split(".")[-1].lower()
        return next((t for t in self.tables if t.name.lower() == key), None)

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    @property
    def all_columns(self) -> list[tuple[Table, Column]]:
        return [(t, c) for t in self.tables for c in t.columns]

    @property
    def recognised(self) -> bool:
        """Whether this run read anything at all.

        A review that reads nothing has to be able to tell the difference
        between a clean script and one its reader did not understand.  Zero
        findings on an empty read is the one output that must never look like
        a pass.
        """
        return bool(self.tables or self.changes)


_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _skip_dollar(text: str, i: int) -> int | None:
    """Index just past the dollar-quoted body starting at ``i``, or None.

    A migration script runs out of reach of plain statement splitting: a
    ``DO $$ ... $$`` block contains semicolons, comments and quotes of its
    own.  Copying the body whole keeps the split correct; nothing inside is
    read, which is the honest position for a reader this small.
    """
    m = _DOLLAR_TAG.match(text, i)
    if not m:
        return None
    end = text.find(m.group(0), m.end())
    return len(text) if end == -1 else end + len(m.group(0))


def _strip_comments(text: str) -> str:
    """Remove -- and /* */ comments, leaving string literals untouched."""
    out: list[str] = []
    i, n = 0, len(text)
    in_quote = False
    while i < n:
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == "$":
            nxt = _skip_dollar(text, i)
            if nxt is not None:
                out.append(text[i:nxt])
                i = nxt
                continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _collect_comments(raw: str) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    table_comments: dict[str, str] = {}
    column_comments: dict[tuple[str, str], str] = {}
    for kind, target, body in _COMMENT_ON.findall(raw):
        text = body.replace("''", "'").strip()
        parts = [p.strip('"').lower() for p in target.split(".")]
        if kind.upper() == "TABLE":
            table_comments[".".join(parts)] = text
        else:
            column_comments[(".".join(parts[:-1]), parts[-1])] = text
    return table_comments, column_comments


def _matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses in CREATE TABLE")


def _split_items(body: str) -> list[str]:
    """Split a CREATE TABLE body on top-level commas."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_quote = False
    for ch in body:
        if in_quote:
            buf.append(ch)
            if ch == "'":
                in_quote = False
            continue
        if ch == "'":
            in_quote = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return [i for i in items if i]


def _parse_column(item: str) -> Column | None:
    m = _COLUMN_DEF.match(item.strip())
    if not m:
        return None
    name, rest = m.group(1), m.group(2).strip()
    cm = _CONSTRAINT_KEYWORDS.search(rest)
    type_part = (rest[: cm.start()] if cm else rest).strip()
    if not type_part:
        return None
    upper = rest.upper()
    nullable = "NOT NULL" not in upper and "PRIMARY KEY" not in upper
    dm = _DEFAULT.search(rest)
    refm = _REFERENCE.search(rest)
    return Column(
        name=name,
        type=re.sub(r"\s+", " ", type_part),
        nullable=nullable,
        default=dm.group(1).strip() if dm else None,
        references=refm.group(1) if refm else None,
        primary="PRIMARY KEY" in upper,
        unique=bool(re.search(r"\bUNIQUE\b", upper)),
    )


# ------------------------------------------------------- statements, and actions
_CREATE_INDEX = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s+ON\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    re.IGNORECASE,
)
_ALTER_TABLE = re.compile(
    r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    re.IGNORECASE,
)
_ADD_COLUMN = re.compile(
    r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_ADD_CONSTRAINT = re.compile(
    r"^ADD\s+(?:CONSTRAINT\s+[\w$]+\s+)?(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_DROP_COLUMN = re.compile(r"^DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?([A-Za-z_][\w$]*)",
                          re.IGNORECASE)
_DROP_CONSTRAINT = re.compile(r"^DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w$]*)",
                              re.IGNORECASE)
_ALTER_COLUMN = re.compile(r"^ALTER\s+(?:COLUMN\s+)?([A-Za-z_][\w$]*)\s+(.+)$",
                           re.IGNORECASE | re.DOTALL)
_RENAME_COLUMN = re.compile(r"^RENAME\s+(?:COLUMN\s+)?([A-Za-z_][\w$]*)\s+TO\s+([A-Za-z_][\w$]*)",
                            re.IGNORECASE)
_RENAME_TABLE = re.compile(r"^RENAME\s+TO\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
                           re.IGNORECASE)
_DROP_TABLE = re.compile(
    r"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.IGNORECASE)
_DROP_INDEX = re.compile(
    r"^DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.IGNORECASE)
_COMMENT_ON_TARGET = re.compile(
    r"^COMMENT\s+ON\s+(TABLE|COLUMN)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){0,2})", re.IGNORECASE)
_DML = re.compile(
    r"^(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE|TRUNCATE\s+TABLE)\s+"
    r"(?:ONLY\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.IGNORECASE)
_SESSION = re.compile(
    r"^(SET|RESET|BEGIN|START\s+TRANSACTION|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ANALYZE"
    r"|VACUUM|SELECT)\b", re.IGNORECASE)
_GRANT = re.compile(r"^(GRANT|REVOKE)\b", re.IGNORECASE)
_OTHER_DDL = re.compile(
    r"^(CREATE|DROP|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+|MATERIALIZED\s+)?"
    r"(SCHEMA|SEQUENCE|VIEW|FUNCTION|PROCEDURE|TRIGGER|TYPE|DOMAIN|EXTENSION|POLICY|ROLE"
    r"|USER|GROUP|TABLESPACE|DATABASE|PUBLICATION|SUBSCRIPTION|RULE|AGGREGATE|OPERATOR"
    r"|COLLATION|STATISTICS|SERVER|LANGUAGE|CAST|TRANSFORM|ACCESS\s+METHOD)\b",
    re.IGNORECASE)
_DO = re.compile(r"^DO\b", re.IGNORECASE)


def _split_statements(text: str) -> list[str]:
    """Split a script on semicolons that are not inside a literal or a body."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(text[i:j])
            i = j
            continue
        if ch == '"':
            j = text.find('"', i + 1)
            j = n if j == -1 else j + 1
            buf.append(text[i:j])
            i = j
            continue
        if ch == "$":
            j = _skip_dollar(text, i)
            if j is not None:
                buf.append(text[i:j])
                i = j
                continue
        if ch == ";":
            out.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return [s for s in out if s]


def _excerpt(statement: str, limit: int = 96) -> str:
    flat = re.sub(r"\s+", " ", statement).strip()
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _norm(name: str) -> str:
    return name.replace('"', "").strip().lower()


def _ensure_table(schema: Schema, name: str) -> Table:
    """The named table, created as a stub if the script only alters it."""
    found = schema.table(name)
    if found is not None:
        return found
    parts = [p.strip('"') for p in name.split(".")]
    stub = Table(name=parts[-1], schema=".".join(parts[:-1]))
    stub.created_by = None
    schema.tables.append(stub)
    return stub


def _find_column(table: Table, name: str) -> Column | None:
    return next((c for c in table.columns if c.name.lower() == name.lower()), None)


def _apply_comments(schema: Schema, table_comments: dict[str, str],
                    column_comments: dict[tuple[str, str], str]) -> None:
    """Attach COMMENT ON text to the tables and columns it names.

    Done in one pass at the end rather than where each statement is read, so a
    comment is found whether it is written before or after the definition it
    belongs to — which is ordinary practice in a migration script, and which a
    column added by ALTER needs.
    """
    for table in schema.tables:
        for key in (table.qualified.lower(), table.name.lower()):
            if key in table_comments:
                table.comment = table_comments[key]
                break
        for col in table.columns:
            for key in ((table.qualified.lower(), col.name.lower()),
                        (table.name.lower(), col.name.lower())):
                if key in column_comments:
                    col.comment = column_comments[key]
                    break


def _read_create_table(schema: Schema, statement: str, position: int) -> None:
    m = _CREATE_TABLE.search(statement)
    assert m is not None
    full = m.group(1)
    open_idx = statement.index("(", m.start())
    body = statement[open_idx + 1: _matching_paren(statement, open_idx)]

    parts = [p.strip('"') for p in full.split(".")]
    table = Table(name=parts[-1], schema=".".join(parts[:-1]))
    table.created_by = _excerpt(statement)

    for item in _split_items(body):
        if _TABLE_CONSTRAINT.match(item):
            if re.match(r"^PRIMARY\s+KEY", item, re.IGNORECASE):
                inner = item[item.find("(") + 1: item.rfind(")")]
                table.primary_key.extend(
                    p.strip().strip('"') for p in inner.split(",") if p.strip()
                )
            elif re.match(r"^UNIQUE\b", item, re.IGNORECASE):
                inner = item[item.find("(") + 1: item.rfind(")")] if "(" in item else ""
                cols = [p.strip().strip('"') for p in inner.split(",") if p.strip()]
                if cols:
                    table.unique_keys.append(cols)
            continue
        col = _parse_column(item)
        if col:
            table.columns.append(col)

    for col in table.columns:
        if col.primary and col.name not in table.primary_key:
            table.primary_key.append(col.name)

    for pk in table.primary_key:
        col = table.column(pk)
        if col:
            col.nullable = False

    schema.tables.append(table)
    schema.changes.append(Change(
        kind="create_table", table=table.qualified.lower(),
        detail=f"{len(table.columns)} column(s)"
               + (f", primary key {'(' + ', '.join(table.primary_key) + ')'}"
                  if table.primary_key else ", no primary key"),
        statement=_excerpt(statement), position=position))


def _read_alter(schema: Schema, target: str, body: str, statement: str,
                position: int) -> None:
    """Read the actions of one ALTER TABLE, and let them change the schema."""
    table = _ensure_table(schema, target)
    qualified = table.qualified.lower()
    seen = False

    for action in _split_items(body):
        seen = True

        m = _ADD_CONSTRAINT.match(action)
        if m:
            kind, inner = m.group(1).upper(), m.group(2)
            columns: list[str] = []
            if "(" in inner:
                columns = [p.strip().strip('"').lower()
                           for p in inner[inner.find("(") + 1: inner.rfind(")")].split(",")
                           if p.strip()]
            if kind == "PRIMARY KEY" and columns:
                for name in columns:
                    if name not in table.primary_key:
                        table.primary_key.append(name)
                    col = _find_column(table, name)
                    if col:
                        col.nullable = False
                table.unique_keys.append(columns)
            elif kind == "UNIQUE" and columns:
                table.unique_keys.append(columns)
            schema.changes.append(Change(
                kind="add_constraint", table=qualified,
                column=columns[0] if len(columns) == 1 else None,
                detail=f"{kind.lower()} added"
                       + (f" on ({', '.join(columns)})" if columns else ""),
                statement=_excerpt(statement), position=position))
            continue

        m = _DROP_CONSTRAINT.match(action)
        if m:
            schema.changes.append(Change(
                kind="drop_constraint", table=qualified, column=None,
                detail=f"constraint {m.group(1)} dropped",
                statement=_excerpt(statement), position=position))
            continue

        m = _ADD_COLUMN.match(action)
        if m and m.group(1).upper() not in ("CONSTRAINT", "PRIMARY", "FOREIGN", "CHECK"):
            name, rest = m.group(1), m.group(2).strip()
            col = _parse_column(f"{name} {rest}")
            if col is not None:
                col.added_by = _excerpt(statement)
                existing = _find_column(table, name)
                if existing is not None:
                    table.columns[table.columns.index(existing)] = col
                else:
                    table.columns.append(col)
                schema.changes.append(Change(
                    kind="add_column", table=qualified, column=name.lower(),
                    detail=f"{name} {col.type}"
                           + ("" if col.nullable else " NOT NULL")
                           + ("" if not col.default else f" DEFAULT {col.default}")
                           + ("" if not col.references else f" REFERENCES {col.references}"),
                    statement=_excerpt(statement), position=position))
                continue

        m = _DROP_COLUMN.match(action)
        if m:
            name = m.group(1)
            if _find_column(table, name) is not None or table.columns:
                table.columns = [c for c in table.columns if c.name.lower() != name.lower()]
            schema.changes.append(Change(
                kind="drop_column", table=qualified, column=name.lower(),
                detail=f"{name} dropped", statement=_excerpt(statement), position=position))
            continue

        m = _RENAME_COLUMN.match(action)
        if m:
            old, new = m.group(1), m.group(2)
            col = _find_column(table, old)
            if col is not None:
                col.name = new
            schema.changes.append(Change(
                kind="rename", table=qualified, column=new.lower(),
                detail=f"{old} renamed to {new}", statement=_excerpt(statement),
                position=position))
            continue

        m = _RENAME_TABLE.match(action)
        if m:
            parts = [p.strip('"') for p in m.group(1).split(".")]
            schema.changes.append(Change(
                kind="rename", table=qualified, column=None,
                detail=f"renamed to {'.'.join(parts).lower()}",
                statement=_excerpt(statement), position=position))
            table.name, table.schema = parts[-1], ".".join(parts[:-1])
            continue

        m = _ALTER_COLUMN.match(action)
        if m:
            name, change = m.group(1), m.group(2)
            col = _find_column(table, name)
            upper = change.upper()
            if col is not None:
                if "SET NOT NULL" in upper:
                    col.nullable = False
                elif "DROP NOT NULL" in upper:
                    col.nullable = True
                if re.match(r"^SET\s+DEFAULT", change, re.IGNORECASE):
                    col.default = change[change.find(" "):].strip()
                elif re.match(r"^DROP\s+DEFAULT", change, re.IGNORECASE):
                    col.default = None
            schema.changes.append(Change(
                kind="alter_column", table=qualified, column=name.lower(),
                detail=f"{name} {change}", statement=_excerpt(statement), position=position))
            continue

        schema.changes.append(Change(
            kind="alter_table", table=qualified, column=None,
            detail=_excerpt(action, 72), statement=_excerpt(statement), position=position))

    if not seen:
        schema.changes.append(Change(
            kind="alter_table", table=qualified, column=None,
            detail="empty ALTER TABLE", statement=_excerpt(statement), position=position))


def _read_statement(schema: Schema, statement: str, position: int) -> bool:
    """Read one statement.  False means no reader here claims it."""
    if _CREATE_TABLE.match(statement):
        _read_create_table(schema, statement, position)
        return True

    m = _ALTER_TABLE.match(statement)
    if m:
        _read_alter(schema, m.group(1), statement[m.end():], statement, position)
        return True

    m = _COMMENT_ON_TARGET.match(statement)
    if m:
        target = _norm(m.group(2))
        parts = target.split(".")
        schema.changes.append(Change(
            kind="comment", table=".".join(parts[:-1]) if m.group(1).upper() == "COLUMN"
            else target,
            column=parts[-1] if m.group(1).upper() == "COLUMN" else None,
            detail=f"{m.group(1).upper()} comment set", statement=_excerpt(statement),
            position=position))
        return True

    m = _CREATE_INDEX.match(statement)
    if m:
        schema.changes.append(Change(
            kind="create_index", table=_norm(m.group(2)), column=None,
            detail=f"index {_norm(m.group(1))} on {_norm(m.group(2))}",
            statement=_excerpt(statement), position=position))
        return True

    for pattern, kind, label in ((_DROP_TABLE, "drop_table", "table dropped"),
                                 (_DROP_INDEX, "drop_index", "index dropped")):
        m = pattern.match(statement)
        if m:
            schema.changes.append(Change(
                kind=kind, table=_norm(m.group(1)), column=None,
                detail=f"{_norm(m.group(1))} — {label}", statement=_excerpt(statement),
                position=position))
            return True

    m = _DML.match(statement)
    if m:
        schema.changes.append(Change(
            kind="dml", table=_norm(m.group(2)), column=None,
            detail=f"{' '.join(m.group(1).upper().split())} on {_norm(m.group(2))}",
            statement=_excerpt(statement), position=position))
        return True

    if _GRANT.match(statement):
        schema.changes.append(Change(kind="grant", detail="privileges changed",
                                     statement=_excerpt(statement), position=position))
        return True

    if _SESSION.match(statement):
        schema.changes.append(Change(kind="session", detail=statement.split()[0].upper(),
                                     statement=_excerpt(statement), position=position))
        return True

    m = _OTHER_DDL.match(statement)
    if m:
        label = " ".join(g.upper() for g in m.groups() if g)
        schema.changes.append(Change(
            kind="other_ddl", detail=f"{label}, read as an action only",
            statement=_excerpt(statement), position=position))
        return True

    if _DO.match(statement):
        schema.changes.append(Change(
            kind="other_ddl", detail="DO block, read as an action only",
            statement=_excerpt(statement), position=position))
        return True

    return False


def parse_ddl(raw: str) -> Schema:
    """Read a DDL script into a schema, plus the actions it performs."""
    table_comments, column_comments = _collect_comments(raw)
    text = _strip_comments(raw)
    schema = Schema()

    statements = _split_statements(text)
    schema.statements = len(statements)

    for position, statement in enumerate(statements, 1):
        try:
            handled = _read_statement(schema, statement, position)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            schema.unread.append((position, _excerpt(statement, 40),
                                  f"{type(exc).__name__}: {exc}"))
            continue
        if not handled:
            schema.unread.append((position, _excerpt(statement, 40),
                                  "no reader here claims this statement"))

    _apply_comments(schema, table_comments, column_comments)
    return schema


def parse_file(path) -> Schema:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_ddl(fh.read())
