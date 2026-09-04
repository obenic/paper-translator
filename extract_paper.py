#!/usr/bin/env python
"""Extract a paper PDF into text + figure images for translation.

A paper is text AND figures. Extracting only the text layer silently
produces an incomplete translation that looks complete. This script
extracts both, and cross-checks them against each other.

Usage:
    python extract_paper.py <pdf> [-o OUTDIR] [--dpi 200] [--pages 21-24] [--ocr]
                                  [--split-panels]

Outputs into OUTDIR (default: <pdf_stem>_extract next to the PDF):
    text.txt          full text, one "===== PAGE n =====" marker per page
    figures/p21.png   rendered pages that carry figures
    panels/p21_a.png  individual panels, with --split-panels
    manifest.json     per-page analysis + completeness check

Figure detection covers three cases the text layer misses:
    - standalone figure pages   (little text, large image)
    - figures embedded in text  (raster image above area threshold)
    - vector-drawn charts       (invisible to get_images(); counted as
                                 drawing operations instead)

Scanned PDFs (no text layer):
    --ocr           OCR rendered pages. Backend picked by ocr_engine.py:
                    RapidOCR first, PaddleOCR as fallback.
                    Requires: pip install --no-deps rapidocr
                              pip install onnxruntime shapely pyclipper omegaconf colorlog
                    Without this flag, scanned PDFs produce empty text.txt
                    and you must rely on multimodal model vision.

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

# A caption block opens with "Fig. 2." / "Figure 2:" / "Fig. 1 |" - a figure
# number followed by a separator. Anchored at the start of a text block so a
# mid-sentence "see Fig. 2" cannot pose as a caption.
CAPTION_START_RE = re.compile(
    r"^\**\s*(?:Supplementary\s+|Extended\s+Data\s+)?"
    r"Fig(?:ure)?\.?\s*(\d{1,2})\s*[.|:｜]", re.I)
# Panel groups as Elsevier and IEEE write them: "(a)", "(a, b)", "(a-d)", and
# with sub-indices "(a1-a2)".
PANEL_PAREN_RE = re.compile(
    r"\(\s*([a-z]\d?(?:\s*(?:,|and|&|-|–)\s*[a-z]\d?)*)\s*\)")
# Nature drops the parentheses and bolds the letter; bold is lost in the text
# layer, so the only signal left is "lone letter, then a capital".
PANEL_BARE_RE = re.compile(r"(?<![A-Za-z])([a-z])\s+(?=[A-Z])")
# ... and the same for a bolded range, "h-k SHAP dependence plots".
PANEL_BARE_RANGE_RE = re.compile(
    r"(?<![A-Za-z])([a-z])\s*[-–]\s*([a-z])\s+(?=[A-Z])")


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


def make_ocr_engine(lang):
    """Build an OCR engine, or return (None, None) with an actionable message.

    Backend choice lives in ocr_engine.py: RapidOCR first, PaddleOCR as
    fallback. Both run the same PP-OCR weights, so this is not an accuracy
    trade - RapidOCR is just far cheaper to start and to run (measured 7.9x
    faster on identical weights, 18.7x on its lighter default).

    Returns (engine, backend_name) so the summary can report which one ran.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import ocr_engine
    except ImportError:
        print("ERROR: ocr_engine.py must sit next to this script.",
              file=sys.stderr)
        return None, None

    engine, backend, note = ocr_engine.make_engine(lang=lang)
    if engine is None:
        print(f"ERROR: {note}", file=sys.stderr)
        return None, None
    if note:
        print(f"NOTE: {note}")
    return engine, backend


def ocr_pixmap(engine, pix):
    """OCR one rendered page. Returns recognized text, one line per box."""
    import numpy as np

    import ocr_engine

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    if pix.n == 4:          # RGBA -> RGB
        img = img[:, :, :3]

    try:
        rows = ocr_engine.read(engine, img)     # adapter takes RGB
    except Exception as e:
        print(f"    OCR failed on this page: {e}", file=sys.stderr)
        return ""
    return "\n".join(r["text"] for r in rows)


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


def caption_panel_letters(text):
    """Panel letters a caption claims exist. Returns (letters, how_confident).

    Worth having because it comes from the text layer, entirely independent of
    the image: if the caption enumerates a-o and the splitter produced 14
    panels, one is missing and no amount of OCR agreement would have said so.
    """
    def longest_run(candidates):
        # Panels are always labelled from 'a' with no gaps, so keeping the
        # unbroken run rejects prose enumerations: Fig 3's caption says
        # "demonstrating (i) a rigid shift ... and (ii) a reduction", and
        # without this the stray (i) becomes a ninth panel.
        run = []
        for i in range(26):
            ch = chr(ord("a") + i)
            if ch not in candidates:
                break
            run.append(ch)
        return run

    def expand(part):
        """'a'->[a]; 'a-d'->[a,b,c,d]; 'a1-a2'->[a1,a2]; 'b2'->[b2]."""
        toks = re.findall(r"[a-z]\d?", part)
        if len(toks) == 2 and re.search(r"-|–", part):
            lo, hi = toks
            if lo[0] == hi[0] and len(lo) == 2 and len(hi) == 2:
                return [f"{lo[0]}{d}" for d in range(int(lo[1]), int(hi[1]) + 1)]
            if len(lo) == 1 and len(hi) == 1:
                return [chr(o) for o in range(ord(lo), ord(hi) + 1)]
        return toks

    letters = set()
    for group in PANEL_PAREN_RE.findall(text):
        for part in re.split(r"\s*(?:,|and|&)\s*", group):
            letters.update(expand(part))
    run = longest_run(letters)
    if len(run) >= 2:
        return run, "parenthesised"
    # Sub-indexed panels (a1, a2, b1, b2) have no plain-letter run to find;
    # accept them when every token carries an index and 'a1' is present.
    indexed = sorted(k for k in letters if len(k) == 2)
    if len(indexed) >= 2 and indexed[0] == "a1":
        return indexed, "parenthesised"

    bare = set(PANEL_BARE_RE.findall(text))
    for lo, hi in PANEL_BARE_RANGE_RE.findall(text):
        bare.update(chr(o) for o in range(ord(lo), ord(hi) + 1))
    run = longest_run(bare)
    if len(run) >= 3:
        return run, "bare"
    return [], "none"


def _line_offsets(raw):
    """(char offset, line) for every line in a text block."""
    out, pos = [], 0
    for line in raw.splitlines():
        out.append((pos, line))
        pos += len(line) + 1
    return out


def figure_captions(doc):
    """Every figure caption in the document: [{num, page, rect, text, panels}].

    Captions are not always next to their artwork - the reference Nature paper
    keeps all four captions on page 20 and the figures on pages 21-24 - so they
    are collected document-wide and paired with artwork later.
    """
    import fitz

    found = []
    for pno, page in enumerate(doc, start=1):
        try:
            blocks = page.get_text("blocks")
        except Exception:
            continue
        for b in blocks:
            if len(b) < 5 or not isinstance(b[4], str):
                continue
            raw = b[4]
            # Match at the start of any line, not just the start of the block.
            # PyMuPDF often glues a caption onto the paragraph above it, and
            # Nature-style captions run across a page break, so a block-anchored
            # test misses them entirely.
            for offset, line in _line_offsets(raw):
                text = " ".join(line.split())
                if re.match(r"^\**\s*(Supplementary|Extended)", text, re.I):
                    continue
                m = CAPTION_START_RE.match(text)
                if not m:
                    continue
                tail = " ".join(raw[offset:].split())
                panels, how = caption_panel_letters(tail)
                found.append({
                    "num": int(m.group(1)),
                    "page": pno,
                    "rect": fitz.Rect(b[:4]),
                    "text": tail[:1200],
                    "panels": panels,
                    "panels_from": how,
                })
                break
    found.sort(key=lambda c: (c["num"], c["page"], c["rect"].y0))
    seen, unique = set(), []
    for c in found:
        if c["num"] not in seen:
            seen.add(c["num"])
            unique.append(c)
    return unique


def pair_captions(regions, captions):
    """Attach a figure number to each artwork region.

    Same page first: a caption sits directly under (or over) its own artwork,
    so vertical distance plus horizontal overlap identifies it. Whatever is
    left over is matched in document order, which is what rescues the Nature
    layout where every caption lives on a different page than its figure.
    """
    numbered = {}
    free = list(captions)
    for i, (pno, rect) in enumerate(regions):
        same_page = [c for c in free if c["page"] == pno]
        best, best_key = None, None
        for c in same_page:
            overlap = min(rect.x1, c["rect"].x1) - max(rect.x0, c["rect"].x0)
            if overlap <= 0.3 * min(rect.width, c["rect"].width):
                continue
            gap = min(abs(c["rect"].y0 - rect.y1), abs(rect.y0 - c["rect"].y1))
            if best_key is None or gap < best_key:
                best, best_key = c, gap
        if best is not None:
            numbered[i] = best
            free.remove(best)
    leftovers = [i for i in range(len(regions)) if i not in numbered]
    for i, c in zip(leftovers, free):
        numbered[i] = c
    return numbered


def page_pixmap(page, rect, zoom):
    """Render one clipped region of a page at the given zoom."""
    import fitz

    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)


def figure_regions(page, min_area_frac=0.02, gap=12, max_label_chars=40):
    """Bounding boxes of the figures on a page, without the surrounding prose.

    A two-column journal page carries the figure, its caption, and two columns
    of body text. Rendering the whole page and handing that to the panel
    splitter turns paragraphs into "panels" - page 6 of the ODMR paper produced
    52 of them. So figures are located from what the PDF already knows (raster
    images and vector drawing rectangles) rather than guessed from pixels.

    Short text near a cluster is pulled back in: panel letters and axis labels
    are text objects, and cropping to the artwork alone would slice "(a)" off.
    Long text - captions, paragraphs - is left out by the length cut-off.
    """
    import fitz

    page_area = abs(page.rect.width * page.rect.height) or 1.0
    rects = []
    try:
        for info in page.get_image_info():
            rects.append(fitz.Rect(info["bbox"]))
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            r = fitz.Rect(drawing["rect"])
            if r.width > 4 and r.height > 4:
                rects.append(r)
    except Exception:
        pass
    if not rects:
        return []

    merged = []
    for rect in rects:
        rect = fitz.Rect(rect)
        absorbed = True
        while absorbed:
            absorbed = False
            for other in list(merged):
                if fitz.Rect(other).intersects(rect + (-gap, -gap, gap, gap)):
                    rect |= other
                    merged.remove(other)
                    absorbed = True
        merged.append(rect)

    blocks = [(fitz.Rect(b[:4]), b[4]) for b in page.get_text("blocks")
              if len(b) >= 5 and isinstance(b[4], str)]
    out = []
    for cluster in merged:
        if abs(cluster.width * cluster.height) < min_area_frac * page_area:
            continue
        near = cluster + (-2 * gap, -2 * gap, 2 * gap, 2 * gap)
        for rect, text in blocks:
            if len(text.strip()) > max_label_chars:
                continue
            # Mostly inside, not merely touching. On page 6 of the ODMR paper
            # the heading "4. Conclusions" in the far column clips the corner
            # of the search box by five points; a bare intersects() test drags
            # the crop across the gutter and swallows a column of prose.
            overlap = near & rect
            area = abs(rect.width * rect.height) or 1.0
            if overlap.is_valid and abs(overlap.width * overlap.height) >= 0.6 * area:
                cluster |= rect
        out.append(cluster & page.rect)
    out.sort(key=lambda r: (r.y0, r.x0))
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out-dir")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--max-width", type=int, default=1600,
                    help="cap rendered width in px, keeps files embeddable")
    ap.add_argument("--pages", help="render these pages regardless (e.g. 21-24)")
    ap.add_argument("--ocr", action="store_true",
                    help="OCR scanned pages (RapidOCR preferred, "
                         "PaddleOCR fallback - see ocr_engine.py)")
    ap.add_argument("--ocr-lang", default="en",
                    help="OCR language: en, ch, japan, korean, ... "
                         "(default: en; use ch for Chinese-English mixed)")
    ap.add_argument("--split-panels", action="store_true",
                    help="also cut each rendered figure into its panels "
                         "(a, b, c...) under panels/, with OCR completeness "
                         "checks. For an exact split, re-run panel_split.py "
                         "on one figure with --layout")
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
    text_pages = sum(1 for p in pages if p["chars"] >= MIN_TEXT_CHARS)
    scanned = text_pages == 0 and len(pages) > 0

    # OCR pass. Only meaningful for a PDF with no text layer - a text PDF
    # already has better text than OCR would produce.
    ocr_used = False
    ocr_backend = None
    if args.ocr:
        if not scanned:
            print(f"NOTE: --ocr ignored, this PDF already has a text layer "
                  f"({text_pages} text pages).")
        else:
            engine, ocr_backend = make_ocr_engine(args.ocr_lang)
            if engine is None:
                return 1
            print(f"OCR: scanned PDF, reading {len(pages)} pages "
                  f"({ocr_backend}, lang={args.ocr_lang}; "
                  f"first run downloads models)...")
            for i, page in enumerate(doc):
                zoom = args.dpi / 72.0
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                text = ocr_pixmap(engine, pix)
                pages[i]["_text"] = text
                pages[i]["chars"] = len(text)
                print(f"  p{i + 1:>3}: {len(text)} chars")
            text_pages = sum(1 for p in pages if p["chars"] >= MIN_TEXT_CHARS)
            ocr_used = True
            # Judge OCR success on total yield, not on MIN_TEXT_CHARS: a
            # legitimately sparse scan (title pages, short sections) clears
            # 100 chars/page easily but never reaches the body-text threshold,
            # and warning there cries wolf on a perfectly good extraction.
            total_chars = sum(p["chars"] for p in pages)
            if total_chars < 20 * len(pages):
                print("WARNING: OCR produced almost no text. The scan may be "
                      "too low-resolution - retry with --dpi 300.")
            scanned = False

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

    # Optional: cut each rendered figure into its panels. A page image holding
    # 15 sub-plots is one blob to a translator; panel 2e next to caption 2e is
    # what a reader actually needs.
    panel_reports = []
    if args.split_panels and rendered:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from panel_split import split_figure
        except ImportError as e:
            print(f"WARNING: panel splitting unavailable ({e})", file=sys.stderr)
        else:
            panel_dir = os.path.join(out_dir, "panels")
            crop_dir = os.path.join(fig_dir, "cropped")
            os.makedirs(crop_dir, exist_ok=True)

            # Locate every figure in the document, then let the captions say
            # which number each one is. "Fig. 2." in the text layer is far more
            # reliable than counting pages: one page can hold two figures, and
            # a figure's caption can live on another page entirely.
            captions = figure_captions(doc)
            regions = []
            for r in rendered:
                page = doc[r["page"] - 1]
                found = figure_regions(page) or [page.rect]
                regions.extend((r["page"], rect) for rect in found)
            numbered = pair_captions(regions, captions)

            print(f"\ncaptions found: Fig "
                  f"{[c['num'] for c in captions] or 'none'}")
            print(f"splitting {len(regions)} figure regions into panels...")
            for i, (pno, rect) in enumerate(regions):
                cap = numbered.get(i)
                stem = f"fig{cap['num']:02d}" if cap else f"p{pno:02d}x{i:02d}"
                expect = cap["panels"] if cap else []
                zoom = min(args.dpi / 72.0, args.max_width / max(rect.width, 1))
                pix = page_pixmap(doc[pno - 1], rect, zoom)
                src = os.path.join(crop_dir, f"{stem}.png")
                pix.save(src)
                try:
                    rep = split_figure(src, panel_dir, stem=stem,
                                       lang=args.ocr_lang, expect=expect)
                except Exception as e:                  # noqa: BLE001
                    print(f"  {stem}: split failed ({e})")
                    continue
                rep["page"] = pno
                rep["figure_number"] = cap["num"] if cap else None
                rep["caption"] = cap["text"] if cap else None
                rep["caption_panels"] = expect
                rep["figure_crop"] = os.path.relpath(
                    src, out_dir).replace("\\", "/")
                panel_reports.append(rep)
                flag = "ok" if rep["ok"] else f"{len(rep['problems'])} issue(s)"
                exp = f", caption says {len(expect)}" if expect else ""
                print(f"  {stem}: {pix.width}x{pix.height} -> "
                      f"{rep['panel_count']} panels{exp}, "
                      f"ink {rep['ink_kept_frac']:.1%}, {flag}")
                for p in rep["problems"][:3]:
                    print(f"      - {p}")

    # Cross-check: does the figure count match what the text references?
    # Captions ("Fig. 3 | ...") are authoritative when present; a paper that
    # only mentions figures inline falls back to the reference scan.
    captions = main_figure_numbers(full_text, CAPTION_RE)
    referenced = main_figure_numbers(full_text, REFERENCE_RE)
    counted = captions or referenced
    expected = max(counted) if counted else 0

    consistent = expected == 0 or len(rendered) >= expected
    manifest = {
        "pdf": os.path.abspath(args.pdf),
        "out_dir": os.path.abspath(out_dir),
        "pages": len(pages),
        "text_pages": text_pages,
        "is_scanned": scanned,
        "ocr_used": ocr_used,
        "ocr_backend": ocr_backend,
        "ocr_lang": args.ocr_lang if ocr_used else None,
        "figures_referenced_in_text": sorted(counted),
        "figures_expected": expected,
        "figures_rendered": len(rendered),
        "consistent": consistent,
        "rendered": rendered,
        "panels": panel_reports,
        "per_page": [{k: v for k, v in p.items() if k != "_text"} for p in pages],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Human-readable summary
    print(f"pdf         : {os.path.basename(args.pdf)}")
    print(f"out         : {out_dir}")
    print(f"pages       : {len(pages)}  (text pages: {text_pages})")
    if scanned:
        print("SCANNED     : no text layer - use --ocr, or read figures/*.png visually")
    if ocr_used:
        print(f"OCR         : {ocr_backend} (lang={args.ocr_lang})")
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
