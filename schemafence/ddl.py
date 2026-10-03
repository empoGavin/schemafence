"""A very small DDL reader.

Why not a real SQL parser?  Because the offline demo must run on a fresh
clone with *zero* pip installs (``python demo.py``), and the subset we need
is tiny: CREATE TABLE, column definitions, table/column COMMENTs.

Known limits — see README "Known limitations":
  * assumes one column definition per line (true of the bundled example)
  * does not understand CREATE TABLE ... AS SELECT, inheritance or domains
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

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def column(self, name: str) -> Column | None:
        key = name.lower()
        return next((c for c in self.columns if c.name.lower() == key), None)

    @property
    def commented_columns(self) -> int:
        return sum(1 for c in self.columns if c.comment)


@dataclass
class Schema:
    tables: list[Table] = field(default_factory=list)

    def table(self, name: str) -> Table | None:
        key = name.split(".")[-1].lower()
        return next((t for t in self.tables if t.name.lower() == key), None)

    @property
    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables)

    @property
    def all_columns(self) -> list[tuple[Table, Column]]:
        return [(t, c) for t in self.tables for c in t.columns]


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
    )


def parse_ddl(raw: str) -> Schema:
    """Read CREATE TABLE / COMMENT ON statements out of a DDL script."""
    table_comments, column_comments = _collect_comments(raw)
    text = _strip_comments(raw)
    schema = Schema()

    for m in _CREATE_TABLE.finditer(text):
        full = m.group(1)
        open_idx = text.index("(", m.start())
        body = text[open_idx + 1: _matching_paren(text, open_idx)]

        parts = [p.strip('"') for p in full.split(".")]
        table = Table(name=parts[-1], schema=".".join(parts[:-1]))

        for item in _split_items(body):
            if _TABLE_CONSTRAINT.match(item):
                if re.match(r"^PRIMARY\s+KEY", item, re.IGNORECASE):
                    inner = item[item.find("(") + 1: item.rfind(")")]
                    table.primary_key.extend(
                        p.strip().strip('"') for p in inner.split(",") if p.strip()
                    )
                continue
            col = _parse_column(item)
            if col:
                table.columns.append(col)

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

        for pk in table.primary_key:
            col = table.column(pk)
            if col:
                col.nullable = False

        schema.tables.append(table)

    return schema


def parse_file(path) -> Schema:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_ddl(fh.read())
