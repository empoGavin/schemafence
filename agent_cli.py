#!/usr/bin/env python3
"""One entry point for the whole agent: ingest, ask, evaluate, tune.

    python agent_cli.py --ingest examples/knowledge
    python agent_cli.py --ask "PG 里表膨胀怎么处理？"
    python agent_cli.py --ask "有几条订单？"          # needs --db to return rows
    python agent_cli.py --eval
    python agent_cli.py --tune
    python agent_cli.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo \
                        --ingest examples/knowledge

Offline (no --db, no key) uses a JSON store and a lexical hashed embedding:
clone it, run it, see the whole loop work.  Adding a key or a database swaps
one backend at a time — the loop, the tools and the guard never change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from schemafence import parse_ddl                                    # noqa: E402
from schemafence.agent import LLMClient, route_reasons, run_agent      # noqa: E402
from schemafence.knowledge import (DEFAULT_EMBED_MODEL, Embedder, JsonStore,  # noqa: E402
                                   PgStore, build_idf, ingest_directory,
                                   load_idf, read_corpus, save_idf)
from schemafence.tools import Toolbox                                  # noqa: E402

WIDTH = 74
ENV_FILE = Path.home() / ".schemafence.env"
DEFAULT_CORPUS = ROOT / "examples" / "knowledge"
DEFAULT_STORE = ROOT / ".schemafence" / "knowledge.json"
DEFAULT_EVAL = ROOT / "eval" / "questions.md"
DEFAULT_SCHEMA = ROOT / "examples" / "sample_schema.sql"


def load_env_file(path: Path = ENV_FILE) -> list[str]:
    """Apply SF_* settings from ~/.schemafence.env, without hiding real env vars.

    The handbook keeps keys in this file and tells readers to `source` it by
    hand; in practice people forget the source (or the `set -a`), and the run
    then silently mixes one provider's key with another provider's endpoint —
    which is exactly how a SiliconFlow key ended up hitting DashScope and
    getting HTTP 401.  Loading it here removes that failure mode.  Rules:

      - only SF_* keys are applied (this file is not a general dotenv);
      - variables already in the environment win (setdefault), so a one-off
        `SF_EMBED_MODEL=x python agent_cli.py ...` still overrides;
      - values are never printed, only names.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    applied: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key.startswith("SF_") and key and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


def rel(path) -> str:
    """A path as it should appear in a committed file: relative to the repo.

    The report is part of the repository, and the repository is public.  An
    absolute path puts the author's home directory into the artefact — noise
    for anyone reading it, and a link that breaks the moment someone clones
    this somewhere else.  Paths outside the repo keep their name only.
    """
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return p.name if p.name else str(p)


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def heading(text: str) -> None:
    print()
    print(text)
    rule()


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def make_embedder(args) -> Embedder:
    # `dimensions` is a Matryoshka-model feature (text-embedding-v3/v4,
    # Qwen3-Embedding, OpenAI text-embedding-3-*).  Fixed-dim models such as
    # BAAI/bge-m3 reject the field outright with HTTP 400, so it is sent only
    # when explicitly asked for.  A store-dim mismatch is caught anyway by the
    # loud check on the response — that check is the contract, this field is
    # just an optimisation for models that can honour it.
    raw = os.environ.get("SF_EMBED_DIMENSIONS", "").strip()
    dimensions = int(raw) if raw.isdigit() else None
    return Embedder(mode=args.mode, dim=args.dim, dimensions=dimensions)


def idf_sidecar(args) -> Path:
    return Path(args.store).parent / "idf.json"


def store_has_idf(args) -> bool:
    """Does the store already carry corpus weights?  Peek, do not load.

    The header is printed before anything is opened, so the answer has to come
    from a cheap inspection of the artefact itself rather than from the live
    object.
    """
    if args.db:
        return idf_sidecar(args).exists()
    path = Path(args.store)
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("idf"))
    except (OSError, ValueError):
        return False


def embedding_line(args, embedder: Embedder) -> str:
    """The header must not under-report the method actually in use.

    `Embedder.label` cannot know about IDF yet — the weights live in the store
    or in the sidecar and are only attached when the store is opened.  Ingest
    builds them from the corpus it is about to read, and a query reuses
    whatever the store carries, so both cases have weights even though the
    in-memory dict is still empty at print time.
    """
    if embedder.mode == "api":
        return embedder.label
    weights = bool(embedder.idf) or bool(getattr(args, "ingest", None)) \
        or store_has_idf(args)
    suffix = " + corpus idf" if weights else " / no idf"
    return f"offline / hashed-lexical{suffix} / {embedder.dim}d"


def open_store(args, embedder: Embedder, create: bool = False):
    """Open whichever backend the flags ask for, and make sure the query
    side uses the same feature weights the ingest side used."""
    if args.db:
        store = PgStore(args.db, embedder)
        if create:
            store.ensure_schema(with_index=not args.no_index)
        if embedder.mode == "offline" and not embedder.idf:
            # In pgvector mode the weights live in a sidecar next to --store, not
            # in the table.  If it went missing the query vector would silently
            # differ from the one that was stored, and the rankings would drift
            # with no error anywhere — so say it out loud instead.
            embedder.idf = load_idf(idf_sidecar(args))
            if not embedder.idf and not create:
                print(f"  note: no corpus weights at {idf_sidecar(args)} — "
                      f"running without IDF, so ranking will differ from the "
                      f"ingest run.  Re-ingest if this is unexpected.")
        return store

    store = JsonStore.load(args.store, dim=args.dim)
    if embedder.mode == "offline" and not embedder.idf:
        embedder.idf = store.idf
    store.embedder = embedder
    return store


def load_schema(args):
    """The router needs a schema to turn a question into SQL."""
    if args.db:
        try:
            from schemafence import snapshot
            conn = snapshot.connect(args.db)
            with conn:
                return snapshot.read_schema(conn)
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not read the live catalogue: {exc} — using the sample DDL)")
    return parse_ddl(Path(DEFAULT_SCHEMA).read_text(encoding="utf-8"))


def attach_database(args, store):
    if not args.db:
        return None
    if isinstance(store, PgStore):
        return store.conn
    from schemafence import snapshot
    return snapshot.connect(args.db)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_ingest(args, embedder: Embedder) -> int:
    heading("[ingest] chunk → embed → store")
    corpus = Path(args.ingest)
    started = time.perf_counter()
    store = open_store(args, embedder, create=True)
    chunks = ingest_directory(corpus, store, embedder=embedder,
                              target_tokens=args.chunk, overlap_tokens=args.overlap)
    elapsed = time.perf_counter() - started
    if isinstance(store, JsonStore):
        store.save()
    elif embedder.mode == "offline":
        save_idf(embedder.idf, idf_sidecar(args))
        print(f"  idf weights : {len(embedder.idf)} features → {idf_sidecar(args)}")

    stats = store.stats()
    print(f"  corpus      : {corpus}")
    print(f"  embedding   : {embedder.label}")
    print(f"  chunk size  : {args.chunk} tokens, overlap {args.overlap}")
    print(f"  documents   : {stats['documents']}")
    print(f"  chunks      : {stats['chunks']}   avg {stats['avg_tokens']} tokens")
    print(f"  wall time   : {elapsed:.2f}s")
    if isinstance(store, PgStore):
        print(f"  table       : doc_chunks, {stats.get('table_size')}")
        store.close()
    else:
        print(f"  store       : {store.path}")
    return 0


def cmd_ask(args, embedder: Embedder) -> int:
    store = open_store(args, embedder)
    if isinstance(store, JsonStore) and not store.chunks:
        print(f"  the store is empty — run:  python agent_cli.py --ingest {DEFAULT_CORPUS}")
        return 2

    conn = attach_database(args, store)
    toolbox = Toolbox(store=store, conn=conn, whitelist=args.whitelist or None,
                      trace_path=args.trace, max_rows=args.max_rows,
                      search_path=args.search_path,
                      doc_min_score=getattr(args, "min_score", None))
    schema = load_schema(args)
    llm = LLMClient() if args.llm != "rules" else None
    if llm is not None:
        # Same failure class as the embedding 401: a key from one provider sent
        # to another provider's endpoint, or a model name that only exists on
        # one of them.  Say it before the first HTTP call instead of after.
        if llm.model == "deepseek-chat" and "deepseek" not in llm.base_url:
            print(f"  note: model 'deepseek-chat' is the DeepSeek default but the "
                  f"endpoint is {llm.base_url} — set SF_LLM_MODEL (e.g. "
                  f"Qwen/Qwen3-8B or deepseek-ai/DeepSeek-V3.2 on SiliconFlow, "
                  f"both support function calling).")
        if not llm.available:
            print("  note: --llm openai needs SF_LLM_API_KEY — falling back to "
                  "the rule-based router.")
            llm = None

    for question in args.ask:
        heading(f"[ask] {question}")
        for reason in route_reasons(question, schema):
            print(f"  · {reason}")
        print()
        result = run_agent(question, toolbox, schema=schema, llm=llm)
        for index, step in enumerate(result.steps, 1):
            print(step.render(index))
        print()
        print(f"  driver: {result.driver}   rounds: {result.rounds}   "
              f"{result.ms:.0f} ms")
        print()
        for line in result.answer.splitlines():
            print(f"  {line}")
    print()
    print(f"  audit trail appended to {args.trace}")
    return 0


def load_questions(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in {"#", ""} or set(cells[0]) <= set("-: "):
            continue
        rows.append((cells[1], cells[2]))
    return rows


# --------------------------------------------------------------------------- #
# --genq: turn "I would have to write a question bank" into "edit a draft"
# --------------------------------------------------------------------------- #

# Heading-word → question template.  Ordered: first match wins per heading.
_CUES = [
    (("成因", "为什么"),        "{t}是怎么产生的？"),
    (("怎么防",),               "{t}应该怎么防？"),
    (("治理", "处理", "手段", "解决"), "{t}应该怎么处理？"),
    (("定位", "排查", "故障", "卡死"), "{t}出了问题怎么排查？"),
    (("风险", "误区", "低估", "坑"),   "{t}有哪些常见的坑？"),
    (("选型", "权衡", "设计"),  "{t}做设计决策时要权衡什么？"),
    (("阶段", "流程", "步骤"),  "{t}应该分几步推进？"),
    (("有效", "效果", "判断"),  "怎么判断{t}做得好不好？"),
]
_FALLBACK = "关于{t}，笔记的核心结论是什么？"


def _topic(h1: str) -> str:
    """The bare subject of a note title: '表膨胀（table bloat）的成因与治理' → '表膨胀'."""
    t = h1.split("（")[0].split("(")[0]
    t = t.split("：")[0].split(":")[0].strip()      # '半夜卡死：15 分钟定位流程' → '半夜卡死'
    for suffix in ("的成因与治理", "的排查与定位", "与生命周期管理", "的成因",
                   "的治理", "的处理", "笔记", "方法论", "的排查", "排查"):
        if t.endswith(suffix) and len(t) > len(suffix):
            t = t[: -len(suffix)].strip()
    return t or h1


def _scan_corpus(directory: Path) -> list[tuple[str, str, list[str]]]:
    """(source name, H1 topic, H2 headings) per markdown file."""
    docs = []
    for path in sorted(directory.glob("*.md")):
        h1, h2s = "", []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and not h1:
                h1 = line[2:].strip()
            elif line.startswith("## "):
                h2s.append(line[3:].strip())
        if h1:
            docs.append((path.stem, _topic(h1), h2s))
    return docs


def cmd_genq(args, embedder: Embedder) -> int:
    """Generate eval/questions.draft.md — one or two draft questions per note.

    The point is not that a template can write good questions.  It cannot.
    The point is that the expensive part of an eval set is not the wording,
    it is the gold labels and the coverage — which file must be watched by
    at least one question — and that part is fully mechanical.
    """
    heading("[genq] draft question set from the corpus")
    corpus = Path(args.corpus)
    docs = _scan_corpus(corpus)
    if not docs:
        print(f"  no markdown documents under {corpus}")
        return 2

    out = Path(args.genq)
    rows: list[str] = []
    for source, topic, h2s in docs:
        made: list[str] = []
        for h2 in h2s:
            if len(made) >= 2:
                break
            for keys, template in _CUES:
                if any(k in h2 for k in keys):
                    question = template.format(t=topic)
                    if question not in made:        # two headings, same question → one row
                        made.append(question)
                    break
        if not made:
            made.append(_FALLBACK.format(t=topic))
        for question in made:
            rows.append(f"| {len(rows) + 1} | {question} | {source} |")

    lines = [
        "# 评测题库草稿（--genq 自动生成，改完再并入 questions.md）",
        "",
        f"- 语料：{rel(corpus)}（{len(docs)} 篇笔记）",
        "",
        "## 这份草稿怎么用（三步，预计 10 分钟）",
        "",
        "1. **删**：任何你不会那样问的题，直接删掉——评测题必须是真实问法，",
        "   不是模板句。留下你觉得\"这是我真会问的\"的那些。",
        "2. **改**：把模板腔改成你平时说话的问法（\"表膨胀应该怎么处理？\"",
        "   → \"PG 里表膨胀怎么治？\"），问法越像你真实提问，命中率越可信。",
        "3. **补**：加上模板生成不了的三类题——真实故障、真实权衡、",
        "   本公司具体场景（脱敏）。这三类才是面试时最有说服力的。",
        "",
        "> 题库的作用是**量尺**，不是**门槛**：它不需要覆盖你未来会问的一切，",
        "> 它只需要稳定——下次改切片、换嵌入、加语料之后，同一套题重跑，",
        "> 看命中率是涨是跌。这正是回归测试的思路。",
        "",
        "## 草稿（每篇笔记至少 1 题，先保覆盖、再抠问法）",
        "",
        "| # | 问题 | 期望来源 |",
        "|---|------|---------|",
    ]
    lines += rows
    lines += [
        "",
        "## 改完之后",
        "",
        "```bash",
        "python agent_cli.py --eval-file eval/questions.draft.md --eval   # 先试跑",
        "# 满意后把表粘进 eval/questions.md（或直接 --eval-file 指向草稿文件）",
        "```",
        "",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    per_doc: dict[str, int] = {}
    for row in rows:
        gold = row.strip().strip("|").split("|")[-1].strip()
        per_doc[gold] = per_doc.get(gold, 0) + 1
    print(f"  corpus     : {rel(corpus)} ({len(docs)} notes)")
    print(f"  draft      : {out}  ({len(rows)} draft questions)")
    for source, count in per_doc.items():
        print(f"    {source:<36} {count}")
    print()
    print("  what this gave you: coverage + gold labels, for free.")
    print("  what it did NOT give you: your real phrasing.  Edit the draft —")
    print("  a question you would never ask has no place in an eval set.")
    return 0


def _gold_sources(gold: str) -> set[str]:
    """A question may legitimately be answerable by more than one note.

    Once the corpus has adjacent notes ('table bloat, treatment' next to
    'table bloat, misdiagnosis') a single-label ruler scores a true hit as a
    miss and the hit rate drifts down for a reason that has nothing to do
    with retrieval.  A question's 期望来源 may therefore list alternatives
    separated by '/'.
    """
    return {part.strip() for part in gold.replace("|", "/").split("/") if part.strip()}


def evaluate(store, embedder: Embedder, questions, k: int) -> dict:
    hits, ranks, misses = 0, [], []
    elapsed = 0.0
    for question, gold in questions:
        started = time.perf_counter()
        found = store.search(embedder.one(question), k=k)
        elapsed += time.perf_counter() - started
        wanted = _gold_sources(gold)
        rank = next((i for i, hit in enumerate(found, 1) if hit.source in wanted), None)
        # The top-1 score is the raw material for calibrating
        # SF_DOC_MIN_SCORE: the lowest top-1 score among the questions that
        # DID retrieve their gold note is the highest floor that loses
        # nothing — anything above it starts throwing away real hits.
        ranks.append((question, gold, rank, found[0].score if found else None))
        if rank:
            hits += 1
        else:
            misses.append((question, gold, [h.source for h in found]))
    return {"hit_rate": hits / len(questions) if questions else 0.0,
            "avg_ms": 1000 * elapsed / len(questions) if questions else 0.0,
            "rows": ranks, "misses": misses}


def cmd_eval(args, embedder: Embedder) -> int:
    heading("[eval] top-k retrieval hit rate")
    questions = load_questions(Path(args.eval_file))
    if not questions:
        print(f"  no questions found in {args.eval_file}")
        return 2
    store = open_store(args, embedder)
    if isinstance(store, JsonStore) and not store.chunks:
        print("  the store is empty — run --ingest first")
        return 2

    result = evaluate(store, embedder, questions, args.k)
    print(f"  questions   : {len(questions)}")
    print(f"  embedding   : {embedder.label}")
    print(f"  top-k       : {args.k}")
    print(f"  hit rate    : {result['hit_rate'] * 100:.1f}%")
    print(f"  avg latency : {result['avg_ms']:.1f} ms")
    print()
    for question, gold, rank, top_score in result["rows"]:
        mark = f"hit  (rank {rank})" if rank else "MISS"
        score = f"{top_score:.3f}" if top_score is not None else "  -  "
        print(f"  {mark:<14} {score:>6}  {question[:40]:<42} → {gold}")
    if result["misses"]:
        print()
        print("  what the misses retrieved instead:")
        for question, gold, got in result["misses"]:
            print(f"    {question[:40]}  (want {gold}, got {', '.join(got[:3]) or 'nothing'})")
    scored = [s for _q, _g, rank, s in result["rows"] if rank and s is not None]
    if scored:
        print()
        print(f"  top-1 score floor to calibrate against: the lowest score among "
              f"the questions that hit is {min(scored):.3f}.")
        print(f"  Set SF_DOC_MIN_SCORE just below it (e.g. {min(scored) - 0.05:.2f}) "
              f"to keep every real hit and drop the near-misses.")
    return 0


def tune_rows(args, embedder: Embedder, questions) -> list[dict]:
    """The grid the plan asks for: chunk size x overlap x top-k."""
    rows: list[dict] = []
    for chunk in (256, 512, 1024):
        for overlap in (0, 64, 128):
            if overlap >= chunk:
                continue
            chunks = read_corpus(args.corpus, target_tokens=chunk, overlap_tokens=overlap)
            if not chunks:
                raise FileNotFoundError(f"no documents under {args.corpus}")
            texts = [c.content for c in chunks]
            if embedder.mode == "offline":
                embedder.idf = build_idf(texts)
            vectors = embedder.many(texts)
            for c, v in zip(chunks, vectors):
                c.vector = v
            store = JsonStore(ROOT / ".schemafence" / "_tune.json", dim=embedder.dim)
            store.embedder = embedder
            store.replace(chunks)
            for k in (3, 5, 10):
                result = evaluate(store, embedder, questions, k)
                rows.append({"chunk": chunk, "overlap": overlap, "k": k,
                             "chunks_n": len(chunks),
                             "hit_rate": result["hit_rate"], "avg_ms": result["avg_ms"]})
    return rows


def cmd_tune(args, embedder: Embedder) -> int:
    heading("[tune] chunk size × overlap × top-k")
    questions = load_questions(Path(args.eval_file))
    if not questions:
        print("  no eval questions — nothing to tune against")
        return 2
    if embedder.mode == "api":
        print("  note: api embedding — this costs one embedding call per chunk per row")
    print(f"  {'chunk':>6} {'overlap':>8} {'k':>4} {'pieces':>7} {'hit rate':>10} {'avg ms':>8}")
    rule()
    rows = tune_rows(args, embedder, questions)
    for row in rows:
        print(f"  {row['chunk']:>6} {row['overlap']:>8} {row['k']:>4} {row['chunks_n']:>7} "
              f"{row['hit_rate'] * 100:>9.1f}% {row['avg_ms']:>8.1f}")

    spread = max(r["hit_rate"] for r in rows) - min(r["hit_rate"] for r in rows)
    best = max(rows, key=lambda r: (r["hit_rate"], -r["avg_ms"]))
    print()
    print(f"  best: chunk {best['chunk']}, overlap {best['overlap']}, k={best['k']} "
          f"→ {best['hit_rate'] * 100:.1f}%")
    if spread == 0:
        print()
        print("  ⚠ every configuration scores the same, so this table proves nothing yet.")
        print(f"    A {len(questions)}-question quiz over {best['chunks_n']} pieces cannot")
        print("    separate the settings — grow the corpus past ~100 pieces (your own")
        print("    notes), then run this again and put the real numbers in eval/report.md.")
    return 0


def cmd_report(args, embedder: Embedder) -> int:
    """Generate eval/report.md — the retrieval-layer artefact, numbers included.

    Re-running this rewrites sections 3 and 5 (the hand-written failure analysis
    and conclusions) with placeholders.  Write them back after every run — the
    numbers are reproducible, the judgement is not.
    """
    from datetime import date
    questions = load_questions(Path(args.eval_file))
    if not questions:
        print("  no eval questions")
        return 2
    store = open_store(args, embedder)
    if isinstance(store, JsonStore) and not store.chunks:
        print("  run --ingest first")
        return 2
    result = evaluate(store, embedder, questions, args.k)
    rows = tune_rows(args, embedder, questions)
    spread = max(r["hit_rate"] for r in rows) - min(r["hit_rate"] for r in rows)
    best = max(rows, key=lambda r: (r["hit_rate"], -r["avg_ms"]))
    stats = store.stats()

    lines = [
        "# 检索评测报告（RAG 检索层）",
        "",
        "> 本文件由 `python agent_cli.py --report eval/report-dba.md` 生成；"
        "第 3 节（失效原因）与第 5 节（三行结论）是手写的，重跑会覆盖，需补回。",
        "",
        f"- 生成日期：{date.today().isoformat()}",
        f"- 语料：{rel(args.corpus)}（{stats['documents']} 篇 / {stats['chunks']} 片，"
        f"平均 {stats['avg_tokens']} token）",
        f"- 嵌入：{embedder.label}",
        f"- 题库：{rel(args.eval_file)}（{len(questions)} 题）",
        f"- 判定：top-{args.k} 中出现期望来源记为命中",
        "",
        "## 1. 顶层结果",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| top-{args.k} 命中率 | {result['hit_rate'] * 100:.1f}% |",
        f"| 平均检索延迟 | {result['avg_ms']:.1f} ms |",
        f"| 语料规模 | {stats['chunks']} 片 |",
        "",
        "> **读数纪律**：命中率必须与语料规模一起报。"
        "在 16 片的语料上 100%，说明不了任何工程能力；"
        "自己 3 万字素材进去之后的那组数字才算数。",
        "",
        "## 2. 逐题结果",
        "",
        "| # | 问题 | 期望来源 | 命中 | 排名 | top-1 分数 |",
        "|---|------|---------|------|------|-----------|",
    ]
    for index, (question, gold, rank, top_score) in enumerate(result["rows"], 1):
        score = f"{top_score:.3f}" if top_score is not None else "—"
        lines.append(f"| {index} | {question} | {gold} | "
                     f"{'✅' if rank else '❌'} | {rank or '—'} | {score} |")

    lines += ["", "## 3. 失效样本", ""]
    if result["misses"]:
        lines += ["| 问题 | 期望 | 实际检索到 | 我的判断 |", "|---|---|---|---|"]
        for question, gold, got in result["misses"]:
            lines.append(f"| {question} | {gold} | {', '.join(got[:3])} | (写原因："
                         "换词？切片太小？语料里根本没有？) |")
    else:
        lines.append("本轮无失效样本。**但请把这一节留着**：语料扩到 100 片以上必然出现失效，"
                     "届时按上面的表记录原因。")

    lines += ["", "## 4. 调参对比", "",
              "| 切片 token | 重叠 | top-k | 片数 | 命中率 | 平均延迟 ms |",
              "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['chunk']} | {row['overlap']} | {row['k']} | "
                     f"{row['chunks_n']} | {row['hit_rate'] * 100:.1f}% | {row['avg_ms']:.1f} |")
    lines += ["", "## 5. 三行结论（必须自己写，不许留空）", "",
              "1. 最好的一组是：____，依据是____。",
              "2. 语料或切片上最大的意外是：____。",
              "3. 下一轮要改的一件事是：____。"]
    if spread == 0:
        lines += ["", "> ⚠ 本轮所有组合得分相同 → 语料过小，本表暂无区分度。"
                      "扩到 100 片以上再跑一次 `--tune`，用新数字替换本节。"]
    lines.append("")

    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {path}")
    print(f"  top-{args.k} hit rate {result['hit_rate'] * 100:.1f}%, "
          f"{len(result['misses'])} miss(es), {len(rows)} tuning rows")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_cli.py",
        description="A hand-written database agent: knowledge retrieval, read-only "
                    "SQL, and a guardrail that neither the model nor the caller can "
                    "talk its way past.")
    parser.add_argument("--ingest", metavar="DIR", help="chunk and embed a corpus")
    parser.add_argument("--ask", action="append", metavar="QUESTION",
                        help="ask the agent (repeatable)")
    parser.add_argument("--eval", action="store_true", help="measure retrieval hit rate")
    parser.add_argument("--genq", metavar="PATH", nargs="?", const="eval/questions.draft.md",
                        help="generate a draft question set from the corpus (edit it, then eval against it)")
    parser.add_argument("--tune", action="store_true", help="chunk/overlap/k comparison")
    parser.add_argument("--report", metavar="PATH",
                        help="write eval/report.md with the numbers already filled in")
    parser.add_argument("--eval-file", default=str(DEFAULT_EVAL))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--db", default=None, help="PostgreSQL DSN → pgvector + live SQL")
    parser.add_argument("--mode", default="auto", choices=["auto", "offline", "api"],
                        help="embedding backend (auto = api when a key is present)")
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--chunk", type=int, default=512, help="target tokens per chunk")
    parser.add_argument("--overlap", type=int, default=64)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=None, metavar="F",
                        help="relevance floor for search_docs (default: 0.45 with "
                             "API embeddings, 0 with the offline hash embedder). "
                             "Calibrate from the top-1 score column of --eval")
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--whitelist", nargs="*", default=[],
                        help="tables the guard will allow (default: any)")
    parser.add_argument("--search-path", default=None, metavar="SCHEMA",
                        help="SET search_path before every query (e.g. shop) "
                             "so unqualified table names resolve")
    parser.add_argument("--no-index", action="store_true",
                        help="create doc_chunks without the HNSW index")
    parser.add_argument("--llm", default="rules", choices=["rules", "openai"],
                        help="rules = deterministic router; openai = function calling")
    parser.add_argument("--trace", default="agent_trace.jsonl")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not any([args.ingest, args.ask, args.eval, args.tune, args.report, args.genq]):
        parser.print_help()
        return 0

    applied = load_env_file(ENV_FILE)
    # Bad operator input (a missing corpus, a malformed --search-path, an
    # api embedder with no key) used to surface as a raw traceback with exit
    # 1.  The message was already actionable; only the delivery was wrong.
    # Catch the input-shaped failures, say them in one line, exit non-zero.
    try:
        embedder = make_embedder(args)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("schemafence agent — answers from a DBA knowledge base, executes behind a fence")
    if applied:
        print(f"env file   : {ENV_FILE} sets {', '.join(applied)}")
    elif ENV_FILE.exists():
        print(f"env file   : {ENV_FILE} (found, nothing new applied)")
    else:
        print(f"env file   : {ENV_FILE} not found — keys come from the "
              f"environment only (see handbook appendix A)")
    print(f"embedding : {embedding_line(args, embedder)}")
    print(f"storage   : {'pgvector ' + args.db if args.db else 'json ' + args.store}")
    print(f"driver    : {'model function calling' if args.llm == 'openai' else 'rules'}")
    if embedder.mode == "api" and embedder.model == DEFAULT_EMBED_MODEL \
            and "dashscope" not in embedder.base_url:
        print(f"  note: model '{embedder.model}' is the DashScope default but the "
              f"endpoint is {embedder.base_url} — most providers will reject it. "
              f"Set SF_EMBED_MODEL (e.g. BAAI/bge-m3 on SiliconFlow, also 1024d).")

    try:
        if args.ingest:
            return cmd_ingest(args, embedder)
        if args.ask:
            return cmd_ask(args, embedder)
        if args.genq:
            return cmd_genq(args, embedder)
        # --report first: it runs the eval *and* the tuning grid and writes both.
        # Checked before --eval because `--eval --report x.md` used to fall into
        # cmd_eval, print the numbers and silently write no file.
        if args.report:
            return cmd_report(args, embedder)
        if args.eval:
            return cmd_eval(args, embedder)
        return cmd_tune(args, embedder)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
