#!/usr/bin/env python
"""Check that every figure sits in the same *place* as in the source paper.

insert_figures.py (5.2) moves each figure to the paragraph that first mentions
it, and nothing ever checks the move afterwards. It catches exactly one
failure - a figure with no mention at all - and stays silent when a figure
lands under the wrong section, when the numbering drifts, or when the order of
figures comes out scrambled. 7.0 lists "figures follow the paragraph that
mentions them" as a manual eyeball check with no script evidence behind it.

This script closes that gap. It does NOT compare page numbers: the translation
is re-typeset (Chinese is shorter than English, different fonts, figures
re-flowed), so page-for-page identity is neither achievable nor meaningful.
What it does compare is context:

    hard checks (script decides, exit 3 on failure)
        1. the set of figure numbers matches
        2. the order of figures matches

    report (the model decides)
        for each figure, the source section title next to the source caption
        against the translation section heading the figure now sits under.
        "Results and Discussion" vs "结果与讨论" is a judgement call no string
        comparison can make, and the model already knows both languages.

When the model is multimodal (see SKILL.md 6.0.1), --render-dir also puts the
source page and the translation page of each figure side by side as PNGs, so
the comparison can be done by looking at the actual pages instead of by
comparing two strings.

The source anchor is the CAPTION position, not the artwork position. Journals
routinely park all four captions on one page and the artwork on the next four
(crop_figures.py uses the same convention), and the caption is what carries
the figure's number and section.

Usage:
    python verify_figure_positions.py <source.pdf> --md "<译文.md>"
    python verify_figure_positions.py <source.pdf> --md "<译文.md>" --pdf "<译文.pdf>"
    python verify_figure_positions.py <source.pdf> --md "<译文.md>" --pdf "<译文.pdf>" \
        --render-dir <work>/_pos_check
    python verify_figure_positions.py <source.pdf> --md "<译文.md>" --json

Exit codes:
    0  figure numbers and order match; read the report for section alignment
    3  numbers or order disagree - a figure is missing, extra, or shuffled
    1  error
"""

import argparse
import json
import os
import re
import sys

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_paper                                   # noqa: E402
import insert_figures                                  # noqa: E402

# Running heads, journal banners and the like sit at the very top of the page
# and are often set larger than body text; the margin band keeps them out of
# the heading candidates.
MARGIN_BAND = 0.06
# A heading candidate must be this much larger than the body text.
HEADING_SCALE = 1.15
HEADING_MAX_CHARS = 100
HEADING_EXCLUDE = re.compile(
    r"^(?:Figure|Fig\.|Table|Scheme|Chart|Equation|Eq\.|References|Ref\.)\b", re.I)


def page_lines(page):
    """[(rect, text, size)] for every text line, in reading order."""
    out = []
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:                                  # noqa: BLE001
        return out
    for b in blocks:
        for line in b.get("lines", []):
            spans = line["spans"]
            text = " ".join("".join(s["text"] for s in spans).split())
            if not text:
                continue
            size = max((s["size"] for s in spans if s["text"].strip()), default=0)
            out.append((pymupdf.Rect(line["bbox"]), text, size))
    out.sort(key=lambda item: (item[0].y0, item[0].x0))
    return out


def body_size(doc):
    """Median line size over lines long enough to be prose."""
    sizes = []
    for page in doc:
        for _rect, text, size in page_lines(page):
            if len(text) >= 40 and size > 0:
                sizes.append(size)
    if not sizes:
        return 0.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def headings_by_page(doc, body):
    """{page: [(rect, text)]} for lines that look like section headings."""
    out = {}
    if body <= 0:
        return out
    for pno, page in enumerate(doc, start=1):
        top = page.rect.y0 + page.rect.height * MARGIN_BAND
        bottom = page.rect.y1 - page.rect.height * MARGIN_BAND
        for rect, text, size in page_lines(page):
            if size < body * HEADING_SCALE:
                continue
            if len(text) > HEADING_MAX_CHARS or rect.y1 < top or rect.y0 > bottom:
                continue
            if HEADING_EXCLUDE.match(text):
                continue
            out.setdefault(pno, []).append((rect, text))
    return out


def section_above(headings, page, y):
    """Nearest heading at or above (page, y), walking back through pages."""
    while page >= 1:
        for rect, text in reversed(headings.get(page, [])):
            if rect.y1 <= y + 1:
                return text
        page -= 1
    return ""


def source_anchors(pdf_path):
    """{fig_num: {...}} from the source PDF, ordered by page then y."""
    with pymupdf.open(pdf_path) as doc:
        caps = extract_paper.figure_captions(doc)
        body = body_size(doc)
        heads = headings_by_page(doc, body)
        rows = []
        for cap in caps:
            rows.append({
                "figure": cap["num"],
                "page": cap["page"],
                "y": cap["rect"].y0,
                "section": section_above(heads, cap["page"], cap["rect"].y0),
                "caption": " ".join(cap["text"].split()),
            })
    rows.sort(key=lambda r: (r["page"], r["y"]))
    return rows, bool(heads)


def is_supplementary(*texts):
    """补充图 2 / Supplementary Fig. 2 is a different figure, not figure 2."""
    return any(re.match(r"^\s*(?:补充|附录|附|扩展数据|Supplementary|Extended\s+Data|SI)\s*图",
                        t, re.I) for t in texts if t)


# "### 图 3 | ..." - the figure's own title, written by this skill's 5.1.
# A section heading may mention a figure too ("图 3 的讨论"), so require the
# separator the skill actually emits before treating it as a figure title.
FIG_TITLE_RE = re.compile(r"^图\s*\d{1,2}\s*[|｜:]")


def translation_anchors(md_path):
    """{fig_num: {...}} from the translated Markdown, in document order."""
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    chunks = insert_figures.split_chunks(text)

    rows, section, pending_heading, order = {}, "", "", []
    total_images = 0
    for i, chunk in enumerate(chunks):
        stripped = chunk.strip()
        if not stripped:
            continue
        if insert_figures.HEADING_RE.match(stripped):
            heading = insert_figures.HEADING_RE.sub("", stripped).strip()
            if FIG_TITLE_RE.match(heading):
                pending_heading = heading
            elif not stripped.startswith("# "):
                # "# " is the paper title, not a section
                section = heading
                pending_heading = ""
            continue
        num = insert_figures.figure_number(chunk)
        if num is None:
            continue
        alt = insert_figures.IMAGE_RE.match(stripped)
        alt_text = alt.group("alt") if alt else ""
        if is_supplementary(alt_text, pending_heading, section):
            continue
        total_images += 1
        if num not in rows:
            order.append(num)
            rows[num] = {
                "figure": num,
                "md_index": i,
                "section": section,
                "heading": pending_heading,
                "caption": " ".join(alt_text.split())[:200],
            }
        pending_heading = ""
    ordered = [rows[n] for n in order]
    return ordered, text, total_images


def translation_pages(pdf_path, ordered, total_images):
    """Fill in the page each figure actually lands on.

    Primary method is image-object order: md_to_pdf renders the Markdown
    linearly, so the Nth image in the PDF is the Nth image in the Markdown.
    That is immune to font subsetting and to the Markdown markers (**bold**)
    that make a caption unsearchable as literal text - the first version of
    this function searched for them and found nothing, printing "p.?".

    Text search stays as a fallback for a PDF whose image count does not match
    the Markdown (a re-encode, or a PDF generated before the last edit to the
    .md), where the positional mapping cannot be trusted.
    """
    if not pdf_path:
        return None
    with pymupdf.open(pdf_path) as doc:
        img_pages = []
        for pno, page in enumerate(doc, start=1):
            img_pages.extend([pno] * len(page.get_images()))

        if len(img_pages) == total_images and total_images:
            # md_to_pdf renders the Markdown linearly and every image becomes
            # exactly one PDF image object (panel-per-image files included), so
            # the Nth image object is the Nth Markdown image.
            for row, pno in zip(ordered, img_pages):
                row["page"] = pno
            return None

        why = (f"translated PDF holds {len(img_pages)} image(s) but the "
               f"Markdown has {total_images}; falling back to text search")
        for row in ordered:
            probes = []
            if "|" in row["heading"]:
                probes.append(row["heading"].split("|", 1)[1].strip())
            probes.append(row["caption"].replace("**", "")[:20])
            row["page"] = None
            for probe in probes:
                if len(probe) < 3:
                    continue
                for pno, page in enumerate(doc, start=1):
                    if page.search_for(probe):
                        row["page"] = pno
                        break
                if row["page"]:
                    break
        return why


def render_pair(src_pdf, zh_pdf, ordered, out_dir):
    """Source page and translation page of each figure, side by side on disk."""
    os.makedirs(out_dir, exist_ok=True)
    written = []
    with pymupdf.open(src_pdf) as src, pymupdf.open(zh_pdf) as zh:
        for row in ordered:
            for tag, doc, page in (("src", src, row.get("src_page")),
                                   ("zh", zh, row.get("page"))):
                if not page or page < 1 or page > doc.page_count:
                    continue
                name = f"{tag}_fig{row['figure']:02d}.png"
                path = os.path.join(out_dir, name)
                doc[page - 1].get_pixmap(dpi=110).save(path)
                written.append(path)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_pdf")
    ap.add_argument("--md", required=True, help="translated Markdown")
    ap.add_argument("--pdf", default="", help="translated PDF (for page numbers)")
    ap.add_argument("--render-dir", default="",
                    help="also render both pages of every figure here (needs --pdf)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # A Chinese Windows console is GBK and a physics paper is full of
    # characters it cannot encode. Printing progress must never kill the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                  # noqa: BLE001
        pass

    for path in (args.source_pdf, args.md):
        if not os.path.isfile(path):
            print(f"ERROR: no such file: {path}", file=sys.stderr)
            return 1

    src, sections_found = source_anchors(args.source_pdf)
    zh, md_text, total_images = translation_anchors(args.md)

    zh_figs = {row["figure"] for row in zh}
    page_note = translation_pages(args.pdf, zh, total_images)

    src_seq = [row["figure"] for row in src]
    zh_seq = [row["figure"] for row in zh]

    for row in zh:                                     # for --render-dir pairing
        row["src_page"] = next((c["page"] for c in src if c["figure"] == row["figure"]), None)

    if args.render_dir:
        if not args.pdf:
            print("ERROR: --render-dir needs --pdf", file=sys.stderr)
            return 1
        render_pair(args.source_pdf, args.pdf, zh, args.render_dir)

    problems = []
    missing = sorted(set(src_seq) - zh_figs)
    extra = sorted(zh_figs - set(src_seq))
    if missing:
        problems.append(f"figure(s) {missing} are in the source but not in the "
                        f"translation")
    if extra:
        problems.append(f"figure(s) {extra} are in the translation but not in "
                        f"the source")
    if not problems and src_seq != zh_seq:
        problems.append(f"figure order differs: source {src_seq} vs "
                        f"translation {zh_seq}")

    if args.json:
        print(json.dumps({
            "source": args.source_pdf, "translation_md": args.md,
            "translation_pdf": args.pdf or None,
            "source_sequence": src_seq, "translation_sequence": zh_seq,
            "sections_detected": sections_found,
            "source_figures": src, "translation_figures": zh,
            "problems": problems,
        }, ensure_ascii=False, indent=2))
        return 3 if problems else 0

    print(f"source      : {args.source_pdf}")
    print(f"translation : {args.md}")
    print(f"figures     : {len(src_seq)} in source, {len(zh_seq)} in translation")
    if page_note:
        print(f"NOTE        : {page_note}")
    if not sections_found:
        print("NOTE        : no section headings detected in the source PDF; "
              "compare by order and caption, not by section name")
    print()

    src_by_num = {row["figure"]: row for row in src}
    print(f"{'figure':<7}| {'source':<43}| translation")
    print("-" * 7 + "+" + "-" * 44 + "+" + "-" * 43)
    for row in zh:
        cap = src_by_num.get(row["figure"], {})
        left = f"p.{cap.get('page', '?')}  {cap.get('section', '') or '(no section)'}"
        right = f"p.{row.get('page') or '?'}  {row['section'] or '(no section)'}"
        print(f"  {row['figure']:<5}| {left:<43}| {right}")
    for num in missing:
        cap = src_by_num[num]
        print(f"  {num:<5}| p.{cap['page']}  {cap['section'] or '(no section)':<32}| MISSING")
    print()

    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  " + p)
        print("\nA figure that is missing, extra or out of order means 5.2 did "
              "not place it\nwhere the source has it. Go and look before "
              "delivering.")
        return 3

    print("numbers and order match. Now check the section column: for every "
          "row the source\nsection and the translation section must be the "
          "same part of the paper. A\nmultimodal model should also compare "
          "the rendered pages (--render-dir) rather\nthan the two strings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
