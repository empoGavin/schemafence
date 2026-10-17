#!/usr/bin/env python3
"""schemafence QA harness — zero dependencies, offline only.

Reproduces every case in ``tests/test-cases.md`` that can run without a live
database, an API key, or a network.  Prints one PASS/FAIL line per case and
exits non-zero if any case failed.

    python tests/qa_probe.py

Temporary stores and outputs land under ``tests/tmp/`` (gitignored).

Every CLI child runs with an empty home and no ``SF_*`` inherited — see
``child_env``, and section I, which asserts it.  ``SF_QA_TIMEOUT`` (seconds,
default 300) caps a single CLI call.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from schemafence import __version__ as shipped                   # noqa: E402
from schemafence.agent import route, run_route_selftest          # noqa: E402
from schemafence.checks_sql import (SQL_SAMPLE_DDL, check_sql,    # noqa: E402
                                    run_sql_selftest)
from schemafence.ddl import CHANGE_KINDS, parse_ddl               # noqa: E402
from schemafence.facts import FACT_MODELS, PROVIDERS, rows_for    # noqa: E402
from schemafence.guard import guard, run_selftest                # noqa: E402
from schemafence.knowledge import (Chunk, Embedder, JsonStore,    # noqa: E402
                                   chunk_markdown, ingest_directory)
from schemafence.rulepack import (find_overlaps, infer_rule,      # noqa: E402
                                  load_rulepack, review_schema,
                                  run_rulepack_selftest, vocabulary)
from schemafence.snapshot import (COLUMNS_SQL, TABLES_SQL,        # noqa: E402
                                  apply_script, build_schema)
from schemafence.tools import Toolbox, run_tool_selftest          # noqa: E402

PY = sys.executable
CLI = str(ROOT / "agent_cli.py")
TRACE = str(TMP / "qa-trace.jsonl")
# The children's home — a directory with no .schemafence.env in it.
CHILD_HOME = TMP / "home"
CHILD_HOME.mkdir(parents=True, exist_ok=True)
TIMEOUT = int(os.environ.get("SF_QA_TIMEOUT", "300"))
RESULTS: list[tuple[str, str, bool, str]] = []
TIMINGS: list[tuple[str, float]] = []


def check(cid: str, desc: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((cid, desc, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {cid:<4} {desc}" + (f"   [{detail}]" if detail else ""))


def selftest_check(cid: str, desc: str, run) -> None:
    """Run a built-in selftest as one case.  A raise becomes a FAIL line.

    An exception here used to end the process, and the summary that names the
    failing cases is printed at the very end, so a crashing unit erased the
    diagnosis it was supposed to produce.
    """
    try:
        rows, ok = run()
    except Exception as exc:
        check(cid, desc, False, f"{type(exc).__name__}: {exc}")
        return
    check(cid, desc, ok, f"{len(rows)} cases")


def child_env(extra: dict | None = None) -> dict:
    """The environment every child process here runs under.

    ``agent_cli.py`` loads ``~/.schemafence.env`` at startup, so the operator's
    key file reached this harness — which claims to be offline.  With
    ``SF_EMBED_API_KEY`` in it the embedder switches to the API and ``--eval``
    stops being 44 hash lookups: it becomes 44 HTTP calls, and on a VM that
    exceeded the subprocess cap without printing anything but a timeout.
    ``load_env_file`` never overrides a variable that is already set, so
    dropping ``SF_*`` also covers keys exported in the calling shell, and
    pointing HOME at an empty directory removes the file itself.  Cases that
    need a setting pass it in ``extra`` and get exactly that.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SF_")}
    env["HOME"] = str(CHILD_HOME)
    env["USERPROFILE"] = str(CHILD_HOME)        # the same directory on Windows
    if extra:
        env.update(extra)
    return env


def cli(args, env=None, timeout=None):
    if timeout is None:
        timeout = TIMEOUT
    started = time.perf_counter()
    try:
        return subprocess.run([PY, CLI, *args], capture_output=True, text=True,
                              env=child_env(env), cwd=str(ROOT), timeout=timeout)
    finally:
        label = args[0] if args else "(no args)"
        if label == "--ask" and len(args) > 1:
            label += " " + args[1][:12]
        TIMINGS.append((label, time.perf_counter() - started))


def review_cli(args, env=None, timeout=None):
    """The review entry point, run the way a pipeline would run it."""
    if timeout is None:
        timeout = TIMEOUT
    started = time.perf_counter()
    try:
        return subprocess.run([PY, "-m", "schemafence.review", *args],
                              capture_output=True, text=True, env=child_env(env),
                              cwd=str(ROOT), timeout=timeout)
    finally:
        label = args[0] if args else "(no args)"
        TIMINGS.append(("review " + label, time.perf_counter() - started))


# --------------------------------------------------------------------------- #
# A. guard gates
# --------------------------------------------------------------------------- #
def guard_cases() -> None:
    print("\n[A] guard — seven layers + bypasses")
    selftest_check("A0", "run_selftest() 13 built-in cases all pass", run_selftest)

    def blocked(sql, layer):
        r = guard(sql)
        return (not r.ok) and r.layer == layer, f"ok={r.ok} layer={r.layer}"

    def allowed(sql):
        r = guard(sql)
        return r.ok, f"ok={r.ok} layer={r.layer} reason={r.reason}"

    check("A1", "L1 blocks multi-statement SELECT;DROP", *blocked(
        "SELECT 1; DROP TABLE shop.users", "L1"))
    check("A2", "L1 blocks comment-hidden second statement", *blocked(
        "SELECT 1 -- \n; DROP TABLE shop.users", "L1"))
    check("A3", "L1 blocks empty statement", *blocked("   ", "L1"))
    check("A4", "L2 allows SELECT 1", *allowed("SELECT 1"))
    check("A5", "L2 blocks DDL (DROP)", *blocked("DROP TABLE shop.orders", "L2"))
    check("A6", "L2 blocks lowercase DML (delete)", *blocked(
        "  delete from shop.orders", "L2"))
    check("A7", "L2 blocks SET (session write)", *blocked(
        "SET search_path TO evil", "L2"))
    check("A8", "L3 blocks DELETE hidden in a CTE", *blocked(
        "WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT * FROM d", "L3"))
    check("A9", "L3 blocks UPDATE hidden in a CTE", *blocked(
        "WITH u AS (UPDATE shop.orders SET amount=0 RETURNING *) SELECT * FROM u", "L3"))
    check("A10", "L4 blocks pg_sleep", *blocked("SELECT pg_sleep(60)", "L4"))
    check("A11", "L4 blocks lo_import", *blocked("SELECT lo_import('/etc/passwd')", "L4"))
    r = guard("SELECT * FROM shop.orders", table_whitelist=["orders"])
    check("A12", "L5 allows a whitelisted table", r.ok, f"ok={r.ok}")
    r = guard("SELECT * FROM secret.creds", table_whitelist=["orders"])
    check("A13", "L5 blocks a table outside the whitelist",
          (not r.ok) and r.layer == "L5", f"ok={r.ok} layer={r.layer}")
    r = guard("SELECT * FROM shop.orders")
    check("A14", "L6 rewrites a LIMIT-less query (LIMIT 100)",
          r.ok and r.rewritten and "LIMIT 100" in r.sql, r.sql)
    r = guard("SELECT * FROM shop.orders LIMIT 5")
    check("A15", "L6 leaves an explicit LIMIT alone",
          r.ok and not r.rewritten, f"rewritten={r.rewritten}")
    r = guard("SELECT 1")
    check("A16", "L7 audit dict + session hardening SQL present",
          r.audit.get("decision") == "allowed" and len(r.session_sql) == 3,
          str(r.audit))
    r2 = guard("DROP TABLE t")
    check("A17", "L7 blocked audit records the layer",
          r2.audit.get("decision") == "blocked" and r2.audit.get("layer") == "L2")
    check("A18", "-- comment cannot hide a keyword (line comment)",
          *allowed("SELECT * FROM shop.orders -- ; DROP TABLE shop.users"))
    check("A19", "/* */ comment cannot hide a keyword",
          *allowed("SELECT /* DROP TABLE shop.users */ 1"))
    check("A20", "keyword inside a string literal is not a false positive",
          *allowed("SELECT 'DROP TABLE t' AS s"))
    # write-capable SELECT forms (regression for BUG-1)
    check("A21", "SELECT ... INTO (CREATE TABLE AS) is blocked", *blocked(
        "SELECT * INTO shop.orders_bak FROM shop.orders", "L3"))
    check("A22", "nextval() sequence write is blocked", *blocked(
        "SELECT nextval('shop.orders_id_seq')", "L4"))
    check("A23", "setval() sequence write is blocked", *blocked(
        "SELECT setval('shop.orders_id_seq', 1)", "L4"))
    check("A24", "FOR SHARE locking clause is blocked", *blocked(
        "SELECT * FROM shop.orders FOR SHARE", "L2"))
    # A read-only statement may name a column `comment`; L3 used to take that
    # word for the COMMENT statement and refuse the statement outright.
    check("A25", "L3 leaves a column aliased `comment` alone", *allowed(
        "SELECT obj_description(c.oid, 'pg_class') AS comment FROM pg_class c"))
    check("A26", "COMMENT ON is still refused, and at L2", *blocked(
        "COMMENT ON TABLE shop.orders IS 'x'", "L2"))
    # A LIMIT inside parentheses bounds that subquery, not this call.
    nested = guard("SELECT * FROM shop.orders WHERE id IN "
                   "(SELECT order_id FROM shop.items LIMIT 1)")
    check("A27", "L6 still caps a statement whose only LIMIT is nested",
          nested.ok and nested.rewritten and "LIMIT 100" in nested.sql,
          f"rewritten={nested.rewritten} sql={nested.sql!r}")
    plain = guard("SELECT * FROM shop.orders LIMIT 5 OFFSET 2")
    check("A28", "L6 leaves a top-level LIMIT with OFFSET alone",
          plain.ok and not plain.rewritten, f"rewritten={plain.rewritten}")
    tables_read, columns_read = guard(TABLES_SQL), guard(COLUMNS_SQL)
    check("A29", "both catalogue reads pass the guard",
          tables_read.ok and columns_read.ok,
          f"tables: {tables_read.layer} {tables_read.reason}; "
          f"columns: {columns_read.layer} {columns_read.reason}")
    check("A30", "the column read gets its outer cap from the guard",
          columns_read.ok and "LIMIT 100" in columns_read.sql,
          columns_read.sql[-40:])


# --------------------------------------------------------------------------- #
# B. routing
# --------------------------------------------------------------------------- #
def route_cases() -> None:
    print("\n[B] router — write intent vs practice question")
    selftest_check("B0", "run_route_selftest() 10 cases all pass", run_route_selftest)

    def first(q):
        calls, reasons = route(q)
        return (calls[0][0] if calls else "?"), (reasons[0] if reasons else "")

    kind, reason = first("帮我删掉 orders 表 2024 年之前的数据")
    check("B1", "write imperative is refused", kind == "__refuse__", reason)
    kind, reason = first("truncate 大表会锁多久？")
    check("B2", "write keyword + question goes to the knowledge base",
          kind == "search_docs", reason)
    kind, reason = first("把参数从配置文件里删掉和设成 0，效果一样吗？")
    check("B3", "删除 + 吗 is answered, not refused", kind == "search_docs", reason)
    kind, reason = first("请给我建表语句")
    check("B4", "请给我 + 建表 (ACT_REQUEST) is refused", kind == "__refuse__", reason)
    kind, reason = first("建表时要注意什么")
    check("B5", "建表 + question goes to the knowledge base",
          kind == "search_docs", reason)
    kind, reason = first("我不确定")
    check("B6", "vague question is clarified", kind == "__clarify__", reason)
    kind, reason = first("索引没被用上，怎么排查？")
    check("B7", "a how-to question reaches search_docs with a reason",
          kind == "search_docs" and reason, reason)


# --------------------------------------------------------------------------- #
# C. knowledge retrieval
# --------------------------------------------------------------------------- #
def knowledge_cases() -> None:
    print("\n[C] knowledge — recall, floor, clamp, chunking")
    dba = TMP / "dba.json"
    if not dba.exists():
        from schemafence.knowledge import ingest_directory as _ing
        store = JsonStore(dba, dim=1024)
        _ing(ROOT / "examples" / "knowledge-dba", store, embedder=Embedder(mode="offline"))
        store.save()

    store = JsonStore.load(dba, dim=1024)
    emb = Embedder(mode="offline", dim=1024)
    emb.idf = store.idf
    store.embedder = emb
    box = Toolbox(store=store, trace_path=TRACE)

    r = box.call("search_docs", {"query": "死元组占比多少需要处理膨胀？", "k": 5})
    check("C1", "offline recall returns >=1 passage",
          r.ok and len(r.data.get("hits") or []) >= 1,
          f"hits={len(r.data.get('hits') or [])}")
    check("C2", "offline mode floor defaults to 0.0",
          r.data.get("min_score") == 0.0, str(r.data.get("min_score")))

    class ApiEmbedder:
        mode, dim = "api", 8
        def one(self, text, *a):
            return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    s8 = JsonStore(TMP / "floor8.json", dim=8)
    s8.chunks = [Chunk("d", "s", "x", 1, [0.5, 0, 0, 0, 0, 0, 0, 0])]
    s8.embedder = ApiEmbedder()
    ra = Toolbox(store=s8, trace_path=TRACE).call("search_docs", {"query": "x", "k": 3})
    check("C3", "api mode floor defaults to 0.45", ra.data.get("min_score") == 0.45,
          str(ra.data.get("min_score")))

    strict = Toolbox(store=store, trace_path=TRACE, doc_min_score=0.99)
    rs = strict.call("search_docs", {"query": "死元组占比多少需要处理膨胀？", "k": 5})
    check("C4", "a floor above every score drops all hits and says so",
          rs.ok and not rs.data.get("hits") and "no matching note" in (rs.data.get("note") or ""))

    os.environ["SF_DOC_MIN_SCORE"] = "0.99"
    re_ = Toolbox(store=store, trace_path=TRACE).call("search_docs", {"query": "x", "k": 3})
    del os.environ["SF_DOC_MIN_SCORE"]
    check("C5", "SF_DOC_MIN_SCORE env overrides the default floor",
          re_.data.get("min_score") == 0.99, str(re_.data.get("min_score")))

    r99 = box.call("search_docs", {"query": "x", "k": 99})
    check("C6", "k is clamped at 20", r99.data.get("k") == 20, str(r99.data.get("k")))
    r0 = box.call("search_docs", {"query": "x", "k": 0})
    check("C7", "k=0 clamps up to 1, not the default 5", r0.data.get("k") == 1,
          str(r0.data.get("k")))

    doc = "# 判定指南\n\n## 治理标准\n死元组占比超过 20% 就先治理膨胀。\n"
    chunks = chunk_markdown(doc, "note")
    check("C8", "chunk content keeps the heading text inline",
          bool(chunks) and any("治理标准" in c.content for c in chunks),
          f"chunks={len(chunks)}")

    empty = JsonStore(TMP / "empty.json", dim=8)
    empty.embedder = Embedder(mode="offline", dim=8)
    remp = Toolbox(store=empty, trace_path=TRACE).call("search_docs", {"query": "x", "k": 3})
    check("C9", "empty store degrades to a no-matching-note message",
          remp.ok and not remp.data.get("hits") and remp.data.get("note"))

    rn = Toolbox(store=None, trace_path=TRACE).call("search_docs", {"query": "x", "k": 3})
    check("C10", "no store at all reports 'not loaded' instead of raising",
          (not rn.ok) and "not loaded" in (rn.data.get("error") or ""),
          rn.data.get("error", ""))

    # C11/C12 — orphan detection.  PgStore.replace deletes by source, so a
    # corpus swap leaves the previous notes held and nothing errors; the JSON
    # store swaps its whole list.  The comparison direction is what has to be
    # right, so it is tested directly rather than only through an ingest.
    from agent_cli import orphan_sources
    two = JsonStore(TMP / "orphan.json", dim=8)
    two.chunks = [Chunk("a.md", "s", "x", 1, [1.0] + [0.0] * 7),
                  Chunk("b.md", "s", "y", 1, [0.0, 1.0] + [0.0] * 6)]
    kept = [c for c in two.chunks if c.source == "a.md"]
    check("C11", "orphan_sources names the rows the corpus did not cover",
          orphan_sources(two, kept) == ["b.md"], str(orphan_sources(two, kept)))
    check("C12", "orphan_sources is empty when the corpus covers the store",
          orphan_sources(two, two.chunks) == [])


# --------------------------------------------------------------------------- #
# D. CLI behaviour
# --------------------------------------------------------------------------- #
def cli_cases() -> None:
    print("\n[D] CLI — ingest / ask / eval / report / bad input")
    store = str(TMP / "dba.json")
    corpus = "examples/knowledge-dba"

    p = cli(["--ingest", corpus, "--store", store])
    check("D1", "--ingest offline writes a store, exit 0",
          p.returncode == 0 and Path(store).exists(), f"rc={p.returncode}")

    p = cli(["--ask", "表膨胀怎么处理？", "--store", store, "--corpus", corpus,
             "--trace", TRACE])
    check("D2", "--ask doc question answers from the knowledge base",
          p.returncode == 0 and "From the knowledge base:" in p.stdout
          and "search_docs" in p.stdout, f"rc={p.returncode}")

    p = cli(["--ask", "帮我删掉 orders 表", "--store", store, "--corpus", corpus,
             "--trace", TRACE])
    check("D3", "--ask write imperative is refused",
          p.returncode == 0 and "I cannot do that" in p.stdout, f"rc={p.returncode}")

    p = cli(["--ask", "有几条订单？", "--store", store, "--corpus", corpus,
             "--trace", TRACE])
    check("D4", "--ask numeric question reports no database (no crash)",
          p.returncode == 0 and "no database attached" in p.stdout)

    p = cli(["--eval", "--store", store, "--corpus", corpus,
             "--eval-file", "eval/questions-dba.md"])
    check("D5", "--eval prints 44 questions, hit rate 97.7%",
          p.returncode == 0 and "questions   : 44" in p.stdout
          and "hit rate    : 97.7%" in p.stdout, f"rc={p.returncode}")

    rpt = TMP / "report-gen.md"
    p = cli(["--report", str(rpt), "--store", store, "--corpus", corpus,
             "--eval-file", "eval/questions-dba.md"])
    check("D6", "--report writes to the given path, exit 0",
          p.returncode == 0 and rpt.exists(), f"rc={p.returncode}")

    p = cli([])
    check("D7", "no arguments prints help and exits 0",
          p.returncode == 0 and "usage:" in p.stdout, f"rc={p.returncode}")

    p = cli(["--ask", "x", "--store", str(TMP / "does-not-exist.json")])
    check("D8", "--ask on an empty store exits 2 with a hint",
          p.returncode == 2 and "the store is empty" in p.stdout, f"rc={p.returncode}")

    p = cli(["--eval", "--eval-file", str(TMP / "nope.md"), "--store", store])
    check("D9", "--eval on a missing question file exits 2",
          p.returncode == 2 and "no questions found" in p.stdout, f"rc={p.returncode}")

    for cid, flag, val in (("D10", "--min-score", "abc"), ("D11", "--llm", "bogus")):
        p = cli(["--ask", "x", flag, val])
        check(cid, f"{flag} {val} is rejected by argparse, exit 2",
              p.returncode == 2 and "error:" in p.stderr, f"rc={p.returncode}")

    p = cli(["--ask", "表膨胀怎么处理？", "--store", store, "--corpus", corpus,
             "--llm", "openai", "--trace", TRACE])
    check("D12", "--llm openai without a key falls back to the rules driver",
          p.returncode == 0 and "falling back to the rule-based router" in p.stdout
          and "driver: rules" in p.stdout, f"rc={p.returncode}")

    p = cli(["--ask", "表膨胀怎么处理？", "--store", store, "--corpus", corpus,
             "--llm", "openai", "--trace", TRACE],
            env={"SF_LLM_API_KEY": "dummy", "SF_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                 "SF_LLM_TIMEOUT": "2", "SF_LLM_RETRIES": "0"})
    check("D13", "dead model endpoint degrades to an evidence report, no traceback",
          p.returncode == 0 and "became unreachable" in p.stdout
          and "Traceback" not in p.stderr, f"rc={p.returncode}")

    p = cli(["--ingest", str(TMP / "no-such-dir"), "--store", str(TMP / "x.json")])
    check("D14", "--ingest a missing dir fails cleanly (no traceback)",
          p.returncode != 0 and "Traceback" not in p.stderr
          and "no such corpus" in (p.stdout + p.stderr), f"rc={p.returncode}")

    p = cli(["--ask", "表膨胀怎么处理？", "--store", store, "--corpus", corpus,
             "--search-path", "bad;drop", "--trace", TRACE])
    check("D15", "a malformed --search-path fails cleanly (no traceback)",
          p.returncode != 0 and "Traceback" not in p.stderr
          and "search-path" in (p.stdout + p.stderr), f"rc={p.returncode}")

    p = cli(["--ask", "x", "--mode", "api", "--store", store])
    check("D16", "--mode api without a key fails cleanly (no traceback)",
          p.returncode != 0 and "Traceback" not in p.stderr
          and "SF_EMBED_API_KEY" in (p.stdout + p.stderr), f"rc={p.returncode}")

    p = cli(["--report", str(TMP / "r2.md"), "--store", store,
             "--corpus", str(TMP / "no-such-dir"), "--eval-file", "eval/questions-dba.md"])
    check("D17", "--report with a missing corpus fails cleanly (no traceback)",
          p.returncode != 0 and "Traceback" not in p.stderr, f"rc={p.returncode}")

    p = cli(["--ask", "表膨胀怎么处理？", "--ask", "帮我删掉 orders 表",
             "--store", store, "--corpus", corpus, "--trace", TRACE])
    check("D18", "repeatable --ask handles every question",
          p.returncode == 0 and p.stdout.count("[ask]") == 2, f"rc={p.returncode}")

    p = cli(["--ingest", corpus, "--store", store, "--rebuild"])
    check("D19", "--rebuild on the JSON backend is a stated no-op, exit 0",
          p.returncode == 0 and "nothing to empty" in p.stdout, f"rc={p.returncode}")


# --------------------------------------------------------------------------- #
# E. tool layer
# --------------------------------------------------------------------------- #
def tool_cases() -> None:
    print("\n[E] tools — selftest, explain, search_path, dispatch")
    selftest_check("E0", "run_tool_selftest() 7 cases all pass", run_tool_selftest)

    class FakeCur:
        def __init__(self, log):
            self.log = log
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            self.log.append((sql, params))
        @property
        def description(self):
            return None
        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.log = []
        def cursor(self):
            return FakeCur(self.log)

    conn = FakeConn()
    b = Toolbox(conn=conn, trace_path=TRACE)
    r = b.call("explain_sql", {"sql": "SELECT 1"})
    sql = conn.log[-1][0]
    check("E1", "explain_sql wraps the body in EXPLAIN (COSTS ON, VERBOSE OFF)",
          r.ok and sql.startswith("EXPLAIN (COSTS ON, VERBOSE OFF)"),
          sql.replace("\n", " | "))

    r = b.call("explain_sql", {"sql": "DROP TABLE t"})
    check("E2", "explain_sql sends a write through the guard and blocks it",
          (not r.ok) and r.data.get("layer") == "L3", str(r.data.get("reason")))

    conn2 = FakeConn()
    b2 = Toolbox(conn=conn2, search_path="shop", trace_path=TRACE)
    b2.call("run_sql", {"sql": "SELECT 1"})
    sets = [s for s, _ in conn2.log if s.startswith("SET search_path")]
    check("E3", "search_path=shop is applied as 'shop, public'",
          sets == ["SET search_path TO shop, public"], str(sets))

    conn3 = FakeConn()
    b3 = Toolbox(conn=conn3, search_path="shop, public", trace_path=TRACE)
    b3.call("run_sql", {"sql": "SELECT 1"})
    sets = [s for s, _ in conn3.log if s.startswith("SET search_path")]
    check("E4", "public is not duplicated when already present",
          sets == ["SET search_path TO shop, public"], str(sets))

    r = b.call("definitely_not_a_tool", {})
    check("E5", "unknown tool name is reported, not crashed",
          (not r.ok) and r.decision == "unknown-tool", r.decision)

    conn4 = FakeConn()
    b4 = Toolbox(conn=conn4, trace_path=TRACE)
    b4.call("get_table_stats", {"table": "shop.orders"})
    params = conn4.log[-1][1]
    check("E6", "get_table_stats splits a qualified table name into schema+table",
          params == {"schema": "shop", "table": "orders"}, str(params))

    r = b.call("get_table_stats", {"bogus": 1})
    check("E7", "bad tool arguments are reported, not crashed",
          r.decision == "bad-arguments", r.decision)

    bnodb = Toolbox(trace_path=TRACE)          # no store, no connection
    r = bnodb.call("run_sql", {"sql": "SELECT 1"})
    check("E8", "run_sql with no database reports rather than raises",
          (not r.ok) and (not r.data.get("blocked"))
          and "no database" in (r.data.get("error") or ""),
          r.data.get("error", ""))


# --------------------------------------------------------------------------- #
# F. eval baselines
# --------------------------------------------------------------------------- #
def eval_cases() -> None:
    print("\n[F] eval — retrieval baselines")
    store = str(TMP / "dba.json")
    corpus = "examples/knowledge-dba"
    p = cli(["--ingest", corpus, "--store", store])
    p = cli(["--eval", "--store", store, "--corpus", corpus,
             "--eval-file", "eval/questions-dba.md"])
    hits = p.stdout.count("hit  (rank")
    check("F1", "knowledge-dba hit rate >= 97.7% (>=43/44)",
          p.returncode == 0 and hits >= 43, f"hits={hits}/44")

    knstore = str(TMP / "kn.json")
    cli(["--ingest", "examples/knowledge", "--store", knstore])
    p = cli(["--eval", "--store", knstore, "--corpus", "examples/knowledge"])
    check("F2", "examples/knowledge 16-question baseline is 100%",
          p.returncode == 0 and "hit rate    : 100.0%" in p.stdout)


# --------------------------------------------------------------------------- #
# G. artefact integrity — the layer suites as two commands over one path
# --------------------------------------------------------------------------- #
def layer_case() -> None:
    """The bug class this guards against: the terminal is right, the file is not.

    ``--suite guard`` needs no database and ``--suite checks`` does, so the run
    is two commands; both default to the same ``--out``.  The second used to
    replace the first, and nothing anywhere failed.  Run both, then read the
    file and assert the totals describe the file rather than the last command.
    """
    print("\n[G] layer artefact — two commands, one output path")
    out = TMP / "layers-split.json"
    if out.exists():
        out.unlink()

    first = subprocess.run([PY, str(ROOT / "scripts" / "test_layers.py"),
                            "--suite", "guard", "--out", str(out)],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    check("G1", "--suite guard alone exits 0", first.returncode == 0,
          f"rc={first.returncode}")

    second = subprocess.run([PY, str(ROOT / "scripts" / "test_layers.py"),
                             "--suite", "checks", "--out", str(out)],
                            capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    check("G2", "--suite checks over the same path exits 0", second.returncode == 0,
          f"rc={second.returncode}")

    if not out.exists():
        check("G3", "the second run does not delete the first suite's rows", False,
              "no file written")
        return
    payload = json.loads(out.read_text(encoding="utf-8"))
    suites = payload.get("suites") or {}
    guard_rows = suites.get("guard") or []
    checks_rows = suites.get("checks") or []

    check("G3", "the second run does not delete the first suite's rows",
          len(guard_rows) == 44, f"guard rows={len(guard_rows)}")
    check("G4", "the second suite's own rows are present too",
          len(checks_rows) == 18, f"checks rows={len(checks_rows)}")

    on_disk = len(guard_rows) + len(checks_rows)
    check("G5", "total matches the rows actually in the file",
          payload.get("total") == on_disk,
          f"total={payload.get('total')} rows={on_disk}")


# --------------------------------------------------------------------------- #
# H. scale corpus generator — the resource benchmark's input
# --------------------------------------------------------------------------- #
def scale_corpus_cases() -> None:
    """The scale benchmark is only reproducible if its corpus is.

    ``examples/knowledge-scale`` is generated rather than committed, so the
    generator is the artefact that has to keep working.  Assert the two things
    the runbook promises: the file count it was asked for, and a chunk count
    proportional to it (the benchmark sizes its expectations off the latter).
    """
    print("\n[H] scale corpus generator")
    out = TMP / "scale-corpus"
    p = subprocess.run([PY, str(ROOT / "scripts" / "make_scale_corpus.py"),
                        "--notes", "60", "--out", str(out)],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    check("H1", "generator writes the requested number of notes", p.returncode == 0
          and len(list(out.glob("*.md"))) == 60,
          f"rc={p.returncode} files={len(list(out.glob('*.md')))}")

    if p.returncode != 0:
        return
    sample = sorted(out.glob("*.md"))[0].read_text(encoding="utf-8")
    check("H2", "generated notes carry the synthetic-corpus disclaimer",
          "合成语料" in sample and "不含任何公司内部信息" in sample)

    # Re-running with a smaller --notes must not leave the previous, larger
    # run's files behind -- the benchmark would ingest them silently.
    subprocess.run([PY, str(ROOT / "scripts" / "make_scale_corpus.py"),
                    "--notes", "20", "--out", str(out)],
                   capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    check("H3", "a smaller re-run clears the previous run's notes",
          len(list(out.glob("*.md"))) == 20,
          f"files={len(list(out.glob('*.md')))}")

    chunks = 0
    for f in sorted(out.glob("*.md")):
        chunks += len(chunk_markdown(f.read_text(encoding="utf-8"), f.name))
    check("H4", "generated notes chunk (they are not one empty chunk each)",
          chunks >= 20, f"chunks={chunks}")


# --------------------------------------------------------------------------- #
# I. hermetic children — the harness runs on its own settings, not the machine's
# --------------------------------------------------------------------------- #
def hermetic_cases() -> None:
    """The CLI children must not pick up the operator's ``~/.schemafence.env``.

    Not hygiene for its own sake.  That file lives on any machine set up for
    live work, and a key in it switches the embedder to the API, which turns
    ``--eval`` from 44 hash lookups into 44 HTTP calls.  The dev box has no
    such file and the case takes 1.7 s; the VM that has one blew the cap and
    showed a timeout with nothing to say which run it came from.  Both cases
    fail if ``child_env`` ever stops stripping.
    """
    print("\n[I] hermetic children — no key file, no exported key")
    store = str(TMP / "hermetic.json")

    p = cli(["--ingest", "examples/knowledge", "--store", store])
    env_line = next((l for l in p.stdout.splitlines() if l.startswith("env file")), "")
    check("I1", "the child looks for its env file under this harness's empty home",
          env_line.startswith(f"env file   : {CHILD_HOME}") and "not found" in env_line,
          env_line.strip() or "no env file line in the output")

    os.environ["SF_EMBED_API_KEY"] = "not-a-real-key"
    os.environ["SF_EMBED_BASE_URL"] = "http://127.0.0.1:9/v1"
    try:
        p = cli(["--ingest", "examples/knowledge", "--store", store])
    finally:
        os.environ.pop("SF_EMBED_API_KEY", None)
        os.environ.pop("SF_EMBED_BASE_URL", None)
    embed_line = next((l for l in p.stdout.splitlines() if l.startswith("embedding")), "")
    check("I2", "a key exported in the calling shell does not reach the child",
          p.returncode == 0 and "offline" in embed_line,
          f"rc={p.returncode} {embed_line.strip()}")


def sql_semantics_cases() -> None:
    """``checks.py`` reads the schema, ``guard.py`` reads the statement.

    These read both, because a fan-out and a cancelled LEFT JOIN are only
    visible where the two meet: each half is valid on its own.  The cases below
    pin the two checks that fire, the five near misses that must stay silent,
    and the two abstentions that must produce a note instead of a guess.
    """
    print("\n[J] sql-checks — the schema and the statement, read together")
    selftest_check("J0", "run_sql_selftest() 12 cases all pass", run_sql_selftest)

    schema = parse_ddl(SQL_SAMPLE_DDL)

    review = check_sql(schema, "SELECT SUM(o.paid_amount) FROM shop.orders o "
                               "JOIN shop.order_items oi ON o.id = oi.order_id")
    check("J1", "fan-out through a 1:N join is high, and names both sides",
          len(review.findings) == 1 and review.findings[0].check == "S1"
          and review.findings[0].severity == "high",
          review.findings[0].subject if review.findings else "no finding")

    review = check_sql(schema, "SELECT o.id FROM shop.orders o "
                               "LEFT JOIN shop.refunds r ON o.id = r.order_id "
                               "WHERE r.refund_amount > 0")
    check("J2", "a LEFT JOIN cancelled by WHERE is high, and names the column",
          len(review.findings) == 1 and review.findings[0].check == "S3",
          review.findings[0].subject if review.findings else "no finding")

    review = check_sql(schema, "SELECT o.id FROM shop.orders o "
                               "LEFT JOIN shop.refunds r ON o.id = r.order_id "
                               "WHERE r.id IS NULL")
    check("J3", "the anti-join idiom stays silent", not review.findings,
          review.findings[0].subject if review.findings else "no finding")

    review = check_sql(schema, "SELECT SUM(oi.line_amount) FROM shop.orders o "
                               "JOIN shop.order_items oi ON o.id = oi.order_id")
    check("J4", "a measure from the multiplying side stays silent",
          not review.findings, review.findings[0].subject if review.findings else "no finding")

    review = check_sql(schema, "SELECT SUM(o.paid_amount) FROM shop.orders o "
                               "JOIN shop.order_items oi ON o.id = oi.order_id "
                               "GROUP BY oi.order_id")
    check("J5", "aggregating at the child's grain stays silent and says why",
          not review.findings and any("grain" in n for n in review.notes),
          review.notes[0] if review.notes else "no note")

    review = check_sql(schema, "SELECT SUM(o.paid_amount) FROM shop.orders o "
                               "JOIN shop.subscription s ON o.id = s.order_id")
    check("J6", "a join key under UNIQUE does not multiply",
          not review.findings, review.findings[0].subject if review.findings else "no finding")

    review = check_sql(schema, "SELECT t.amt FROM (SELECT SUM(o.paid_amount) AS amt "
                               "FROM shop.orders o JOIN shop.order_items oi "
                               "ON o.id = oi.order_id) t")
    check("J7", "a nested query is abstained on, with a note",
          not review.findings and any("nested" in n for n in review.notes),
          review.notes[0] if review.notes else "no note")

    ambiguous = parse_ddl(
        "CREATE TABLE s.a (id BIGSERIAL PRIMARY KEY, amount NUMERIC(12,2) NOT NULL);"
        "CREATE TABLE s.b (id BIGSERIAL PRIMARY KEY, a_id BIGINT NOT NULL,"
        " amount NUMERIC(12,2) NOT NULL);")
    review = check_sql(ambiguous, "SELECT SUM(amount) FROM s.a x JOIN s.b y ON x.id = y.a_id")
    check("J8", "a column owned by two tables is abstained on, not guessed",
          not review.findings and any("belongs to 2 tables" in n for n in review.notes),
          review.notes[0] if review.notes else "no note")

    wrote = "DELETE FROM shop.orders WHERE id = 1"
    check("J9", "these checks are not a guard: a write gets no verdict from them, "
                "and guard() still blocks it",
          guard(wrote).ok is False and not check_sql(schema, wrote).findings,
          f"guard_ok={guard(wrote).ok} findings={len(check_sql(schema, wrote).findings)}")

    p = subprocess.run([PY, str(ROOT / "demo.py")], capture_output=True, text=True,
                       cwd=str(ROOT), env=child_env(), timeout=120)
    check("J10", "demo.py runs the block and exits 0",
          p.returncode == 0 and "[sql-checks]" in p.stdout
          and "12 cases → all passed" in p.stdout,
          f"rc={p.returncode} block={'yes' if '[sql-checks]' in p.stdout else 'no'} "
          f"count={'yes' if '12 cases → all passed' in p.stdout else 'no'}")


def rulepack_cases() -> None:
    """A review rule is a file, and the file has to prove itself before it applies.

    The cases here are the load-bearing part of that claim: the examples in a
    pack are its acceptance test, a rule that matched nothing has to say so,
    and a script no reader understood has to come back as exit 2 rather than as
    "0 finding(s)" — the one output that reads as approval.
    """
    print("\n[K] rule packs — a rule as a file, applied to somebody else's script")
    selftest_check("K0", "run_rulepack_selftest() 15 cases all pass", run_rulepack_selftest)

    hygiene = str(ROOT / "rules" / "table-hygiene.json")
    change = str(ROOT / "rules" / "change-review.json")
    script = str(ROOT / "examples" / "change_script_review.sql")

    p = review_cli(["--rules", hygiene, "--rules", change, "--ddl", script])
    check("K1", "the shipped packs verify, and the example script is reviewed and fails",
          p.returncode == 1 and "examples verified" in p.stdout
          and "cannot reproduce" not in p.stdout
          and "9 finding(s), worst high" in p.stdout,
          f"rc={p.returncode} "
          f"verdict={'yes' if '9 finding(s), worst high' in p.stdout else 'no'}")

    p = review_cli(["--rules", hygiene, "--verify"])
    check("K2", "`--verify` runs the pack's own examples and exits 0",
          p.returncode == 0 and "14 example(s) → all passed" in p.stdout,
          f"rc={p.returncode}")

    p = review_cli(["--vocabulary"])
    try:
        vocab = json.loads(p.stdout)
    except json.JSONDecodeError:
        vocab = {}
    check("K3", "`--vocabulary` exports the closed vocabulary as the authoring prompt",
          p.returncode == 0
          and set(vocab.get("targets", {})) == {"table", "column", "change", "index"}
          and "authoring_contract" in vocab,
          f"rc={p.returncode} targets={sorted(vocab.get('targets', {}))}")

    clean = TMP / "k-clean.sql"
    clean.write_text("CREATE TABLE shop.t (id BIGSERIAL PRIMARY KEY);\n"
                     "COMMENT ON TABLE shop.t IS '订单表 用途:支付事实 在线';\n"
                     "COMMENT ON COLUMN shop.t.id IS '主键 标准:TS-001';\n", encoding="utf-8")
    p = review_cli(["--rules", hygiene, "--ddl", str(clean)])
    check("K4", "a script that satisfies every rule exits 0 and says what was checked",
          p.returncode == 0 and "no rule fired" in p.stdout
          and "matched" in p.stdout,
          f"rc={p.returncode}")

    blind = TMP / "k-blind.sql"
    blind.write_text("\\copy shop.orders from 'x.csv'\nVACUUM FULL;\n", encoding="utf-8")
    p = review_cli(["--rules", hygiene, "--ddl", str(blind)])
    check("K5", "a script no reader understood exits 2, never 0 with no findings",
          p.returncode == 2 and "NOTHING RECOGNISED" in p.stdout
          and "0 finding(s)" not in p.stdout,
          f"rc={p.returncode}")

    broken = TMP / "k-broken.json"
    pack = json.loads(Path(hygiene).read_text(encoding="utf-8"))
    pack["rulepack"] = "broken"
    pack["rules"] = [pack["rules"][3]]
    pack["rules"][0]["examples"]["silent"] = ["CREATE TABLE shop.t (id INT);"]
    broken.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    p = review_cli(["--rules", str(broken), "--ddl", script])
    check("K6", "a pack that fails its own examples is refused, and nothing is applied",
          p.returncode == 2 and "refused" in p.stdout
          and "failed its own examples" in p.stdout,
          f"rc={p.returncode}")

    schema = parse_ddl(Path(script).read_text(encoding="utf-8"))
    kinds = [c.kind for c in schema.changes]
    stubs = [t.qualified for t in schema.tables if t.created_by is None]
    check("K7", "a change script reads as actions: a dropped column, a dropped table, "
                "an altered table with no CREATE behind it",
          schema.statements == 9 and not schema.unread
          and ["drop_column", "drop_table"] == [k for k in kinds
                                                if k in ("drop_column", "drop_table")]
          and stubs == ["shop.orders", "shop.orders_2023"]
          and schema.recognised,
          f"{schema.statements} stmt, {len(schema.changes)} changes, stubs={stubs}")

    added = next((c for t in schema.tables for c in t.columns if c.name == "channel_code"), None)
    check("K8", "a column added by ALTER is a column the rules can read, and it carries "
                "what the ALTER said about it",
          added is not None and added.nullable is False and added.default is None
          and added.added_by is not None,
          f"nullable={None if added is None else added.nullable} "
          f"added_by={'yes' if added and added.added_by else 'no'}")

    p = review_cli(["--rules", hygiene, "--ddl", str(ROOT / "examples" / "sample_schema.sql")])
    check("K9", "the same pack reviews a full snapshot: every rule matched a non-empty set",
          p.returncode == 1 and "0 row(s)" not in p.stdout
          and "(nothing matched)" not in p.stdout,
          f"rc={p.returncode}")


def inference_cases() -> None:
    """From a description and its examples, to a rule that is ready to apply.

    This is the claim that matters outside the repository: a rule someone writes
    because they know the requirement — not because they can read the engine —
    has to be usable the same day.  The cases below hold that claim at both ends:
    the examples have to separate cleanly, and the rule that comes out has to
    review a script on the next command line.
    """
    print("\n[L] inferred rules — a description, its examples, and a rule that applies")
    request = str(ROOT / "examples" / "rule-request-primary-key.json")

    # L0  the four spellings of one primary key are one fact
    spellings = [
        "CREATE TABLE shop.o (id BIGSERIAL PRIMARY KEY);",
        "CREATE TABLE shop.o (id BIGSERIAL, PRIMARY KEY (id));",
        "CREATE TABLE shop.o (id BIGSERIAL, CONSTRAINT pk_o PRIMARY KEY (id));",
        "CREATE TABLE shop.o (id BIGSERIAL); ALTER TABLE shop.o ADD PRIMARY KEY (id);",
    ]
    key_fields = ("primary_key_columns", "has_primary_key", "unique_key_count")
    rows = [rows_for("table", parse_ddl(sql))[0] for sql in spellings]
    columns = [rows_for("column", parse_ddl(sql))[0] for sql in spellings]
    # Not the whole row: the fourth script really does perform one more action, and
    # change_count is right to say so.  What has to be identical is the fact the
    # rule reads, and the column it reads it off.
    same = all(all(row[k] == rows[0][k] for k in key_fields) for row in rows)
    on_the_column = all((c["is_primary"], c["nullable"]) == (True, False)
                        for c in columns)
    check("L0", "one concept, one place: four spellings of a primary key read as one fact",
          same and on_the_column and rows[0]["has_primary_key"] is True
          and rows[0]["unique_key_count"] == 0,
          f"identical={same} column={on_the_column} "
          f"pk={rows[0]['primary_key_columns']} unique={rows[0]['unique_key_count']}")

    # L1  every change kind the reader produces is a kind the vocabulary declares
    sample = "\n".join(spellings + [
        "ALTER TABLE shop.o DROP COLUMN channel;",
        "ALTER TABLE shop.o ALTER COLUMN id SET NOT NULL;",
        "ALTER TABLE shop.o RENAME TO shop.p;",
        "ALTER TABLE shop.o SET (fillfactor = 70);",
        "CREATE INDEX idx_o ON shop.o (id);", "DROP INDEX idx_o;",
        "DROP TABLE shop.o;", "COMMENT ON TABLE shop.o IS 'x';",
        "INSERT INTO shop.o (id) VALUES (1);", "GRANT SELECT ON shop.o TO r;",
        "SET search_path = shop;", "CREATE OR REPLACE VIEW v AS SELECT 1;",
    ])
    produced = {c.kind for c in parse_ddl(sample).changes}
    check("L1", "every change kind the reader produces is one the vocabulary declares",
          produced <= set(CHANGE_KINDS) and len(produced) >= 10,
          f"{len(produced)} kind(s), undeclared={sorted(produced - set(CHANGE_KINDS))}")

    # L2  the search finds the rule the examples describe
    out = infer_rule(json.loads(Path(request).read_text(encoding="utf-8")))
    first = out["candidates"][0] if out["candidates"] else {}
    check("L2", "the examples yield a candidate, and the plainest one is the fact "
                "a DBA would have named",
          first.get("assert") == [{"field": "has_primary_key", "op": "present"}]
          and "select" not in first,
          f"{len(out['candidates'])} candidate(s), first={first.get('assert')}")

    # L3  what comes back is loadable, examples and all
    pack = {"rulepack": "inferred", "version": 1, "applies_to": ["ddl"],
            "rules": out["candidates"][:1]}
    loaded = load_rulepack(pack, verify=True)
    check("L3", "the candidate passes the example gate it will be held to anyway",
          loaded["rules"][0]["examples"]["silent"] ==
          json.loads(Path(request).read_text(encoding="utf-8"))["examples"]["silent"],
          f"{len(loaded['rules'])} rule(s), examples carried over")

    # L4  --write, then use the file on the next command line
    written = TMP / "l-inferred.json"
    p = review_cli(["--infer", request, "--limit", "1", "--write", str(written)])
    colleague = TMP / "l-colleague.sql"
    colleague.write_text("CREATE TABLE shop.orders (id BIGSERIAL, channel TEXT);\n"
                         "CREATE TABLE shop.audit (id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ);\n",
                         encoding="utf-8")
    q = review_cli(["--rules", str(written), "--ddl", str(colleague)])
    check("L4", "`--infer --write` leaves a pack that reviews a colleague's script "
                "on the next command line",
          p.returncode == 0 and written.exists() and q.returncode == 1
          and "table-needs-a-primary-key" in q.stdout
          and "shop.orders" in q.stdout and "shop.audit" not in q.stdout.split("[notes]")[0],
          f"write rc={p.returncode} review rc={q.returncode}")

    # L5  when the examples cannot be separated, say so instead of answering
    impossible = TMP / "l-impossible.json"
    impossible.write_text(json.dumps({
        "id": "inherited-table", "severity": "low", "target": "table",
        "title": "t", "detail": "d", "fix": "f",
        "examples": {
            "fires": ["CREATE TABLE shop.child (a INT) INHERITS (shop.parent);"],
            "silent": ["CREATE TABLE shop.child (a INT);"],
        }}), encoding="utf-8")
    p = review_cli(["--infer", str(impossible)])
    check("L5", "examples the current facts cannot separate exit 2, name the near "
                "misses, and print no empty pack",
          p.returncode == 2 and "no rule over the current facts" in p.stdout
          and "separates 1 of 2" in p.stdout and '"rules": []' not in p.stdout,
          f"rc={p.returncode}")

    # L6  the fact models that are not built say why, and where
    p = review_cli(["--facts"])
    check("L6", "`--facts` names the unbuilt model and points at the change it needs",
          p.returncode == 0 and "sql" in p.stdout and "not built" in p.stdout
          and "roadmap" in p.stdout,
          f"rc={p.returncode}")

    # L7  the vocabulary is the registry, not a copy of it
    vocab = vocabulary()
    check("L7", "the exported vocabulary is built from the fact registry itself",
          set(vocab["targets"]) == set(PROVIDERS)
          and all(set(vocab["targets"][name]) == set(PROVIDERS[name].fields)
                  for name in PROVIDERS)
          and set(vocab["fact_models"]) == set(FACT_MODELS),
          f"{len(PROVIDERS)} target(s), {len(FACT_MODELS)} model(s)")

    # L8-L11  a pack that grows by copying is caught before it is applied
    def overlaps(rules):
        return find_overlaps({"rulepack": "x", "version": 1, "applies_to": ["ddl"],
                              "rules": rules})

    def rule(rid, select=None, op="present", field="comment"):
        built = {"id": rid, "severity": "low", "target": "table", "title": "t",
                 "detail": "d", "fix": "f", "assert": [{"field": field, "op": op}]}
        if select:
            built["select"] = select
        return built

    same = overlaps([rule("a"), rule("b")])
    check("L8", "two rules that mean the same thing are named, not silently applied twice",
          len(same["exact"]) == 1 and "`b`" in same["exact"][0]
          and "`a`" in same["exact"][0],
          f"exact={len(same['exact'])}")

    inside = overlaps([rule("wide"), rule("narrow", select={"created_by_change": True})])
    check("L9", "a rule that sits inside a wider one is reported as adding wording only",
          not inside["exact"] and len(inside["redundant"]) == 1
          and "`narrow` sits inside `wide`" in inside["redundant"][0],
          f"redundant={len(inside['redundant'])} exact={len(inside['exact'])}")

    clash = overlaps([rule("must"), rule("must-not", op="absent")])
    check("L10", "two rules that select the same rows and disagree are reported",
          len(clash["conflicting"]) == 1 and "disagree" in clash["conflicting"][0],
          f"conflicting={len(clash['conflicting'])}")

    dup = TMP / "l-dup.json"
    pack = json.loads(Path(ROOT / "rules" / "table-hygiene.json").read_text(encoding="utf-8"))
    pack["rulepack"] = "duplicated"
    pack["rules"] = [pack["rules"][1],
                     dict(pack["rules"][1], id="the-same-rule-again")]
    dup.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    p = review_cli(["--rules", str(dup), "--verify"])
    check("L11", "a pack with one rule written twice is refused at load",
          p.returncode == 2 and "written twice" in p.stdout,
          f"rc={p.returncode}")


def catalogue_cases() -> None:
    """An index is one fact written five ways, and one of the five is a database.

    "No table may carry two indexes on the same columns, in the same order" is
    the rule a reviewer actually asks for, and it cannot be answered from a
    script: only the catalog knows the indexes the table already has.  These
    cases hold the three claims that makes: the spellings collapse to one fact,
    the catalog reaches the same fact as the reader does, and a rule that needs
    the catalog is refused over a script instead of reported clean.
    """
    print("\n[M] the catalog — the fifth spelling, and the rule that needs it")
    hygiene = str(ROOT / "rules" / "index-hygiene.json")

    # M1  six spellings of one index, five from a script and one from a catalog
    spellings = [
        "CREATE TABLE shop.t (a INT UNIQUE);",
        "CREATE TABLE shop.t (a INT, UNIQUE (a));",
        "CREATE TABLE shop.t (a INT, CONSTRAINT uq_t_a UNIQUE (a));",
        "CREATE TABLE shop.t (a INT); ALTER TABLE shop.t ADD CONSTRAINT uq_t_a UNIQUE (a);",
        "CREATE TABLE shop.t (a INT); CREATE UNIQUE INDEX uq_t_a ON shop.t (a);",
    ]
    shared = ("columns", "columns_text", "column_count", "unique", "primary",
              "from_constraint", "method", "comparable", "duplicate_count")
    rows = [rows_for("index", parse_ddl(sql))[0] for sql in spellings]
    # What the catalog returns for the same index, in the shape psycopg hands back
    # (…, is_unique, is_primary, method, from_constraint, columns, expressions,
    #  is_partial, predicate) — the reader is not trusted to be the only one.
    rows.append(rows_for("index", build_schema(
        table_rows=[("shop", "t", None)],
        column_rows=[("shop", "t", "a", "integer", False, None, None, False, None)],
        index_rows=[("shop", "t", "uq_t_a", True, False, "btree", True, ["a"], 0,
                     False, None)]))[0])
    same = all(all(row[key] == rows[0][key] for key in shared)
               for row in rows[:4] + [rows[5]])
    # `CREATE UNIQUE INDEX` is the same index and not the same constraint:
    # dropping the constraint's index drops the constraint, so the reader keeps
    # the two apart.  That one field is the whole difference.
    differs = {key for key in shared if rows[4][key] != rows[0][key]}
    provenance = [row["created_here"] for row in rows]
    check("M1", "one concept, one place: four constraint spellings and the catalog "
                "read as one index fact, and a bare unique index differs only in "
                "the constraint it does not have",
          same and differs == {"from_constraint"} and provenance == [True] * 5 + [False],
          f"identical={same} differs={sorted(differs)} "
          f"created_here={provenance} columns={rows[0]['columns']}")

    # M2  the catalog, then the script — and the finding names the other index
    catalog = build_schema(
        table_rows=[("shop", "orders", "订单主表 用途:支付事实 在线")],
        column_rows=[("shop", "orders", "paid_at", "timestamptz", True, None, None,
                      False, None)],
        index_rows=[("shop", "orders", "idx_orders_paid_at", False, False, "btree",
                     False, ["paid_at"], 0, False, None)])
    script = parse_ddl("CREATE INDEX idx_orders_paid_at_2 "
                       "ON shop.orders (paid_at);")
    applied = apply_script(catalog, script)
    pack = load_rulepack(hygiene, verify=True)
    report = review_schema([pack], catalog)
    detail = report.findings[0].detail if report.findings else ""
    check("M2", "a script adds an index the catalog already has on that key: the "
                "finding names the index it repeats",
          applied.indexes == 1 and len(report.findings) == 2
          and "idx_orders_paid_at" in detail and "{" not in detail,
          f"{applied.indexes} added, {len(report.findings)} finding(s), "
          f"subjects={sorted(f.subject for f in report.findings)}")

    # M3  the near misses the rule was written around
    def counts(sql):
        return [(r["name"], r["duplicate_count"])
                for r in rows_for("index", parse_ddl(sql)) if r["table"] == "shop.t"]

    order = counts("CREATE TABLE shop.t (a INT, b INT);"
                   "CREATE INDEX i1 ON shop.t (a, b);"
                   "CREATE INDEX i2 ON shop.t (b, a);")
    methods = counts("CREATE TABLE shop.t (a INT);"
                     "CREATE INDEX i1 ON shop.t (a);"
                     "CREATE INDEX i2 ON shop.t USING hash (a);")
    partial = counts("CREATE TABLE shop.t (a INT);"
                     "CREATE INDEX i1 ON shop.t (a);"
                     "CREATE INDEX i2 ON shop.t (a) WHERE a > 0;")
    expression = counts("CREATE TABLE shop.t (a INT);"
                        "CREATE INDEX i1 ON shop.t (a);"
                        "CREATE INDEX i2 ON shop.t (lower(a));")
    check("M3", "the same columns in the other order are two indexes, and a "
                "different method, a different predicate or an expression is not a "
                "duplicate",
          [n for _name, n in order] == [0, 0]
          and [n for _name, n in methods] == [0, 0]
          and [n for _name, n in expression] == [0, 0]
          and dict(partial) == {"i1": 0, "i2": 1},
          f"order={order} methods={methods} partial={partial} expression={expression}")

    # M4  the pack that needs the catalog, and a run that did not read one
    script_file = TMP / "m-script.sql"
    script_file.write_text("CREATE TABLE shop.orders (id BIGSERIAL, paid_at TIMESTAMPTZ);\n"
                           "CREATE INDEX idx_orders_paid_at ON shop.orders (paid_at);\n",
                           encoding="utf-8")
    p = review_cli(["--rules", hygiene, "--ddl", str(script_file)])
    check("M4", "a pack that declares applies_to catalog is refused over a script, "
                "and says which command would supply the facts",
          p.returncode == 2 and "WAS NOT APPLIED" in p.stdout
          and "--db" in p.stdout and "no rule fired" not in p.stdout,
          f"rc={p.returncode}")

    p = review_cli(["--rules", hygiene, "--verify"])
    check("M5", "the pack's own examples are the acceptance test for the rule",
          p.returncode == 0 and "5 example(s) → all passed" in p.stdout,
          f"rc={p.returncode}")

    p = review_cli(["--facts"])
    check("M6", "`--facts` lists the catalog model as built, and the index target "
                "as read from it",
          p.returncode == 0 and "catalog" in p.stdout
          and "index" in p.stdout and "ddl, schema, catalog" in p.stdout,
          f"rc={p.returncode}")

    # M7  a table the catalog has never seen is counted, not silently merged
    fresh = apply_script(build_schema(), parse_ddl(
        "CREATE TABLE shop.new (a INT); CREATE INDEX i ON shop.new (a);"))
    check("M7", "an index on a table the catalog does not have is reported as "
                "uncompared rather than merged into it",
          fresh.indexes == 0 and fresh.new_tables == ["shop.new"],
          f"indexes={fresh.indexes} new_tables={fresh.new_tables}")

    # M8  the same rule, inferred from the description and its examples
    request = str(ROOT / "examples" / "rule-request-duplicate-index.json")
    out = infer_rule(json.loads(Path(request).read_text(encoding="utf-8")))
    first = out["candidates"][0] if out["candidates"] else {}
    written = TMP / "m-inferred.json"
    p = review_cli(["--infer", request, "--limit", "1", "--write", str(written)])
    q = review_cli(["--rules", str(written), "--ddl", str(script_file)])
    check("M8", "described in a sentence and two examples, the rule comes back as "
                "the count assertion a DBA would write, and carries applies_to",
          first.get("assert") == [{"field": "duplicate_count", "op": "lte", "value": 0}]
          and out["applies_to"] == ["catalog"]
          and p.returncode == 0 and json.loads(written.read_text(encoding="utf-8")
                                               )["applies_to"] == ["catalog"]
          and q.returncode == 2,
          f"first={first.get('assert')} applies_to={out['applies_to']} "
          f"write rc={p.returncode} review rc={q.returncode}")

    # M9  a catalog that cannot be reached is not a clean script
    p = review_cli(["--rules", hygiene,
                    "--db", "postgresql://nobody@127.0.0.1:1/none?connect_timeout=2"])
    check("M9", "a catalog that cannot be reached exits 2 and never prints a clean "
                "verdict",
          p.returncode == 2 and "no rule was applied" in p.stdout
          and "no rule fired" not in p.stdout and "0 finding(s)" not in p.stdout,
          f"rc={p.returncode}")

    # M10  the exported vocabulary carries the new facts and the new syntax
    vocab = vocabulary()
    check("M10", "the vocabulary exports the index facts, the catalog model and "
                 "`{field}` in a finding's text",
          "catalog" in vocab["applies_to"] and "index" in vocab["targets"]
          and "duplicate_count" in vocab["targets"]["index"]
          and "{field}" in vocab["finding_text"]["path"],
          f"applies_to={vocab['applies_to']}")


# --------------------------------------------------------------------------- #
# N. the docs — the numbers they quote, and the files they point at
# --------------------------------------------------------------------------- #
def _split_top(text: str) -> list[str]:
    """Split on the commas that are not inside parentheses or quotes."""
    parts, buf, depth, inq = [], "", 0, False
    for ch in text:
        if inq:
            buf += ch
            if ch == "'":
                inq = False
            continue
        if ch == "'":
            inq = True
            buf += ch
        elif ch == "(":
            depth += 1
            buf += ch
        elif ch == ")":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _value_tuples(body: str) -> list[str]:
    """The parenthesised value groups of a VALUES clause, at depth one."""
    out, buf, depth, inq = [], "", 0, False
    for ch in body:
        if inq:
            buf += ch
            if ch == "'":
                inq = False
            continue
        if ch == "'":
            inq = True
        if ch == "(":
            depth += 1
            if depth == 1:
                buf = ""
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                out.append(buf)
                continue
        if depth >= 1:
            buf += ch
    return out


def _null_fraction(sql_text: str, table: str, column: str) -> float | None:
    """How much of one column the shipped INSERT statements leave NULL.

    Just enough of a reader for ``examples/sample_schema.sql``.  The docs quote
    these as measured output, so they have to be recomputed rather than trusted.
    """
    pat = re.compile(r"INSERT\s+INTO\s+" + re.escape(table)
                     + r"\s*\(([^)]*)\)\s*VALUES\s*(.*?);", re.S | re.I)
    m = pat.search(sql_text)
    if not m:
        return None
    cols = [c.strip().lower() for c in m.group(1).split(",")]
    if column.lower() not in cols:
        return None
    pos = cols.index(column.lower())
    rows = [_split_top(t) for t in _value_tuples(m.group(2))]
    rows = [r for r in rows if len(r) == len(cols)]
    if not rows:
        return None
    nulls = sum(1 for r in rows if r[pos].strip().upper() == "NULL")
    return nulls / len(rows)


# Wording that talks to the author instead of to the reader. Every entry was in
# a shipped doc once: a note about the author's own machine, an unfinished check
# reported back to whoever asked for it, an errand. They are retired here the way
# N2 retires the slogans, so a rewrite cannot quietly bring them back.
_NOTES_TO_THE_AUTHOR = {
    "开发机": "the author's machine, standing in for a description of yours",
    "上一轮": "refers to a development run the reader never saw",
    "我记得": "a first-person recollection of one setup",
    "没有量过": "reports back what has not been measured yet",
    "值得重跑一遍": "an errand left for the author",
    "踩过一次": "turns one development incident into prose",
    "把结果带回来": "an instruction to whoever runs the benchmark",
    "你在虚拟机里试一次": "tells one person what to do, instead of how it works",
    "待 vm 补跑": "an internal to-do written as prose",
    "has not yet been run against a server": "reports an unfinished check",
    "the two `...` gaps are mine": "a note about one paste, not about the tool",
    "questions of record": "names questions only the author has settled on",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _prose_lines(text: str) -> list:
    """The lines a reader reads, with fenced blocks and inline code taken out.

    Line numbers are kept, so a fault can name the line that carries it. Inline
    code comes out because it is data — a Chinese question handed to `--ask`,
    the word a router test greps for — not the language the doc is written in.
    """
    out, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else re.sub(r"`[^`]*`", "", line))
    return out


def _language_faults(readme_text: str, doc_texts: dict) -> list:
    """One language per side: the README in English, docs/ in Chinese.

    The README carried its opening twice, once per language, and docs/ was split
    between the two. Both readings shipped. This keeps the split from returning.
    """
    faults = [f"README.md line {i} is not English: {line.strip()[:52]!r}"
              for i, line in enumerate(_prose_lines(readme_text), 1) if _CJK.search(line)]
    faults += [f"{name} carries no Chinese prose"
               for name, text in sorted(doc_texts.items())
               if not _CJK.search("\n".join(_prose_lines(text)))]
    return faults


def _chatter_faults(doc_texts: dict) -> list:
    """Docs that talk to the author rather than to the reader, sorted.

    A bare requirement number counts as chatter anywhere but the roadmap: a
    reader who has not read the ledger cannot follow it, and the gap it names
    is spelled out in words instead.
    """
    faults = {f"{name} — {phrase!r}: {why}"
              for name, text in doc_texts.items()
              for phrase, why in _NOTES_TO_THE_AUTHOR.items()
              if phrase in text.lower()}
    faults |= {f"{name} — quotes the bare requirement number {r}"
               for name, text in doc_texts.items()
               if not name.endswith("roadmap.md")
               for r in sorted(set(re.findall(r"\bR\d+\b", text)))}
    return sorted(faults)


_DIAGRAM_KINDS = ("flowchart", "graph", "sequenceDiagram", "stateDiagram-v2",
                  "erDiagram", "classDiagram", "journey", "gantt", "pie")


def _diagram_faults(text: str) -> list:
    """Faults in the one mermaid diagram the README carries, [] when sound.

    Runs on synthetic text as well as on the README: the N7 case proves this
    gate can fail by feeding it broken diagrams, not by editing the README
    into a broken state and hoping to restore it.
    """
    blocks = re.findall(r"^```mermaid[ \t]*\r?\n(.*?)^```[ \t]*\r?$", text, re.M | re.S)
    if len(blocks) != 1:
        return [f"carries {len(blocks)} mermaid blocks, expected one"]
    body = blocks[0]
    faults = []
    lines = [line for line in body.splitlines() if line.strip()]
    if not re.match(r"^(" + "|".join(_DIAGRAM_KINDS) + r")\b", lines[0].strip()):
        faults.append(f"first line {lines[0].strip()!r} does not declare a diagram kind")
    for line in lines:
        if line.count("[") != line.count("]") or line.count("(") != line.count(")"):
            faults.append(f"unbalanced brackets: {line.strip()[:44]!r}")
        if line.count('"') % 2:
            faults.append(f"odd number of quotes: {line.strip()[:44]!r}")
    for name in ("review", "fence"):
        if name not in body:
            faults.append(f"does not name {name!r}")
    if "virtual DBA" not in body and "diagnostic agent" not in body:
        faults.append("does not name the agent")
    # A bare requirement number means nothing to a reader who has not read
    # roadmap.md, and it is how the diagram once drew a path the README itself
    # says is not wired. The two gaps are named on the dashed edges instead, so
    # both dashed edges have to be there, and the diagram stays in English.
    ids = sorted(set(re.findall(r"\bR\d+\b", body)))
    if ids:
        faults.append(f"quotes the bare requirement numbers {ids}")
    dashed = re.findall(r'-\.\s*"([^"]*)"\s*\.->', body)
    if len(dashed) != 2:
        faults.append(f"carries {len(dashed)} dashed edge(s), expected the two gaps")
    if _CJK.search(body):
        faults.append("the diagram is not in English")
    return faults


def doc_cases() -> None:
    """The outward-facing docs quote numbers and point at files; both go stale in silence.

    Every case here has already gone wrong once or is one edit away from it: a link
    to a file a merge renamed, wording the rewrite retired but left behind in a
    second doc, a NULL fraction copied forward from an older sample, a version
    the README quotes while the package defines another, a Contents block
    that listed subsections among the sections while leaving sections out, a
    diagram that can silently claim a path the README itself says is not wired,
    and two languages inside one set of docs.
    """
    docs = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))
    link = re.compile(r"\]\(([^)\s]+)\)")
    dead: list[str] = []
    for doc in docs:
        for target in link.findall(doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (doc.parent / target.split("#")[0]).resolve().exists():
                dead.append(f"{doc.relative_to(ROOT).as_posix()} → {target}")
    check("N1", "every relative link in README and docs/ resolves", not dead,
          "; ".join(dead[:4]))

    # The package docstring counts too: the slogan went out of the README
    # while both the docstring and the architecture note still led with it.
    n2_reads = docs + [ROOT / "schemafence" / "__init__.py"]
    retired = {
        "constraint layer between LLMs and databases": "the banner the project retired",
        "two halves": "a section name the restructure removed",
        "It never calls a language model": "a banner the positioning revision replaced with where the model sits",
        "AI × DBA": "a slogan the docs no longer explain; the three parts carry the point",
        "Every statement that runs against a live database passes one fence first":
            "a claim wider than the gate reaches, which the README itself corrects (R13, R15)",
        "执行到活库上的每条语句先过同一道闸门":
            "the same claim in the Chinese half",
        "Lives in": "a table label that names the module rather than the reader's question",
        "the tiers above it":
            "an allusion to a ranking the README never defines; roadmap's own heading spells it out",
    }
    stale = sorted({f"{d.relative_to(ROOT).as_posix()} — {phrase!r}: {why}"
                    for d in n2_reads for phrase, why in retired.items()
                    if phrase in d.read_text(encoding="utf-8")})
    check("N2", "no doc still carries wording this project retired", not stale,
          "; ".join(stale[:4]))

    # The three reasons are still stated, but no longer twice in one file: the
    # README opens in English and docs/README.md carries the Chinese opening.
    # The repeat was part of what made the docs read as two projects.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    en_lead = readme.split("**Contents**")[0].lower()
    overview = ROOT / "docs" / "README.md"
    zh_lead = overview.read_text(encoding="utf-8").lower() if overview.exists() else ""
    points = [
        ("why the database needs a fence", "nl2sql", "nl2sql"),
        ("why a rule can be added without a release", "plain words", "自然语言"),
        ("what the agent is for", "inspect", "巡检"),
    ]
    absent = [f"{what} ({side} half)"
              for what, en_mark, zh_mark in points
              for side, text, mark in (("English", en_lead, en_mark),
                                       ("Chinese", zh_lead, zh_mark))
              if mark not in text]
    if not overview.exists():
        absent.append("docs/README.md, which carries the Chinese opening, is missing")
    check("N5", "the three parts each still say why they exist: the README in English, "
                "the overview in docs/ in Chinese",
          not absent,
          "; ".join(absent) if absent
          else "fence, rules and the agent, in English and in Chinese")

    # N6 exists because N1 cannot see this: it skips every anchor, and the
    # Contents block is nothing but anchors. It had drifted twice — listing two
    # subsections among the sections, and leaving two sections out entirely.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def anchor(heading: str) -> str:
        return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))

    top = {anchor(h): h for h in re.findall(r"^## (.+)$", readme, re.M)}
    any_head = {anchor(h) for h in re.findall(r"^#{2,6} (.+)$", readme, re.M)}
    inside = [a[1:] for a in re.findall(r"\]\((#[^)\s]+)\)", readme)]
    listed = [a[1:] for a in
              re.findall(r"\]\((#[^)\s]+)\)", readme.split("**Contents**", 1)[1].split("\n\n", 1)[0])]
    trouble = ([f"anchor goes nowhere: #{a}" for a in inside if a not in any_head]
               + [f"Contents omits {h!r}" for a, h in top.items() if a not in listed]
               + [f"Contents lists #{a}, which is not a section" for a in listed if a not in top]
               + [f"Contents lists #{a} twice" for a in sorted(set(listed)) if listed.count(a) > 1])
    check("N6", "every in-page anchor resolves and Contents lists the sections, and nothing deeper",
          not trouble,
          "; ".join(trouble[:5]) if trouble else f"{len(top)} sections, all listed")

    # A diagram is the one part of the README no other gate reads: N1 skips it
    # (it is not a link), N2 sees only its words. It renders as an error box
    # when the syntax breaks, and nothing else stops it from quietly drawing a
    # path the README itself says does not exist yet — the same overclaim the
    # fence's all-statement sentence made once and had to retire.
    broken_samples = [
        "no diagram here at all",
        "```mermaid\nflowchart LR\n    W[\"review and the fence\"\n```\n",
        "```mermaid\nwaterfall LR\n    W[\"review, the fence, the virtual DBA\"]\n```\n",
        "```mermaid\nflowchart LR\n    W[\"review, the fence, the virtual DBA\"]\n```\n",
        "```mermaid\nflowchart LR\n    W[\"review, the fence, the virtual DBA\"]\n"
        "    W -. \"no way in yet (R15)\" .-> F\n    W -. \"skipped (R13)\" .-> D\n```\n",
        "```mermaid\nflowchart LR\n    W[\"review, the fence, the virtual DBA\"]\n"
        "    W -. \"no way in yet\" .-> F\n```\n",
        "```mermaid\nflowchart LR\n    W[\"复审与闸门，虚拟 DBA\"]\n"
        "    W -. \"进不来\" .-> F\n    W -. \"跳过\" .-> D\n```\n",
    ]
    toothless = [i for i, sample in enumerate(broken_samples) if not _diagram_faults(sample)]
    faults = _diagram_faults(readme)
    trouble = [f"the gate itself missed broken sample {i}" for i in toothless] + faults
    check("N7", "the one diagram the README carries is sound: well formed, in English, names "
                "the three parts, and carries the two gaps as dashed edges rather than numbers",
          not trouble,
          "; ".join(trouble[:5]) if trouble else "one diagram, well formed, two dashed edges")

    sample = (ROOT / "examples" / "sample_schema.sql").read_text(encoding="utf-8")
    quick = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    bad: list[str] = []
    for table, column in (("shop.orders", "user_id"), ("shop.users", "email")):
        frac = _null_fraction(sample, table, column)
        if frac is None or frac == 0.0:
            bad.append(f"{table}.{column}: cannot recompute")
            continue
        want = f"{frac * 100:.1f}% NULL"
        if f"{table}.{column}" not in quick or want not in quick:
            bad.append(f"{table}.{column}: quickstart does not say {want}")
    check("N3", "the NULL fractions the quickstart quotes are the ones the sample inserts",
          not bad, "; ".join(bad))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quoted = re.search(r"`__version__` is `([^`]+)`", readme)
    check("N4", "the version the README quotes is the one the package defines",
          quoted is not None and quoted.group(1) == shipped,
          f"README says {quoted.group(1) if quoted else 'nothing'}, "
          f"schemafence.__version__ is {shipped}")

    # N8 and N9 keep one documentation pass from undoing the next: pages that
    # talk to the author, and two languages in one set of docs. Both follow N7's
    # rule — the gate is fed broken samples, not the file it is checking.
    doc_texts = {d.relative_to(ROOT).as_posix(): d.read_text(encoding="utf-8")
                 for d in docs}
    chatter = _chatter_faults(doc_texts)
    chatter_samples = [
        ("a note about the author's machine", {**doc_texts, "x.md": "开发机没有这个视图"}, True),
        ("a first-person recollection", {**doc_texts, "x.md": "我记得当时的配置"}, True),
        ("the docs as they are", doc_texts, False),
    ]
    toothless = []
    for name, texts, expect_fault in chatter_samples:
        faulted = bool(_chatter_faults(texts))
        if faulted != expect_fault:
            toothless.append(f"sample {name!r}: expected {expect_fault}, got {faulted}")
    trouble = toothless + chatter
    check("N8", "no doc talks to the author instead of to the reader: no note about the "
                "author's machine, no unfinished check, no errand",
          not trouble, "; ".join(trouble[:4]) if trouble else "no notes to the author")

    docs_only = {name: text for name, text in doc_texts.items() if name != "README.md"}
    language = _language_faults(readme, docs_only)
    language_samples = [
        ("a Chinese paragraph in the README", "> 中文引言\n", {"x.md": "中文\n"}, True),
        ("an all-English doc under docs/", "# English\n", {"x.md": "# English\n"}, True),
        ("one language per side", "# English\n", {"x.md": "中文\n"}, False),
    ]
    toothless = []
    for name, readme_sample, docs_sample, expect_fault in language_samples:
        faulted = bool(_language_faults(readme_sample, docs_sample))
        if faulted != expect_fault:
            toothless.append(f"sample {name!r}: expected {expect_fault}, got {faulted}")
    trouble = toothless + language
    check("N9", "the README is written in English and every doc under docs/ is written in "
                "Chinese, code blocks and example data aside",
          not trouble, "; ".join(trouble[:4]) if trouble else "one language per side")


def main() -> int:
    print("schemafence QA harness — offline, zero dependencies")
    guard_cases()
    route_cases()
    knowledge_cases()
    cli_cases()
    tool_cases()
    eval_cases()
    layer_case()
    scale_corpus_cases()
    hermetic_cases()
    sql_semantics_cases()
    rulepack_cases()
    inference_cases()
    catalogue_cases()
    doc_cases()

    passed = sum(1 for _cid, _d, ok, _x in RESULTS if ok)
    total = len(RESULTS)
    failed = [cid for cid, _d, ok, _x in RESULTS if not ok]
    print(f"\n=== {passed}/{total} cases passed ===")
    if TIMINGS:
        top = sorted(TIMINGS, key=lambda row: -row[1])[:3]
        print("slowest CLI calls: "
              + ", ".join(f"{cmd} {sec:.1f}s" for cmd, sec in top)
              + f"  (cap {TIMEOUT}s per call, SF_QA_TIMEOUT overrides)")
    if failed:
        print("failed:", ", ".join(failed))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
