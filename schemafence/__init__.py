"""schemafence — a script, a query, a live instance, a runbook.

A DBA has four things on their desk: a script to review, a query somebody says is returning the
wrong number, a question you would otherwise go to the runbook for, and a live instance to
diagnose.  The script and the query are pure functions here, the question is answered with the
note quoted back, and the live read is how a rule finds out what the tables already carry.

It never calls a language model, and that is deliberate: give it the same script twice and you
get the same report twice, whatever model is in the room, or with none.  The agent is the half
allowed to be wrong.

Public surface:
    parse_ddl(text)  -> Schema          (a script as a schema, plus its change actions)
    read_schema(conn)-> Schema          (the same Schema, read from a live catalog)
    apply_script(schema, script)        (the catalog, plus the indexes a script adds)
    analyze(schema)  -> list[Finding]   (the schema checks)
    check_sql(schema, sql) -> SqlReview (the statement checks: reports, never decides)
    guard(sql)       -> GuardResult     (the seven-layer guardrail)
    load_rulepack(path, verify=True) -> dict   (a review rule, as a file)
    review_ddl(packs, text) -> RuleReport      (apply rules to somebody else's script)
    review_schema(packs, schema) -> RuleReport (…to facts you read yourself)
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
from .ddl import Change, Column, Index, Schema, Table, parse_ddl
from .facts import (FACT_MODELS, PROVIDERS, TARGETS, register,  # noqa: F401
                    rows_for)
from .guard import GuardResult, guard, run_selftest
from .knowledge import (Chunk, Embedder, Hit, JsonStore, PgStore,  # noqa: F401
                        chunk_markdown, cosine, embed_offline,
                        estimate_tokens, ingest_directory, read_corpus)
from .rulepack import (RulePackError, RuleReport, infer_rule,     # noqa: F401
                       load_rulepack, review_ddl, review_schema,
                       run_rulepack, run_rulepack_selftest, verify_rulepack,
                       vocabulary)
from .snapshot import (Applied, apply_script, build_schema,       # noqa: F401
                       read_schema)
from .tools import TOOL_SPECS, Toolbox, ToolResult                 # noqa: F401

__all__ = [
    "Column",
    "Table",
    "Index",
    "Schema",
    "Change",
    "parse_ddl",
    "build_schema",
    "read_schema",
    "apply_script",
    "Applied",
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
    "review_schema",
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
