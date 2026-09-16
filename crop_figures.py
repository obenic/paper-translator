#!/usr/bin/env python
"""Crop whole figures straight out of the source PDF, by caption position.

Why this exists: Acrobat's Word export splits a multi-column figure (four
colour-centre panels side by side) into separate picture objects, and
docx_extract.py then names the FIRST column fig02.png. Every check passes -
the figure is numbered, captioned and referenced - but the reader gets one
panel out of four. Nothing downstream can notice, because a 245x499 image is
a perfectly plausible figure.

This script does not touch the .docx at all. For every "Figure N." caption it
finds in the PDF it takes the image/drawing blocks stacked above that caption
(down to the previous caption or the page top), unions them, and renders that
clip at --dpi. Vector-drawn plots are covered as well, because drawings count
as blocks. Captions above their figure are handled by --below.

Usage:
    python crop_figures.py <paper.pdf> -o <out_dir>              # every main figure
    python crop_figures.py <paper.pdf> -o <out_dir> --figs 2-5,11
    python crop_figures.py <paper.pdf> --dry-run                 # report clips only
    python crop_figures.py <paper.pdf> -o <out_dir> --compare <docx_dir>/media

--compare lists, for each figure, the Word-extracted width against the crop
width. A Word image narrower than 60% of the crop is a fragment: use the crop.

Exit codes:
    0  every requested figure cropped
    3  a caption was found but no graphic block sits above (or below) it, or a
       requested figure number has no caption at all - look at the page
    1  error
"""

import argparse
import os
import re
import sys

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

# "Figure 2." / "Fig. 2 |" / Wiley's "FIGURE 2 Overview" (no punctuation)
CAPTION = re.compile(r"^\s*(?:Figure|Fig\.)\s*(\d{1,3})\s*(?:[.|:]|\s(?=[A-Z(]))", re.I)


def parse_figs(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def captions_on_page(page):
    """[(fig_number, caption_rect)] for every 'Figure N.' text block on the page."""
    found = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        text = " ".join(s["text"] for l in b["lines"] for s in l["spans"])
        m = CAPTION.match(text)
        if m:
            found.append((int(m.group(1)), pymupdf.Rect(b["bbox"])))
    found.sort(key=lambda t: t[1].y0)
    return found


def graphic_blocks(page):
    td = page.get_text("dict")["blocks"]
    rects = [pymupdf.Rect(b["bbox"]) for b in td if b["type"] == 1]
    rects += [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
    return [r for r in rects if r.width > 40 and r.height > 40]


def clip_for(page, cap_rect, floor, ceiling, below):
    """Union of graphic blocks between floor and the caption (or caption and ceiling)."""
    blocks = graphic_blocks(page)
    if below:
        cand = [r for r in blocks if r.y0 >= cap_rect.y1 - 2 and r.y1 <= ceiling + 2]
    else:
        cand = [r for r in blocks if r.y0 >= floor - 2 and r.y1 <= cap_rect.y0 + 2]
    if not cand:
        return None
    u = pymupdf.Rect(cand[0])
    for r in cand[1:]:
        u |= r
    pad = 6
    return pymupdf.Rect(u.x0 - pad, u.y0 - pad, u.x1 + pad, u.y1 + pad) & page.rect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--figs", default="", help="e.g. 2-5,11 (default: all)")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--below", action="store_true",
                    help="captions sit ABOVE their figure")
    ap.add_argument("--compare", default="", help="docx media dir to compare widths")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc = pymupdf.open(args.pdf)
    wanted = parse_figs(args.figs) if args.figs else None
    if args.out and not args.dry_run:
        os.makedirs(args.out, exist_ok=True)

    seen, problems = {}, []
    for pno, page in enumerate(doc, 1):
        caps = captions_on_page(page)
        if not caps:
            continue
        text_blocks = [pymupdf.Rect(b["bbox"]) for b in page.get_text("dict")["blocks"]
                       if b["type"] == 0]
        for k, (num, cr) in enumerate(caps):
            if wanted is not None and num not in wanted:
                continue
            if num in seen:          # "Figure 2" mentioned again in body text
                continue
            floor = caps[k - 1][1].y1 if k else 0
            ceiling = caps[k + 1][1].y0 if k + 1 < len(caps) else page.rect.y1
            if not args.below:
                # body text between the previous caption and this figure is the
                # real floor - a figure never straddles a paragraph
                for t in text_blocks:
                    if floor <= t.y1 <= cr.y0 - 20 and t.height > 30:
                        floor = max(floor, t.y1)
            clip = clip_for(page, cr, floor, ceiling, args.below)
            if clip is None:
                problems.append(f"Figure {num} (page {pno}): caption found, no graphic block "
                                f"{'below' if args.below else 'above'} it")
                continue
            seen[num] = (pno, clip)
            msg = f"fig{num:02d}  page {pno:<3d} clip=({clip.x0:.0f},{clip.y0:.0f},{clip.x1:.0f},{clip.y1:.0f})"
            if not args.dry_run and args.out:
                pix = page.get_pixmap(dpi=args.dpi, clip=clip)
                pix.save(os.path.join(args.out, f"fig{num:02d}.png"))
                msg += f" -> {pix.width}x{pix.height}"
            print(msg)

    if wanted is not None:
        for n in sorted(wanted - set(seen)):
            problems.append(f"Figure {n}: no caption found in the PDF")

    if args.compare and seen:
        print("\ncompare (word width / crop width at 72 dpi):")
        for num, (pno, clip) in sorted(seen.items()):
            cand = [f for f in os.listdir(args.compare)
                    if re.fullmatch(rf"fig0*{num}\.(png|jpe?g)", f, re.I)]
            if not cand:
                print(f"  fig{num:02d}: no Word image")
                continue
            pix = pymupdf.Pixmap(os.path.join(args.compare, cand[0]))
            ratio = pix.width / (clip.width * args.dpi / 72)
            flag = "FRAGMENT - use the crop" if ratio < 0.6 else "ok"
            print(f"  fig{num:02d}: {pix.width}px vs {clip.width * args.dpi / 72:.0f}px  ({ratio:.2f})  {flag}")

    print(f"\nfigures : {len(seen)} cropped" + (" (dry run)" if args.dry_run else ""))
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  " + p)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
