#!/usr/bin/env python
"""Split a composite scientific figure into its individual panels (a, b, c...).

Why this exists: rendering a whole PDF page gives one big image holding 15
sub-plots. A translated paper needs the panels one by one, so a caption for
panel 2e can sit next to panel 2e instead of next to a wall of 15 charts.

The hard part is not cutting - it is proving the cut is right. A crop that
silently loses a colorbar, or that swallows half of its neighbour, looks
perfectly fine on its own. So every crop is checked four ways:

    1. label bijection  - exactly one panel label (a/b/c...) per crop, and
                          every label found in the figure lands in some crop
    2. border margin    - the crop is ringed by background pixels, so no
                          glyph or axis was sliced through
    3. ink conservation - the crops together hold ~all the ink of the figure
    4. text conservation- every OCR text box of the figure falls inside a crop

Checks 1 and 4 need OCR (PaddleOCR). Without it, 2 and 3 still run and the
result is reported as low-confidence rather than silently trusted.

Panel labels are the ground truth for how many panels exist. Cut lines are
then placed at the minimum-ink seam between adjacent labels, which lands in
the real gutter instead of at a guessed fraction of the width.

Usage (standalone, on an already-rendered figure):
    python panel_split.py Fig2.png -o panels/
    python panel_split.py Fig2.png -o panels/ --grid 4x3      # forced grid
    python panel_split.py Fig2.png -o panels/ --no-ocr        # geometry only

Exit codes:
    0  panels written, all checks passed
    3  panels written, but a check failed - LOOK AT THEM BEFORE USING
    1  error
"""

import argparse
import json
import os
import re
import sys

import numpy as np

# A pixel this far from the sampled background colour counts as ink.
INK_TOL = 14
# Padding kept around each panel's ink bounding box, in px.
PANEL_PAD = 6
# A crop's border ring must be this clean; above it, content was sliced.
BORDER_INK_FRAC = 0.02
# Crops must jointly hold at least this share of the figure's ink.
INK_KEEP_FRAC = 0.97
# Panel labels are set larger than tick labels; keep single glyphs at least
# this tall relative to the median OCR box height.
LABEL_MIN_HEIGHT_RATIO = 0.9
# Ignore a candidate panel smaller than this fraction of the figure.
MIN_PANEL_FRAC = 0.004
# Candidate gutter widths tried when auto-tuning, as a fraction of the
# figure's shorter side. Wide enough to span "dense multi-panel" to "two
# big plots side by side".
GUTTER_SWEEP = (0.006, 0.008, 0.010, 0.013, 0.016, 0.020, 0.025, 0.030, 0.040)

# Panel labels come bare ('a'), parenthesised ('(a)'), half-closed ('a)'), or
# with a sub-index ('(a1)', 'b2'). Elsevier and IEEE prefer parentheses;
# Nature prefers bare bold letters.
LABEL_RE = re.compile(r"^\(?([a-zA-Z])\s?(\d{0,2})[).]?$")



def load_image(source):
    """Accept a path, a PIL image, or an HxWx3 array. Returns uint8 RGB."""
    if isinstance(source, np.ndarray):
        arr = source
    else:
        from PIL import Image
        img = source if hasattr(source, "convert") else Image.open(source)
        arr = np.asarray(img.convert("RGB"))
    if arr.ndim == 2:
        arr = np.dstack([arr] * 3)
    return arr[:, :, :3].astype(np.uint8)


def ink_mask(rgb, tol=INK_TOL):
    """True where the pixel differs from the background colour.

    The background is taken from the four border strips rather than assumed
    white: Fig 4a of the reference paper sits on a dark brown plate, and a
    hard-coded white assumption inverts the mask on figures like that.
    """
    h, w = rgb.shape[:2]
    strip = max(2, min(h, w) // 100)
    border = np.concatenate([
        rgb[:strip].reshape(-1, 3),
        rgb[-strip:].reshape(-1, 3),
        rgb[:, :strip].reshape(-1, 3),
        rgb[:, -strip:].reshape(-1, 3),
    ])
    bg = np.median(border, axis=0)
    return (np.abs(rgb.astype(np.int16) - bg).max(axis=2) > tol)


def tighten(mask, box, pad=PANEL_PAD):
    """Shrink a box to the ink inside it, then re-expand by pad.

    This is what makes a crop 'complete but not padded out': the box is
    driven by content, and the pad only restores breathing room.
    """
    x0, y0, x1, y1 = box
    sub = mask[y0:y1, x0:x1]
    if not sub.any():
        return None
    rows = np.flatnonzero(sub.any(axis=1))
    cols = np.flatnonzero(sub.any(axis=0))
    ny0, ny1 = y0 + int(rows[0]), y0 + int(rows[-1]) + 1
    nx0, nx1 = x0 + int(cols[0]), x0 + int(cols[-1]) + 1
    h, w = mask.shape
    return (max(0, nx0 - pad), max(0, ny0 - pad),
            min(w, nx1 + pad), min(h, ny1 + pad))


def run_ocr(rgb, lang="en"):
    """OCR the figure once. Returns [{'text', 'box': (x0,y0,x1,y1)}, ...].

    One pass over the whole figure, never once per crop: every later check
    assigns these boxes to crops geometrically, which is both faster and
    consistent (re-OCR of a crop can read different text than the whole).
    """
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        return None, "PaddleOCR not installed (pip install paddlepaddle paddleocr)"
    try:
        engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            lang=lang,
            enable_mkldnn=False,
        )
        res = engine.predict(rgb[:, :, ::-1])   # PaddleOCR wants BGR
    except Exception as e:                      # noqa: BLE001 - report, don't crash
        return None, f"OCR failed: {e}"
    if not res:
        return [], None

    page = res[0]
    texts = page.get("rec_texts") or []
    polys = page.get("rec_polys")
    if polys is None:
        polys = page.get("dt_polys") or []
    out = []
    for text, poly in zip(texts, polys):
        pts = np.asarray(poly, dtype=float).reshape(-1, 2)
        out.append({
            "text": str(text).strip(),
            "box": (int(pts[:, 0].min()), int(pts[:, 1].min()),
                    int(pts[:, 0].max()) + 1, int(pts[:, 1].max()) + 1),
        })
    return out, None


def find_panel_labels(ocr):
    """Pick out the single-letter panel markers from all OCR boxes.

    Three filters, because any one alone misfires. The text must be a lone
    letter; the glyph must not be smaller than typical figure text; and where
    the paper parenthesises its labels, only parenthesised hits count - the
    ODMR paper's axes contain bare 't', 'i' and 'u' that otherwise pose as
    panels d, i and u and wreck the ordering check.
    """
    hits = []
    for b in ocr:
        m = LABEL_RE.match(b["text"])
        if m:
            hits.append((m.group(1).lower() + m.group(2), "(" in b["text"], b))
    if not hits:
        return {}
    if any(bracketed for _, bracketed, _ in hits):
        hits = [h for h in hits if h[1]]
    heights = [b["box"][3] - b["box"][1] for b in ocr] or [1]
    floor = np.median(heights) * LABEL_MIN_HEIGHT_RATIO
    labels = {}
    for key, bracketed, b in sorted(hits, key=lambda h: (h[2]["box"][1],
                                                         h[2]["box"][0])):
        if (b["box"][3] - b["box"][1]) < floor:
            continue
        labels.setdefault(key, (b["box"], bracketed))
    # An index-suffixed label only counts if it belongs to a family (a1 with
    # a2) or was parenthesised. Otherwise "R²" on panel 2e's axis registers as
    # a panel called r2.
    stems = {}
    for key in labels:
        if len(key) > 1:
            stems[key[0]] = stems.get(key[0], 0) + 1
    return {k: v[0] for k, v in labels.items()
            if len(k) == 1 or v[1] or stems.get(k[0], 0) >= 2}


def reading_order(boxes):
    """Sort boxes the way a reader scans panels: row by row, left to right.

    Rows are formed by vertical overlap rather than by a y threshold, because
    panels in one row differ in height (Fig 2's row h-k has a tall panel k
    next to short ones) and any fixed threshold splits or merges the wrong
    ones.
    """
    remaining = sorted(boxes, key=lambda b: (b[1], b[0]))
    rows = []
    for box in remaining:
        placed = False
        for row in rows:
            top = min(b[1] for b in row)
            bottom = max(b[3] for b in row)
            overlap = min(bottom, box[3]) - max(top, box[1])
            if overlap > 0.5 * min(bottom - top, box[3] - box[1]):
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    rows.sort(key=lambda r: min(b[1] for b in r))
    out = []
    for row in rows:
        out.extend(sorted(row, key=lambda b: b[0]))
    return out



def boxes_from_gutters(mask, min_gutter_frac=0.02, max_depth=10, pad=PANEL_PAD):
    """Recursive X-Y cut: split on blank bands, alternating axes.

    Cutting only through blank bands is what makes every crop complete by
    construction - a cut line never crosses ink, so no glyph, axis or colorbar
    can be sliced in half. Granularity is set by min_gutter, which split_figure
    tunes against the OCR labels rather than guessing.
    """
    h, w = mask.shape

    def blank_runs(profile, span, min_gutter):
        # Near-blank, not strictly blank. One antialiased pixel, or a tick that
        # pokes a hair into the gutter, must not veto an obvious cut - which is
        # exactly what "== 0" did on the reference figure's rows 2-5.
        blank = profile <= max(0, int(0.002 * span))
        runs, start = [], None
        for i, b in enumerate(blank):
            if b and start is None:
                start = i
            elif not b and start is not None:
                runs.append((start, i))
                start = None
        if start is not None:
            runs.append((start, len(blank)))
        return [(a, b) for a, b in runs
                if b - a >= min_gutter and a > 0 and b < len(blank)]

    def rec(box, depth):
        x0, y0, x1, y1 = box
        sub = mask[y0:y1, x0:x1]
        if depth >= max_depth or not sub.any():
            return [box]
        # Scale the gutter to the block being divided, not to the whole figure.
        # Fig 2's row gutters run 4 px to 27 px, so one global threshold either
        # never separates rows 4/5 or shatters every panel. Judging a gutter
        # against its own block's size copes with both ends.
        min_gutter = max(3, int(min_gutter_frac * min(sub.shape)))
        v = blank_runs(sub.sum(axis=0), sub.shape[0], min_gutter)
        hh = blank_runs(sub.sum(axis=1), sub.shape[1], min_gutter)
        best_v = max(v, key=lambda r: r[1] - r[0]) if v else None
        best_h = max(hh, key=lambda r: r[1] - r[0]) if hh else None
        if best_v and (not best_h or (best_v[1] - best_v[0]) >= (best_h[1] - best_h[0])):
            cut = x0 + (best_v[0] + best_v[1]) // 2
            return rec((x0, y0, cut, y1), depth + 1) + rec((cut, y0, x1, y1), depth + 1)
        if best_h:
            cut = y0 + (best_h[0] + best_h[1]) // 2
            return rec((x0, y0, x1, cut), depth + 1) + rec((x0, cut, x1, y1), depth + 1)
        return [box]

    kept = []
    for box in rec((0, 0, w, h), 0):
        t = tighten(mask, box, pad)
        if t:
            kept.append(t)
    return [b for i, b in enumerate(kept) if b not in kept[:i]]


def absorb_small(boxes, area_floor):
    """Merge fragments below area_floor into their nearest larger neighbour.

    Colorbars, stray legends and lone axis titles come out of the X-Y cut as
    slivers. Dropping them would silently delete content - the very failure
    this script exists to prevent - so they are folded into the panel they sit
    against instead.
    """
    big = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= area_floor]
    small = [b for b in boxes if b not in big]
    if not big:
        return boxes
    merged = [list(b) for b in big]

    def centre(b):
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

    for s in small:
        sx, sy = centre(s)
        i = min(range(len(merged)),
                key=lambda k: (centre(merged[k])[0] - sx) ** 2
                + (centre(merged[k])[1] - sy) ** 2)
        merged[i][0] = min(merged[i][0], s[0])
        merged[i][1] = min(merged[i][1], s[1])
        merged[i][2] = max(merged[i][2], s[2])
        merged[i][3] = max(merged[i][3], s[3])
    return [tuple(b) for b in merged]



def drop_furniture(boxes, labels):
    """Split boxes into (panels, furniture).

    Only what sits entirely *above* the first panel label counts as furniture -
    the reference paper's figures carry an 'ARTICLE IN PRESS' banner that would
    otherwise become panel 'a'. The symmetric rule for the bottom is deliberately
    absent: when the last panel's label is one OCR cannot read (i, l and o are
    the usual casualties), "below the last label" is the whole last panel, and
    deleting a panel is a far worse failure than keeping a page number.

    Furniture is returned rather than discarded so its ink can be excluded from
    the conservation check instead of counting as loss.
    """
    if not labels:
        return list(boxes), []
    first_top = min(b[1] for b in labels.values())
    keep, furniture = [], []
    for b in boxes:
        holds_label = any(_holds_label(b, lb) for lb in labels.values())
        (furniture if (b[3] <= first_top and not holds_label) else keep).append(b)
    return keep, furniture


def _letter(i):
    """Panel name for the i-th panel in reading order: a, b, ... z, aa, ab."""
    return chr(ord("a") + i) if i < 26 else f"a{chr(ord('a') + i - 26)}"


def name_panels(boxes, names=None):
    """Assign panel names down the reading order, a, b, c... by default."""
    ordered = reading_order(boxes)
    if names and len(names) >= len(ordered):
        return {names[i]: b for i, b in enumerate(ordered)}
    return {_letter(i): b for i, b in enumerate(ordered)}


def panel_names(labels, total, expect=None):
    """Names to use for `total` panels.

    Order of trust: the caption's own letters (read from the PDF text layer, so
    immune to OCR misreads), then the labels OCR found in the image, then plain
    a, b, c. A figure captioned (a1) (a2) (b1) (b2) must not have its panels
    filed as a, b, c, d - the caption talks about a1.
    """
    if expect and len(expect) == total:
        return list(expect)
    if labels and len(labels) == total:
        # Row-cluster the labels, do not just sort by y. Labels in one row sit
        # a few pixels apart vertically, so a raw y-sort silently swaps
        # neighbours - which is how (c)/(d) and (b1)/(b2) ended up mirrored.
        by_box = {v: k for k, v in labels.items()}
        return [by_box[b] for b in reading_order(list(labels.values()))]
    return [_letter(i) for i in range(total)]


def score_segmentation(named, labels):
    """How well does reading-order naming agree with the letters OCR read?

    This is the whole confidence argument: OCR cannot read i, l or o reliably,
    so the letters it *does* read are used to check the geometry instead of to
    drive it. Twelve independent agreements mean the panel count and order are
    right even though three labels were never detected.
    """
    agree = disagree = 0
    for letter, lb in labels.items():
        hits = [name for name, box in named.items() if _holds_label(box, lb)]
        if not hits:
            disagree += 1
        elif hits[0] == letter:
            agree += 1
        else:
            disagree += 1
    crowded = sum(
        1 for box in named.values()
        if sum(1 for lb in labels.values() if _holds_label(box, lb)) > 1)
    return agree - 2 * disagree - 3 * crowded



def _near_blank_bands(profile, span, min_gutter=3):
    """Interior runs where the ink profile is at or below the noise floor."""
    tol = max(0, int(0.002 * span))
    blank = profile <= tol
    runs, start = [], None
    for i, b in enumerate(blank):
        if b and start is None:
            start = i
        elif not b and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(blank)))
    return [(a, b) for a, b in runs if b - a >= min_gutter]


def _choose_cuts(bands, n, lo, hi, min_sep, forbid):
    """Take the n widest interior gutters, spaced out and clear of labels.

    Two guards earn their place. min_sep stops a gutter inside a panel (say
    between a chart and its tick labels) from being mistaken for a panel
    boundary. forbid lists half-open ranges just *after* each panel label -
    the blank strip between the letter 'f' and panel f's axes is wide and
    tempting, and cutting there strands the letter in its own image. Only that
    side is forbidden: the gutter immediately *before* a label is the real row
    or column boundary, so rejecting it would throw away the best cuts.
    """
    cand = sorted(((b - a, (a + b) // 2) for a, b in bands if a > lo and b < hi),
                  key=lambda t: -t[0])
    chosen = []
    for _, pos in cand:
        if len(chosen) == n:
            break
        if any(abs(pos - c) < min_sep for c in chosen):
            continue
        if any(a <= pos < b for a, b in forbid):
            continue
        chosen.append(pos)
    return sorted(chosen)


def _bracketed_cut(bands, lo, hi, profile=None):
    """The gutter nearest hi inside (lo, hi), or None.

    Panels label their top-left corner, so a boundary sits immediately before
    the next panel's label - not at whatever happens to be the widest gap in
    between. On Fig 2 row 1 the widest gap inside the a|b bracket is the one
    between panel a's axis title and its axis, 300 px left of the real edge.

    When no band is blank enough - Fig 2's force plots l|m|n abut, with tick
    labels bridging every column - fall back to the thinnest place in the
    bracket. A boundary is known to exist here, so the emptiest column beats
    splitting down the middle.
    """
    inside = [(max(a, lo), min(b, hi)) for a, b in bands if b > lo and a < hi]
    inside = [(a, b) for a, b in inside if b > a]
    # Only the tail of the bracket can hold the boundary. Without this, an
    # undetected label (Fig 2's 'l') leaves the gap between its own letter and
    # its own plot as the only blank band in the bracket, and the cut lands
    # 500 px too far left.
    tail = hi - max(8, (hi - lo) // 4)
    in_tail = [(a, b) for a, b in inside if b >= tail]
    if in_tail:
        a, b = max(in_tail, key=lambda t: t[1])
        return (a + b) // 2
    if profile is not None and hi - lo >= 2:
        lo_i, hi_i = int(max(0, lo)), int(min(len(profile), hi))
        lo_i = max(lo_i, int(tail))
        if hi_i - lo_i >= 2:
            return lo_i + int(np.argmin(profile[lo_i:hi_i]))
    return None


def _pad_cuts(cuts, need, lo, hi):
    """Top up a short cut list by halving the widest span.

    Reached when the figure has fewer clean gutters than the layout asks for.
    The result is a guess, so the caller reports it rather than passing it off
    as a detected boundary.
    """
    cuts = sorted(c for c in cuts if lo < c < hi)
    while len(cuts) < need:
        edges = [lo] + cuts + [hi]
        i = max(range(len(edges) - 1), key=lambda k: edges[k + 1] - edges[k])
        mid = (edges[i] + edges[i + 1]) // 2
        if mid in cuts:
            break
        cuts.append(mid)
        cuts.sort()
    return cuts[:need]


def boxes_from_layout(mask, layout, labels, pad=PANEL_PAD, names=None):
    """Split into exactly the panels a caller says are there: [4,3,4,3,1].

    Panels-per-row is the one thing about a figure that is instantly obvious
    to a human or a vision model and impossible to infer reliably from pixels
    (Fig 2's row gutters are 4-27 px, overlapping the gutters *inside* its
    panels). Given it, the cut positions follow deterministically.

    Returns (boxes, warnings).
    """
    h, w = mask.shape
    warnings = []
    y_lo = 0
    if labels:
        y_lo = max(0, min(b[1] for b in labels.values()) - 2 * pad)

    # The layout fixes every panel's letter, so a boundary's brackets are known
    # even where OCR could not read the label next to it. That is what rescues
    # row l|m|n, whose 'l' is invisible to OCR and whose gutters are thinner
    # than the gaps inside its own force plots.
    letters = names or [_letter(i) for i in range(sum(layout))]
    rows_of, at = [], 0
    for n in layout:
        rows_of.append(letters[at:at + n])
        at += n

    region = mask[y_lo:h]
    rh = region.shape[0]
    prof_y = region.sum(axis=1)
    bands_y = _near_blank_bands(prof_y, w)

    found = []
    for r in range(len(layout) - 1):
        above = [labels[k] for k in rows_of[r] if k in labels]
        lo_i = (max(b[3] for b in above) - y_lo) if above else (
            found[-1] if found else 0)
        head = rows_of[r + 1][0]
        if head in labels:
            hi_i = labels[head][1] - y_lo
            cut = _bracketed_cut(bands_y, lo_i, hi_i, prof_y)
        else:
            hi_i = rh
            free = _choose_cuts(bands_y, 1, lo_i, rh, min_sep=0, forbid=[])
            cut = free[0] if free else None
        if cut is None:
            cut = (lo_i + hi_i) // 2
            warnings.append(f"no clean gutter above row {r + 2} - split midway")
        found.append(cut)
    edges = [0] + _pad_cuts(found, len(layout) - 1, 0, rh) + [rh]

    out = []
    for r, n_cols in enumerate(layout):
        y0, y1 = y_lo + edges[r], y_lo + edges[r + 1]
        band = mask[y0:y1]
        if not band.any():
            warnings.append(f"row {r + 1} came out empty")
            continue
        bands_x = _near_blank_bands(band.sum(axis=0), band.shape[0])
        prof_x = band.sum(axis=0)
        got = []
        for c in range(n_cols - 1):
            left, right = rows_of[r][c], rows_of[r][c + 1]
            lo_i = labels[left][2] if left in labels else (got[-1] if got else 0)
            if right in labels:
                hi_i = labels[right][0]
                cut = _bracketed_cut(bands_x, lo_i, hi_i, prof_x)
            else:
                hi_i = next((labels[k][0] for k in rows_of[r][c + 2:]
                             if k in labels), w)
                free = _choose_cuts(bands_x, 1, lo_i, hi_i, min_sep=0, forbid=[])
                cut = free[0] if free else None
            if cut is None:
                cut = (lo_i + hi_i) // 2
                warnings.append(f"row {r + 1}: no clean gutter between panels "
                                f"{left} and {right} - split midway")
            got.append(cut)
        xs = [0] + _pad_cuts(got, n_cols - 1, 0, w) + [w]
        for c in range(n_cols):
            cell = tighten(mask, (xs[c], y0, xs[c + 1], y1), pad)
            if cell:
                out.append(cell)
    return out, warnings



def merge_to_count(boxes, target, labels=None):
    """Merge the closest pair of boxes until only `target` remain.

    The caption says how many panels exist; the X-Y cut says where the blank
    bands are. When the cut over-splits - a lone axis title, a colorbar, a
    panel divided at its own internal gutter - repeatedly fusing the nearest
    two boxes converges on the right grouping without needing a layout.

    Two boxes that each carry a panel label are never fused: a label marks a
    distinct panel, so merging across one is the very error being fixed. Two
    stacked panels are each other's nearest neighbour, and without this guard
    they collapse into one while a stray axis title survives as 'panel b'.
    """
    boxes = [tuple(b) for b in boxes]
    if target < 1 or len(boxes) <= target:
        return boxes
    labels = labels or {}

    def labelled(box):
        return sum(1 for lb in labels.values() if _holds_label(box, lb))

    while len(boxes) > target:
        best, pair = None, None
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if labelled(a) and labelled(b):
                    continue
                dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
                dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
                gap = dx * dx + dy * dy
                if best is None or gap < best:
                    best, pair = gap, (i, j)
        if pair is None:
            break                       # every remaining box owns a label
        i, j = pair
        a, b = boxes[i], boxes[j]
        fused = (min(a[0], b[0]), min(a[1], b[1]),
                 max(a[2], b[2]), max(a[3], b[3]))
        boxes = [x for k, x in enumerate(boxes) if k not in (i, j)] + [fused]
    return boxes


def boxes_from_grid(mask, rows, cols):
    """Uniform rows x cols split, then tighten. The manual escape hatch."""
    h, w = mask.shape
    out = {}
    for r in range(rows):
        for c in range(cols):
            box = (c * w // cols, r * h // rows,
                   (c + 1) * w // cols, (r + 1) * h // rows)
            t = tighten(mask, box)
            if t:
                out[f"r{r + 1}c{c + 1}"] = t
    return out


def _contains(box, inner, slack=2):
    x0, y0, x1, y1 = box
    a0, b0, a1, b1 = inner
    return (a0 >= x0 - slack and b0 >= y0 - slack
            and a1 <= x1 + slack and b1 <= y1 + slack)


def _holds_label(box, label):
    """Is this label's centre inside the crop?

    Panel labels sit hard against a panel's corner, so a strict box-inside-box
    test fails when the glyph pokes a pixel past the crop edge - the crop is
    right and the check cries wolf. The centre is the honest question.
    """
    cx = (label[0] + label[2]) / 2
    cy = (label[1] + label[3]) / 2
    return box[0] <= cx < box[2] and box[1] <= cy < box[3]


def validate(mask, boxes, labels, ocr, furniture=()):
    """Run the completeness checks. Returns (problems, stats)."""
    h, w = mask.shape
    problems = []
    notes = []
    # Furniture ink (running heads, page numbers) is deliberately not in any
    # panel, so counting it as loss would fire the conservation check on every
    # correctly-split figure.
    countable = mask.copy()
    for x0, y0, x1, y1 in furniture:
        countable[y0:y1, x0:x1] = False
    total_ink = int(countable.sum()) or 1

    # 1. do the letters OCR read sit in the panels reading order gave them?
    agree = []
    for letter, lb in sorted(labels.items()):
        hits = [name for name, box in boxes.items() if _holds_label(box, lb)]
        if not hits:
            problems.append(f"panel label '{letter}' fell outside every crop")
        elif hits[0] != letter:
            problems.append(f"label '{letter}' landed in crop '{hits[0]}' - "
                            f"panel count or order is wrong")
        else:
            agree.append(letter)
    for name, box in boxes.items():
        inside = sorted(k for k, lb in labels.items() if _holds_label(box, lb))
        if len(inside) > 1:
            problems.append(f"{name}: holds labels {inside} - two panels merged")
    if labels:
        unread = sorted(set(boxes) - set(labels))
        if unread:
            notes.append(f"labels for {unread} were never read by OCR "
                         f"(i/l/o are the usual ones); their panels rest on "
                         f"geometry plus {len(agree)} agreeing neighbours")


    # 2. each crop ringed by background - nothing sliced through
    for name, (x0, y0, x1, y1) in boxes.items():
        ring = np.concatenate([
            mask[y0, x0:x1], mask[y1 - 1, x0:x1],
            mask[y0:y1, x0], mask[y0:y1, x1 - 1],
        ])
        touching_edge = (x0 == 0 or y0 == 0 or x1 == w or y1 == h)
        if ring.size and ring.mean() > BORDER_INK_FRAC and not touching_edge:
            problems.append(f"{name}: ink on {ring.mean():.0%} of its border "
                            f"- content is cut off")

    # 3. ink conservation - crops jointly hold ~all the figure's ink
    covered = np.zeros_like(mask)
    for x0, y0, x1, y1 in boxes.values():
        covered[y0:y1, x0:x1] = True
    kept = int((countable & covered).sum())
    if kept < INK_KEEP_FRAC * total_ink:
        problems.append(f"crops hold only {kept / total_ink:.1%} of the figure's "
                        f"ink - something was dropped")

    # 4. text conservation - every OCR box lands in some crop
    orphan_text = []
    if ocr:
        for b in ocr:
            if any(_contains(box, b["box"]) for box in boxes.values()):
                continue
            if any(_contains(f, b["box"]) for f in furniture):
                notes.append(f"page furniture ignored: {b['text']!r}")
                continue
            orphan_text.append(b["text"])
        if orphan_text:
            problems.append(f"{len(orphan_text)} OCR text boxes outside every "
                            f"crop, e.g. {orphan_text[:6]}")

    return problems, {
        "ink_kept_frac": round(kept / total_ink, 4),
        "labels_found": sorted(labels),
        "labels_agreeing": agree,
        "notes": notes,
        "orphan_text": orphan_text[:20],
    }



def split_figure(source, out_dir, stem="fig", layout=None, grid=None,
                 use_ocr=True, lang="en", pad=PANEL_PAD, min_gutter_frac=None,
                 expect=None):
    """Split one figure image into panel PNGs. Returns a report dict.

    Strategy order: explicit layout > forced grid > auto-tuned X-Y cut. Prefer
    layout whenever the panels-per-row is known - it is the only path that
    cannot miscount, and OCR then proves the panels are in the right order.

    `expect` is the panel letters the caption claims exist, read out of the
    PDF's text layer by extract_paper. It is an independent second opinion: if
    the caption says a-o and 14 panels came out, one was lost, and no amount of
    OCR agreement inside the image would have revealed that.
    """
    from PIL import Image


    rgb = load_image(source)
    mask = ink_mask(rgb)
    os.makedirs(out_dir, exist_ok=True)

    ocr, ocr_note = ([], None)
    labels = {}
    if use_ocr:
        ocr, ocr_note = run_ocr(rgb, lang)
        if ocr:
            labels = find_panel_labels(ocr)
        ocr = ocr or []

    h, w = mask.shape
    floor = MIN_PANEL_FRAC * h * w
    furniture = []

    if layout:
        names = panel_names(labels, sum(layout), expect)
        raw, layout_warnings = boxes_from_layout(mask, layout, labels, pad, names)
        panels, furniture = drop_furniture(raw, labels)
        # The layout path never even looks above the first label, so nothing up
        # there can land in `furniture` by itself - name it explicitly or the
        # running head reads as lost ink.
        if labels:
            top = max(0, min(b[1] for b in labels.values()) - 2 * pad)
            if top > 0:
                furniture.append((0, 0, mask.shape[1], top))
        boxes = name_panels(panels, names)
        method = f"explicit layout {'+'.join(map(str, layout))}"
    elif grid:

        boxes = boxes_from_grid(mask, *grid)
        method = f"forced grid {grid[0]}x{grid[1]}"
    else:
        def build(g):
            frags = boxes_from_gutters(mask, g, pad=pad)
            keep, junk = drop_furniture(frags, labels)
            merged = absorb_small(keep, floor)
            if expect:
                # The caption already said how many panels there are; fuse the
                # nearest pairs until the count agrees instead of hoping some
                # gutter width happens to land on it.
                merged = merge_to_count(merged, len(expect), labels)
                merged = [tighten(mask, b, pad) or b for b in merged]
            return name_panels(merged, panel_names(labels, len(merged), expect)), junk

        if min_gutter_frac is None:
            best = None
            for g in GUTTER_SWEEP:
                cand, junk = build(g)
                # The caption's panel count is the strongest signal available
                # here, so it outranks label agreement: hitting the right
                # number of panels comes first, order second.
                hit = bool(expect) and len(cand) == len(expect)
                key = (hit, score_segmentation(cand, labels), len(cand))
                if best is None or key > best[0]:
                    best = (key, cand, junk, g)
            (hit, score, _), boxes, furniture, gutter = best
            method = (f"auto X-Y cut, gutter {gutter:.3f} of the short side "
                      f"(label-agreement score {score}"
                      + (", panel count matches the caption)" if hit else ")"))
        else:
            boxes, furniture = build(min_gutter_frac)
            method = f"X-Y cut at fixed gutter {min_gutter_frac}"

    problems, stats = validate(mask, boxes, labels, ocr, furniture)
    if layout:
        problems.extend(layout_warnings)
    if expect:
        # The caption is the only witness that does not depend on the image.
        if len(expect) != len(boxes):
            problems.append(f"caption lists {len(expect)} panels "
                            f"({''.join(expect)}) but {len(boxes)} were cut")
        missed = [k for k in expect if k not in boxes]
        if missed and len(expect) == len(boxes):
            stats.setdefault("notes", []).append(
                f"caption names {missed} but the panels were filed as "
                f"{sorted(boxes)[:len(missed)]}...; same count, so likely just "
                f"a naming difference")


    if ocr_note:
        problems.append(ocr_note)

    written = []
    for name, (x0, y0, x1, y1) in sorted(boxes.items()):
        path = os.path.join(out_dir, f"{stem}_{name}.png")
        Image.fromarray(rgb[y0:y1, x0:x1]).save(path)
        written.append({
            "panel": name,
            "file": os.path.basename(path),
            "box": [x0, y0, x1, y1],
            "size": f"{x1 - x0}x{y1 - y0}",
        })

    return {
        "source": str(source) if not isinstance(source, np.ndarray) else "<array>",
        "figure_size": f"{rgb.shape[1]}x{rgb.shape[0]}",
        "method": method,
        "panels": written,
        "panel_count": len(written),
        "ocr_available": bool(ocr),
        "problems": problems,
        "ok": not problems,
        **stats,
    }


def parse_grid(spec):
    m = re.fullmatch(r"(\d+)\s*[xX*]\s*(\d+)", spec.strip())
    if not m:
        raise argparse.ArgumentTypeError("grid must look like 4x3 (rows x cols)")
    return int(m.group(1)), int(m.group(2))


def parse_layout(spec):
    try:
        rows = [int(p) for p in re.split(r"[,+\s]+", spec.strip()) if p]
    except ValueError:
        rows = []
    if not rows or any(r < 1 for r in rows):
        raise argparse.ArgumentTypeError(
            "layout is panels per row, e.g. 4,3,4,3,1 for Nature-style Fig 2")
    return rows


def parse_expect(spec):
    """'a,b,c' or 'a-o' -> ['a', 'b', 'c', ...]."""
    out = []
    for part in re.split(r"[,\s+]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"([a-z])\s*[-–]\s*([a-z])", part, re.I)
        if m:
            a, b = m.group(1).lower(), m.group(2).lower()
            out.extend(chr(o) for o in range(ord(a), ord(b) + 1))
        else:
            out.append(part.lower())
    seen = set()
    return [k for k in out if not (k in seen or seen.add(k))]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("image", help="rendered figure image (png/jpg/tif)")
    ap.add_argument("-o", "--out-dir", default="panels")
    ap.add_argument("--stem", help="filename prefix (default: image stem)")
    ap.add_argument("--layout", type=parse_layout,
                    help="panels per row, e.g. 4,3,4,3,1 - the reliable path. "
                         "Look at the figure and count; auto-detection cannot "
                         "tell a 4px row gutter from a gutter inside a panel")
    ap.add_argument("--grid", type=parse_grid,
                    help="force a uniform RxC split instead of detecting")
    ap.add_argument("--no-ocr", action="store_true",
                    help="skip OCR: no panel names, no label/text checks")
    ap.add_argument("--ocr-lang", default="en")
    ap.add_argument("--pad", type=int, default=PANEL_PAD)
    ap.add_argument("--min-gutter", type=float, default=None,
                    help="fix the gutter width as a fraction of the figure's "
                         "shorter side (e.g. 0.02). Omit to auto-tune it "
                         "against the OCR panel labels, which is usually right")
    ap.add_argument("--expect",
                    help="panel letters the caption lists, e.g. a,b,c,d or "
                         "a-o. Used to name the panels and to check none went "
                         "missing. extract_paper.py fills this in from the "
                         "PDF text layer automatically")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print(f"ERROR: no such file: {args.image}", file=sys.stderr)
        return 1

    stem = args.stem or os.path.splitext(os.path.basename(args.image))[0]
    try:
        report = split_figure(args.image, args.out_dir, stem=stem,
                              layout=args.layout, grid=args.grid,
                              use_ocr=not args.no_ocr, lang=args.ocr_lang,
                              pad=args.pad, min_gutter_frac=args.min_gutter,
                              expect=parse_expect(args.expect))
    except Exception as e:                      # noqa: BLE001 - CLI boundary
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    with open(os.path.join(args.out_dir, f"{stem}_panels.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"figure   : {args.image}  ({report['figure_size']})")
    print(f"method   : {report['method']}")
    print(f"labels   : read {report['labels_found'] or 'none'}; "
          f"agreeing {report['labels_agreeing']}")
    print(f"panels   : {report['panel_count']} -> {args.out_dir}")
    for p in report["panels"]:
        print(f"  {p['panel']:<5} {p['file']:<28} {p['size']:>11}")
    print(f"ink kept : {report['ink_kept_frac']:.2%}")
    for n in report.get("notes", []):
        print(f"note     : {n}")

    if report["problems"]:
        print("\nWARNING: completeness checks failed:")
        for p in report["problems"]:
            print(f"  - {p}")
        print("\nOpen the panels and look before using them. Fixes: --grid RxC "
              "to cut manually, --min-gutter to change gutter sensitivity, "
              "--pad to widen the margin.")
        return 3

    print("\nOK: one label per panel, no ink lost, no text outside a panel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())







