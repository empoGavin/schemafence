"""The four tools, and the one rule they all obey.

  search_docs(query, k)              — pgvector over the operational notes
  run_sql(sql)                       — a guarded read-only query
  explain_sql(sql)                   — the query plan for a read-only query
  get_table_stats(schema, table)     — size, dead tuples, vacuum/analyse age

Nothing here talks to a language model either.  A tool is a function with a
JSON schema; whether a model or a rule picked it out of the toolbox, the
call arrives at exactly the same door — and the door is ``guard()``.

    advice lowers the chance of a mistake; a guard lowers its blast radius.

Every call is appended to an audit log (``agent_trace.jsonl``) with the
decision, the layer it was decided at, and how long it took.  That file is
the raw material for the Day 6 statistics panel and for the interview
question "how do you know it behaved?".
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .guard import guard

MAX_CELL = 200

# Where a retrieved passage stops being evidence.  Similarities from a real
# embedding model live around 0.5-0.7, so 0.45 is a starting point to be
# calibrated from the top-1 score column of --eval.  It is NOT applied to the
# offline hashed embedder, whose scores are an order of magnitude lower
# (0.089-0.285 on the bundled corpus) — an absolute floor there would throw
# away every real hit.
DEFAULT_DOC_MIN_SCORE = 0.45

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "Semantic search over the operational knowledge base "
                           "(runbooks, incident notes, design write-ups). Use this "
                           "for anything that is a matter of experience rather than "
                           "a matter of the current data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the question, in the "
                                                               "user's own words"},
                    "k": {"type": "integer", "description": "how many passages "
                                                            "(default 5, max 20)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Run one read-only SQL statement and return the rows. "
                           "Only SELECT / WITH / EXPLAIN are accepted; anything that "
                           "writes is rejected before it reaches the database.",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_sql",
            "description": "Return the execution plan of a read-only query. Use this "
                           "when a question is about why something is slow.",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_stats",
            "description": "Table-level statistics from the catalogue: size, estimated "
                           "rows, dead tuples, last vacuum/analyse and scan counts. "
                           "Use this for bloat or 'which table is the problem' "
                           "questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schema": {"type": "string",
                               "description": "schema name, e.g. shop; omit to "
                                              "search all user schemas"},
                    "table": {"type": "string",
                              "description": "bare table name, or a "
                                             "schema-qualified name like "
                                             "shop.orders (both accepted)"},
                },
                "required": [],
            },
        },
    },
]

TABLE_STATS_SQL = """
SELECT n.nspname                                   AS schema_name,
       c.relname                                   AS table_name,
       pg_total_relation_size(c.oid)               AS total_bytes,
       c.reltuples::bigint                         AS est_rows,
       s.n_dead_tup                                AS dead_tuples,
       s.n_live_tup                                AS live_tuples,
       s.seq_scan, s.idx_scan,
       s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE c.relkind = 'r'
   AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
   AND (%(schema)s::text IS NULL OR n.nspname = %(schema)s::text)
   AND (%(table)s::text  IS NULL OR c.relname  = %(table)s::text)
 ORDER BY pg_total_relation_size(c.oid) DESC
 LIMIT 20
"""


def _clip(value) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


def human_bytes(n: int | None) -> str:
    if not n:
        return "0 B"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= step
    return f"{n:.1f} TB"


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: dict = field(default_factory=dict)
    ms: float = 0.0
    decision: str = "ok"
    layer: str = ""

    def brief(self) -> str:
        if not self.ok:
            if self.layer:
                reason = self.data.get("reason") or self.decision
                return f"BLOCKED at {self.layer}: {reason}"
            reason = self.data.get("error") or self.data.get("reason") or self.decision
            return f"not run: {reason}"
        if self.tool == "search_docs":
            return f"{len(self.data.get('hits', []))} passage(s)"
        if self.tool == "run_sql":
            return f"{self.data.get('rowcount', 0)} row(s)"
        if self.tool == "explain_sql":
            return f"{len(self.data.get('plan', []))} plan line(s)"
        if self.tool == "get_table_stats":
            return f"{len(self.data.get('tables', []))} table(s)"
        return "ok"


class Toolbox:
    def __init__(self, store=None, conn=None, whitelist=None,
                 trace_path: str | Path = "agent_trace.jsonl",
                 max_rows: int = 100, timeout_ms: int = 10_000,
                 search_path: str | None = None,
                 doc_min_score: float | None = None):
        self.store = store
        self.conn = conn
        self.whitelist = whitelist
        # Read-only tools are not the only thing that needs a fence: a
        # retrieval result below the relevance floor is noise, and noise
        # handed to a model that was told to cite becomes a citation.
        # None means "pick a default from the embedding mode" (see
        # search_docs); SF_DOC_MIN_SCORE overrides for everyone.
        if doc_min_score is None:
            raw = os.environ.get("SF_DOC_MIN_SCORE")
            doc_min_score = float(raw) if raw not in (None, "") else None
        self.doc_min_score = doc_min_score
        self.trace_path = Path(trace_path)
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms
        # An unqualified table name only resolves if the session search_path
        # covers its schema.  The demo schema lives in `shop`, the default
        # search_path does not, so an LLM writing `FROM orders` fails with
        # UndefinedTable no matter how correct the rest of the query is.
        # The operator passes --search-path shop; anything else is the caller's
        # own DSN option, not this layer's business.
        if search_path and not re.fullmatch(r"[A-Za-z0-9_,\$ ]+", search_path):
            raise ValueError(f"--search-path looks wrong: {search_path!r}")
        self.search_path = search_path
        self.audit: list[dict] = []

    # -- audit -------------------------------------------------------------- #

    def _log(self, result: ToolResult, args: dict) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": result.tool,
            "args": {k: (_clip(v) if k != "sql" else v) for k, v in args.items()},
            "decision": result.decision,
            "layer": result.layer,
            "ok": result.ok,
            "ms": result.ms,
        }
        self.audit.append(entry)
        try:
            with self.trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # an audit failure must never break the query path

    # -- dispatch ----------------------------------------------------------- #

    def call(self, name: str, args: dict | None = None) -> ToolResult:
        args = args or {}
        started = time.perf_counter()
        handler = getattr(self, name, None)
        if handler is None or name not in {s["function"]["name"] for s in TOOL_SPECS}:
            result = ToolResult(name, False, {"error": f"unknown tool: {name}"},
                                decision="unknown-tool")
        else:
            try:
                data = handler(**args)
                result = ToolResult(name, data.get("ok", True) if isinstance(data, dict) else True,
                                    data)
            except TypeError as exc:
                result = ToolResult(name, False, {"error": f"bad arguments: {exc}"},
                                    decision="bad-arguments")
            except Exception as exc:  # noqa: BLE001 - the agent must survive any tool
                result = ToolResult(name, False, {"error": f"{type(exc).__name__}: {exc}"},
                                    decision="tool-error")
        result.ms = round((time.perf_counter() - started) * 1000, 2)
        self._log(result, args)
        return result

    # -- tools -------------------------------------------------------------- #

    def search_docs(self, query: str, k: int = 5) -> dict:
        if self.store is None:
            return {"ok": False, "error": "knowledge store not loaded — run --ingest first"}
        # k=0 must clamp up to the documented minimum of 1, not fall back to
        # the default: `int(k or 5)` treats 0 as "missing" and returns five
        # passages, so the max(1, ...) below never fired for the one value a
        # caller would use to mean "one".
        k = 5 if k is None else max(1, min(int(k), 20))
        embedder = getattr(self.store, "embedder", None)
        try:
            if embedder is not None:
                hits = self.store.search(embedder.one(query), k=k)
            else:
                from .knowledge import embed_offline
                hits = self.store.search(
                    embed_offline(query, getattr(self.store, "dim", 1024),
                                  getattr(self.store, "idf", None) or None), k=k)
        except Exception as exc:
            # A missing doc_chunks table is deterministic, and the raw
            # UndefinedTable gives the model nothing to act on — it just
            # retried four times, each paying for an embedding request.
            # Name the operator's fix and tell the model to stop.
            if type(exc).__name__ == "UndefinedTable":
                return {"ok": False, "error":
                        f"the knowledge store is not initialised: {exc} — "
                        "run agent_cli.py --ingest first. Do not retry "
                        "search_docs until the operator has done that."}
            raise
        # Relevance floor.  Top-k always returns k passages — the nearest
        # neighbours of a question the corpus does not cover are still
        # returned, and a model told to cite something will cite them
        # (the "idle in transaction" answer leaned on a subtransaction
        # case that had nothing to do with it).  Below the floor a
        # passage is noise; returning nothing is a legitimate answer and
        # the model is told so explicitly.  The default is a starting
        # point — calibrate it against the top-1 score column of --eval.
        #
        # Keyed on the embedder's MODE, not on whether an embedder is
        # attached: the CLI always attaches one (offline runs included),
        # so testing for its presence applied the API floor to hashed
        # lexical scores that run 5-10x lower — every passage dropped,
        # the agent answering "the note may not exist yet" about notes
        # it had just ingested.
        floor = self.doc_min_score
        if floor is None:
            mode = getattr(embedder, "mode", "offline") if embedder is not None else "offline"
            floor = 0.0 if mode == "offline" else DEFAULT_DOC_MIN_SCORE
        kept = [h for h in hits if h.score >= floor]
        out = {"ok": True, "k": k, "min_score": floor,
               "top_score": round(hits[0].score, 4) if hits else None,
               "dropped_below_floor": len(hits) - len(kept),
               "hits": [{"source": h.source, "section": h.section,
                         "score": h.score, "snippet": _clip(h.content)}
                        for h in kept]}
        if not kept:
            out["note"] = ("no passage scored at or above the relevance floor "
                           f"({floor}); the closest scored {out['top_score']}. "
                           "The knowledge base has no matching note — say that "
                           "plainly and answer from the tool evidence, do not "
                           "stretch a near-miss passage into a citation.")
        return out

    def run_sql(self, sql: str) -> dict:
        verdict = guard(sql, max_rows=self.max_rows, table_whitelist=self.whitelist,
                        timeout_ms=self.timeout_ms)
        if not verdict.ok:
            return {"ok": False, "blocked": True, "layer": verdict.layer,
                    "reason": verdict.reason, "audit": verdict.audit}
        if self.conn is None:
            return {"ok": False, "blocked": False, "error":
                    "no database attached — start PostgreSQL and pass --db"}
        columns, rows = self._execute(verdict.sql, verdict.session_sql)
        return {"ok": True, "sql": verdict.sql, "rewritten": verdict.rewritten,
                "tables": verdict.tables, "columns": columns, "rows": rows,
                "rowcount": len(rows), "limit": self.max_rows}

    def explain_sql(self, sql: str) -> dict:
        body = sql.strip().rstrip(";")
        if body.upper().startswith("EXPLAIN"):
            probe = body
        else:
            probe = f"EXPLAIN (COSTS ON, VERBOSE OFF)\n{body}"
        # table_whitelist stays None on purpose: EXPLAIN returns a plan, not
        # rows, and without ANALYZE it executes nothing — L5 is not the layer
        # that matters here.  (A plain whitelist= typo here once made every
        # plan question die with a TypeError before the guard even ran.)
        verdict = guard(probe, max_rows=self.max_rows, table_whitelist=None,
                        timeout_ms=self.timeout_ms)
        if not verdict.ok:
            return {"ok": False, "blocked": True, "layer": verdict.layer,
                    "reason": verdict.reason}
        if self.conn is None:
            return {"ok": False, "blocked": False, "error":
                    "no database attached — start PostgreSQL and pass --db"}
        _columns, rows = self._execute(verdict.sql, verdict.session_sql, clip=False)
        return {"ok": True, "plan": [r[0] for r in rows]}

    def get_table_stats(self, schema: str | None = None, table: str | None = None) -> dict:
        # A model reads the catalogue, sees shop.orders, and passes the whole
        # qualified name in `table` — accept it instead of returning zero rows
        # because relname is only the bare part.
        if table and "." in table and not schema:
            schema, _, table = table.partition(".")
        params = {"schema": schema or None, "table": table or None}
        verdict = guard(TABLE_STATS_SQL, max_rows=20, timeout_ms=self.timeout_ms)
        if not verdict.ok:
            return {"ok": False, "blocked": True, "layer": verdict.layer,
                    "reason": verdict.reason}
        if self.conn is None:
            return {"ok": False, "blocked": False, "error":
                    "no database attached — start PostgreSQL and pass --db"}

        columns, rows = self._execute(verdict.sql, verdict.session_sql, params,
                                      clip=False)
        out = []
        for row in rows:
            record = dict(zip(columns, row))
            dead = record.get("dead_tuples") or 0
            live = record.get("live_tuples") or 0
            total = dead + live
            out.append({
                "table": f"{record['schema_name']}.{record['table_name']}",
                "size": human_bytes(record["total_bytes"]),
                "est_rows": record["est_rows"],
                "dead_tuples": dead,
                "dead_ratio": round(dead / total, 3) if total else 0.0,
                "last_analyze": str(record["last_analyze"] or "never"),
                "last_autovacuum": str(record["last_autovacuum"] or "never"),
                "seq_scan": record["seq_scan"],
                "idx_scan": record["idx_scan"],
            })
        out.sort(key=lambda t: -t["dead_ratio"])
        return {"ok": True, "tables": out}

    # -- execution ---------------------------------------------------------- #

    def _execute(self, sql: str, session_sql: list[str], params: dict | None = None,
                 clip: bool = True):
        """Apply the database-side half of the guardrail, then run the query.

        ``default_transaction_read_only`` is layer 8 on purpose: even if the
        string checker were bypassed, the session still cannot write.

        ``clip`` is for *display* paths.  Callers that consume the values as
        numbers (get_table_stats) or as multi-line text (EXPLAIN plans) must
        pass clip=False — a clipped cell turns total_bytes into a string and
        human_bytes dies on ``n < 1024.0``.
        """
        with self.conn.cursor() as cur:
            if self.search_path:
                # The knowledge store shares this connection, and doc_chunks
                # lives in public.  `SET search_path TO shop` alone erased
                # public from name resolution, so every search_docs died with
                # UndefinedTable right after a successful get_table_stats —
                # while psql, with its default search_path, saw the table fine.
                # --search-path asks for a schema to be *findable*, not for
                # public to disappear.
                parts = re.split(r"[\s,]+", self.search_path.strip())
                if "public" not in parts:
                    parts.append("public")
                cur.execute(f"SET search_path TO {', '.join(parts)}")
            for statement in session_sql:
                cur.execute(statement)
            cur.execute(sql, params)
            columns = [d.name for d in (cur.description or [])]
            rows = cur.fetchall() if cur.description else []
        if clip:
            return columns, [[_clip(v) for v in row] for row in rows[:self.max_rows]]
        return columns, list(rows[:self.max_rows])


# --------------------------------------------------------------------------- #
# [tools] selftest — every tool, with no database and no network
# --------------------------------------------------------------------------- #

def run_tool_selftest() -> tuple[list[tuple[str, str, bool]], bool]:
    """Smoke-test the tool layer with nothing attached.

    This exists because of a bug the unit tests did not catch: the relevance
    floor asked "is an embedder attached?", but ``open_store`` attaches one on
    every path — offline runs included — so offline answers were filtered with
    the API floor (0.45) against hashed lexical scores in the 0.08–0.28 range.
    Every passage was dropped, and the agent told the user "the note may not
    exist yet" about notes it had just ingested.  The test that missed it
    constructed a store with ``embedder = None``, i.e. the wiring the product
    does not use.

    So this builds the store the way the CLI builds it, and asserts on the
    observable behaviour: passages come back offline, the floor is honoured
    when set, a missing database is *reported* rather than raised, and bad
    arguments do not take the process down.

    Returns (rows, passed_all) with rows shaped like the guard and router
    selftests so demo.py can print all three the same way.
    """
    import tempfile
    from pathlib import Path

    from .knowledge import Embedder, JsonStore, ingest_directory

    rows: list[tuple[str, str, bool]] = []

    def check(name: str, expected: str, ok: bool) -> None:
        rows.append((name, expected, bool(ok)))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pg-bloat.md").write_text(
            "# 判定标准\n死元组占比超过 20%，且 autovacuum 长时间没有运行，"
            "就先治理膨胀，再考虑加索引。\n",
            encoding="utf-8")
        embedder = Embedder(mode="offline", dim=64)
        store = JsonStore(root / "store.json", dim=64)
        ingest_directory(root, store, embedder)
        store.embedder = embedder            # exactly what open_store() does
        box = Toolbox(store=store, trace_path=root / "trace.jsonl")

        result = box.call("search_docs", {"query": "死元组占比多少要治理膨胀？", "k": 5})
        check("search_docs / offline hit",
              "ok, hits>=1, floor 0",
              result.ok and len(result.data.get("hits") or []) >= 1
              and result.data.get("min_score") == 0.0)

        strict = Toolbox(store=store, trace_path=root / "trace.jsonl",
                         doc_min_score=0.99)
        result = strict.call("search_docs", {"query": "死元组占比多少要治理膨胀？", "k": 5})
        check("search_docs / floor honoured",
              "ok, 0 hits + note",
              result.ok and not result.data.get("hits")
              and "no matching note" in (result.data.get("note") or ""))

        for name, args in (("run_sql", {"sql": "SELECT 1"}),
                           ("explain_sql", {"sql": "SELECT 1"}),
                           ("get_table_stats", {})):
            result = box.call(name, args)
            message = str(result.data.get("error") or result.data.get("reason") or "")
            check(f"{name} / no database",
                  "ok=False, says so",
                  (not result.ok) and "no database" in message)

        result = box.call("get_table_stats", {"bogus": 1})
        check("get_table_stats / bad args",
              "decision=bad-arguments",
              result.decision == "bad-arguments")

    return rows, all(passed for _, _, passed in rows)
