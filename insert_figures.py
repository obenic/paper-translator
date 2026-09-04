#!/usr/bin/env python
"""Move each figure in a translated paper to where the text first mentions it.

A translated paper whose figures are all parked in a "## 图" section at the end
reads badly: the body says "如图 2 所示" on page 4 and the reader has to jump to
page 12 and back. Journals put the figure next to the discussion, and so should
the translation.

This script does that move mechanically, after the translation is written:

    1. find every figure block already in the Markdown (an image line, plus a
       "### 图 N ..." heading above it if there is one)
    2. lift them out
    3. re-insert each one directly after the first body paragraph that mentions
       that figure number

Doing it by hand across a 40-page translation is where figures get dropped or
land under the wrong caption, which is exactly the failure this whole skill
exists to prevent. Doing it as a separate pass also keeps the translation step
simple: write the figures wherever, in one block, and let this sort them.

Figure numbers are read from the caption ("图 2a", "图 2 |") and fall back to
the filename ("fig02_b.png", "Fig2.png"), so panel-per-image and
whole-figure-per-image translations both work.

Usage:
    python insert_figures.py "<译文.md>"                 # rewrite in place (.bak kept)
    python insert_figures.py "<译文.md>" -o out.md       # write elsewhere
    python insert_figures.py "<译文.md>" --dry-run       # just report

Exit codes:
    0  every figure placed at a real mention
    3  some figure had no mention in the text - left at the end, GO LOOK
       also: image lines exist but NONE could be parsed, so nothing moved
    1  error
"""

import argparse
import os
import re
import shutil
import sys

IMAGE_RE = re.compile(r"^!\[(?P<alt>.*)\]\((?P<path>[^)]*)\)\s*$", re.S)
HEADING_RE = re.compile(r"^#{1,6}\s")
# A COMPLETE fenced block as split_chunks hands it over: same fence at both
# ends. Display formulas in this skill's output are written this way. Matching
# only complete blocks matters - a fence containing a blank line arrives as
# several chunks, and carrying past just the opening one would drop the figure
# inside the code block.
FENCE_BLOCK = re.compile(r"^(```|~~~).*\1\s*$", re.S)
# "图 2" / "图2a" / "Fig. 2" / "Figure 2b" - but not "图 20" when looking for 2.
NUM_IN_TEXT = r"(?:图|图表|Fig(?:ure)?\.?)\s*{n}(?![0-9])"
# A supplementary reference is not a mention of the main figure. Missing this
# put 图 2 after the paragraph that says "光致发光发射峰位于 580 nm（补充图 2）".
SUPPLEMENTARY_BEFORE = re.compile(r"(补充|附录|附|扩展数据|Supplementary|Extended\s+Data|SI)\s*$",
                                  re.I)
FIG_IN_CAPTION = re.compile(r"图\s*(\d{1,2})")
FIG_IN_PATH = re.compile(r"fig(?:ure)?[_\-]?(\d{1,2})", re.I)


def split_chunks(text):
    """Blank-line separated chunks, keeping their order."""
    return [c for c in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]


def figure_number(chunk):
    """Which figure does this image chunk belong to? None if it is not one."""
    m = IMAGE_RE.match(chunk.strip())
    if not m:
        return None
    cap = FIG_IN_CAPTION.search(m.group("alt"))
    if cap:
        return int(cap.group(1))
    path = FIG_IN_PATH.search(os.path.basename(m.group("path")))
    return int(path.group(1)) if path else None


def collect_blocks(chunks):
    """Pull figure blocks out. Returns (remaining_chunks, {fig_num: [chunks]}).

    A block is a run of consecutive image chunks for the same figure, plus the
    "### 图 N ..." heading immediately above it. Headings are carried along so
    the section title travels with its figure instead of being orphaned.
    """
    blocks, keep, i = {}, [], 0
    while i < len(chunks):
        num = figure_number(chunks[i])
        if num is None:
            keep.append(chunks[i])
            i += 1
            continue
        run = []
        # Adopt the heading just above, if it names this same figure.
        if keep and HEADING_RE.match(keep[-1].strip()):
            head = keep[-1].strip()
            m = FIG_IN_CAPTION.search(head)
            if m and int(m.group(1)) == num:
                run.append(keep.pop())
        while i < len(chunks) and figure_number(chunks[i]) == num:
            run.append(chunks[i])
            i += 1
        blocks.setdefault(num, []).extend(run)
    return keep, blocks


def drop_empty_figure_section(chunks):
    """Remove a "## 图" heading that no longer has any figure under it."""
    out = []
    for i, chunk in enumerate(chunks):
        stripped = chunk.strip()
        if re.fullmatch(r"#{1,6}\s*图\s*", stripped):
            rest = chunks[i + 1:]
            if not any(figure_number(c) for c in rest):
                continue
        out.append(chunk)
    return out


def mention_index(chunks, num, taken):
    """First body chunk mentioning figure `num`, skipping ones already used."""
    pattern = re.compile(NUM_IN_TEXT.format(n=num))
    for i, chunk in enumerate(chunks):
        stripped = chunk.strip()
        if not stripped or HEADING_RE.match(stripped) or IMAGE_RE.match(stripped):
            continue
        if i in taken:
            continue
        for m in pattern.finditer(stripped):
            if SUPPLEMENTARY_BEFORE.search(stripped[max(0, m.start() - 14):m.start()]):
                continue        # 补充图 2 / Supplementary Fig. 2 is a different figure
            return i
    return None


def carry_past_display_block(chunks, idx):
    """Push an insertion point past a display block that finishes the sentence.

    A paragraph ending in "按下式估计：" plus the fenced formula under it is one
    unit. Dropping a figure between them makes the prose promise an equation
    and then show a plot instead - the figure is in the right place, the
    sentence is not. Real case: Fig 3's first mention is the sentence that
    introduces equation (1).

    Only fenced blocks are carried, and only when the paragraph actually runs
    into them (no blank-line-separated prose in between, which split_chunks
    already guarantees), so a figure never drifts past unrelated content.
    """
    while idx + 1 < len(chunks) and FENCE_BLOCK.match(chunks[idx + 1].strip()):
        idx += 1
    return idx


def relocate(text):
    """Move every figure block to its first mention. Returns (md, report)."""
    chunks, blocks = collect_blocks(split_chunks(text))
    chunks = drop_empty_figure_section(chunks)
    report, taken, placed = [], set(), {}

    for num in sorted(blocks):
        idx = mention_index(chunks, num, taken)
        if idx is None:
            report.append({"figure": num, "placed": False, "after": None})
            continue
        taken.add(idx)
        at = carry_past_display_block(chunks, idx)
        placed.setdefault(at, []).append(num)
        report.append({
            "figure": num,
            "placed": True,
            "after": " ".join(chunks[idx].split())[:60],
        })

    out = []
    for i, chunk in enumerate(chunks):
        out.append(chunk)
        for num in placed.get(i, []):
            out.extend(blocks[num])

    orphans = [r["figure"] for r in report if not r["placed"]]
    if orphans:
        out.append("## 图")
        for num in orphans:
            out.extend(blocks[num])

    return "\n\n".join(c.strip("\n") for c in out if c.strip()) + "\n", report


def unparsed_image_lines(text):
    """Image chunks that figure_number() cannot pin to a figure number.

    relocate() ignores these silently - they never enter `blocks`, so they are
    never moved and never appear in the report. When EVERY image line is
    unparsed the report comes back empty and the old code printed
    "OK: every figure sits after the paragraph that first mentions it"
    while nothing had moved at all. Same class of silent pass that
    docx_extract.py guards against with its "no caption at all" check.

    The usual cause is a ')' inside the path: IMAGE_RE's path group is
    [^)]*, so a filename like "paper (2024) 中文翻译_figs/fig01.png" ends the
    match early and the trailing \\s*$ then fails.
    """
    out = []
    for chunk in split_chunks(text):
        s = chunk.strip()
        if s.startswith("![") and figure_number(chunk) is None:
            out.append(" ".join(s.split())[:70])
    return out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("markdown")
    ap.add_argument("-o", "--out", help="write here instead of in place")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = ap.parse_args()

    # A Chinese Windows console is GBK, and a physics paper is full of
    # characters it cannot encode (NV⁻, ³A₂, τ). Printing a progress line must
    # never be the thing that kills the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass

    if not os.path.isfile(args.markdown):
        print(f"ERROR: no such file: {args.markdown}", file=sys.stderr)
        return 1
    with open(args.markdown, encoding="utf-8") as f:
        text = f.read()

    before = len(re.findall(r"^!\[", text, re.M))
    result, report = relocate(text)
    after = len(re.findall(r"^!\[", result, re.M))

    if before != after:
        print(f"ERROR: image count changed {before} -> {after}; refusing to write",
              file=sys.stderr)
        return 1

    stranded = unparsed_image_lines(text)
    if stranded and not report:
        print(f"images      : {before} found, 0 recognised")
        print(f"\nERROR: {len(stranded)} image line(s) are present but none could be "
              "matched, so\nnothing was moved. Reporting success here would hide a "
              "figure-less delivery.",
              file=sys.stderr)
        if any(")" in s for s in stranded):
            print("\nA ')' appears in these lines. The image pattern's path group is "
                  "[^)]*, so it\nstops at the first ')' and the line stops matching - "
                  "rename the markdown and its\n_figs folder so the name has no "
                  "parentheses.", file=sys.stderr)
        print("\nfirst unmatched line:\n  " + stranded[0], file=sys.stderr)
        return 3

    print(f"images      : {before} (unchanged)")
    if stranded:
        print(f"WARNING     : {len(stranded)} image line(s) carry no figure number "
              "and were left in place:")
        for s in stranded[:5]:
            print(f"  ? {s}")
    for r in sorted(report, key=lambda r: r["figure"]):
        if r["placed"]:
            print(f"  图 {r['figure']:<2} -> after: {r['after']}")
        else:
            print(f"  图 {r['figure']:<2} -> NO MENTION FOUND, left in '## 图'")

    if args.dry_run:
        print("\ndry run, nothing written")
    else:
        target = args.out or args.markdown
        if not args.out:
            shutil.copyfile(args.markdown, args.markdown + ".bak")
            print(f"backup      : {args.markdown}.bak")
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(result)
        print(f"written     : {target}")

    orphans = [r for r in report if not r["placed"]]
    if orphans:
        print(f"\nWARNING: {len(orphans)} figure(s) are never mentioned in the "
              f"text. Either the translation dropped the sentence that refers "
              f"to them, or the reference is worded differently - check before "
              f"delivering.")
        return 3
    print("\nOK: every figure sits after the paragraph that first mentions it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


