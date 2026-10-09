#!/usr/bin/env python3
"""Convert "PostgreSQL 14 Internals" (Parts I & II, Egor Rogov) into a markdown corpus.

Source
------
``postgresql_internals-14_parts1-2_en.pdf`` — *PostgreSQL 14 Internals*,
Parts I & II, Egor Rogov, translated by Liudmila Mantrova, Postgres
Professional, Moscow 2022.  The book is distributed freely by the publisher
(postgrespro.com/community/books/internals) but it is **not** under an open
licence: the generated tree is for personal local retrieval only and stays
out of the repository (gitignored), same rule as the official-manual corpus.

This script adds no commentary: every word it emits comes from the book.
It splits the book into one file per chapter, grouped under Part directories,
and normalises the markup so ``chunk_markdown`` can read it.

The one problem nobody warns you about
--------------------------------------
The PDF's ``ToUnicode`` CMaps are broken for a subset of glyphs, and the
damage is **per font**: the same code point decodes to different letters
depending on which embedded font carries it.  Every glyph in a contiguous
code-point block is shifted by a constant, so the fix is a per-font,
per-block offset table — ``S`` in PT Serif is ``U+052F`` minus ``0x4DC``,
but ``INSERT`` in PT Sans spells ``U+0526..`` minus ``0x4DD``, and *virtual
xid*'s italic ``XID`` minus ``0x4E1``.  Every offset below was verified
against rendered page crops (``JPEG images``, ``PGDATA``, ``VACUUM``,
``ANALYZE``, ``NOT NULL``, ``version 10``, ``64-bit``, ``9.4``, ``1976``,
``$800``, the ``p. 148`` margin notes, the 2022 title page), not guessed:

======================  =========================  ======  =============
Font                    Code-point block           Offset  Decodes to
======================  =========================  ======  =============
PTSerifPro-Regular      U+051D..U+0536             0x4DC   A..Z
PTSerifPro-Regular      U+047F..U+0488             0x44F   0..9
PTSansPro-Regular       U+051E..U+0537             0x4DD   A..Z
PTSansPro-Regular       U+0480..U+0489             0x450   0..9
PTSerifPro-Italic       U+0525/U+052A/U+0539       0x4E1   D/I/X
PTSerifPro-Italic       U+0480..U+0489             0x450   0..9
======================  =========================  ======  =============

Anything the table misses would survive as a Cyrillic-block character; the
generator counts those and refuses to stay quiet about them.

What else has to go right
-------------------------
1. **Geometry beats content.**  The running header (``y < 50``), the folio
   (bottom band or printed in the outer margin), the margin cross-references
   (``p. 148`` — meaningless without the rest of the book) and the big
   chapter numeral on opening pages are dropped by position and font, not by
   string matching.  The chapter title on an opening page is typeset in
   PTSans-Caption and duplicates the ``# `` heading the file already has.
2. **Headings must be re-promoted, and disambiguated by font.**  Body text
   uses the words "Read Committed" constantly, and page 44 even prints them
   in a table — but *section headings* are the only lines set in the bold
   sans.  Only bold lines are matched against the PDF outline (which knows
   each heading's page), so table rows and prose can never be promoted.
3. **Hyphenation is merged.**  The book breaks words at line ends
   (``ver-`` / ``sions``); left alone, both halves become garbage tokens.
   Prose lines ending in ``-`` are joined with the next line when the next
   one starts lowercase.  Code (PT Mono) and table rows are never merged.

Usage
-----
    python scripts/convert_pg_internals.py --pdf postgresql_internals-14_parts1-2_en.pdf
    python scripts/convert_pg_internals.py --pdf ... --dry-run   # stats only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Per-font decode table: (first_cp, last_cp, offset) -> chr(cp - offset).
DECODE: dict[str, list[tuple[int, int, int]]] = {
    "PTSerifPro-Regular": [(0x051D, 0x0536, 0x4DC), (0x047F, 0x0488, 0x44F)],
    "PTSansPro-Regular": [(0x051E, 0x0537, 0x4DD), (0x0480, 0x0489, 0x450)],
    "PTSerifPro-Italic": [(0x0480, 0x0489, 0x450), (0x0525, 0x0525, 0x4E1),
                          (0x052A, 0x052A, 0x4E1), (0x0539, 0x0539, 0x4E1)],
}
CYR_RANGE = (0x0400, 0x053F)          # where the broken CMaps dump their glyphs

# Characters that extract correctly but tokenize badly in a Latin corpus.
CHAR_MAP = {
    "\u00a0": " ", "\u2212": "-", "\u2013": "-", "\u2014": "-",
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u2a7d": "<=", "\u2a7e": ">=",
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
    "\ufb04": "ffl",
}

# Layout bands (the book is a fixed 467.7 x 666.1 pt trim).
HEADER_Y = 50.0        # running header sits at y0 ~ 31-41
FOLIO_Y = 600.0        # bottom folio sits at y0 ~ 616
MARGIN_X = 45.0        # margin notes / margin folio start left of the text column

BOLD_FONTS = {"PTSansPro-Bold", "PTSerifPro-Bold"}
DROP_FONTS = {"DveKruglyh", "PTSans-Caption"}   # chapter numeral, chapter title

SKIP_CHAPTERS = {"Index"}              # back-matter: an alphabetical key, no prose

NUMBER_PREFIX = re.compile(r"^\d+(?:\.\d+)*\.?\s+")


def norm(text: str) -> str:
    """Fold a string for comparison: NFKC, collapse space, unify quotes."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def plain(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def slug(text: str, maxlen: int = 64) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen].strip("-") or "section"


@dataclass
class Line:
    x0: float
    y0: float
    y1: float
    text: str
    fonts: frozenset[str]


class Decoder:
    """Applies the per-font offset table and counts what it could not map."""

    def __init__(self) -> None:
        self.unmapped: dict[tuple[str, int], int] = {}

    def text(self, raw: str, font: str) -> str:
        out = []
        for ch in raw:
            o = ord(ch)
            mapped = None
            if CYR_RANGE[0] <= o <= CYR_RANGE[1]:
                for lo, hi, off in DECODE.get(font, ()):
                    if lo <= o <= hi:
                        mapped = chr(o - off)
                        break
                if mapped is None:
                    key = (font, o)
                    self.unmapped[key] = self.unmapped.get(key, 0) + 1
                    mapped = "\ufffd"
            out.append(CHAR_MAP.get(ch, mapped if mapped is not None else ch))
        return "".join(out)


def chapter_items(toc, n_pages: int):
    """(title, first_page, last_page, part_title) per level-2 chapter."""
    bounds = []
    for level, title, page in toc:
        if level == 1:
            bounds.append((page, 0, title))        # Part divider: no file
        elif level == 2 and title not in SKIP_CHAPTERS:
            bounds.append((page, 1, title))
    bounds.sort(key=lambda b: (b[0], b[1]))

    items, part = [], "front-matter"
    for i, (page, kind, title) in enumerate(bounds):
        if kind == 0:
            part = title
            continue
        end = bounds[i + 1][0] - 1 if i + 1 < len(bounds) else n_pages
        if end < page:
            continue
        items.append((title, page, end, part))
    return items


def headings_index(toc) -> dict[int, list[tuple[int, str]]]:
    per_page: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in toc:
        if level >= 3:
            per_page.setdefault(page, []).append((level, title))
    return per_page


SPACE_GAP = 1.0        # pt; a swallowed word-space shows up as a char gap
                       # of 1.0-1.5, while real kerning stays below 0.3


def page_lines(doc, pageno: int, decoder: Decoder) -> list[Line]:
    page = doc[pageno - 1]
    lines: list[Line] = []
    for block in page.get_text("rawdict")["blocks"]:
        for l in block.get("lines", []):
            pieces: list[str] = []
            fonts: set[str] = set()
            prev_right = None
            for s in l["spans"]:
                font = s["font"]
                fonts.add(font)
                for c in s["chars"]:
                    ch = decoder.text(c["c"], font)
                    if not ch:
                        continue
                    if prev_right is not None and not ch.isspace():
                        gap = c["bbox"][0] - prev_right
                        if (gap >= SPACE_GAP and pieces
                                and not pieces[-1].endswith(" ")):
                            pieces.append(" ")
                    pieces.append(ch)
                    if not ch.isspace():
                        prev_right = c["bbox"][2]
            text = plain("".join(pieces))
            if text:
                x0, y0, _x1, y1 = l["bbox"]
                lines.append(Line(x0, y0, y1, text, frozenset(fonts)))
    lines.sort(key=lambda ln: (round(ln.y0 * 2) / 2, ln.x0))
    return lines


def keep(line: Line) -> bool:
    if line.y1 < HEADER_Y:                 # running header
        return False
    if line.y0 > FOLIO_Y:                  # bottom folio
        return False
    if line.x0 < MARGIN_X:                 # margin cross-refs and margin folio
        return False
    if line.fonts <= DROP_FONTS:           # chapter numeral / opening title
        return False
    return True


def promote_headings(lines: list[Line], page_headings, used: set[str],
                     stats: dict[str, int]) -> tuple[dict[int, str], set[int]]:
    """Map the first line of each outline-matched bold run to a ``#``-heading.

    A heading may wrap over up to three lines, so up to three consecutive bold
    lines are joined before comparison.  Only bold lines are eligible: body
    prose and table rows use the same words and must never be promoted.
    """
    head_map: dict[int, str] = {}
    consumed: set[int] = set()
    for i, line in enumerate(lines):
        if i in consumed or not line.fonts <= BOLD_FONTS:
            continue
        for level, title in page_headings:
            key = norm(title)
            if key in used:
                continue
            for span in (1, 2, 3):
                end = i + span
                if end > len(lines) or any(j in consumed for j in range(i, end)):
                    break
                window = lines[i:end]
                if not all(w.fonts <= BOLD_FONTS for w in window):
                    break
                joined = " ".join(NUMBER_PREFIX.sub("", w.text) for w in window)
                if norm(joined) == key:
                    depth = 2 if level == 3 else 3
                    head_map[i] = "#" * depth + " " + plain(title)
                    consumed.update(range(i, end))
                    used.add(key)
                    stats["matched"] += 1
                    break
            if i in consumed:
                break
    return head_map, consumed


# Fonts whose lines are flowing prose and may be de-hyphenated; PT Mono (code)
# and PT Sans Narrow (table rows) are never merged.
PROSE_FONTS = {"PTSerifPro-Regular", "PTSerifPro-Italic",
               "PTSansPro-Regular", "PTSansPro-Bold"}


def render_chapter(doc, per_page_headings, first: int, last: int,
                   decoder: Decoder, stats: dict[str, int]) -> str:
    out: list[str] = []
    for pageno in range(first, last + 1):
        if pageno < 1 or pageno > doc.page_count:
            continue
        lines = [ln for ln in page_lines(doc, pageno, decoder) if keep(ln)]
        head_map, consumed = promote_headings(
            lines, per_page_headings.get(pageno, []), set(), stats)
        pieces: list[tuple[str, frozenset[str]]] = []
        for idx, line in enumerate(lines):
            if idx in consumed and idx not in head_map:
                continue
            pieces.append((head_map.get(idx, line.text), line.fonts))
        # De-hyphenate prose: "ver-" + "sions" -> "versions".
        merged: list[tuple[str, frozenset[str]]] = []
        for text, fonts in pieces:
            if (merged
                    and merged[-1][0].endswith("-")
                    and not merged[-1][0].endswith("--")
                    and text[:1].islower()
                    and fonts & PROSE_FONTS
                    and merged[-1][1] & PROSE_FONTS):
                merged[-1] = (merged[-1][0][:-1] + text, merged[-1][1] | fonts)
            else:
                merged.append((text, fonts))
        out.extend(text for text, _ in merged)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="path to the book PDF")
    ap.add_argument("--out", default="examples/knowledge-pg-internals",
                    help="output corpus directory")
    ap.add_argument("--title", default="PostgreSQL 14 Internals, Parts I-II")
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
            print("convert_pg_internals: needs pymupdf  (pip install pymupdf)",
                  file=sys.stderr)
            return 2

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"convert_pg_internals: no such PDF: {pdf_path}", file=sys.stderr)
        return 2

    doc = pymupdf.open(str(pdf_path))
    toc = doc.get_toc(simple=True)
    if not toc:
        print("convert_pg_internals: the PDF has no outline", file=sys.stderr)
        return 2

    n_pages = doc.page_count
    items = chapter_items(toc, n_pages)
    per_page_headings = headings_index(toc)
    total_headings = sum(len(v) for v in per_page_headings.values())

    print(f"{args.title} (Egor Rogov, Postgres Professional)")
    print(f"  pages          : {n_pages}")
    print(f"  outline        : {len(toc)} entries, {len(items)} chapters, "
          f"{total_headings} sub-headings to promote")

    decoder = Decoder()
    stats = {"matched": 0}
    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest, seen, written, total_chars = [], {}, 0, 0
    for i, (title, first, last, part) in enumerate(items, 1):
        body = render_chapter(doc, per_page_headings, first, last,
                              decoder, stats)
        total_chars += len(body)
        if len(body) < args.min_chars:
            print(f"  [skip] {title!r} p{first}-{last}: only {len(body)} chars")
            continue
        name = f"pgi-{slug(title)}"
        if name in seen:
            seen[name] += 1
            name = f"{name}-{seen[name]}"
        else:
            seen[name] = 1
        filename = name + ".md"
        page_label = f"p{first}" if first == last else f"p{first}-{last}"
        header = (f"> Source: PostgreSQL 14 Internals, Parts I-II "
                  f"(Egor Rogov, Postgres Professional, 2022), "
                  f"chapter \"{plain(title)}\" ({page_label}).\n"
                  f"> Freely distributed by the publisher; extracted for "
                  f"personal local retrieval only — not for redistribution.\n"
                  f"> Repaginated by `scripts/convert_pg_internals.py`; "
                  f"no editorial content added.\n\n")
        text = header + f"# {title}\n\n" + body + "\n"
        if not args.dry_run:
            part_dir = out_dir / slug(part, 48)
            part_dir.mkdir(parents=True, exist_ok=True)
            (part_dir / filename).write_text(text, encoding="utf-8", newline="\n")
        written += 1
        manifest.append({
            "file": f"{slug(part, 48)}/{filename}", "title": plain(title),
            "part": plain(part), "pages": [first, last], "chars": len(body),
        })

    if not args.dry_run and manifest:
        (out_dir / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n")

    print(f"\n  headings promoted : {stats['matched']}/{total_headings}")
    print(f"  written           : {written} files, {total_chars:,} chars")
    if decoder.unmapped:
        print(f"  UNMAPPED GLYPHS   : {sum(decoder.unmapped.values())} chars — "
              f"decode table needs extending:", file=sys.stderr)
        for (font, cp), n in sorted(decoder.unmapped.items(),
                                    key=lambda kv: -kv[1])[:20]:
            print(f"      {font} U+{cp:04X} x{n}", file=sys.stderr)
    else:
        print("  unmapped glyphs   : none — every broken CMap char recovered")
    if args.dry_run:
        print("  (dry run — nothing written)")
    else:
        print(f"  output            : {out_dir}")
    doc.close()
    return 1 if decoder.unmapped else 0


if __name__ == "__main__":
    raise SystemExit(main())
