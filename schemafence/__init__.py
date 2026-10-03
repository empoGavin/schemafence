"""schemafence — a constraint layer between LLMs and databases.

Nothing in this package calls a language model.  That is deliberate:
every check here is deterministic, so it behaves identically with any
model, or with no model at all.

Public surface:
    parse_ddl(text)  -> Schema          (read a schema definition)
    analyze(schema)  -> list[Finding]   (the checks)
    guard(sql)       -> GuardResult     (the seven-layer guardrail)
"""

from .checks import Finding, analyze, summarize
from .ddl import Column, Schema, Table, parse_ddl
from .guard import GuardResult, guard, run_selftest

__all__ = [
    "Column",
    "Table",
    "Schema",
    "parse_ddl",
    "Finding",
    "analyze",
    "summarize",
    "GuardResult",
    "guard",
    "run_selftest",
]

__version__ = "0.1.0"
