#!/usr/bin/env python
"""Skill-local RapidOCR for scanned pages and figure labels.

    engine, backend, note = make_engine(lang="en")
    boxes = read(engine, rgb)     # [{'text', 'box': (x0,y0,x1,y1), 'score'}]

Input images are RGB uint8. Missing or broken local OCR stops the operation;
installation or repair requires user consent through setup_ocr.py.
"""

import sys

import ocr_runtime

BACKEND_RAPID = "RapidOCR"

INSTALL_RAPID = ocr_runtime.INSTALL_HINT

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
    """Report only verified skill-local RapidOCR, without building an engine."""
    return [BACKEND_RAPID] if ocr_runtime.check_local_ocr()[0] else []


def make_engine(lang="en", prefer=None, model_type="small", quiet=True):
    """Build RapidOCR and return (engine, backend_name, error_note).

    Retain prefer for existing callers, accepting only None or "RapidOCR".
    model_type selects RapidOCR weights: tiny | small | medium.
    """
    if prefer not in (None, BACKEND_RAPID):
        return None, None, "Only RapidOCR is supported."
    ok, detail = ocr_runtime.check_local_ocr()
    repair = f"After user consent, install or repair local RapidOCR: {INSTALL_RAPID}"
    if not ok:
        return None, None, f"{detail}. {repair}"
    try:
        engine = _build_rapid(lang, model_type, quiet)
    except Exception as exc:
        return None, None, f"RapidOCR failed to start ({exc}). {repair}"
    return engine, BACKEND_RAPID, None


def _build_rapid(lang, model_type, quiet):
    ok, detail = ocr_runtime.check_local_ocr()
    if not ok:
        raise RuntimeError(f"{detail}. After user consent: {INSTALL_RAPID}")
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType

    params = {"Global.model_root_dir": str(ocr_runtime.MODEL_ROOT)}
    if quiet:
        params["Global.log_level"] = "error"
    if model_type:
        mt = ModelType(model_type)
        params["Det.model_type"] = mt
        params["Rec.model_type"] = mt
    rec_lang = _RAPID_LANG.get((lang or "").lower(), None)
    if rec_lang:
        params["Rec.lang_type"] = LangRec(rec_lang)
    return RapidOCR(params=params or None)


def read(engine, rgb):
    """OCR one RGB image. Returns [{'text', 'box', 'score'}], box axis-aligned.

    Empty list for an image with no text - that is a real answer, not an error.
    Raises whatever the backend raises; callers decide whether a failed page is
    fatal.
    """
    if engine is None:
        return []
    out = engine(rgb)
    texts = list(out.txts or [])
    polys = out.boxes if out.boxes is not None else []
    scores = list(out.scores or [])
    return _rows(texts, polys, scores)


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
    """`python ocr_engine.py [image]` - check RapidOCR, optionally read an image."""
    found = available()
    print("RapidOCR:", "available" if found else "unavailable")
    if not found:
        print("\nAfter user consent, install or repair local RapidOCR:\n   ",
              INSTALL_RAPID)
        return 4
    if len(sys.argv) < 2:
        print("pass an image path to actually run it")
        return 0

    import numpy as np
    from PIL import Image

    rgb = np.asarray(Image.open(sys.argv[1]).convert("RGB"), dtype=np.uint8)
    engine, name, note = make_engine()
    if engine is None:
        print(f"\nRapidOCR: {note}")
        return 1
    rows = read(engine, rgb)
    print(f"\n{name}: {len(rows)} boxes")
    for r in rows[:8]:
        print(f"  {r['text'][:48]!r:<52} {r['box']}")
    if len(rows) > 8:
        print(f"  ... {len(rows) - 8} more")
    return 0


if __name__ == "__main__":
    ocr_runtime.relaunch_in_local_runtime(__file__)
    sys.exit(main())
