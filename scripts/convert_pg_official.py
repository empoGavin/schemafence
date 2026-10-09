#!/usr/bin/env python3
"""Re-paginate the PostgreSQL 16 official documentation into a markdown corpus.

Source
------
``postgresql-16-A4.pdf`` — *PostgreSQL 16.15 Documentation*, The PostgreSQL
Global Development Group.  Distributed under the PostgreSQL License, which
permits copying and redistribution.  This script adds no commentary: every
word it emits comes from the manual.  It only splits the book into one file
per Chapter and normalises the markup so the chunker can read it.

Why a script rather than a checked-in corpus
--------------------------------------------
The generated tree is ~5 MB of text and thousands of chunks.  Committing it
would bloat the repository and freeze a derivative of someone else's document
at a point in time.  The generator is checked in; the output is gitignored —
the same rule the scale corpus follows.

Three things this has to get right
----------------------------------
1. **A line starting with ``#`` is not a heading.**  ``chunk_markdown`` treats
   ``^(#{1,6})\\s+`` as a section boundary, and the manual is full of shell
   transcripts and SQL comments that start with ``#``.  Unescaped, they shred
   the chapter into hundreds of one-line chunks.  Every such line is escaped
   as ``\\#`` here.
2. **The running header and folio are plumbing, not evidence.**  Each page
   opens with the document title and closes with a bare page number; both are
   dropped, otherwise they become the highest-frequency tokens in the corpus
   and dominate IDF.
3. **Sub-section headings must survive as headings.**  The manual's own
   ``19.3. Starting the Database Server`` lines come out of the PDF as plain
   text.  Left alone they are invisible to the chunker, so a question phrased
   the way the manual titles that section retrieves nothing.  They are lifted
   back to ``##`` / ``###`` using the PDF outline, which knows both the
   heading text and the page it lives on.

Usage
-----
    python scripts/convert_pg_official.py --pdf postgresql-16-A4.pdf \
                                          --out examples/knowledge-pg-official
    python scripts/convert_pg_official.py --pdf ... --dry-run   # stats only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

HEADER_LINE = "PostgreSQL 16.15 Documentation"

# Whole top-level sections that are not corpus material: the cover, the
# generated table of contents, and the two back-matter indexes.  Their text is
# either duplicate or an alphabetical key with no prose in it.
SKIP_TOP_LEVEL = {
    HEADER_LINE,
    "Table of Contents",
    "Bibliography",
    "Index",
}


def norm(text: str) -> str:
    """Fold a string for comparison: NFKC, collapse space, unify quotes."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def plain(text: str) -> str:
    """Collapse the manual's non-breaking spaces for display.

    DocBook emits ``Part\u00a0III.\u00a0Server Administration``; the NBSP is
    invisible in a terminal but breaks every string comparison against a
    hand-written label.
    """
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def slug(text: str, maxlen: int = 64) -> str:
    """ASCII file-name slug; non-Latin characters are dropped."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "section"


def top_level_spans(toc: list[tuple[int, str, int]], n_pages: int):
    """Page ranges covered by each level-1 entry, and the set to skip."""
    l1 = [(title, page) for level, title, page in toc if level == 1]
    spans, skipped = [], []
    for i, (title, page) in enumerate(l1):
        end = l1[i + 1][1] - 1 if i + 1 < len(l1) else n_pages
        spans.append((title, page, end))
        if title in SKIP_TOP_LEVEL:
            skipped.append((page, end))
    return spans, skipped


def in_any(page: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= page <= hi for lo, hi in ranges)


def chapter_items(toc, n_pages, skipped):
    """(title, first_page, last_page, part_title) for every Chapter to emit.

    Chapters are grouped under their Part so the output can be ingested a Part
    at a time: ``rglob`` means ingesting the top directory still picks up
    everything, while ingesting one Part directory searches only that Part.
    """
    bounds: list[tuple[int, int, str]] = []
    for level, title, page in toc:
        if level == 1 and title not in SKIP_TOP_LEVEL:
            bounds.append((page, 0, title))       # 0 sorts before a chapter
        elif level == 2:
            bounds.append((page, 1, title))
    bounds.sort(key=lambda b: (b[0], b[1]))

    items, part = [], "front-matter"
    for i, (page, kind, title) in enumerate(bounds):
        if kind == 0:                              # a Part divider: no file
            part = title
            continue
        end = bounds[i + 1][0] - 1 if i + 1 < len(bounds) else n_pages
        if end < page or in_any(page, skipped):
            continue
        items.append((title, page, end, part))
    return items


def build_headings_index(toc, min_level: int = 3):
    """page -> [(level, title)] for sub-section headings on that page."""
    per_page: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in toc:
        if level >= min_level:
            per_page.setdefault(page, []).append((level, title))
    return per_page


def render_pages(doc, per_page_headings, first: int, last: int) -> str:
    """Text of pages [first, last] (1-based), header/folio stripped and
    sub-headings promoted."""
    out: list[str] = []
    for pageno in range(first, last + 1):
        if pageno < 1 or pageno > doc.page_count:
            continue
        raw = doc[pageno - 1].get_text("text")
        lines = raw.splitlines()
        headings = per_page_headings.get(pageno, [])
        used: set[int] = set()
        kept = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]
        for pos, (idx, line) in enumerate(kept):
            text = line.strip()
            if text == HEADER_LINE:
                continue
            # A bare number in the last few lines of the page is the folio.
            if text.isdigit() and pos >= len(kept) - 3:
                continue
            key = norm(text)
            matched = None
            for j, (level, title) in enumerate(headings):
                if j in used:
                    continue
                if norm(title) == key:
                    matched = (j, level, title)
                    break
            if matched is not None:
                j, level, title = matched
                used.add(j)
                depth = 2 if level <= 3 else min(level - 1, 4)
                out.append("#" * depth + " " + re.sub(r"\s+", " ", title).strip())
                continue
            if text.startswith("#"):
                line = "\\" + line.lstrip()
            out.append(line.rstrip())
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="path to the PostgreSQL manual PDF")
    ap.add_argument("--out", default="examples/knowledge-pg-official",
                    help="output corpus directory")
    ap.add_argument("--title", default="PostgreSQL 16.15 Documentation")
    ap.add_argument("--subsection-level", type=int, default=3,
                    help="outline level at which sub-headings are promoted (default 3)")
    ap.add_argument("--min-chars", type=int, default=400,
                    help="skip chapters whose extracted text is shorter than this")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)

    try:
        import pymupdf
    except ImportError:                            # pragma: no cover
        try:
            import fitz as pymupdf
        except ImportError:
            print("convert_pg_official: needs pymupdf  (pip install pymupdf)",
                  file=sys.stderr)
            return 2

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"convert_pg_official: no such PDF: {pdf_path}", file=sys.stderr)
        return 2

    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc(simple=True)
    if not toc:
        print("convert_pg_official: the PDF has no outline — cannot split it "
              "by chapter", file=sys.stderr)
        return 2

    n_pages = doc.page_count
    spans, skipped = top_level_spans(toc, n_pages)
    items = chapter_items(toc, n_pages, skipped)
    per_page_headings = build_headings_index(toc, args.subsection_level)

    print(f"{args.title}")
    print(f"  pages          : {n_pages}")
    print(f"  outline        : {len(toc)} entries, {len(spans)} top-level")
    print(f"  skipped ranges : {skipped}")
    print(f"  chapters       : {len(items)}")

    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest, seen, total_chars, total_kept, written = [], {}, 0, 0, 0
    for i, (title, first, last, part) in enumerate(items, 1):
        body = render_pages(doc, per_page_headings, first, last)
        total_chars += len(body)
        if len(body) < args.min_chars:
            print(f"  [skip] {title!r} p{first}-{last}: only {len(body)} chars")
            continue
        name = f"pg16-{slug(title)}"
        if name in seen:
            seen[name] += 1
            name = f"{name}-{seen[name]}"
        else:
            seen[name] = 1
        filename = name + ".md"
        page_label = f"p{first}" if first == last else f"p{first}-{last}"
        header = (f"> Source: {args.title}, {plain(title)} ({page_label}). "
                  f"The PostgreSQL Global Development Group, PostgreSQL License.\n"
                  f"> Repaginated from the official manual by "
                  f"`scripts/convert_pg_official.py`; no editorial content added.\n\n")
        text = header + f"# {title}\n\n" + body + "\n"
        if not args.dry_run:
            part_dir = out_dir / slug(part, 48)
            part_dir.mkdir(parents=True, exist_ok=True)
            (part_dir / filename).write_text(text, encoding="utf-8", newline="\n")
        written += 1
        total_kept += len(body)
        manifest.append({
            "file": f"{slug(part, 48)}/{filename}", "title": plain(title),
            "part": plain(part), "pages": [first, last], "chars": len(body),
        })

    if not args.dry_run and manifest:
        (out_dir / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n")

    print(f"\n  written        : {written} files, {total_kept:,} chars "
          f"({total_kept / max(n_pages, 1):.0f} chars/page)")
    print(f"  dropped        : {total_chars - total_kept:,} chars in "
          f"{len(items) - written} stub chapters")
    if args.dry_run:
        print("  (dry run — nothing written)")
    else:
        print(f"  output         : {out_dir}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
