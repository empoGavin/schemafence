"""schemafence — a read-only database agent, and the deterministic layer in front of it.

The deterministic layer never calls a language model, and that is deliberate:
every check and the guardrail are pure functions, so they behave identically with
any model, or with no model at all.  The agent is the half allowed to be wrong.

Public surface:
    parse_ddl(text)  -> Schema          (a script as a schema, plus its change actions)
    analyze(schema)  -> list[Finding]   (the schema checks)
    check_sql(schema, sql) -> SqlReview (the statement checks: reports, never decides)
    guard(sql)       -> GuardResult     (the seven-layer guardrail)
    load_rulepack(path, verify=True) -> dict   (a review rule, as a file)
    review_ddl(packs, text) -> RuleReport      (apply rules to somebody else's script)
    vocabulary()     -> dict            (the closed fact vocabulary, for authoring)
    infer_rule(request) -> dict         (the rule that separates your examples, offline)
    PROVIDERS / rows_for / register     (the facts a rule reads, and how to add one)
    Embedder         -> text -> vector  (offline hash, or any OpenAI-compatible API)
    JsonStore/PgStore                   (the knowledge layer, two backends)
    Toolbox                             (the four tools, all behind guard())
    run_agent(...)   -> AgentRun        (the loop: tools, evidence, answer)
"""

from .checks import Finding, analyze, summarize
from .checks_sql import SqlReview, check_sql, run_sql_selftest  # noqa: F401
from .ddl import Change, Column, Schema, Table, parse_ddl
from .facts import (FACT_MODELS, PROVIDERS, TARGETS, register,  # noqa: F401
                    rows_for)
from .guard import GuardResult, guard, run_selftest
from .knowledge import (Chunk, Embedder, Hit, JsonStore, PgStore,  # noqa: F401
                        chunk_markdown, cosine, embed_offline,
                        estimate_tokens, ingest_directory, read_corpus)
from .rulepack import (RulePackError, RuleReport, infer_rule,     # noqa: F401
                       load_rulepack, review_ddl, run_rulepack,
                       run_rulepack_selftest, verify_rulepack, vocabulary)
from .tools import TOOL_SPECS, Toolbox, ToolResult                 # noqa: F401

__all__ = [
    "Column",
    "Table",
    "Schema",
    "Change",
    "parse_ddl",
    "Finding",
    "analyze",
    "summarize",
    "FACT_MODELS",
    "PROVIDERS",
    "TARGETS",
    "register",
    "rows_for",
    "SqlReview",
    "check_sql",
    "run_sql_selftest",
    "GuardResult",
    "guard",
    "run_selftest",
    "RuleReport",
    "RulePackError",
    "load_rulepack",
    "infer_rule",
    "verify_rulepack",
    "run_rulepack",
    "review_ddl",
    "run_rulepack_selftest",
    "vocabulary",
    "Chunk",
    "Hit",
    "Embedder",
    "JsonStore",
    "PgStore",
    "chunk_markdown",
    "read_corpus",
    "ingest_directory",
    "embed_offline",
    "estimate_tokens",
    "cosine",
    "Toolbox",
    "ToolResult",
    "TOOL_SPECS",
]

__version__ = "0.2.0"
