#!/usr/bin/env python3
"""把 examples/ 下的语料灌进 pgvector —— 两种形状，各有各的用途。

形状一：一套语料一个库（默认）

    python scripts/ingest_pgvector.py --base postgresql://postgres:pw@localhost:5432

    建 5 个库 kb_default / kb_dba / kb_hand / kb_official / kb_internals。
    每套语料的词权重（IDF）独立，各自那套评测基线可以逐条复现。
    适合「量尺」：你想知道每套语料本身好不好用。

形状二：全部语料合并成一个库（--single-db）

    python scripts/ingest_pgvector.py --base ... --single-db

    建 1 个库 kb_schemafence，164 篇语料同住一张 doc_chunks，
    一个问题问遍全部 —— 现场诊断要的就是这个形状。
    适合「干活」：agent 手上只有一份知识库，不会先问你「这问题该去哪套语料找」。

为什么合并必须「先并目录、再灌一次」，而不是分 5 次灌同一个库：

  `PgStore.replace()` 是「按 source 删旧的、再插新的」，不是清表 ——
  所以分 5 次灌不会留下重复行，这部分本来就是支持的
  （它 docstring 里写着 "lets two corpora share one table"）。

  真正会坏掉的是**词权重**：离线嵌入的 IDF 落在 `<--store 的父目录>/idf.json`，
  每次 ingest 都整份覆盖它。

    第 1 次灌 kb_default → idf.json = 第 1 套的权重，向量按这份权重存进库
    第 2 次灌 kb_dba     → idf.json 被换成第 2 套的权重
    ...
    查询时读到的 idf.json = **最后一套**的权重
    → 前 4 套的向量是按别人的权重存的，权重对不上
    → 排序悄悄漂移，**而且不报错**

  并成一个目录、只灌一次，`build_idf` 在并集上算一次，所有向量共用同一份权重，
  这个错配就不存在了。所以本脚本先做合并（顺带断言全局 0 处文件名重名），
  再灌一次。

  （另一条干净的路是 `--mode api`：真实嵌入没有 IDF，分几次灌都一致。
  见 docs/pg-corpus.md 第五节。）
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 库名 · 语料目录 · 题库。库名一律带前缀，避免和 fence_demo 抢名字。
CORPORA = [
    ("kb_default",   "examples/knowledge",             "eval/questions.md"),
    ("kb_dba",       "examples/knowledge-dba",         "eval/questions-dba.md"),
    ("kb_hand",      "examples/knowledge-pg",          "eval/questions-pg.md"),
    ("kb_official",  "examples/knowledge-pg-official", "eval/questions-pg-official.md"),
    ("kb_internals", "examples/knowledge-pg-internals","eval/questions-pg-internals.md"),
]

CORPUS_SUFFIXES = {".md", ".txt"}
DEFAULT_MERGE_DIR = "examples/knowledge-merged"
DEFAULT_SINGLE_DB = "kb_schemafence"


def die(msg: str) -> None:
    print(f"error: {msg}")
    raise SystemExit(2)


CONNECT_TIMEOUT = 5  # seconds — libpq's default is "wait forever"


def with_timeout(dsn: str, seconds: int = CONNECT_TIMEOUT) -> str:
    """Add ``?connect_timeout=`` to a DSN.

    A wrong host or a closed port with the default libpq settings hangs on the
    TCP handshake instead of failing.  Five seconds turns "hangs forever" into
    one clear line.  It is carried in the DSN (not just the admin connection)
    so the per-corpus ``agent_cli.py`` subprocesses inherit it too.
    """
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}connect_timeout={seconds}"


def split_base(base: str) -> str:
    """``postgresql://u:p@host:5432`` — no database name.

    Accept either form: with a trailing ``/db`` it is stripped, because the
    per-corpus DSNs are built by appending the database name.
    """
    if "://" not in base:
        die("--base must look like postgresql://user:password@host:port")
    scheme, rest = base.split("://", 1)
    head, _, tail = rest.rpartition("/")
    # rpartition splits on the last slash.  "…:5432/fence_demo" means the tail is
    # a database name and must go; "…:5432" leaves head empty, so nothing to do.
    if head and tail and "@" not in tail and ":" not in tail:
        rest = head
    return f"{scheme}://{rest.rstrip('/')}"


def assemble_merged(selected, merge_dir: Path, reuse: bool) -> tuple[int, int]:
    """Copy every .md / .txt of ``selected`` into one tree; prove stems are unique.

    ``chunk_markdown(source=path.stem)`` is what the retrieval layer keys on, so
    two files sharing a stem would silently fuse into one source — the duplicate
    would be overwritten in the table and the question banks would point at the
    wrong text.  Nothing about that is loud at query time, so it is checked here.

    The source layout is mirrored under ``merge_dir/<corpus name>/`` so the
    provenance of every file stays visible on disk even though the chunker
    throws the directory away.
    """
    if merge_dir.exists():
        if reuse:
            files = [p for p in merge_dir.rglob("*") if p.is_file()]
            kept = sum(1 for p in files if p.suffix.lower() in CORPUS_SUFFIXES)
            print(f"[reuse]  {merge_dir}  ({kept} files already there)")
            return kept, len({p.stem for p in files})
        shutil.rmtree(merge_dir)

    stems: dict[str, str] = {}
    duplicates: list[tuple[str, str, str]] = []
    per_pack: list[tuple[str, int]] = []
    total = 0

    for _db, corpus, _eval_file in selected:
        src = ROOT / corpus
        label = src.name
        count = 0
        for path in sorted(src.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in CORPUS_SUFFIXES:
                continue
            rel = path.relative_to(src)
            dst = merge_dir / label / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dst)
            total += 1
            count += 1
            where = f"{label}/{rel.as_posix()}"
            seen = stems.setdefault(path.stem, where)
            if seen != where:
                duplicates.append((path.stem, seen, where))
        per_pack.append((label, count))

    print(f"[merge]  {len(per_pack)} 套语料 → {merge_dir}")
    for label, count in per_pack:
        print(f"           {label:32s} {count:>4} 篇")
    print(f"           {'合计':32s} {total:>4} 篇 / {len(stems)} 个唯一文件名")

    if duplicates:
        print()
        print(f"error: {len(duplicates)} 处文件名重名 —— 合并会把两篇文档压成一个 source：")
        for stem, a, b in duplicates[:10]:
            print(f"         {stem}")
            print(f"           {a}")
            print(f"           {b}")
        more = "" if len(duplicates) <= 10 else f"（另有 {len(duplicates) - 10} 处）"
        die(f"aborting: 先给重名的文件改名再合并 {more}")
    return total, len(stems)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True,
                    help="DSN without the database name, e.g. postgresql://postgres:pw@localhost:5432")
    ap.add_argument("--admin-db", default="postgres",
                    help="database used to issue CREATE DATABASE (default: postgres)")
    ap.add_argument("--only", action="append", default=[],
                    help="ingest only these database names (repeatable)")
    ap.add_argument("--single-db", action="store_true",
                    help="merge every corpus into ONE database (default db name: %s)" % DEFAULT_SINGLE_DB)
    ap.add_argument("--db-name", default=None,
                    help=f"database name for --single-db (default: {DEFAULT_SINGLE_DB})")
    ap.add_argument("--merge-dir", default=DEFAULT_MERGE_DIR,
                    help=f"where the merged corpus is assembled (default: {DEFAULT_MERGE_DIR})")
    ap.add_argument("--reuse-merged", action="store_true",
                    help="keep an existing --merge-dir instead of rebuilding it")
    ap.add_argument("--mode", default=None, choices=["auto", "offline", "api"],
                    help="passed through to agent_cli.py; default is its own default (auto)")
    ap.add_argument("--rebuild", action="store_true",
                    help="TRUNCATE doc_chunks before the ingest")
    ap.add_argument("--chunk", type=int, default=None, help="override --chunk")
    ap.add_argument("--overlap", type=int, default=None, help="override --overlap")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    args = ap.parse_args()

    base = split_base(args.base)
    admin_dsn = with_timeout(f"{base}/{args.admin_db}")
    selected = [(db, c, e) for db, c, e in CORPORA if not args.only or db in args.only]
    if not selected:
        die(f"--only matched nothing; known names: {', '.join(d for d, _, _ in CORPORA)}")

    missing = [c for _, c, _ in selected if not (ROOT / c).exists()]
    if missing:
        for c in missing:
            print(f"  skipped  {c}  (not on disk — generated corpora are gitignored, "
                  f"re-run the converter first)")
        selected = [(d, c, e) for d, c, e in selected if (ROOT / c).exists()]
    if not selected:
        die("nothing to ingest")

    merge_dir = ROOT / args.merge_dir
    db_name = args.db_name or DEFAULT_SINGLE_DB

    print(f"base      : {base}")
    print(f"admin db  : {args.admin_db}")
    print(f"timeout   : {CONNECT_TIMEOUT}s per connection")
    print(f"shape     : {'SINGLE DATABASE — 全部语料合并' if args.single_db else 'one database per corpus'}")
    print(f"corpora   : {len(selected)}")
    for db, corpus, _ in selected:
        print(f"  {db:14s} ← {corpus}")
    if args.single_db:
        print(f"target db : {db_name}")
        print(f"merge dir : {args.merge_dir}")
    print()

    if args.dry_run:
        if args.single_db:
            files = sum(1 for _d, c, _e in selected
                        for p in (ROOT / c).rglob("*")
                        if p.is_file() and p.suffix.lower() in CORPUS_SUFFIXES)
            print(f"would merge {files} files into {args.merge_dir} and ingest once "
                  f"into {db_name}")
        print("dry run — nothing was executed")
        return 0

    # ---------------------------------------------------------------- merge --
    # Before the psycopg check on purpose: assembling the merged tree touches no
    # database, so it works on a machine that has no psycopg and no PostgreSQL.
    if args.single_db:
        assemble_merged(selected, merge_dir, args.reuse_merged)
        print()

    try:
        import psycopg
    except ImportError:
        die("psycopg is not installed — run:  "
            f"{sys.executable} -m pip install -r requirements.txt")

    # --------------------------------------------------------------- create --
    plan = [(db_name, args.merge_dir)] if args.single_db else [(d, c) for d, c, _ in selected]
    print(f"[admin] connecting to {args.admin_db}")
    created: list[str] = []
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            for db, _corpus in plan:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
                    if cur.fetchone() is None:
                        # Identifiers cannot be parameterised; the name comes from
                        # the table above or --db-name, never from user data.
                        cur.execute(f'CREATE DATABASE "{db}"')
                        created.append(db)
                        print(f"[create] {db}")
                    else:
                        print(f"[skip]   {db} already exists")
    except Exception as exc:  # noqa: BLE001
        die(f"cannot reach the database: {exc}\n"
            "       is PostgreSQL running on that host/port, and are the credentials right?")

    # --------------------------------------------------------------- ingest --
    # (db, corpus, eval_file, store, dsn)
    jobs: list[tuple[str, str, str, Path, str]] = []
    if args.single_db:
        jobs.append((db_name, args.merge_dir, "", 
                     ROOT / ".schemafence" / "pgv" / db_name / "store.json",
                     with_timeout(f"{base}/{db_name}")))
    else:
        for db, corpus, eval_file in selected:
            jobs.append((db, corpus, eval_file,
                         ROOT / ".schemafence" / "pgv" / db / "store.json",
                         with_timeout(f"{base}/{db}")))

    ingested: list[tuple[str, str, str, Path, str]] = []
    for db, corpus, eval_file, store, dsn in jobs:
        store.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(ROOT / "agent_cli.py"),
               "--db", dsn,
               "--store", str(store),
               "--ingest", str(ROOT / corpus)]
        if args.mode:
            cmd += ["--mode", args.mode]
        if args.chunk:
            cmd += ["--chunk", str(args.chunk)]
        if args.overlap:
            cmd += ["--overlap", str(args.overlap)]
        if args.rebuild:
            cmd.append("--rebuild")
        print()
        print("=" * 72)
        print(f"▶ {db}  ←  {corpus}{'  (+ 其余全部语料)' if args.single_db else ''}")
        print("=" * 72)
        rc = subprocess.call(cmd, cwd=str(ROOT))
        if rc != 0:
            print(f"\nerror: ingest of {db} exited {rc} — stopping")
            return rc
        ingested.append((db, corpus, eval_file, store, dsn))

    if created:
        print(f"\n新建 {len(created)} 个库：{', '.join(created)}")

    # ------------------------------------------------------------ self-test --
    print()
    print("=" * 72)
    if args.single_db:
        db, corpus, _e, store, dsn = ingested[0]
        print(f"done. 全部语料已在 {db} 一个库里，下面按用途分三类自检")
        print("=" * 72)
        print()
        print("# ① 看规模（几篇文档、几个切片）")
        print(f"python agent_cli.py --db {dsn} --store {store} --corpus {corpus}")
        print()
        print("# ② 逐个题库量它的命中率 —— 五套题库都指向同一个库，")
        print("#    这正是「合并之后哪些问题变难了」的直接量尺")
        print("#    注意 --k 20：合并库有 3000+ 片，正确篇章平均下沉 2–3 名，")
        print("#    默认的 --k 5 会低估这个库（实测 74% vs 90%，见 docs/pg-corpus.md）")
        for db_i, _corpus_i, eval_i in CORPORA:
            print(f"python agent_cli.py --db {dsn} --store {store} \\")
            print(f"                    --corpus {corpus} --eval --eval-file {eval_i} --k 20"
                  f"    # {db_i}")
        print()
        print("# ③ 现场诊断：直接提问（live 模式下还会去读真实库的 schema）")
        print("#    —— 注意 --db 同时也是「被诊断的库」：想让它诊断 fence_demo 那 7 张表，")
        print("#       就 --single-db --db-name fence_demo，让语料和目标库是同一个。")
        print(f"python agent_cli.py --db {dsn} --store {store} \\")
        print(f'                    --ask "主从复制延迟高，先看哪些指标？" --k 20')
    else:
        print(f"done. {len(ingested)} 套语料都灌完了，下面是各自的自检命令")
        print("=" * 72)
        for db, corpus, eval_file, store, dsn in ingested:
            print()
            print(f"# {db}  ({corpus})")
            print(f"  统计  : python agent_cli.py --db {dsn} --store {store} --corpus {corpus}")
            print(f"  评测  : python agent_cli.py --db {dsn} --store {store} "
                  f"--corpus {corpus} --eval --eval-file {eval_file}")
            print(f"  提问  : python agent_cli.py --db {dsn} --store {store} "
                  f"--corpus {corpus} --ask \"你的问题\"")

    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT datname, pg_size_pretty(pg_database_size(datname))"
                            "  FROM pg_database WHERE datname = ANY(%s) ORDER BY datname",
                            ([db for db, _, _, _, _ in ingested],))
                rows = cur.fetchall()
        if rows:
            print()
            print("各库体量：")
            for name, size in rows:
                print(f"  {name:16s} {size}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(note: could not read the per-database sizes: {exc})")

    print()
    print("两个提醒：")
    print("  1. --store 必须和灌库时用同一个路径 —— 离线模式下查询要读同一个 idf.json，")
    print("     路径变了，排序就会和灌库时不一致。")
    print("  2. 灌进去 ≠ 存住了。容器化的数据库（比如 Cloud Studio 工作空间）重启后可能丢，")
    print("     要长期留就得把 PGDATA 放到持久化卷上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
