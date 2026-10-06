#!/usr/bin/env python
"""Assemble the benchmark JSON files into one report.

Reads whatever the other three scripts left in ``bench/`` and writes
``docs/bench-report.md``.  A section whose input is missing is rendered as
"not run, here is the command" rather than silently omitted — the report has
to make the boundary between measured and unmeasured obvious, because the
whole point of a benchmark is knowing which numbers you actually have.

    python scripts/bench_report.py                 # default in/out paths
    python scripts/bench_report.py --bench bench --out docs/bench-report.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bench_util import human_bytes, md_table, read_json  # noqa: E402
from test_layers import LIVE_ENV_SQL, LIVE_VERIFY_CMDS  # noqa: E402

COMMANDS = {
    "embedding": "python scripts/bench_embedding.py --mode offline "
                 "--chunks 256,512,1024 --overlaps 0,64,128 --idf on,off",
    "embedding_api": "SF_EMBED_API_KEY=... python scripts/bench_embedding.py "
                     "--mode api --dims 1024 --chunks 512 --overlaps 64",
    "store_json": "python scripts/bench_store.py --backend json --repeat 10",
    "store_pg": "python scripts/bench_store.py --backend pg --repeat 10 "
                "--db postgresql://agent_ro:ro_only@localhost:5432/fence_demo",
    "layers": "python scripts/test_layers.py",
}


def embedding_rows(payload: dict) -> tuple[list[str], list[list]]:
    ks = sorted({int(key.split("@")[1]) for row in payload["rows"]
                 for key in row if key.startswith("hit_rate@")})
    k0 = ks[0] if ks else 5
    headers = ["embedder", "dim", "chunk", "overlap", "idf", "pieces", "avg tok",
               "embed ms", "ms/piece", "pieces/s", "store", "B/piece",
               f"hit@{k0}", f"MRR@{k0}", f"mean rank@{k0}", "q p50 ms", "q p95 ms",
               "peak RSS MB"]
    rows = []
    for r in payload["rows"]:
        if "error" in r:
            continue
        peak = r["phases"].get("embed", {}).get("rss_peak_mb")
        rows.append([r["mode"], r["dim"], r["chunk"], r["overlap"],
                     "on" if r["idf"] else "off", r["chunks"], r["avg_tokens"],
                     f"{r['phases']['embed']['wall_ms']:.0f}",
                     f"{r['embed_ms_per_chunk']:.2f}", f"{r['chunks_per_s']:.0f}",
                     human_bytes(r.get("store_bytes")), r.get("bytes_per_chunk"),
                     f"{r.get(f'hit_rate@{k0}', 0) * 100:.1f}%",
                     f"{r.get(f'mrr@{k0}', 0):.3f}", r.get(f"mean_rank@{k0}"),
                     f"{r['query_stats']['p50_ms']:.2f}",
                     f"{r['query_stats']['p95_ms']:.2f}", peak])
    return headers, rows


def best_worst(rows: list[list], hit_col: int, chunk_col: int, idf_col: int) -> str:
    def pct(cell: str) -> float:
        return float(str(cell).rstrip("%"))
    best = max(rows, key=lambda r: pct(r[hit_col]))
    worst = min(rows, key=lambda r: pct(r[hit_col]))
    return (f"best hit rate {best[hit_col]} at chunk={best[chunk_col]} "
            f"idf={best[idf_col]}; worst {worst[hit_col]} at "
            f"chunk={worst[chunk_col]} idf={worst[idf_col]}")


def store_section(payload: dict) -> str:
    runs = payload.get("runs", {})
    headers = ["backend", "pieces", "ingest ms", "open ms", "on disk", "peak RSS MB",
               "p50 ms", "p95 ms", "max ms", "p50 seqscan ms"]
    rows = []
    for backend, row in runs.items():
        if "error" in row:
            continue
        s = row.get("search_stats", {})
        seq = row.get("search_seqscan_stats", {})
        rows.append([backend, row.get("chunks"),
                     f"{row['phases']['ingest_total']['wall_ms']:.0f}",
                     f"{row['phases']['cold_open']['wall_ms']:.1f}",
                     human_bytes(row.get("store_bytes")),
                     row["phases"]["ingest_total"].get("rss_peak_mb"),
                     s.get("p50_ms"), s.get("p95_ms"), s.get("max_ms"), seq.get("p50_ms")])
    out = []
    if rows:
        out.append(md_table(headers, rows, ["---"] + ["---:"] * (len(headers) - 1)))
        ingest = runs.get("json", {}).get("phases", {}).get("ingest_total", {})
        if ingest:
            out.append("")
            out.append(f"插入方式决定了两者的差距：JSON 写一个文件"
                       f"（{human_bytes(runs['json'].get('store_bytes'))}），"
                       f"pgvector 在 autocommit 连接上逐行 INSERT。下表是这两种做法的"
                       f"CPU 与 I/O 代价。")
    for backend, row in runs.items():
        if "error" not in row:
            continue
        out.append("")
        out.append(f"`{backend}` did not run: {row['error']}")
    res = []
    for key in ("agreement_index_vs_json", "agreement_seqscan_vs_json"):
        a = payload.get(key)
        if a:
            res.append(f"- {key}: same top-1 {a['same_top1_pct']}%, "
                       f"mean overlap {a['mean_overlap_at_k']}")
    for key in ("speedup_index_vs_seqscan", "speedup_json_vs_pg_index"):
        if payload.get(key):
            res.append(f"- {key}: {payload[key]:.1f}x")
    if res:
        out.append("")
        out.append("\n".join(res))
    return "\n".join(out)


def resource_section(payload: dict) -> str:
    headers = ["phase", "wall ms", "cpu ms", "cpu % of wall", "RSS delta MB",
               "RSS peak MB", "read bytes", "write bytes"]
    rows = []
    for name, ph in payload["phases"].items():
        rows.append([name, f"{ph['wall_ms']:.1f}", ph.get("cpu_ms"),
                     ph.get("cpu_pct_of_wall"), ph.get("rss_delta_mb"),
                     ph.get("rss_peak_mb"), ph.get("read_bytes"), ph.get("write_bytes")])
    return md_table(headers, rows, ["---"] + ["---:"] * (len(headers) - 1))


def layers_section(payload: dict) -> str:
    out = []
    for suite, title in (("guard", "运行时七层护栏 (guard.py)"),
                         ("checks", "静态七层检查 (checks.py)")):
        rows = payload["suites"].get(suite) or []
        if not rows:
            continue
        passed = sum(1 for r in rows if r["passed"] and not r.get("skipped"))
        skipped = sum(1 for r in rows if r.get("skipped"))
        out.append(f"### {title}")
        out.append("")
        out.append(f"{passed}/{len(rows) - skipped} passed"
                   + (f", {skipped} skipped (needs --db)" if skipped else ""))
        out.append("")
        table = [[r["id"], r["layer"], r["name"],
                  "PASS" if r["passed"] and not r.get("skipped")
                  else ("SKIP" if r.get("skipped") else "FAIL")]
                 for r in rows]
        out.append(md_table(["case", "layer", "what it asserts", "result"], table,
                            ["---", "---", "---", ":-:"]))
        notes = [r for r in rows if r.get("known_false_positive")]
        for r in notes:
            out.append("")
            out.append(f"> `{r['id']}` is a deliberate false positive: {r['name']} — "
                       f"`{r['sql']}` is read-only, but the layer refuses it rather than "
                       f"allow a function that can also change a session. Change it only "
                       f"if you need session introspection more than you need the ban.")
        out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="assemble bench/ into one report")
    parser.add_argument("--bench", default=str(ROOT / "bench"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "bench-report.md"))
    args = parser.parse_args(argv)

    bench = Path(args.bench)
    files = sorted(bench.glob("*.json")) if bench.exists() else []
    parsed = []
    for path in files:
        data = read_json(path)
        if isinstance(data, dict):
            parsed.append((path.name, data))

    embedding = [(n, d) for n, d in parsed if d.get("benchmark") == "embedding"]
    embedding.sort(key=lambda pair: ("dims" in pair[0], pair[0]))
    stores = [(n, d) for n, d in parsed if d.get("benchmark") == "store"]
    layers = [d for _n, d in parsed if "suites" in d]

    env = (embedding or stores or [("", {"env": {}})])[0][1].get("env", {})
    today = dt.date.today().isoformat()

    md: list[str] = []
    md.append(f"# schemafence 基准测试报告 · {today}")
    md.append("")
    md.append(f"- 环境：{env.get('os', platform.system())} · python {env.get('python', '?')} · "
              f"{env.get('cpu_count', '?')} 逻辑核")
    md.append(f"- 进程计数器：RSS {'可用' if env.get('counters', {}).get('rss') else '不可用'} · "
              f"I/O {'可用' if env.get('counters', {}).get('io') else '不可用'}")
    md.append("- 生成方式：`python scripts/bench_report.py`（数据来自 bench/*.json，"
              "本文件不要手改）")
    md.append("")
    md.append("## 0. 这份报告回答什么")
    md.append("")
    md.append("| 问题 | 脚本 |")
    md.append("|---|---|")
    md.append("| 语料在不同超参下向量化的速度、体积、命中率如何 | `scripts/bench_embedding.py` |")
    md.append("| pgvector 与 JSON 两种存储的检索性能与内存/IO/CPU 对比 | `scripts/bench_store.py` |")
    md.append("| SQL 七层静态检查与运行时七层护栏，逐层行为是否正确 | `scripts/test_layers.py` |")
    md.append("")
    md.append("复现命令见第 5 节；测量方法与口径见第 1 节。"
              "结论与解读写在 [`docs/bench-findings.md`](bench-findings.md)，"
              "重跑基准不会覆盖它。")
    md.append("")

    md.append("## 1. 方法")
    md.append("")
    md.append("三段测试都直接调用产品代码（`read_corpus`、`build_idf`、`Embedder`、"
              "`JsonStore`/`PgStore.search`、`guard()`、`analyze()`），")
    md.append("不重写被测量对象——重写一遍等于在测基准脚本自己。")
    md.append("")
    md.append("- **阶段口径**：wall 为 `perf_counter`，CPU 为 `process_time`，"
              "RSS 取进程当前值与峰值，I/O 取进程累计读写字节的阶段差值。")
    md.append("- **命中率口径**：用 `eval/questions-dba.md` 题库，"
              "多来源标注（`甲 / 乙`）命中任一即算命中。")
    md.append("- **pgvector 的插入**是逐行 `INSERT`（autocommit），慢是真实行为，"
              "不是基准脚本的实现选择；HNSW 与顺序扫描分开测。")
    md.append("- 未在本机运行的部分在第 4 节标明命令与预期，不做推测性数字。")
    md.append("")

    md.append("## 2. 向量化基准")
    md.append("")
    if embedding:
        for name, payload in embedding:
            headers, rows = embedding_rows(payload)
            md.append(f"### `{name}`（{payload.get('corpus', '').replace(chr(92), '/')}）")
            md.append("")
            md.append(f"题库 {payload.get('questions', '?')} 题，"
                      f"每配置查询重复 {payload.get('repeat', '?')} 次。")
            md.append("")
            md.append(md_table(headers, rows, ["---"] + ["---:"] * (len(headers) - 1)))
            md.append("")
            failed = [r for r in payload["rows"] if "error" in r]
            for r in failed:
                md.append(f"- 跳过：{r.get('mode')} dim={r.get('dim')} chunk={r.get('chunk')} "
                          f"overlap={r.get('overlap')} — {r['error']}")
            if failed:
                md.append("")
            # the miss list is the most useful artefact of the whole run
            for r in payload["rows"]:
                misses = r.get("misses")
                if misses:
                    md.append(f"失效样本（chunk={r['chunk']}, overlap={r['overlap']}, "
                              f"idf={'on' if r['idf'] else 'off'}）：")
                    for m in misses:
                        md.append(f"- {m['question']} → 期望 `{m['gold']}`，"
                                  f"实际 {', '.join('`%s`' % g for g in m['got'][:3])}")
                    md.append("")
    else:
        md.append(f"未运行。命令：`{COMMANDS['embedding']}`")
        md.append("")
        md.append(f"API 向量模型（需要 key）：`{COMMANDS['embedding_api']}`")
        md.append("")

    md.append("## 3. 存储对比")
    md.append("")
    measured = {b for _n, p in stores for b in (p.get("runs") or {})}
    if stores:
        for name, payload in stores:
            md.append(f"### `{name}`")
            md.append("")
            md.append(f"嵌入方式 `{payload.get('embedder')}`，"
                      f"top-k {payload.get('k')}，每题重复 {payload.get('repeat')} 次"
                      + ("，HNSW 启用" if payload.get("runs", {}).get("pg") else ""))
            md.append("")
            md.append(store_section(payload))
            md.append("")
            for backend, row in payload.get("runs", {}).items():
                if "error" in row:
                    continue
                md.append(f"**{backend} 各阶段资源**（同一进程内测量）")
                md.append("")
                md.append(resource_section(row))
                md.append("")
    for backend, command in (("json", COMMANDS["store_json"]), ("pg", COMMANDS["store_pg"])):
        if backend not in measured:
            md.append(f"- `{backend}` 未测：`{command}`")
    if not stores or "pg" not in measured:
        md.append("")
        md.append("pgvector 那一半需要运行中的 PostgreSQL（`--db`）与 `psycopg`；"
                  "本机环境没有，故本节只有 JSON 侧的数字，不做推测。")
    if not stores:
        md.append("")
        md.append(f"未运行。JSON：`{COMMANDS['store_json']}`")
    md.append("")

    md.append("## 4. 七层检查与七层护栏")
    md.append("")
    if layers:
        md.append(layers_section(layers[0]))
    else:
        md.append(f"未运行。命令：`{COMMANDS['layers']}`")
        md.append("")

    md.append("### 需要数据库的环境准备")
    md.append("")
    md.append("```sql")
    md.append(LIVE_ENV_SQL.strip())
    md.append("```")
    md.append("")
    md.append("验证：")
    md.append("")
    md.append("```bash")
    for line in LIVE_VERIFY_CMDS:
        md.append(line)
    md.append("```")
    md.append("")

    md.append("## 5. 复现命令")
    md.append("")
    md.append("```bash")
    md.append("# 环境：离线路径零依赖；live 路径需要 psycopg 与运行中的 PostgreSQL")
    md.append("pip install -r requirements.txt        # psycopg[binary] = 唯一的第三方依赖")
    md.append("")
    md.append("# 1) 向量化：离线哈希 vs 向量模型，多超参网格")
    md.append(COMMANDS["embedding"])
    md.append(COMMANDS["embedding_api"])
    md.append("")
    md.append("# 2) 存储：JSON 与 pgvector 分进程各跑一次，内存数字才干净")
    md.append(COMMANDS["store_json"])
    md.append(COMMANDS["store_pg"])
    md.append("")
    md.append("# 3) 七层检查与护栏")
    md.append(COMMANDS["layers"])
    md.append("python scripts/test_layers.py --show-env     # 打印库侧环境准备")
    md.append("")
    md.append("# 4) 汇总成这份报告")
    md.append("python scripts/bench_report.py")
    md.append("```")
    md.append("")
    md.append("> 本报告由脚本生成；结论性段落请写在 `docs/bench-findings.md`，"
              "重跑基准不会覆盖它。")
    md.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), encoding="utf-8")
    print(f"report: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    print(f"inputs: {len(embedding)} embedding, {len(stores)} store, {len(layers)} layer file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
