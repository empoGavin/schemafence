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
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from schemafence.agent import route, run_route_selftest          # noqa: E402
from schemafence.checks_sql import (SQL_SAMPLE_DDL, check_sql,    # noqa: E402
                                    run_sql_selftest)
from schemafence.ddl import parse_ddl                            # noqa: E402
from schemafence.guard import guard, run_selftest                # noqa: E402
from schemafence.knowledge import (Chunk, Embedder, JsonStore,    # noqa: E402
                                   chunk_markdown, ingest_directory)
from schemafence.rulepack import run_rulepack_selftest            # noqa: E402
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
    selftest_check("K0", "run_rulepack_selftest() 13 cases all pass", run_rulepack_selftest)

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
          p.returncode == 0 and set(vocab.get("targets", {})) == {"table", "column", "change"}
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
