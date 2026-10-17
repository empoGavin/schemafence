"""Fact providers — the rows a rule selects from, and the one place they come from.

The rule engine knows nothing about DDL.  It knows about *rows with named
fields*, and it asks this module for them.  That boundary is what makes a rule
file possible at all: a rule names a fact, and if the fact is absent the answer
is a change here, not a cleverer rule.

Why a registry rather than three functions
------------------------------------------
Three functions would work today, and would need editing the day a fourth fact
model arrives.  A provider declares three things — the fields it offers, the
models it can be read from, and how to turn a model into rows — and the engine
reads the registry instead of naming the functions.  Adding the SQL fact model
(R8: a rule over the statement rather than over the schema) is therefore a new
provider in this file and no change to ``rulepack.py``.

The same registry is what the vocabulary export is built from, so the prompt
handed to a model and the evaluator that will judge its output cannot drift:
both read this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .ddl import CHANGE_KINDS, Index, Schema, Table

# The fact models a pack may declare in ``applies_to``.  A value of None means
# it is built; anything else is the reason it is not, quoted back to whoever
# asked for it.
#
# The model is also what ``run_rulepack`` checks the facts against before it
# applies a pack: `catalog` and `ddl` carry the same fields, but they do not
# know the same things.  A script knows only the indexes it creates; the
# catalogue knows the ones already on the table.  A rule that asks whether a
# table *already* has an index can only be answered from the catalogue, and a
# pack that says so is refused rather than reported green over a script.
FACT_MODELS: dict[str, str | None] = {
    "ddl": None,                       # a script, read as actions and as a schema
    "schema": None,                    # a schema on its own, no actions
    "catalog": None,                   # a live PostgreSQL catalogue, read-only
    "sql": ("a statement plus a schema — not built.  The SQL-side checks hold the "
            "facts a rule would need (the FROM chain, join keys, uniqueness) but "
            "do not expose them as rows yet.  This is R8 in docs/roadmap.md, and "
            "it is a change to the reader, not to the rule file"),
}


@dataclass(frozen=True)
class FactProvider:
    """One target a rule can select from."""

    name: str
    fields: dict[str, str]                  # field -> type, the closed vocabulary
    provides: tuple[str, ...]               # which fact models it can be read from
    rows: Callable[[Any], list[dict]]
    subject: Callable[[dict], str]


PROVIDERS: dict[str, FactProvider] = {}


def register(name: str, fields: dict[str, str], provides: tuple[str, ...],
             rows: Callable[[Any], list[dict]],
             subject: Callable[[dict], str]) -> None:
    """Add one target.  The only way a rule learns a new kind of row."""
    if name in PROVIDERS:                                        # pragma: no cover
        raise RuntimeError(f"two fact providers are called `{name}`")
    PROVIDERS[name] = FactProvider(name=name, fields=fields, provides=provides,
                                   rows=rows, subject=subject)


def TARGETS() -> tuple[str, ...]:
    """The registered targets, in registration order."""
    return tuple(PROVIDERS)


def FIELDS() -> dict[str, dict[str, str]]:
    return {name: p.fields for name, p in PROVIDERS.items()}


def rows_for(target: str, model: Any) -> list[dict]:
    return PROVIDERS[target].rows(model)


def subject_of(target: str, row: dict) -> str:
    return PROVIDERS[target].subject(row)


def is_built(model: str) -> bool:
    return model in FACT_MODELS and FACT_MODELS[model] is None


def models_for(target: str) -> tuple[str, ...]:
    return PROVIDERS[target].provides


# --------------------------------------------------------------------------- #
# The DDL fact model: a Schema in, rows out
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


def _table_rows(schema: Schema) -> list[dict]:
    counts, touched = _change_counts(schema)
    return [{
        "name": t.name,
        "schema": t.schema,
        "qualified": t.qualified.lower(),
        "comment": t.comment,
        "column_count": len(t.columns),
        "primary_key_columns": list(t.primary_key),
        "has_primary_key": bool(t.primary_key),
        "unique_key_count": len(t.unique_keys),
        "index_count": len(t.indexes),
        "commented_columns": t.commented_columns,
        "uncommented_columns": len(t.columns) - t.commented_columns,
        "created_by_change": t.created_by is not None,
        "touched_by_change": t.qualified.lower() in touched,
        "change_count": counts.get(t.qualified.lower(), 0),
    } for t in schema.tables]


def _table_subject(row: dict) -> str:
    return str(row.get("qualified") or row.get("name") or "table")


def _column_rows(schema: Schema) -> list[dict]:
    out = []
    for t in schema.tables:
        key = t.qualified.lower()
        pk = {k.lower() for k in t.primary_key}
        for c in t.columns:
            out.append({
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
                "is_primary": c.name.lower() in pk,
                "is_unique": t.is_unique(c.name),
                "looks_like_key": c.looks_like_key,
                "looks_like_money": c.looks_like_money,
                "looks_like_time": c.looks_like_time,
                "added_by_change": c.added_by is not None,
                "added_by": c.added_by,
            })
    return out


def _column_subject(row: dict) -> str:
    return str(row.get("qualified") or row.get("name") or "column")


def _change_rows(schema: Schema) -> list[dict]:
    return [{
        "kind": c.kind,
        "table": c.table,
        "column": c.column,
        "subject": c.subject,
        "detail": c.detail,
        "statement": c.statement,
        "position": c.position,
        "has_where": c.has_where,
    } for c in schema.changes]


def _change_subject(row: dict) -> str:
    return str(row.get("subject") or row.get("kind") or "change")


def _duplicates(table: Table, index: Index, other: Index) -> bool:
    """Whether ``other`` is the same index as ``index``.

    Same table, same method, same columns **in the same order** — order is what
    makes an index usable for a query, so ``(a, b)`` and ``(b, a)`` are two
    different indexes and only one of them can serve an equality lookup on
    ``a``.  On top of that, an index only duplicates one that is at least as
    strong: a full index covers a partial one on the same key, because every
    row the partial index could serve the full one serves too, and not the
    other way round.  Two partial indexes duplicate each other only when their
    predicates say the same thing.

    An expression key is never compared: ``lower(email)`` is not ``email``, and
    comparing the text of two expressions would call two different indexes one
    index.  Calling an index a duplicate of a different one is the error that
    costs a reviewer's trust in the tool: under-reporting is a missing finding,
    over-reporting is a wrong one.
    """
    if index.expression or other.expression or not index.columns:
        return False
    if other.columns != index.columns or other.method != index.method:
        return False
    return other.predicate is None or other.predicate == index.predicate


def _index_rows(schema: Schema) -> list[dict]:
    """One row per index, with the count a per-row rule cannot compute itself.

    "Is this index a duplicate" is a question about two rows of the same
    target, and an assertion here only ever sees one.  So the comparison is
    made once, here, and exported as a number: the rule reads it like any other
    fact and stays a declaration.
    """
    out: list[dict] = []
    for table in schema.tables:
        key = table.qualified.lower()
        for index in table.indexes:
            duplicates = [other.name for other in table.indexes
                          if other is not index and _duplicates(table, index, other)]
            out.append({
                "name": index.name,
                "table": key,
                "qualified": f"{key}.{index.name.lower()}",
                "columns": list(index.columns),
                "columns_text": ", ".join(index.columns),
                "column_count": len(index.columns),
                "unique": index.unique,
                "primary": index.primary,
                "from_constraint": index.from_constraint,
                "method": index.method,
                "expression": index.expression,
                "partial": index.predicate is not None,
                "predicate": index.predicate,
                "comparable": bool(index.columns) and not index.expression,
                "created_here": index.created_here,
                "duplicate_count": len(duplicates),
                "duplicate_of": duplicates,
            })
    return out


def _index_subject(row: dict) -> str:
    return str(row.get("qualified") or row.get("name") or "index")


TABLE_FIELDS = {
    "name": "str",
    "schema": "str",
    "qualified": "str",
    "comment": "str|null",
    "column_count": "int",
    "primary_key_columns": "list[str]",
    "has_primary_key": "bool",
    "unique_key_count": "int",
    "index_count": "int",
    "commented_columns": "int",
    "uncommented_columns": "int",
    "created_by_change": "bool",       # a CREATE TABLE in this script
    "touched_by_change": "bool",       # named by at least one change action
    "change_count": "int",
}
COLUMN_FIELDS = {
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
    "added_by_change": "bool",         # an ALTER TABLE ADD COLUMN in this script
    "added_by": "str|null",
}
CHANGE_FIELDS = {
    "kind": "str",
    "table": "str",
    "column": "str|null",
    "subject": "str",
    "detail": "str",
    "statement": "str",
    "position": "int",
    "has_where": "bool",               # a WHERE that narrows this action's own rows
}
INDEX_FIELDS = {
    "name": "str",
    "table": "str",
    "qualified": "str",
    "columns": "list[str]",
    "columns_text": "str",             # the same thing, as text, for a regex
    "column_count": "int",
    "unique": "bool",
    "primary": "bool",
    "from_constraint": "bool",         # a PK or UNIQUE constraint backs it
    "method": "str",                   # btree, hash, gin, ...
    "expression": "bool",              # a key is not a bare column
    "partial": "bool",                 # the index has a WHERE clause
    "predicate": "str|null",           # shown, not compared: the two readers
    #                                    render the same predicate differently
    "comparable": "bool",              # false: its key cannot be compared at all
    "created_here": "bool",            # a statement in this file creates it
    "duplicate_count": "int",          # 0 = the only index serving its key
    "duplicate_of": "list[str]",       # the indexes it repeats, by name
}

register("table", TABLE_FIELDS, ("ddl", "schema", "catalog"),
         _table_rows, _table_subject)
register("column", COLUMN_FIELDS, ("ddl", "schema", "catalog"),
         _column_rows, _column_subject)
register("index", INDEX_FIELDS, ("ddl", "schema", "catalog"),
         _index_rows, _index_subject)
register("change", CHANGE_FIELDS, ("ddl",), _change_rows, _change_subject)


__all__ = ["FACT_MODELS", "FactProvider", "PROVIDERS", "TARGETS", "FIELDS",
           "rows_for", "subject_of", "is_built", "models_for", "register",
           "TABLE_FIELDS", "COLUMN_FIELDS", "CHANGE_FIELDS", "INDEX_FIELDS",
           "CHANGE_KINDS"]
