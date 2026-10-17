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
class Index:
    """One index on one table, however it was written.

    Four spellings in a script produce one: a column-level ``UNIQUE``, a
    table-level ``UNIQUE`` or ``PRIMARY KEY``, a named ``CONSTRAINT``, and
    ``CREATE [UNIQUE] INDEX``.  The live catalogue is a fifth reader of the
    same fact (see ``snapshot.py``).  A rule asks which columns an index
    covers and in what order; the order is the part that makes an index usable
    for a query, so ``(a, b)`` and ``(b, a)`` are two different indexes and
    two different facts.
    """

    name: str
    columns: list[str] = field(default_factory=list)   # ordered, lowercased
    unique: bool = False
    primary: bool = False
    from_constraint: bool = False   # backed by a PK/UNIQUE constraint
    method: str = "btree"
    expression: bool = False        # at least one key is not a bare column
    predicate: str | None = None    # ... WHERE ... — a partial index
    created_here: bool = False      # a statement in the file under review made it


@dataclass
class Table:
    name: str
    schema: str = ""
    columns: list[Column] = field(default_factory=list)
    comment: str | None = None
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    created_by: str | None = None      # None when the table is only reached by ALTER

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> Column | None:
        key = name.lower()
        return next((c for c in self.columns if c.name.lower() == key), None)

    def add_index(self, index: Index) -> Index:
        """Record an index, replacing one of the same name.

        Replacing rather than appending keeps a script that drops and rebuilds
        an index from looking like a script that added a second copy of it.
        """
        existing = self.index(index.name)
        if existing is not None:
            self.indexes[self.indexes.index(existing)] = index
            return index
        self.indexes.append(index)
        return index

    def index(self, name: str) -> Index | None:
        key = name.split(".")[-1].lower()
        return next((i for i in self.indexes if i.name.lower() == key), None)

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
    "create_table", "add_column", "drop_column", "alter_column", "alter_table",
    "add_constraint", "drop_constraint", "create_index", "drop_index", "drop_table",
    "rename", "comment", "insert", "update", "delete", "truncate",
    "grant", "session", "other_ddl",
)
# `alter_table` fell outside this tuple for one commit: the reader produced it
# and a rule written against it was refused by the vocabulary, so the rule could
# never fire.  `_check_kinds` below is what stops that happening quietly again.
#
# `dml` was one kind until a rule needed to tell an UPDATE from a DELETE.  Four
# kinds cost nothing and are the thing a reviewer names: `insert`, `update`,
# `delete`, `truncate`.  A pack written against `dml` is refused when it loads
# rather than left matching no row at all.


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
    has_where: bool = False            # a WHERE at the top level of this statement

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
    origin: str = "ddl"        # which fact model these rows were read from
    #                            "ddl"     a script, read as actions and as a schema
    #                            "catalog" the live catalogue, read as a schema
    # A rule that asserts what a table *already* holds can only be answered
    # from the catalogue; ``applies_to`` in the pack is what says so, and
    # ``run_rulepack`` refuses to apply the pack to the wrong origin rather
    # than reporting silence as a pass.

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


def _key_columns(text: str) -> list[str]:
    """The column list inside the first parenthesis, lowercased.

    One reader for the four places a key is written — the column's own line,
    the table body, a named table constraint, and an ``ALTER TABLE`` — because
    four readers is how they drift apart.  A rule asks whether a table has a
    primary key, never how it was written.
    """
    open_idx = text.find("(")
    if open_idx == -1:
        return []
    try:
        close = _matching_paren(text, open_idx)
    except ValueError:
        return []
    return [p.strip().strip('"').lower()
            for p in text[open_idx + 1: close].split(",") if p.strip()]


_INDEX_KEY = re.compile(
    r'^"?([A-Za-z_][\w$]*)"?(?:\s+(?:ASC|DESC))?(?:\s+NULLS\s+(?:FIRST|LAST))?$',
    re.IGNORECASE,
)


def _index_key_columns(text: str) -> tuple[list[str], bool]:
    """An index's key columns, in order, plus whether any key is an expression.

    The same question ``_key_columns`` answers for a constraint, asked about a
    ``CREATE INDEX`` — where a key may carry ``ASC``/``DESC`` and an opclass,
    or may not be a column at all.  ``lower(a)`` is not ``a``, and comparing
    two expressions by their text would call two different indexes the same
    index, so the ambiguity is recorded as a fact rather than resolved.
    """
    open_idx = text.find("(")
    if open_idx == -1:
        return [], False
    try:
        close = _matching_paren(text, open_idx)
    except ValueError:
        return [], False
    columns, expression = [], False
    for item in _split_items(text[open_idx + 1: close]):
        key = _INDEX_KEY.match(item.strip())
        if key:
            columns.append(key.group(1).lower())
        else:
            expression = True
    return columns, expression


def _index_predicate(text: str) -> str | None:
    """The WHERE clause of a partial index, whitespace-collapsed."""
    m = re.search(r"\bWHERE\b", text, re.IGNORECASE)
    if not m:
        return None
    return re.sub(r"\s+", " ", text[m.end():]).strip().rstrip(";").lower() or None


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
    r"^CREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\s+ON\s+(?:ONLY\s+)?"
    r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
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
    r"^ADD\s+(?:CONSTRAINT\s+([\w$]+)\s+)?"
    r"(PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b(.*)$",
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


def _top_level_text(statement: str) -> str:
    """The statement with string literals and parenthesised spans removed.

    What is left is the part of the statement that speaks for its own rows.  A
    ``WHERE`` inside a subquery filters that subquery, and a ``WHERE`` inside a
    literal is text; neither one narrows the rows this statement touches, so
    neither counts as the statement being filtered.  Dropping the parenthesised
    text also drops the parentheses, which is why depth is tracked here rather
    than counted afterwards.

    A dollar-quoted body is skipped the same way the statement splitter skips
    it.  Nothing else is parsed: this is a text question, and the answer it
    gives is only ever read by a rule that says "this action has no WHERE".
    """
    out: list[str] = []
    depth, i, n = 0, 0, len(statement)
    while i < n:
        ch = statement[i]
        if ch in "'\"":
            j = i + 1
            while j < n:
                if statement[j] == ch:
                    if j + 1 < n and statement[j + 1] == ch:
                        j += 2                     # '' inside a literal is one quote
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if ch == "$":
            j = _skip_dollar(statement, i)
            if j is not None:
                i = j
                continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
        i += 1
    return "".join(out)


def _has_where(statement: str) -> bool:
    """Whether a WHERE at the top level of this statement narrows its own rows."""
    return re.search(r"\bWHERE\b", _top_level_text(statement), re.IGNORECASE) is not None


_DML_LABEL = {"insert": "INSERT INTO", "update": "UPDATE",
              "delete": "DELETE FROM", "truncate": "TRUNCATE"}


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


def _constraint_index_name(table: Table, kind: str, columns: list[str]) -> str:
    """The name PostgreSQL itself gives this constraint's index.

    Only a name.  It is what the server generates, not a guess at what the
    author meant, and it exists so a report can point at the row.
    """
    if kind == "primary":
        return f"{table.name}_pkey"
    return f"{table.name}_{columns[0]}_key" if columns else f"{table.name}_key"


def _record_key(table: Table, kind: str, columns: list[str],
                name: str | None = None) -> None:
    """Record one key constraint, as both of the facts it is.

    ``unique_keys`` is what the SQL-side checks ask about a join key;
    ``indexes`` is what a duplicate-index rule asks about.  One constraint
    written one way produces both, so one function produces both — a second
    recorder is how the two would come to disagree about the same statement.

    A primary key is already unique, so it is not also counted in
    ``unique_keys``: counting it twice inflates ``unique_key_count`` and makes
    that fact mean two different things.
    """
    columns = [c.lower() for c in columns]
    if not columns:
        return
    if kind == "primary":
        if not set(columns) <= {k.lower() for k in table.primary_key}:
            for column in columns:
                if column not in table.primary_key:
                    table.primary_key.append(column)
                found = _find_column(table, column)
                if found is not None:
                    found.nullable = False
    elif not any(k == columns for k in table.unique_keys):
        table.unique_keys.append(list(columns))
    table.add_index(Index(
        name=name or _constraint_index_name(table, kind, columns),
        columns=list(columns), unique=True, primary=(kind == "primary"),
        from_constraint=True, created_here=True))


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
            # `CONSTRAINT pk_orders PRIMARY KEY (id)` is the same key as
            # `PRIMARY KEY (id)`.  Reading the name first is the whole
            # difference, and missing it made an unnamed constraint the only
            # spelling that counted.
            named = re.match(r"^CONSTRAINT\s+([\w$]+)\s+", item, re.IGNORECASE)
            constraint = re.sub(r"^CONSTRAINT\s+[\w$]+\s+", "", item, flags=re.IGNORECASE)
            name = named.group(1) if named else None
            if re.match(r"^PRIMARY\s+KEY", constraint, re.IGNORECASE):
                _record_key(table, "primary", _key_columns(constraint), name)
            elif re.match(r"^UNIQUE\b", constraint, re.IGNORECASE):
                _record_key(table, "unique", _key_columns(constraint), name)
            elif re.match(r"^FOREIGN\s+KEY\b", constraint, re.IGNORECASE):
                ref = _REFERENCE.search(constraint)
                if ref:
                    for name_ in _key_columns(constraint):
                        col = table.column(name_)
                        if col is not None:
                            col.references = ref.group(1)
            continue
        col = _parse_column(item)
        if col:
            table.columns.append(col)

    for col in table.columns:
        # One concept, one place.  A key written on the column's own line is
        # the same constraint as one written in the table body, so both go
        # through the one recorder and nothing downstream asks twice.
        if col.primary:
            _record_key(table, "primary", [col.name])
        elif col.unique:
            _record_key(table, "unique", [col.name])

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
            cname, kind, inner = m.group(1), m.group(2).upper(), m.group(3)
            columns = _key_columns(inner)
            if kind in ("PRIMARY KEY", "UNIQUE") and columns:
                _record_key(table, "primary" if kind == "PRIMARY KEY" else "unique",
                            columns, cname)
            elif kind == "FOREIGN KEY" and columns:
                ref = _REFERENCE.search(inner)
                if ref:
                    for name in columns:
                        col = _find_column(table, name)
                        if col is not None:
                            col.references = ref.group(1)
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


def _read_create_index(schema: Schema, statement: str, position: int) -> None:
    """Read a CREATE INDEX as the action it is, and as the index it leaves."""
    m = _CREATE_INDEX.match(statement)
    assert m is not None
    unique, name, target = bool(m.group(1)), _norm(m.group(2)), _norm(m.group(3))
    table = _ensure_table(schema, target)
    rest = statement[m.end():]
    method = re.match(r"\s*USING\s+([A-Za-z_][\w$]*)", rest, re.IGNORECASE)
    columns, expression = _index_key_columns(rest)
    table.add_index(Index(
        name=name.split(".")[-1], columns=columns, unique=unique,
        method=method.group(1).lower() if method else "btree",
        expression=expression, predicate=_index_predicate(rest), created_here=True))
    schema.changes.append(Change(
        kind="create_index", table=table.qualified.lower(), column=None,
        detail=f"index {name} on {target}",
        statement=_excerpt(statement), position=position))


def _forget_index(schema: Schema, name: str) -> None:
    """Drop a known index when the script drops it by name.

    Without this a script that rebuilds an index — DROP then CREATE — would
    read as two indexes on the same columns, which is the opposite of what it
    does.
    """
    key = _norm(name).split(".")[-1]
    for table in schema.tables:
        table.indexes = [i for i in table.indexes if i.name.lower() != key]


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
        _read_create_index(schema, statement, position)
        return True

    for pattern, kind, label in ((_DROP_TABLE, "drop_table", "table dropped"),
                                 (_DROP_INDEX, "drop_index", "index dropped")):
        m = pattern.match(statement)
        if m:
            if kind == "drop_index":
                _forget_index(schema, m.group(1))
            schema.changes.append(Change(
                kind=kind, table=_norm(m.group(1)), column=None,
                detail=f"{_norm(m.group(1))} — {label}", statement=_excerpt(statement),
                position=position))
            return True

    m = _DML.match(statement)
    if m:
        kind = m.group(1).upper().split()[0].lower()
        table = _norm(m.group(2))
        schema.changes.append(Change(
            kind=kind, table=table, column=None,
            detail=f"{_DML_LABEL[kind]} {table}",
            statement=_excerpt(statement), position=position,
            has_where=_has_where(statement)))
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
    _check_kinds(schema)
    return schema


def _check_kinds(schema: Schema) -> None:
    """Report any change kind the vocabulary does not declare.

    A kind that is produced but not declared is the worst of both: a rule
    written against it is refused at load, so the rule never fires, and the
    report says nothing.  This is the one place that can see both sides.
    """
    for change in schema.changes:
        if change.kind not in CHANGE_KINDS:
            schema.unread.append((
                change.position, change.kind,
                f"change kind `{change.kind}` is not in CHANGE_KINDS, so no rule "
                f"can name it — the reader and the vocabulary disagree"))


def parse_file(path) -> Schema:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_ddl(fh.read())
