"""Read a schema snapshot from a live PostgreSQL database.

The same checks run on this as on a DDL file — the only difference is
where the facts come from.  Three deliberate choices:

  * comments come from pg_description, because a comment is data too;
  * row counts come from pg_class.reltuples, not COUNT(*), because the
    point of a health check is not to load the instance it is checking;
  * the catalogue is read, never written.  Reading a database to review a
    script is not the write path, and this module holds no statement that
    changes anything.  Every read still goes through ``guard()`` like every
    other statement this project puts in front of a database — same door,
    and a cap of its own per read, because a catalog read that fills its
    cap is a cut and not an answer.

The rows built here are the rows ``ddl.py`` builds, field for field: a rule
written against a script applies to a catalogue unchanged, and the catalogue
is the fifth reader of a fact a script can only write four ways.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ddl import Column, Index, Schema, Table
from .guard import guard

SKIP_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# How many rows each catalog read may come back with.  Shaped by the read, not
# one number: a table is one row per table, a column is one row per column, and
# a column count runs an order of magnitude past the table count on any real
# database.  A read that fills its cap has not been read — it has been cut —
# and half a catalog reviews clean for every table in the half that never came
# back.  So filling the cap is a refusal, not a short answer.
CATALOG_CAPS = {
    "tables": 20_000,        # one row per table; 20k tables is already a fleet
    "columns": 500_000,      # 25 columns a table at the tables cap
    "indexes": 200_000,      # 10 indexes a table at the tables cap
    "statistics": 500_000,   # only columns that carry NULLs, so column-scale
    "row_estimates": 20_000, # one row per table, same as tables
}


class CatalogRefused(RuntimeError):
    """The gate blocked a catalog read.  Nothing reached the server."""


class CatalogTooLarge(RuntimeError):
    """A catalog read filled its cap.  What came back is a cut, not a read."""

TABLES_SQL = """
SELECT n.nspname            AS schema_name,
       c.relname            AS table_name,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2
"""

COLUMNS_SQL = """
SELECT n.nspname, c.relname, a.attname,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull,
       pg_get_expr(ad.adbin, ad.adrelid) AS default_expr,
       col_description(c.oid, a.attnum) AS comment,
       EXISTS (SELECT 1 FROM pg_constraint p
               WHERE p.conrelid = c.oid AND p.contype = 'p'
                 AND a.attnum = ANY (p.conkey)) AS is_pk,
       (SELECT ref.relname FROM pg_constraint f
        JOIN pg_class ref ON ref.oid = f.confrelid
        WHERE f.conrelid = c.oid AND f.contype = 'f'
          AND a.attnum = ANY (f.conkey)
        LIMIT 1) AS refs
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
LEFT JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
WHERE c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2, a.attnum
"""

NULL_FRACTION_SQL = """
SELECT schemaname, tablename, attname, null_frac, n_distinct
FROM pg_stats
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND null_frac > 0
ORDER BY null_frac DESC
"""

INDEXES_SQL = """
SELECT n.nspname                                       AS schema_name,
       t.relname                                       AS table_name,
       i.relname                                       AS index_name,
       ix.indisunique                                  AS is_unique,
       ix.indisprimary                                 AS is_primary,
       am.amname                                       AS method,
       EXISTS (SELECT 1 FROM pg_constraint con
                WHERE con.conindid = i.oid
                  AND con.contype IN ('p', 'u'))       AS from_constraint,
       (SELECT array_agg(a.attname ORDER BY k.ord)
          FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
         WHERE k.ord <= ix.indnkeyatts)                AS columns,
       (SELECT count(*)
          FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
         WHERE k.attnum = 0
           AND k.ord <= ix.indnkeyatts)                AS expressions,
       ix.indpred IS NOT NULL                          AS is_partial,
       pg_get_expr(ix.indpred, ix.indrelid)            AS predicate
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_am am ON am.oid = i.relam
WHERE t.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2, 3
"""
# Three details carry the meaning.  `indkey::int2[]` because indkey is an
# int2vector and the cast is what makes it an array like any other; ORDINALITY,
# because the order of the key columns is the difference between an index that
# serves a query and one that cannot; and `indnkeyatts`, because an INCLUDE
# column is stored in the index and is not part of its key — taking the two
# together would report `(a) INCLUDE (b)` as a duplicate of `(a, b)`.  That
# last column needs PostgreSQL 11 or later.

ROW_ESTIMATE_SQL = """
SELECT n.nspname, c.relname, c.reltuples::bigint AS est_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND c.reltuples >= 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2
"""


def build_schema(table_rows=(), column_rows=(), index_rows=()) -> Schema:
    """Turn catalogue rows into the Schema the DDL reader produces.

    Separate from the connection on purpose: this is the part that has to be
    right, and a function of three row lists can be checked without a database.
    """
    schema = Schema(origin="catalog")
    by_name: dict[tuple[str, str], Table] = {}

    for ns, name, comment in table_rows:
        table = Table(name=name, schema=ns, comment=comment)
        schema.tables.append(table)
        by_name[(ns, name)] = table

    for (ns, tname, cname, dtype, notnull, default_expr,
         comment, is_pk, refs) in column_rows:
        table = by_name.get((ns, tname))
        if table is None:
            continue
        table.columns.append(Column(
            name=cname,
            type=dtype,
            nullable=not notnull,
            default=default_expr,
            references=refs,
            comment=comment,
        ))
        if is_pk:
            table.primary_key.append(cname)

    for (ns, tname, iname, is_unique, is_primary, method,
         from_constraint, columns, expressions, is_partial, predicate) in index_rows:
        table = by_name.get((ns, tname))
        if table is None:
            continue
        table.indexes.append(Index(
            name=iname,
            columns=[c.lower() for c in (columns or [])],
            unique=bool(is_unique),
            primary=bool(is_primary),
            from_constraint=bool(from_constraint),
            method=method or "btree",
            expression=bool(expressions),
            predicate=predicate if is_partial else None,
            created_here=False,
        ))

    return schema


def _rows(conn, sql: str, what: str) -> list[tuple]:
    """Run one catalog read the only way a read may run: through the gate.

    ``what`` names the read and picks its cap, so an unknown name cannot slip
    through on a default.  The statement the server sees is the one ``guard``
    handed back — with the cap appended, since every constant here asks for the
    whole table — and a read that comes back full is refused rather than
    returned half done.
    """
    if what not in CATALOG_CAPS:
        raise ValueError(f"unknown catalog read {what!r}; "
                         f"known reads are {sorted(CATALOG_CAPS)}")
    cap = CATALOG_CAPS[what]
    verdict = guard(sql, max_rows=cap)
    if not verdict.ok:
        raise CatalogRefused(
            f"the {what} read was blocked by the gate at {verdict.layer}: "
            f"{verdict.reason}")
    with conn.cursor() as cur:
        cur.execute(verdict.sql)
        rows = cur.fetchall()
    if len(rows) >= cap:
        raise CatalogTooLarge(
            f"the {what} read filled its cap of {cap} rows — the catalog is "
            f"larger than this tool reads whole, so it was refused rather "
            f"than reviewed half read")
    return rows


def read_schema(conn) -> Schema:
    """Build the same Schema object the DDL reader produces."""
    tables = _rows(conn, TABLES_SQL, "tables")
    columns = _rows(conn, COLUMNS_SQL, "columns")
    indexes = _rows(conn, INDEXES_SQL, "indexes")
    return build_schema(tables, columns, indexes)


@dataclass
class Applied:
    """What a script added on top of a catalogue snapshot."""

    indexes: int = 0
    new_tables: list[str] = field(default_factory=list)


def apply_script(schema: Schema, script: Schema) -> Applied:
    """Carry the indexes a script creates onto a catalogue snapshot.

    "Is this index a duplicate" is a question about the state *after* the
    migration, and neither side can answer it alone: the catalogue holds the
    indexes already on the table, the script holds the one about to be added.

    Only index facts cross over.  A script's view of a table's columns is the
    list its author typed, and the catalogue's is what the server has; mixing
    them would let a rule report on a table shape that never existed.

    A table the script creates that the catalogue has never seen is counted,
    not merged, and the count is reported: its indexes were compared with
    nothing, and silence there is not a pass.
    """
    applied = Applied()
    for table in script.tables:
        if not table.indexes:
            continue
        qualified = table.qualified.lower()
        target = next((t for t in schema.tables if t.qualified.lower() == qualified), None)
        if target is None:
            applied.new_tables.append(qualified)
            continue
        for index in table.indexes:
            target.add_index(replace(index, created_here=True))
            applied.indexes += 1
    applied.new_tables.sort()
    return applied


def null_fractions(conn) -> list[tuple]:
    return _rows(conn, NULL_FRACTION_SQL, "statistics")


def row_estimates(conn) -> dict[tuple[str, str], int]:
    return {(ns, name): rows
            for ns, name, rows in _rows(conn, ROW_ESTIMATE_SQL, "row_estimates")}


def connect(dsn: str):
    import psycopg  # imported lazily: offline mode has no third-party deps
    return psycopg.connect(dsn)
