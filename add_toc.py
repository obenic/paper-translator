#!/usr/bin/env python
"""Insert or refresh a table of contents in a translated Markdown paper.

A 24-page translation opened in a plain editor has no way to jump between
sections. This writes a real "目录" list, delimited by HTML comments so it
can be refreshed in place instead of accumulating copies:

    <!-- TOC -->
    ## 目录
    - [摘要](#摘要)
    - [1. 引言](#1-引言)
      - [1.1 背景](#11-背景)
    <!-- /TOC -->

Anchors follow github-slugger (what GitHub, VS Code preview and Obsidian
use): lowercase, drop punctuation and symbols, spaces become hyphens, CJK
survives untouched, duplicates get -1, -2. Headings are read from the file,
never invented.

Run this AFTER insert_figures.py: that step moves figure blocks and deletes
the emptied "## 图" section, which would leave a stale TOC behind.

Figure and table headings ("### 图 2 | ...") are skipped by default - a paper
with a dozen figures would otherwise bury its own sections under a list of
caption titles. --include-figures keeps them.

The PDF gets its own TOC from pandoc, so md_to_pdf.py strips this block
before rendering; the two never both appear.

Usage:
    python add_toc.py <译文.md> [--depth 3] [--dry-run] [-o out.md]
    python add_toc.py <译文.md> --remove

Exit codes:
    0  TOC written (or already current, or removed)
    1  error
    3  no section headings found - check the translation before continuing
"""

import argparse
import re
import sys
import unicodedata
from pathlib import Path

BEGIN = "<!-- TOC -->"
END = "<!-- /TOC -->"
TOC_HEADING = "## 目录"

FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Figure/table headings: the figure number is what identifies them, so a
# translated caption title after the number does not matter.
FIGURE_LIKE = re.compile(r"^(图|表|figure|table|fig\.?|附图|补充图)\s*\d", re.I)


def slug(text: str, seen: dict) -> str:
    """github-slugger's rule, enough of it for paper headings.

    Strips inline Markdown, lowercases, drops every Unicode punctuation and
    symbol except - and _, turns whitespace runs into single hyphens, and
    disambiguates repeats with a numeric suffix.
    """
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)       # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)      # links -> label
    s = re.sub(r"[*_`~]+", "", s)                        # emphasis / code
    s = s.lower().strip()
    kept = []
    for ch in s:
        if ch in "-_" or ch.isspace():
            kept.append(ch)
            continue
        if unicodedata.category(ch)[0] in ("P", "S"):    # punctuation, symbols
            continue
        kept.append(ch)
    s = re.sub(r"\s+", "-", "".join(kept)).strip("-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else f"{s}-{n}"


def strip_block(lines: list) -> list:
    """Drop a previously written TOC block, if there is one.

    Also eats the single blank line that insert_at() puts after the block, so
    strip + insert is a round trip: running this script twice must produce a
    byte-identical file, not one that grows a blank line each time.
    """
    try:
        i = lines.index(BEGIN)
        j = lines.index(END, i)
    except ValueError:
        return list(lines)
    tail = lines[j + 1:]
    if tail and not tail[0].strip():
        tail = tail[1:]
    return lines[:i] + tail


def headings(lines: list, depth: int, include_figures: bool) -> list:
    """(level, text, slug) for every heading that belongs in the TOC.

    Slugs are computed over ALL headings, including the ones left out of the
    TOC, because a duplicate that is skipped here still consumes a suffix in
    the viewer's own numbering.
    """
    seen, out, in_fence = {}, [], False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ATX.match(line)
        if not m:
            continue
        level, text = len(m.group(1)), m.group(2).strip()
        anchor = slug(text, seen)                        # always, see docstring
        if level < 2 or level > depth:                   # H1 is the paper title
            continue
        if text == TOC_HEADING.lstrip("# ").strip():
            continue
        if not include_figures and FIGURE_LIKE.match(text):
            continue
        out.append((level, text, anchor))
    return out


def render(items: list) -> list:
    """The TOC block, as lines."""
    top = min(lv for lv, _, _ in items)
    body = [f"{'  ' * (lv - top)}- [{text}](#{anchor})"
            for lv, text, anchor in items]
    return [BEGIN, TOC_HEADING, ""] + body + ["", END]


def insert_at(lines: list, block: list) -> list:
    """Put the block just before the first section heading.

    That lands it after the title and the author line, where a reader looks
    for it, and it does not depend on how many lines of front matter the
    translation happens to have.
    """
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ATX.match(line)
        if m and 2 <= len(m.group(1)) <= 6:
            return lines[:i] + block + [""] + lines[i:]
    return lines + [""] + block


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Insert or refresh a 目录 block in a translated paper.")
    ap.add_argument("md")
    ap.add_argument("-o", "--out", help="write here instead of in place")
    ap.add_argument("--depth", type=int, default=3, metavar="N",
                    help="deepest heading level to list (default 3)")
    ap.add_argument("--include-figures", action="store_true",
                    help="also list 图 / 表 headings")
    ap.add_argument("--remove", action="store_true",
                    help="delete the TOC block and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the result, write nothing")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        pass

    src = Path(args.md)
    if not src.exists():
        print(f"ERROR: not found: {src}", file=sys.stderr)
        return 1
    if args.depth < 2:
        print("ERROR: --depth must be 2 or more (H1 is the paper title)",
              file=sys.stderr)
        return 1

    raw = src.read_text(encoding="utf-8")
    lines = raw.splitlines()
    body = strip_block(lines)
    had = BEGIN in lines

    if args.remove:
        if not had:
            print("no TOC block found; nothing to remove")
            return 0
        return write(src, args, body, "TOC removed")

    items = headings(body, args.depth, args.include_figures)
    if not items:
        print(f"ERROR: no headings between H2 and H{args.depth} found in "
              f"{src.name}.\nA translated paper without ## sections means "
              f"something went wrong upstream - check the file.",
              file=sys.stderr)
        return 3

    out_lines = insert_at(body, render(items))
    for lv, text, anchor in items:
        print(f"  {'  ' * (lv - 2)}H{lv}  {text}  ->  #{anchor}")
    verb = "TOC refreshed" if had else "TOC inserted"
    return write(src, args, out_lines, f"{verb}: {len(items)} entries")


def write(src: Path, args, lines: list, summary: str) -> int:
    text = "\n".join(lines).rstrip("\n") + "\n"
    if args.dry_run:
        print(f"\n--- dry run, nothing written ({summary}) ---")
        return 0
    dst = Path(args.out) if args.out else src
    if dst == src:
        _put(src.with_suffix(src.suffix + ".bak"),
             src.read_text(encoding="utf-8"))
    _put(dst, text)
    print(f"\n{summary}\nwritten: {dst}")
    return 0


def _put(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings, the way insert_figures.py does.

    Pinning newline is the point: Path.write_text would translate every \\n to
    \\r\\n on Windows, so a file that arrived LF would come back CRLF and the
    whole translation would read as changed.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    sys.exit(main())
