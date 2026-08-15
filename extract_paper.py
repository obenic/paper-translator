#!/usr/bin/env python
"""Extract a paper PDF into text + figure images for translation.

A paper is text AND figures. Extracting only the text layer silently
produces an incomplete translation that looks complete. This script
extracts both, and cross-checks them against each other.

Usage:
    python extract_paper.py <pdf> [-o OUTDIR] [--dpi 200] [--pages 21-24]

Outputs into OUTDIR (default: <pdf_stem>_extract next to the PDF):
    text.txt          full text, one "===== PAGE n =====" marker per page
    figures/p21.png   rendered pages that carry figures
    manifest.json     per-page analysis + completeness check

Figure detection covers three cases the text layer misses:
    - standalone figure pages   (little text, large image)
    - figures embedded in text  (raster image above area threshold)
    - vector-drawn charts       (invisible to get_images(); counted as
                                 drawing operations instead)

Completeness check: the body text is scanned for figure references
("Fig. 3", "Figure 3"). If the paper references N figures, N figures
should come out. A mismatch is reported as a WARNING - it is a
heuristic (one page may hold several figures), not an assertion, so
resolve it by looking rather than by trusting the number.

Exit codes:
    0  extracted, figure count consistent
    3  extracted, but figure count looks wrong - INSPECT BEFORE TRANSLATING
    1  error
"""

import argparse
import json
import os
import re
import sys

# A page below this is not carrying real body text
MIN_TEXT_CHARS = 200
# Raster image covering this fraction of the page implies a figure
IMG_AREA_RATIO = 0.05
# Vector charts draw many paths; body text pages draw a handful of rules
VECTOR_OPS = 50
# Below this, a text-less page is blank rather than a figure
BLANK_DRAW_OPS = 10

CAPTION_RE = re.compile(
    r"(Supplementary|Supp\.|Extended\s+Data|SI)?\s*\bFig(?:ure)?\.?\s*(\d{1,2})\s*\|",
    re.I)
REFERENCE_RE = re.compile(
    r"(Supplementary|Supp\.|Extended\s+Data|SI)?\s*\bFig(?:ure)?s?\.?\s*(\d{1,2})",
    re.I)


def main_figure_numbers(text, pattern):
    """Figure numbers from the main paper, excluding Supplementary/Extended Data.

    Papers reference SI figures ("Supplementary Figure 17") far beyond their
    own figure count. Counting those makes every paper look like it is
    missing figures, so the prefix group is matched in order to discard it.
    """
    out = set()
    for prefix, num in pattern.findall(text):
        if prefix.strip():
            continue
        n = int(num)
        if 1 <= n <= 30:
            out.add(n)
    return out


def parse_pages(spec, n_pages):
    """'3,7-9' -> [3, 7, 8, 9] (1-based, clamped to the document)."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return [p for p in sorted(set(out)) if 1 <= p <= n_pages]


def analyze(page):
    """Classify one page: how much text, how much figure."""
    text = page.get_text("text").strip()
    page_area = abs(page.rect.width * page.rect.height) or 1.0

    img_area = 0.0
    try:
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            img_area += abs((x1 - x0) * (y1 - y0))
    except Exception:
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    img_area += abs(r.width * r.height)
            except Exception:
                pass

    try:
        n_draw = len(page.get_drawings())
    except Exception:
        n_draw = 0

    img_ratio = img_area / page_area
    reasons = []
    if len(text) < MIN_TEXT_CHARS and (img_ratio > 0.01 or n_draw >= BLANK_DRAW_OPS):
        reasons.append("standalone figure page")
    if img_ratio >= IMG_AREA_RATIO:
        reasons.append(f"raster image {img_ratio:.0%} of page")
    if n_draw >= VECTOR_OPS:
        reasons.append(f"vector drawing ({n_draw} ops)")

    return {
        "chars": len(text),
        "img_ratio": round(img_ratio, 3),
        "draw_ops": n_draw,
        "has_figure": bool(reasons),
        "why": reasons,
        "_text": text,
    }


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out-dir")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--max-width", type=int, default=1600,
                    help="cap rendered width in px, keeps files embeddable")
    ap.add_argument("--pages", help="render these pages regardless (e.g. 21-24)")
    args = ap.parse_args()

    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("ERROR: PyMuPDF missing. Run: pip install pymupdf", file=sys.stderr)
        return 1

    try:
        doc = fitz.open(args.pdf)
    except Exception as e:
        print(f"ERROR: cannot open PDF: {e}", file=sys.stderr)
        return 1

    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.pdf)), f"{stem}_extract")
    fig_dir = os.path.join(out_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)

    pages = [analyze(p) for p in doc]
    full_text = "\n".join(
        f"\n===== PAGE {i + 1} =====\n\n" + (p["_text"] or "[no text layer]")
        for i, p in enumerate(pages))

    text_path = os.path.join(out_dir, "text.txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    # Which pages to render
    if args.pages:
        to_render = parse_pages(args.pages, len(pages))
    else:
        to_render = [i + 1 for i, p in enumerate(pages) if p["has_figure"]]

    rendered = []
    for pno in to_render:
        page = doc[pno - 1]
        zoom = min(args.dpi / 72.0, args.max_width / max(page.rect.width, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        path = os.path.join(fig_dir, f"p{pno:02d}.png")
        pix.save(path)
        rendered.append({
            "page": pno,
            "file": os.path.relpath(path, out_dir).replace("\\", "/"),
            "size": f"{pix.width}x{pix.height}",
            "kb": os.path.getsize(path) // 1024,
            "why": pages[pno - 1]["why"],
        })

    # Cross-check: does the figure count match what the text references?
    # Captions ("Fig. 3 | ...") are authoritative when present; a paper that
    # only mentions figures inline falls back to the reference scan.
    captions = main_figure_numbers(full_text, CAPTION_RE)
    referenced = main_figure_numbers(full_text, REFERENCE_RE)
    counted = captions or referenced
    expected = max(counted) if counted else 0

    text_pages = sum(1 for p in pages if p["chars"] >= MIN_TEXT_CHARS)
    scanned = text_pages == 0 and len(pages) > 0

    consistent = expected == 0 or len(rendered) >= expected
    manifest = {
        "pdf": os.path.abspath(args.pdf),
        "out_dir": os.path.abspath(out_dir),
        "pages": len(pages),
        "text_pages": text_pages,
        "is_scanned": scanned,
        "figures_referenced_in_text": sorted(counted),
        "figures_expected": expected,
        "figures_rendered": len(rendered),
        "consistent": consistent,
        "rendered": rendered,
        "per_page": [{k: v for k, v in p.items() if k != "_text"} for p in pages],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Human-readable summary
    print(f"pdf         : {os.path.basename(args.pdf)}")
    print(f"out         : {out_dir}")
    print(f"pages       : {len(pages)}  (text pages: {text_pages})")
    if scanned:
        print("SCANNED     : no text layer anywhere - read figures/*.png visually")
    print(f"text        : {text_path}")
    print(f"figures     : {len(rendered)} rendered -> {fig_dir}")
    for r in rendered:
        print(f"  p{r['page']:>3}  {r['file']:<18} {r['size']:>10} {r['kb']:>5}KB  "
              f"({'; '.join(r['why']) or 'forced'})")
    print(f"referenced  : Fig {sorted(counted) or 'none found'}")

    if not consistent:
        print(f"\nWARNING: text references {expected} figures but only "
              f"{len(rendered)} were rendered.")
        print("Do NOT translate yet. Open manifest.json, find the missing")
        print("figures, and re-run with --pages to force-render them.")
        return 3

    print("\nOK: figure count consistent with text references.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
