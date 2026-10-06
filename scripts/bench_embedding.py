#!/usr/bin/env python
"""Embedding benchmark: the same corpus, many hyperparameters, both embedders.

What it answers
---------------
1. How long does it take to chunk + embed this corpus, per configuration?
2. How much does that cost in memory, CPU and disk (the store artefact)?
3. What does retrieval quality look like at each setting — hit rate over the
   bundled question bank, not a feeling?
4. Where is the lexical baseline actually worse than a real embedding model,
   and by how much?  Same questions, same chunks, only the embedder differs.

Hyperparameters swept
---------------------
  --chunks      target tokens per chunk      (the dominant knob)
  --overlaps    overlap tokens               (ignored across a heading)
  --dims        vector dimension             (offline; API models are fixed)
  --idf         corpus weighting on/off      (offline only, and it matters)
  --ks          top-k for the hit rate

Every number printed here comes from the product's own code path —
``read_corpus``, ``build_idf``, ``Embedder``, ``JsonStore.search`` and the
multi-source gold labels from ``agent_cli``.  A benchmark that re-implements
its subject measures the benchmark.

Usage
-----
    # offline grid, the default: no key, no network, no database
    python scripts/bench_embedding.py

    # one configuration, quick look
    python scripts/bench_embedding.py --chunks 512 --overlaps 64 --idf on

    # the API embedder (needs SF_EMBED_API_KEY); each row costs one pass
    python scripts/bench_embedding.py --mode api --dims 1024

Results: bench/embedding-<mode>.json, plus a markdown table on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import agent_cli  # noqa: E402  (load_questions / evaluate / _gold_sources)
from bench_util import (Phase, env_fingerprint, human_bytes, md_table,  # noqa: E402
                        read_json, stats, write_json)
from schemafence.knowledge import (Embedder, JsonStore, build_idf,  # noqa: E402
                                   cosine, read_corpus)

DEFAULT_CORPUS = ROOT / "examples" / "knowledge-dba"
DEFAULT_EVAL = ROOT / "eval" / "questions-dba.md"


def parse_grid(raw: str, cast=int) -> list:
    return [cast(p) for p in str(raw).split(",") if str(p).strip() != ""]


def run_config(mode: str, dim: int, chunk: int, overlap: int, use_idf: bool,
               corpus: Path, questions: list[tuple[str, str]], ks: list[int],
               repeat: int, scratch: Path) -> dict:
    """One (mode, dim, chunk, overlap, idf) cell of the grid."""
    row: dict = {"mode": mode, "dim": dim, "chunk": chunk, "overlap": overlap,
                 "idf": use_idf, "phases": {}}

    with Phase("chunk") as ph:
        chunks = read_corpus(corpus, target_tokens=chunk, overlap_tokens=overlap)
    row["phases"]["chunk"] = ph.record
    if not chunks:
        raise FileNotFoundError(f"no documents under {corpus}")
    row["chunks"] = len(chunks)
    row["sources"] = len({c.source for c in chunks})
    row["avg_tokens"] = round(sum(c.token_count for c in chunks) / len(chunks), 1)
    row["total_chars"] = sum(len(c.content) for c in chunks)

    embedder = Embedder(mode=mode, dim=dim)
    texts = [c.content for c in chunks]

    if embedder.mode == "offline" and use_idf:
        with Phase("idf") as ph:
            embedder.idf = build_idf(texts)
        row["phases"]["idf"] = ph.record
        row["idf_terms"] = len(embedder.idf)

    with Phase("embed") as ph:
        vectors = embedder.many(texts)
    row["phases"]["embed"] = ph.record
    if any(len(v) != dim for v in vectors):
        raise RuntimeError(f"the embedder returned {len(vectors[0])}-dim vectors, "
                           f"the store expects {dim} — recreate or change --dims")
    for c, v in zip(chunks, vectors):
        c.vector = v

    row["embed_ms_per_chunk"] = round(ph.record["wall_ms"] / len(chunks), 3)
    row["chunks_per_s"] = round(len(chunks) / max(ph.record["wall_ms"] / 1000, 1e-9), 1)
    row["chars_per_s"] = round(row["total_chars"] / max(ph.record["wall_ms"] / 1000, 1e-9))

    store_path = scratch / f"store-{mode}-{dim}-{chunk}-{overlap}-{int(use_idf)}.json"
    store = JsonStore(store_path, dim=dim)
    store.embedder = embedder
    store.replace(chunks)
    with Phase("save") as ph:
        store.save()
    row["phases"]["save"] = ph.record
    row["store_bytes"] = store_path.stat().st_size if store_path.exists() else None
    row["bytes_per_chunk"] = (round(row["store_bytes"] / len(chunks))
                              if row["store_bytes"] else None)

    # Retrieval: query-side embedding is part of the cost, so it is measured.
    with Phase("query") as ph:
        latencies: list[float] = []
        for k in ks:
            for _ in range(repeat):
                for question, _gold in questions:
                    t0 = time.perf_counter()
                    store.search(embedder.one(question), k=k)
                    latencies.append((time.perf_counter() - t0) * 1000.0)
    row["phases"]["query"] = ph.record
    row["query_stats"] = stats(latencies)

    for k in ks:
        result = agent_cli.evaluate(store, embedder, questions, k)
        row[f"hit_rate@{k}"] = round(result["hit_rate"], 4)
        row[f"rank_ms@{k}"] = round(result["avg_ms"], 3)
        # hit@k saturates on a small corpus: once everything is somewhere in the
        # top five, the knob that moved looks like it did nothing.  MRR and the
        # mean rank are how a re-ranking effect stays visible — IDF changed the
        # order long before it changed the hit rate.
        ranks = [rank for _q, _g, rank, _s in result["rows"] if rank]
        row[f"mrr@{k}"] = round(sum(1.0 / r for r in ranks) / len(result["rows"]), 4)
        row[f"mean_rank@{k}"] = round(sum(ranks) / len(ranks), 3) if ranks else None
        if k == ks[0]:
            scored = [s for _q, _g, rank, s in result["rows"] if rank and s is not None]
            row["top1_floor_on_hits"] = round(min(scored), 4) if scored else None
            row["top1_max"] = round(max((s for _q, _g, _r, s in result["rows"]
                                         if s is not None), default=0.0), 4)
            row["misses"] = [{"question": q, "gold": g, "got": got}
                             for q, g, got in result["misses"]]
    store_path.unlink(missing_ok=True)
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="embedding benchmark: corpus x hyperparameters x embedder")
    parser.add_argument("--mode", default="offline",
                        help="offline | api | both (default: offline)")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--eval-file", default=str(DEFAULT_EVAL))
    parser.add_argument("--chunks", default="256,512,1024")
    parser.add_argument("--overlaps", default="0,64,128")
    parser.add_argument("--dims", default="1024",
                        help="vector dimensions (offline sweep; API models are fixed)")
    parser.add_argument("--idf", default="on,off", help="corpus weighting on/off")
    parser.add_argument("--ks", default="5", help="top-k values for the hit rate")
    parser.add_argument("--repeat", type=int, default=3,
                        help="query repetitions per question per k")
    parser.add_argument("--limit-questions", type=int, default=0,
                        help="use only the first N questions (smoke runs)")
    parser.add_argument("--out", default=None,
                        help="JSON output (default bench/embedding-<mode>.json)")
    parser.add_argument("--markdown", default=None,
                        help="also write the table to this markdown file")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus)
    questions = agent_cli.load_questions(Path(args.eval_file))
    if args.limit_questions:
        questions = questions[:args.limit_questions]
    if not questions:
        print(f"error: no questions in {args.eval_file} — the hit rate needs a ruler",
              file=sys.stderr)
        return 2

    modes = ["offline", "api"] if args.mode == "both" else [args.mode]
    chunks = parse_grid(args.chunks)
    overlaps = parse_grid(args.overlaps)
    dims = parse_grid(args.dims)
    ks = parse_grid(args.ks)
    idf_flags = [s.strip() != "off" for s in str(args.idf).split(",")]

    payload = {"benchmark": "embedding", "env": env_fingerprint(),
               "corpus": str(corpus.relative_to(ROOT)) if corpus.is_relative_to(ROOT)
               else str(corpus),
               "eval_file": str(Path(args.eval_file).name),
               "questions": len(questions), "repeat": args.repeat, "rows": []}

    print(f"corpus    : {payload['corpus']}")
    print(f"questions : {len(questions)}")
    print(f"env       : {payload['env']['os']}, python {payload['env']['python']}, "
          f"{payload['env']['cpu_count']} cpu")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for mode in modes:
            for dim in (dims if mode == "offline" else [1024]):
                for chunk in chunks:
                    for overlap in overlaps:
                        if overlap >= chunk:
                            continue
                        for use_idf in (idf_flags if mode == "offline" else [False]):
                            label = (f"{mode} dim={dim} chunk={chunk} "
                                     f"overlap={overlap} idf={'on' if use_idf else 'off'}")
                            try:
                                row = run_config(mode, dim, chunk, overlap, use_idf,
                                                 corpus, questions, ks, args.repeat,
                                                 scratch)
                            except (RuntimeError, FileNotFoundError) as exc:
                                print(f"  SKIP  {label}\n        {exc}")
                                payload["rows"].append({"mode": mode, "dim": dim,
                                                        "chunk": chunk,
                                                        "overlap": overlap,
                                                        "idf": use_idf,
                                                        "error": str(exc)})
                                continue
                            payload["rows"].append(row)
                            print(f"  ok    {label:<44} "
                                  f"{row['chunks']:>4} pieces  "
                                  f"embed {row['phases']['embed']['wall_ms']:>8.0f} ms  "
                                  f"store {human_bytes(row['store_bytes']):>8}  "
                                  f"hit@{ks[0]} {row[f'hit_rate@{ks[0]}'] * 100:>5.1f}%  "
                                  f"q {row['query_stats']['p50_ms']:>7.2f} ms")

    rows = [r for r in payload["rows"] if "error" not in r]
    if rows:
        headers = ["mode", "dim", "chunk", "overlap", "idf", "pieces", "avg tok",
                   "embed ms", "ms/piece", "pieces/s", "store", "B/piece",
                   f"hit@{ks[0]}", f"MRR@{ks[0]}", f"mean rank@{ks[0]}",
                   "q p50 ms", "q p95 ms"]
        table = [[r["mode"], r["dim"], r["chunk"], r["overlap"],
                  "on" if r["idf"] else "off", r["chunks"], r["avg_tokens"],
                  f"{r['phases']['embed']['wall_ms']:.0f}", f"{r['embed_ms_per_chunk']:.2f}",
                  f"{r['chunks_per_s']:.0f}", human_bytes(r["store_bytes"]),
                  r["bytes_per_chunk"], f"{r[f'hit_rate@{ks[0]}'] * 100:.1f}%",
                  f"{r.get(f'mrr@{ks[0]}', 0):.3f}", r.get(f"mean_rank@{ks[0]}"),
                  f"{r['query_stats']['p50_ms']:.2f}", f"{r['query_stats']['p95_ms']:.2f}"]
                 for r in rows]
        print()
        print(md_table(headers, table, ["---"] * len(headers)))
        payload["table"] = {"headers": headers, "rows": table}

    out = Path(args.out) if args.out else ROOT / "bench" / f"embedding-{args.mode}.json"
    write_json(out, payload)
    print()
    print(f"json: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    if args.markdown:
        md = Path(args.markdown)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(md_table(payload["table"]["headers"], payload["table"]["rows"])
                      + "\n", encoding="utf-8")
        print(f"md  : {md}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
