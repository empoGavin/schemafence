"""schemafence — a constraint layer between LLMs and databases.

Nothing in this package calls a language model.  That is deliberate:
every check here is deterministic, so it behaves identically with any
model, or with no model at all.

Public surface:
    parse_ddl(text)  -> Schema          (read a schema definition)
    analyze(schema)  -> list[Finding]   (the checks)
    guard(sql)       -> GuardResult     (the seven-layer guardrail)
    Embedder         -> text -> vector  (offline hash, or any OpenAI-compatible API)
    JsonStore/PgStore                   (the knowledge layer, two backends)
    Toolbox                             (the four tools, all behind guard())
    run_agent(...)   -> AgentRun        (the loop: tools, evidence, answer)
"""

from .checks import Finding, analyze, summarize
from .ddl import Column, Schema, Table, parse_ddl
from .guard import GuardResult, guard, run_selftest
from .knowledge import (Chunk, Embedder, Hit, JsonStore, PgStore,  # noqa: F401
                        chunk_markdown, cosine, embed_offline,
                        estimate_tokens, ingest_directory, read_corpus)
from .tools import TOOL_SPECS, Toolbox, ToolResult                 # noqa: F401

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
