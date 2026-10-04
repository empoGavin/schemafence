#!/usr/bin/env bash
# Convert existing notes (Word / HTML / PDF / ODT) into what --ingest reads.
#
#   bash scripts/convert_notes.sh ~/old-notes data/docs
#
# Why this exists: --ingest reads .md / .txt on purpose — zero third-party
# dependencies is what makes "clone and run" true, and a PDF parser in the
# core would end that.  Your 20-year archive does not have to be rewritten
# though: pandoc and pdftotext do the conversion, one directory at a time.
#
# What you get:
#   .docx .html .odt  →  same name .md     (via pandoc)
#   .pdf              →  same name .txt    (via pdftotext)
#
# What you must still do by hand: read the output.  Conversion preserves
# words, not structure — Word headings often arrive as bold paragraphs, and
# a bold paragraph is not a markdown heading.  The chunker navigates by
# headings, so ten minutes of fixing headings in a converted file buys more
# retrieval quality than any parameter you can tune.

set -euo pipefail

SRC="${1:?usage: convert_notes.sh <source-dir> <dest-dir>}"
DST="${2:?usage: convert_notes.sh <source-dir> <dest-dir>}"

need() { command -v "$1" >/dev/null 2>&1 || {
  echo "[FAIL] missing tool: $1"
  echo "       install it first:  sudo dnf install ${2}"
  exit 1; }; }
command -v pandoc     >/dev/null 2>&1 && HAS_PANDOC=1 || HAS_PANDOC=0
command -v pdftotext  >/dev/null 2>&1 && HAS_PDF=1    || HAS_PDF=0

mkdir -p "${DST}"
md_n=0; txt_n=0; skip_n=0; fail_n=0

shopt -s nullglob nocaseglob
for f in "${SRC}"/*.docx "${SRC}"/*.html "${SRC}"/*.odt; do
  out="${DST}/$(basename "${f%.*}").md"
  if [ -e "${out}" ]; then skip_n=$((skip_n + 1)); continue; fi
  if [ "${HAS_PANDOC}" -eq 0 ]; then
    echo "[FAIL] pandoc not installed — cannot convert $(basename "${f}")"
    echo "       sudo dnf install pandoc"
    exit 1
  fi
  if pandoc -f auto -t gfm --wrap=none "${f}" -o "${out}" 2>/dev/null; then
    echo "  docx/html/odt → md : $(basename "${f}") → $(basename "${out}")"
    md_n=$((md_n + 1))
  else
    echo "  [FAIL] $(basename "${f}") — pandoc could not read it"; fail_n=$((fail_n + 1))
  fi
done

for f in "${SRC}"/*.pdf; do
  out="${DST}/$(basename "${f%.*}").txt"
  if [ -e "${out}" ]; then skip_n=$((skip_n + 1)); continue; fi
  if [ "${HAS_PDF}" -eq 0 ]; then
    echo "[FAIL] pdftotext not installed — cannot convert $(basename "${f}")"
    echo "       sudo dnf install poppler-utils"
    exit 1
  fi
  if pdftotext -layout "${f}" "${out}" 2>/dev/null; then
    echo "  pdf → txt          : $(basename "${f}") → $(basename "${out}")"
    txt_n=$((txt_n + 1))
  else
    echo "  [FAIL] $(basename "${f}") — pdftotext could not read it"; fail_n=$((fail_n + 1))
  fi
done
shopt -u nullglob nocaseglob

echo
echo "  converted: ${md_n} → md, ${txt_n} → txt   skipped (already there): ${skip_n}   failed: ${fail_n}"
echo
echo "  next two steps, in this order:"
echo "    1. fix the headings:   converted files need '# 主题' / '## 小节' lines —"
echo "       the chunker navigates by headings, bold paragraphs are invisible to it"
echo "    2. ingest:             python agent_cli.py --ingest ${DST}"
