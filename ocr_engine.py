#!/usr/bin/env python
"""One OCR interface, two backends: RapidOCR first, PaddleOCR as fallback.

Both backends run the SAME model weights - RapidOCR ships PaddleOCR's PP-OCR
models converted to ONNX (the copyright notice in RapidOCR's README says as
much). So this is not a choice between two levels of accuracy; it is a choice
between two ways of running one model, and RapidOCR's way is measurably
cheaper. Measured on 6 real paper figures, same machine, same PP-OCRv6_medium
weights on both sides:

    PaddleOCR  startup 7.4s   16.2s per page   104.7s for 6 pages
    RapidOCR   startup 1.0s    2.0s per page    13.2s for 6 pages   7.9x
    RapidOCR   (PP-OCRv6 small, the default here)  5.6s for 6 pages 18.7x

Recognition quality came out even, each side winning some boxes - so speed and
install size are the whole reason for the preference, not accuracy.

Why small rather than medium is the default: on the one job this skill cannot
do without - reading the (a)/(b)/(c) panel labels panel_split.py checks the
split against - small matched PaddleOCR on all 6 figures, while medium misread
one figure's (b) as (q). A bigger model is not automatically better at picking
single letters out of a plot.

PaddleOCR stays as a fallback because neither backend wins everywhere: the
rotated axis labels each engine fumbles are not the same ones.

Interface, deliberately narrow - it is all the two call sites need:

    engine, backend, note = make_engine(lang="en")
    boxes = read(engine, rgb)     # [{'text', 'box': (x0,y0,x1,y1), 'score'}]

`rgb` is RGB uint8 throughout; each backend converts internally to whatever it
wants, so no call site has to remember which one needs BGR.
"""

import sys

BACKEND_RAPID = "RapidOCR"
BACKEND_PADDLE = "PaddleOCR"

INSTALL_RAPID = ("pip install --no-deps rapidocr\n"
                 "    pip install onnxruntime shapely pyclipper omegaconf colorlog")
INSTALL_PADDLE = "pip install paddlepaddle paddleocr"

# RapidOCR's PP-OCRv6 recognition model is multilingual, so the Latin-script
# languages all resolve to the same weights. Only scripts that need their own
# rec model are mapped; anything unlisted falls through to the default.
_RAPID_LANG = {
    "en": None, "ch": None, "chinese_cht": "chinese_cht",
    "japan": "japan", "korean": "korean", "arabic": "arabic",
    "cyrillic": "cyrillic", "devanagari": "devanagari", "latin": "latin",
    "ta": "ta", "te": "te", "th": "th", "el": "el", "ka": "ka",
    "eslav": "eslav",
}


def available():
    """Which backends can be imported, in preference order. No engine built.

    Uses find_spec rather than import: PaddleOCR pulls in paddle and takes
    seconds, which a capability probe must not pay.
    """
    import importlib.util

    def spec(name):
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    out = []
    if spec("rapidocr"):
        out.append(BACKEND_RAPID)
    if spec("paddleocr") and spec("paddle"):
        out.append(BACKEND_PADDLE)
    return out


def make_engine(lang="en", prefer=None, model_type="small", quiet=True):
    """Build the best available OCR engine.

    Returns (engine, backend_name, note). engine is None when no backend is
    installed; note carries anything the caller should print - a fallback that
    happened, or the install hint when nothing is there.

    prefer forces a backend by name, for A/B comparison. model_type applies to
    RapidOCR only: tiny | small | medium.
    """
    order = available()
    if prefer:
        order = [b for b in order if b == prefer] or []

    notes = []
    for backend in order:
        builder = _build_rapid if backend == BACKEND_RAPID else _build_paddle
        try:
            engine = builder(lang, model_type, quiet)
        except Exception as e:                  # noqa: BLE001 - try the next one
            notes.append(f"{backend} failed to start ({e})")
            continue
        note = "; ".join(notes) + (" - fell back" if notes else "")
        return engine, backend, note.strip("; ").strip() or None

    if prefer:
        hint = (f"{prefer} is not installed. "
                f"{INSTALL_RAPID if prefer == BACKEND_RAPID else INSTALL_PADDLE}")
    else:
        hint = ("No OCR backend installed. RapidOCR is the cheaper one "
                f"(~40MB here):\n    {INSTALL_RAPID}\n"
                f"  or PaddleOCR (~1GB):\n    {INSTALL_PADDLE}")
    return None, None, "; ".join(notes + [hint])


def _build_rapid(lang, model_type, quiet):
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType

    params = {"Global.log_level": "error"} if quiet else {}
    if model_type:
        mt = ModelType(model_type)
        params["Det.model_type"] = mt
        params["Rec.model_type"] = mt
    rec_lang = _RAPID_LANG.get((lang or "").lower(), None)
    if rec_lang:
        params["Rec.lang_type"] = LangRec(rec_lang)
    return RapidOCR(params=params or None)


def _build_paddle(lang, model_type, quiet):
    """PaddleOCR 3.x. The 2.x keywords (use_angle_cls, show_log) are gone.

    enable_mkldnn=False is required, not cosmetic: the oneDNN backend in some
    paddlepaddle builds dies with
      NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
    before a single page is read.
    """
    from paddleocr import PaddleOCR

    return PaddleOCR(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        lang=lang,
        enable_mkldnn=False,
    )


def read(engine, rgb):
    """OCR one RGB image. Returns [{'text', 'box', 'score'}], box axis-aligned.

    Empty list for an image with no text - that is a real answer, not an error.
    Raises whatever the backend raises; callers decide whether a failed page is
    fatal.
    """
    if engine is None:
        return []
    if _is_rapid(engine):
        out = engine(rgb)
        texts = list(out.txts or [])
        polys = out.boxes if out.boxes is not None else []
        scores = list(out.scores or [])
    else:
        res = engine.predict(rgb[:, :, ::-1])       # PaddleOCR wants BGR
        page = res[0] if res else {}
        texts = list(page.get("rec_texts") or [])
        polys = page.get("rec_polys")
        if polys is None:
            polys = page.get("dt_polys") or []
        scores = list(page.get("rec_scores") or [])
    return _rows(texts, polys, scores)


def _is_rapid(engine):
    return type(engine).__module__.split(".")[0] == "rapidocr"


def _rows(texts, polys, scores):
    """Zip the three parallel lists into the row shape both call sites want."""
    import numpy as np

    rows = []
    for i, text in enumerate(texts):
        box = None
        if i < len(polys):
            pts = np.asarray(polys[i], dtype=float).reshape(-1, 2)
            box = (int(pts[:, 0].min()), int(pts[:, 1].min()),
                   int(pts[:, 0].max()) + 1, int(pts[:, 1].max()) + 1)
        rows.append({
            "text": str(text).strip(),
            "box": box,
            "score": float(scores[i]) if i < len(scores) else None,
        })
    return rows


def main():
    """`python ocr_engine.py [image]` - report backends, optionally read one."""
    found = available()
    print("backends available:", ", ".join(found) if found else "none")
    if not found:
        print("\ninstall the cheaper one:\n   ", INSTALL_RAPID)
        return 4
    if len(sys.argv) < 2:
        print("preferred:", found[0])
        print("pass an image path to actually run it")
        return 0

    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(sys.argv[1]).convert("RGB"), dtype=np.uint8)
    for backend in found:
        engine, name, note = make_engine(prefer=backend)
        if engine is None:
            print(f"\n{backend}: {note}")
            continue
        rows = read(engine, rgb)
        print(f"\n{name}: {len(rows)} boxes")
        if note:
            print("  note:", note)
        for r in rows[:8]:
            print(f"  {r['text'][:48]!r:<52} {r['box']}")
        if len(rows) > 8:
            print(f"  ... {len(rows) - 8} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
