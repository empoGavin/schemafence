#!/usr/bin/env python
"""Store benchmark: JsonStore vs PgStore on the same corpus and the same queries.

Both backends implement the identical 1024-d cosine search; the only things
that differ are where the vectors live and whether an ANN index exists.  That
makes the comparison honest: one variable (the store), everything else equal.

Measured per backend
--------------------
  ingest      chunk → embed → write.  For pgvector the write is one INSERT per
              chunk on an autocommit connection, which is *slow* on purpose:
              the number below is what the shipped code does, not what COPY
              could do.  Optimising the benchmark instead of the product
              would hide the thing an operator needs to know.
  cold start  open the store from disk / connect and count rows
  search      p50 / p95 / max latency over the question bank, repeated;
              pgvector is measured twice — with the HNSW index and with
              enable_indexscan/bitmapscan off — because "the index is why it
              is fast" is a claim worth measuring rather than asserting.
  resources   RSS delta and peak, CPU time vs wall time, disk bytes read and
              written, all from this process (see bench_util).
  agreement   how often the two backends return the same top-1 source, and
              their overlap@k.  pgvector's HNSW is approximate, so this is the
              number that says whether the approximation costs anything here.

Run one backend per process for clean memory numbers:

    python scripts/bench_store.py --backend json
    python scripts/bench_store.py --backend pg --db postgresql://.../fence_demo

or both in one process (memory deltas stay meaningful, peaks do not):

    python scripts/bench_store.py --backend both --db ...

Results: bench/store-<backend>.json
"""

from __future__ import annotations

import argparse
import gc
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import agent_cli  # noqa: E402
from bench_util import (Phase, env_fingerprint, human_bytes, md_table,  # noqa: E402
                        read_json, stats, write_json)
from schemafence.knowledge import Embedder, JsonStore, ingest_directory  # noqa: E402

DEFAULT_CORPUS = ROOT / "examples" / "knowledge-dba"
DEFAULT_EVAL = ROOT / "eval" / "questions-dba.md"


def pg_sizes(dsn: str) -> dict:
    """Table, index and total size of doc_chunks, plus the row count."""
    try:
        import psycopg
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM doc_chunks")
            rows = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(pg_relation_size('doc_chunks')),"
                        "       pg_size_pretty(pg_indexes_size('doc_chunks')),"
                        "       pg_size_pretty(pg_total_relation_size('doc_chunks'))")
            rel, idx, total = cur.fetchone()
            cur.execute("SELECT pg_relation_size('doc_chunks'),"
                        "       pg_indexes_size('doc_chunks'),"
                        "       pg_total_relation_size('doc_chunks')")
            rel_b, idx_b, total_b = cur.fetchone()
        return {"rows": rows, "table": rel, "index": idx, "total": total,
                "table_bytes": rel_b, "index_bytes": idx_b, "total_bytes": total_b}
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_json(args, corpus: Path, questions, embedder: Embedder,
             store_path: Path) -> dict:
    out: dict = {"backend": "json", "phases": {}, "notes": []}

    with Phase("ingest_total") as ph:
        chunks = ingest_directory(corpus, JsonStore(store_path, dim=args.dim),
                                  embedder=embedder,
                                  target_tokens=args.chunk,
                                  overlap_tokens=args.overlap)
        store = JsonStore(store_path, dim=args.dim)
        store.embedder = embedder
        store.replace(chunks)
        store.save()
    out["phases"]["ingest_total"] = ph.record
    out["chunks"] = len(chunks)
    out["idf_terms"] = len(embedder.idf)
    out["store_bytes"] = store_path.stat().st_size
    out["bytes_per_chunk"] = round(out["store_bytes"] / max(len(chunks), 1))

    del store
    gc.collect()
    with Phase("cold_open") as ph:
        store = JsonStore.load(store_path, dim=args.dim)
        store.embedder = embedder
        loaded = len(store.chunks)
    out["phases"]["cold_open"] = ph.record
    out["loaded_chunks"] = loaded

    vectors = [embedder.one(q) for q, _g in questions]
    latencies: list[float] = []
    results: list[list[str]] = []
    with Phase("search") as ph:
        for _ in range(args.repeat):
            for vector, (question, _gold) in zip(vectors, questions):
                t0 = time.perf_counter()
                hits = store.search(vector, k=args.k)
                latencies.append((time.perf_counter() - t0) * 1000.0)
        results = [[h.source for h in store.search(v, k=args.k)] for v in vectors]
    out["phases"]["search"] = ph.record
    out["search_stats"] = stats(latencies)
    out["top1_sources"] = [r[0] if r else None for r in results]
    out["top_sources"] = results
    return out


def run_pg(args, corpus: Path, questions, embedder: Embedder) -> dict:
    from schemafence.knowledge import PgStore

    out: dict = {"backend": "pg", "phases": {}, "notes": []}
    store = PgStore(args.db, embedder, dim=args.dim)

    with Phase("ingest_total") as ph:
        store.ensure_schema(with_index=not args.no_index)
        chunks = ingest_directory(corpus, store, embedder=embedder,
                                  target_tokens=args.chunk,
                                  overlap_tokens=args.overlap)
    out["phases"]["ingest_total"] = ph.record
    out["chunks"] = len(chunks)
    out["idf_terms"] = len(embedder.idf)
    out["index"] = not args.no_index
    store.close()

    with Phase("cold_open") as ph:
        store = PgStore(args.db, embedder, dim=args.dim)
    out["phases"]["cold_open"] = ph.record
    out["sizes"] = pg_sizes(args.db)
    out["rows"] = out["sizes"].get("rows")
    if "total_bytes" in out["sizes"]:
        out["store_bytes"] = out["sizes"]["total_bytes"]

    vectors = [embedder.one(q) for q, _g in questions]
    for label, use_index in (("search", True), ("search_seqscan", False)):
        if label == "search_seqscan" and args.no_index:
            continue
        latencies: list[float] = []
        results: list[list[str]] = []
        with Phase(label) as ph:
            for _ in range(args.repeat):
                for vector in vectors:
                    t0 = time.perf_counter()
                    hits = store.search(vector, k=args.k, use_index=use_index)
                    latencies.append((time.perf_counter() - t0) * 1000.0)
            results = [[h.source for h in store.search(v, k=args.k, use_index=use_index)]
                       for v in vectors]
        out["phases"][label] = ph.record
        out[f"{label}_stats"] = stats(latencies)
        out[f"{label}_top_sources"] = results
    store.close()
    return out


def agreement(a: dict, b: dict) -> dict:
    """How much does the approximate index change the answer?"""
    left = a.get("top_sources") or a.get("search_top_sources") or []
    right = b.get("top_sources") or b.get("search_top_sources") or []
    if not left or not right:
        return {}
    n = min(len(left), len(right))
    same_top1 = sum(1 for i in range(n) if left[i] and right[i] and left[i][0] == right[i][0])
    overlaps = []
    for i in range(n):
        ls, rs = set(left[i]), set(right[i])
        overlaps.append(len(ls & rs) / max(len(ls), 1))
    return {"n_questions": n,
            "same_top1": same_top1,
            "same_top1_pct": round(100.0 * same_top1 / n, 1),
            "mean_overlap_at_k": round(sum(overlaps) / n, 3)}


def summarize(name: str, row: dict) -> list:
    search = row.get("search_stats", {})
    seq = row.get("search_seqscan_stats", {})
    return [name,
            row.get("chunks"),
            f"{row['phases'].get('ingest_total', {}).get('wall_ms', 0):.0f}",
            f"{row['phases'].get('cold_open', {}).get('wall_ms', 0):.1f}",
            human_bytes(row.get("store_bytes")),
            f"{row['phases'].get('ingest_total', {}).get('rss_peak_mb')}",
            f"{search.get('p50_ms')}",
            f"{search.get('p95_ms')}",
            f"{search.get('max_ms')}",
            f"{seq.get('p50_ms') or '—'}"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="JsonStore vs PgStore benchmark")
    parser.add_argument("--backend", default="json", choices=["json", "pg", "both"])
    parser.add_argument("--db", default=None, help="PostgreSQL DSN (required for pg)")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--eval-file", default=str(DEFAULT_EVAL))
    parser.add_argument("--mode", default="offline", choices=["auto", "offline", "api"],
                        help="embedder for both backends (kept identical on purpose)")
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--chunk", type=int, default=512)
    parser.add_argument("--overlap", type=int, default=64)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=10,
                        help="query repetitions over the whole question bank")
    parser.add_argument("--limit-questions", type=int, default=0)
    parser.add_argument("--no-index", action="store_true",
                        help="create the pgvector table without HNSW")
    parser.add_argument("--reference", default=None,
                        help="JSON from another backend, for the agreement table")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if args.backend in ("pg", "both") and not args.db:
        print("error: --backend pg needs --db postgresql://user:pass@host:5432/db",
              file=sys.stderr)
        return 2

    corpus = Path(args.corpus)
    questions = agent_cli.load_questions(Path(args.eval_file))
    if args.limit_questions:
        questions = questions[:args.limit_questions]
    if not questions:
        print(f"error: no questions in {args.eval_file}", file=sys.stderr)
        return 2

    embedder = Embedder(mode=args.mode, dim=args.dim)
    payload = {"benchmark": "store", "env": env_fingerprint(),
               "backend_requested": args.backend,
               "embedder": embedder.label, "corpus": corpus.name,
               "questions": len(questions), "k": args.k, "repeat": args.repeat,
               "index": not args.no_index, "runs": {}}

    print(f"embedder  : {embedder.label}")
    print(f"corpus    : {corpus}  ({len(questions)} questions x {args.repeat} repeats)")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_store = Path(tmp) / "store.json"
        backends = ["json", "pg"] if args.backend == "both" else [args.backend]
        for backend in backends:
            print(f"  [{backend}] ingesting …")
            try:
                if backend == "json":
                    row = run_json(args, corpus, questions, embedder, tmp_store)
                else:
                    row = run_pg(args, corpus, questions, embedder)
            except Exception as exc:  # noqa: BLE001 - report, do not traceback
                print(f"  [{backend}] FAILED: {type(exc).__name__}: {exc}")
                payload["runs"][backend] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            payload["runs"][backend] = row
            s = row.get("search_stats", {})
            print(f"  [{backend}] ingest {row['phases']['ingest_total']['wall_ms']:.0f} ms · "
                  f"p50 {s.get('p50_ms')} ms · p95 {s.get('p95_ms')} ms · "
                  f"size {human_bytes(row.get('store_bytes'))} · "
                  f"peak RSS {row['phases']['ingest_total'].get('rss_peak_mb')} MB")

    runs = payload["runs"]
    js, pg = runs.get("json"), runs.get("pg")
    if js and pg and "error" not in js and "error" not in pg:
        payload["agreement_index_vs_json"] = agreement(js, pg)
        payload["agreement_seqscan_vs_json"] = agreement(
            js, {**pg, "search_top_sources": pg.get("search_seqscan_top_sources")})
        payload["speedup_index_vs_seqscan"] = _ratio(
            pg.get("search_seqscan_stats", {}).get("p50_ms"),
            pg.get("search_stats", {}).get("p50_ms"))
        payload["speedup_json_vs_pg_index"] = _ratio(
            js.get("search_stats", {}).get("p50_ms"),
            pg.get("search_stats", {}).get("p50_ms"))

    ref = read_json(args.reference) if args.reference else None
    ref_run = (ref or {}).get("runs", {}).get((ref or {}).get("backend_requested")) \
        or (ref or {}).get("runs", {}).get("json")

    print()
    headers = ["backend", "pieces", "ingest ms", "open ms", "on disk",
               "peak RSS MB", "p50 ms", "p95 ms", "max ms", "p50 seqscan ms"]
    rows = []
    for backend, row in runs.items():
        if "error" in row:
            continue
        rows.append(summarize(backend, row))
    if ref_run and "error" not in ref_run:
        agr = agreement(ref_run, js or pg or {})
        if agr:
            print(f"  agreement with {args.reference or 'reference'}: "
                  f"same top-1 {agr['same_top1_pct']}% · "
                  f"mean overlap@{args.k} {agr['mean_overlap_at_k']}")
    print(md_table(headers, rows, ["---"] * len(headers)))

    for key in ("speedup_index_vs_seqscan", "speedup_json_vs_pg_index"):
        if payload.get(key):
            print(f"  {key}: {payload[key]:.1f}x")
    for key in ("agreement_index_vs_json", "agreement_seqscan_vs_json"):
        if payload.get(key):
            a = payload[key]
            print(f"  {key}: same top-1 {a['same_top1_pct']}% · "
                  f"mean overlap@{args.k} {a['mean_overlap_at_k']}")

    if js and "error" not in js:
        for mismatch in _top1_mismatches(js, pg):
            print(f"  differs: {mismatch}")

    out = Path(args.out) if args.out else ROOT / "bench" / f"store-{args.backend}.json"
    write_json(out, payload)
    print()
    print(f"json: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    errored = [b for b, r in runs.items() if "error" in r]
    return 1 if errored and len(errored) == len(runs) else 0


def _ratio(a, b) -> float | None:
    if not a or not b:
        return None
    return a / b


def _top1_mismatches(js: dict, pg: dict | None) -> list[str]:
    if not pg or "error" in pg:
        return []
    left = js.get("top_sources") or []
    right = pg.get("search_top_sources") or []
    out = []
    for i, (l, r) in enumerate(zip(left, right)):
        if l and r and l[0] != r[0] and len(out) < 5:
            out.append(f"q{i + 1}: json {l[0]} vs pg {r[0]}")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
