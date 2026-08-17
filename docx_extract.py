#!/usr/bin/env python
"""Read a PDF-converted DOCX: body text in order, plus where each figure sits.

This is the fast path into a translation. A PDF-to-DOCX converter has already
done two things that are expensive to do from the PDF directly:

    - the figures are embedded as images, ready to pull out of word/media/
    - they sit at their real position in the text flow, so the translation can
      put each figure back where the original had it instead of guessing from
      the first "as shown in Fig. 3" mention

The DOCX is scaffolding, not the deliverable. What comes out of here is an
ordered stream of paragraphs and figure anchors that a translation is written
against; the final output is still Markdown + PDF + a figures folder.

Text inside floating text boxes counts. Word's PDF importer puts most running
text in them and splits words across boxes ("ScienceDirec" + "t"), so
fragments are stitched back together rather than joined with spaces.

Usage:
    python docx_extract.py <docx> [-o OUTDIR]

Outputs into OUTDIR (default: <docx_stem>_docx next to the file):
    content.md     paragraphs in order, with [[FIG n -> file]] anchors inline
    content.json   the same as structured records
    media/         every embedded image, renamed figNN.ext where identifiable
    manifest.json  figure/caption/section census + completeness check

Exit codes:
    0  text and figures extracted, figure count consistent with the captions
    3  extracted, but the counts disagree - LOOK BEFORE TRANSLATING
    1  error
"""

import argparse
import json
import os
import re
import sys
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def iter_live(node):
    """Depth-first walk that skips mc:Fallback subtrees.

    Word writes every text box twice - once as DrawingML under mc:Choice and
    once as legacy VML under mc:Fallback. Walking both duplicated every
    paragraph and every figure ("Contents lists available at ScienceDirect"
    appeared twice, and Fig 1 was emitted twice before the title).
    """
    if node.tag == f"{MC}Fallback":
        return
    yield node
    for child in node:
        yield from iter_live(child)


CAPTION_START_RE = re.compile(
    r"^\**\s*(?:Supplementary\s+|Extended\s+Data\s+)?"
    r"Fig(?:ure)?\.?\s*(\d{1,2})\s*[.|:｜]", re.I)
REFERENCE_RE = re.compile(
    r"(Supplementary|Supp\.|Extended\s+Data|SI)?\s*\bFig(?:ure)?s?\.?\s*(\d{1,2})",
    re.I)
# Headings whose section is not translated.
SKIP_SECTION_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(References|Bibliography|Acknowledge?ments?|"
    r"Declaration of competing interest|CRediT|Data availability|"
    r"Funding|Appendix|Supplementary (data|material))\b", re.I)


def image_rels(zf):
    """rId -> media path, from word/_rels/document.xml.rels."""
    from lxml import etree

    out = {}
    try:
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
    except KeyError:
        return out
    ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    for rel in rels.findall(f"{ns}Relationship"):
        target = rel.get("Target", "")
        if "media/" in target:
            out[rel.get("Id")] = "word/" + target.lstrip("/").replace("../", "")
    return out


def paragraph_text(node):
    """All text under a node, in document order, stitched not space-joined.

    Word's importer splits a word across text boxes, so inserting a separator
    between fragments would turn "ScienceDirect" into "ScienceDirec t". The
    fragments are concatenated verbatim and whitespace is collapsed afterwards;
    explicit breaks (<w:br>, <w:tab>) contribute a single space.
    """
    parts = []
    for el in iter_live(node):
        tag = el.tag
        if tag == f"{W}t":
            parts.append(el.text or "")
        elif tag in (f"{W}br", f"{W}tab"):
            parts.append(" ")
    text = "".join(parts)
    text = text.replace("­", "")           # soft hyphen from hyphenation
    text = re.sub(r"(\w)-\s+(?=[a-z])", r"\1", text)   # re-join "measure- ment"
    return " ".join(text.split())


def walk_body(zf):
    """Ordered stream of ('text', str) and ('image', media_path) records."""
    from lxml import etree

    rels = image_rels(zf)
    root = etree.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{W}body")
    stream = []
    if body is None:
        return stream
    seen_media = set()
    for block in body:
        if block.tag not in (f"{W}p", f"{W}tbl"):
            continue
        # Images first: a paragraph that carries a drawing is an anchor point,
        # and its own text (if any) belongs after the figure in reading order
        # only when the drawing precedes it - close enough at this resolution.
        for el in iter_live(block):
            if el.tag != f"{A}blip":
                continue
            rid = el.get(f"{R}embed") or el.get(f"{R}link")
            media = rels.get(rid)
            if media and media not in seen_media:
                seen_media.add(media)
                stream.append(("image", media))
        text = paragraph_text(block)
        if text:
            stream.append(("text", text))
    return stream


def classify(stream):
    """Tag each text record: heading, caption, body, or skip.

    'skip' is everything from a References/Acknowledgements-style heading to
    the end of that section - the user does not want those translated, and
    mistranslating a reference list is worse than leaving it in English.
    """
    records, skipping = [], False
    for kind, value in stream:
        if kind == "image":
            records.append({"kind": "image", "media": value})
            continue
        cap = CAPTION_START_RE.match(value)
        if SKIP_SECTION_RE.match(value):
            skipping = True
            records.append({"kind": "heading", "text": value, "translate": False,
                            "section": "skip"})
            continue
        if cap:
            # Captions are translated. They carry the real explanation of a
            # figure - a 15-panel figure's caption runs several hundred words -
            # so leaving them in English hides most of the figure's meaning.
            records.append({"kind": "caption", "text": value, "translate": True,
                            "figure": int(cap.group(1))})
            continue
        if skipping:
            records.append({"kind": "text", "text": value, "translate": False,
                            "section": "skip"})
            continue
        short_heading = len(value) <= 60 and not value.endswith((".", "。", ";"))
        records.append({
            "kind": "heading" if short_heading else "text",
            "text": value,
            "translate": True,
        })
    return records


def image_sizes(zf, records):
    """Pixel size of every referenced image, for telling figures from logos."""
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError:
        return {}
    sizes = {}
    for rec in records:
        if rec["kind"] != "image" or rec["media"] in sizes:
            continue
        try:
            with Image.open(BytesIO(zf.read(rec["media"]))) as im:
                sizes[rec["media"]] = max(im.size)
        except Exception:                       # noqa: BLE001
            sizes[rec["media"]] = 0
    return sizes


def number_figures(zf, records):
    """Pair real figures with caption numbers, in document order.

    Two things had to be right here. Publisher furniture has to go first - the
    Elsevier logo and the journal badge come through as images and, being at
    the top of the file, they otherwise claim "Fig 1" and "Fig 2" and push
    every real figure off by two. And the pairing has to be positional, not
    nearest-caption: Word anchors a floating figure to whatever paragraph
    happened to be nearby, so the distance from an image to its own caption in
    the XML says nothing, while the *order* of the figures still holds.
    """
    sizes = image_sizes(zf, records)
    long_sides = sorted(v for v in sizes.values() if v)
    if long_sides:
        median = long_sides[len(long_sides) // 2]
        floor = max(400, int(0.25 * median))
    else:
        floor = 0

    images = [r for r in records if r["kind"] == "image"]
    for rec in images:
        if sizes.get(rec["media"], 0) < floor:
            rec["furniture"] = True

    figures = [r for r in images if not rec_is_furniture(r)]
    captions = sorted({r["figure"] for r in records if r["kind"] == "caption"})
    for rec, num in zip(figures, captions):
        rec["figure"] = num
    return records


def rec_is_furniture(rec):
    return bool(rec.get("furniture"))


def save_media(zf, records, out_dir):
    """Write every referenced image out, named by figure number where known."""
    media_dir = os.path.join(out_dir, "media")
    os.makedirs(media_dir, exist_ok=True)
    seen, seq = {}, 0
    for rec in records:
        if rec["kind"] != "image":
            continue
        src = rec["media"]
        if src in seen:
            rec["file"] = seen[src]
            continue
        ext = os.path.splitext(src)[1] or ".png"
        num = rec.get("figure")
        seq += 1
        if rec.get("furniture"):
            name = f"logo{seq:02d}{ext}"
        elif num:
            name = f"fig{num:02d}{ext}"
        else:
            name = f"img{seq:02d}{ext}"
        while os.path.exists(os.path.join(media_dir, name)):
            seq += 1
            name = f"{os.path.splitext(name)[0]}_{seq}{ext}"
        try:
            with open(os.path.join(media_dir, name), "wb") as f:
                f.write(zf.read(src))
        except KeyError:
            continue
        rec["file"] = f"media/{name}"
        seen[src] = rec["file"]
    return records


def render_markdown(records):
    """Ordered text with figure anchors, for the translator to work against."""
    lines = []
    for rec in records:
        if rec["kind"] == "image":
            if rec.get("furniture"):
                continue        # publisher logo, not a figure
            num = rec.get("figure")
            lines.append(f"[[FIG {num if num else '?'} -> "
                         f"{rec.get('file', rec['media'])}]]")
        elif rec["kind"] == "caption":
            lines.append(f"**CAPTION Fig {rec['figure']}** {rec['text']}")
        elif rec["kind"] == "heading":
            mark = "" if rec.get("translate", True) else "   <!-- 不翻译 -->"
            lines.append(f"## {rec['text']}{mark}")
        else:
            mark = "   <!-- 不翻译 -->" if not rec.get("translate", True) else ""
            lines.append(rec["text"] + mark)
    return "\n\n".join(lines) + "\n"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("docx")
    ap.add_argument("-o", "--out-dir")
    args = ap.parse_args()

    if not os.path.isfile(args.docx):
        print(f"ERROR: no such file: {args.docx}", file=sys.stderr)
        return 1
    try:
        from lxml import etree  # noqa: F401
    except ImportError:
        print("ERROR: lxml missing. Run: pip install lxml", file=sys.stderr)
        return 1

    stem = os.path.splitext(os.path.basename(args.docx))[0]
    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.docx)), f"{stem}_docx")
    os.makedirs(out_dir, exist_ok=True)

    with zipfile.ZipFile(args.docx) as zf:
        records = save_media(zf, number_figures(zf, classify(walk_body(zf))), out_dir)

    with open(os.path.join(out_dir, "content.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(records))
    with open(os.path.join(out_dir, "content.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    images = [r for r in records if r["kind"] == "image"]
    figures_only = [r for r in images if not r.get("furniture")]
    captions = sorted({r["figure"] for r in records if r["kind"] == "caption"})
    body = [r for r in records if r["kind"] in ("text", "heading")
            and r.get("translate", True)]
    skipped = [r for r in records if not r.get("translate", True)]
    chars = sum(len(r["text"]) for r in body)
    referenced = sorted({int(n) for pre, n in REFERENCE_RE.findall(
        " ".join(r["text"] for r in records if "text" in r))
        if not pre.strip() and 1 <= int(n) <= 30})

    numbered = sorted({r["figure"] for r in images if r.get("figure")})
    consistent = not captions or numbered == captions

    manifest = {
        "docx": os.path.abspath(args.docx),
        "out_dir": os.path.abspath(out_dir),
        "records": len(records),
        "images": len(images),
        "images_numbered": numbered,
        "captions": captions,
        "figures_referenced_in_text": referenced,
        "translatable_paragraphs": len(body),
        "translatable_chars": chars,
        "skipped_paragraphs": len(skipped),
        "consistent": consistent,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"docx        : {os.path.basename(args.docx)}")
    print(f"out         : {out_dir}")
    print(f"paragraphs  : {len(body)} to translate "
          f"({chars} chars), {len(skipped)} skipped")
    print(f"images      : {len(images)} -> {out_dir}\\media")
    print(f"captions    : Fig {captions or 'none found'}")
    print(f"images -> Fig: {numbered or 'unnumbered'}")
    print(f"referenced  : Fig {referenced or 'none'}")
    print(f"content     : {os.path.join(out_dir, 'content.md')}")

    if not consistent:
        print(f"\nWARNING: captions say Fig {captions} but the images map to "
              f"{numbered}.")
        print("Open content.md, check each [[FIG n]] anchor against the "
              "captions, and fix the")
        print("numbering by hand before translating.")
        return 3
    print("\nOK: every figure is numbered and sits where the original had it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())




