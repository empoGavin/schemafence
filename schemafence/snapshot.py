"""Read a schema snapshot from a live PostgreSQL database.

The same checks run on this as on a DDL file — the only difference is
where the facts come from.  Two deliberate choices:

  * comments come from pg_description, because a comment is data too;
  * row counts come from pg_class.reltuples, not COUNT(*), because the
    point of a health check is not to load the instance it is checking.
"""

from __future__ import annotations

from .ddl import Column, Schema, Table

SKIP_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

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

ROW_ESTIMATE_SQL = """
SELECT n.nspname, c.relname, c.reltuples::bigint AS est_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND c.reltuples >= 0
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY 1, 2
"""


def read_schema(conn) -> Schema:
    """Build the same Schema object the DDL reader produces."""
    schema = Schema()
    index: dict[tuple[str, str], Table] = {}

    with conn.cursor() as cur:
        cur.execute(TABLES_SQL)
        for ns, name, comment in cur.fetchall():
            table = Table(name=name, schema=ns, comment=comment)
            schema.tables.append(table)
            index[(ns, name)] = table

        cur.execute(COLUMNS_SQL)
        for (ns, tname, cname, dtype, notnull, default_expr,
             comment, is_pk, refs) in cur.fetchall():
            table = index.get((ns, tname))
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

    return schema


def null_fractions(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(NULL_FRACTION_SQL)
        return cur.fetchall()


def row_estimates(conn) -> dict[tuple[str, str], int]:
    with conn.cursor() as cur:
        cur.execute(ROW_ESTIMATE_SQL)
        return {(ns, name): rows for ns, name, rows in cur.fetchall()}


def connect(dsn: str):
    import psycopg  # imported lazily: offline mode has no third-party deps
    return psycopg.connect(dsn)
