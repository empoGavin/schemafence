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

from .ddl import CHANGE_KINDS, Schema

# The fact models a pack may declare in ``applies_to``.  A value of None means
# it is built; anything else is the reason it is not, quoted back to whoever
# asked for it.
FACT_MODELS: dict[str, str | None] = {
    "ddl": None,                       # a script, read as actions and as a schema
    "schema": None,                    # a schema on its own, no actions
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
    } for c in schema.changes]


def _change_subject(row: dict) -> str:
    return str(row.get("subject") or row.get("kind") or "change")


TABLE_FIELDS = {
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
}

register("table", TABLE_FIELDS, ("ddl", "schema"), _table_rows, _table_subject)
register("column", COLUMN_FIELDS, ("ddl", "schema"), _column_rows, _column_subject)
register("change", CHANGE_FIELDS, ("ddl",), _change_rows, _change_subject)


__all__ = ["FACT_MODELS", "FactProvider", "PROVIDERS", "TARGETS", "FIELDS",
           "rows_for", "subject_of", "is_built", "models_for", "register",
           "TABLE_FIELDS", "COLUMN_FIELDS", "CHANGE_FIELDS", "CHANGE_KINDS"]
