#!/usr/bin/env python
"""Check the Acrobat Pro and RapidOCR routes before processing a paper.

At least one route and its dependencies must be usable, even for a PDF with
a text layer. If neither is available, stop and obtain the user's explicit
consent to download/install RapidOCR. This script never installs anything.

Usage:
    python preflight.py                  # report + verdict
    python preflight.py --json           # machine-readable report

Exit codes:
    0  Acrobat Pro or RapidOCR is usable
    1  PyMuPDF missing - nothing in this skill runs without it
    4  neither route is usable - request consent to install RapidOCR, or stop
"""

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

CAP_CONVERTER = "Acrobat Pro"
CAP_OCR = "RapidOCR"

# RapidOCR first: same PP-OCR weights, ~40MB instead of ~1GB, and 8-19x faster
# to start and run. onnxruntime is a separate install because RapidOCR leaves
# the choice of execution provider to the user.
OCR_INSTALL = ("pip install --no-deps rapidocr\n"
               "    pip install onnxruntime shapely pyclipper omegaconf "
               "colorlog numpy pillow requests tqdm six PyYAML python-docx")


@dataclass(frozen=True)
class Probe:
    """One capability check: what it is, whether it is there, how to fix it."""

    key: str
    label: str
    ok: bool
    detail: str
    fix: str = ""


def _spec(name: str) -> bool:
    """True if a module is discoverable, without starting an OCR engine."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _acrobat_exe() -> Optional[str]:
    """Acrobat's install path from its App Paths entry, or None.

    Kept local so probing does not import the converter or launch an app.
    """
    import winreg

    key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
           r"\Acrobat.exe")
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                path = winreg.QueryValueEx(k, "")[0]
        except OSError:
            continue
        if os.path.isfile(path):
            return path
    return None


def _progid_registered(progid: str) -> bool:
    """True if a COM ProgID is registered - without launching the app.

    Actual export is checked by the converter. A preflight only reads the
    registration and must not start Acrobat.
    """
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid).Close()
        return True
    except OSError:
        return False


def probe_pymupdf() -> Probe:
    """PyMuPDF is the hard dependency: no PDF is readable without it."""
    try:
        import pymupdf as fitz                        # 1.24+ preferred name
    except ImportError:
        try:
            import fitz                               # older installs
        except ImportError:
            return Probe("pymupdf", "PyMuPDF", False, "MISSING",
                         "pip install pymupdf")
    ver = str(getattr(fitz, "__version__", "")
              or getattr(fitz, "VersionBind", "") or "?")
    return Probe("pymupdf", "PyMuPDF", True, ver)


def probe_module(key: str, label: str, mods: List[str], fix: str) -> Probe:
    """Generic importable-or-not probe over a set of required modules."""
    missing = [m for m in mods if not _spec(m)]
    if missing:
        return Probe(key, label, False, "MISSING " + ", ".join(missing), fix)
    return Probe(key, label, True, "installed")


def probe_converter() -> Probe:
    """Capability A - Acrobat registered for COM; Word does not count."""
    if sys.platform != "win32":
        return Probe("converter", CAP_CONVERTER, False,
                     f"no - COM is Windows only (this is {sys.platform})")
    if not _spec("win32com"):
        return Probe("converter", CAP_CONVERTER, False,
                     "no - pywin32 MISSING", "pip install pywin32")

    exe = _acrobat_exe()
    if not exe or not _progid_registered("AcroExch.App"):
        return Probe("converter", CAP_CONVERTER, False,
                     "no - Acrobat is not installed or COM is not registered")
    return Probe("converter", CAP_CONVERTER, True,
                 f"registered ({exe}); export is checked during conversion")


def probe_ocr() -> Probe:
    """Capability B - RapidOCR and its runtime dependencies only."""
    return probe_module(
        "ocr", CAP_OCR,
        ["rapidocr", "onnxruntime", "cv2", "numpy", "PIL", "shapely",
         "pyclipper", "omegaconf", "colorlog", "requests", "tqdm", "six", "yaml"],
        OCR_INSTALL + "\n    install opencv-python only if cv2 is absent",
    )


def probe_browser() -> Probe:
    """Chrome/Edge for the PDF step - reuse md_to_pdf's own search."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from md_to_pdf import find_browser
    except ImportError:
        return Probe("browser", "Chrome / Edge", False,
                     "cannot check - md_to_pdf.py not next to this script")
    found = find_browser()
    if not found:
        return Probe("browser", "Chrome / Edge", False, "not found",
                     "install Chrome or Edge (PDF output only)")
    return Probe("browser", "Chrome / Edge", True, os.path.basename(found))


def collect() -> Dict[str, Probe]:
    """Run every probe once, in report order."""
    probes = [
        probe_pymupdf(),
        probe_module("lxml", "lxml", ["lxml"], "pip install lxml"),
        probe_module("imaging", "Pillow + NumPy", ["PIL", "numpy"],
                     "pip install pillow numpy"),
        probe_module("docx", "python-docx", ["docx"], "pip install python-docx"),
        probe_converter(),
        probe_ocr(),
        probe_pandoc(),
        probe_browser(),
    ]
    return {p.key: p for p in probes}


def probe_pandoc() -> Probe:
    """pandoc drives the Markdown -> HTML leg of the PDF step."""
    found = shutil.which("pandoc")
    if not found:
        return Probe("pandoc", "pandoc", False, "not found",
                     "https://pandoc.org/installing.html (PDF output only)")
    return Probe("pandoc", "pandoc", True, found)


def figure_caps(probes: Dict[str, Probe]) -> List[Probe]:
    """The two capabilities the gate is about, in preference order."""
    return [probes["converter"], probes["ocr"]]


def usable_caps(probes: Dict[str, Probe]) -> List[Probe]:
    """The routes whose own dependencies are also satisfied.

    A converter without lxml is worthless here: docx_extract.py parses the
    exported .docx with lxml, so the Word path dies one step later. Counting
    it would let the gate pass a machine that cannot finish the job.
    """
    out = []
    if probes["converter"].ok and probes["lxml"].ok:
        out.append(probes["converter"])
    if (probes["ocr"].ok and probes["imaging"].ok
            and probes["docx"].ok and probes["lxml"].ok):
        out.append(probes["ocr"])
    return out


def decide(probes: Dict[str, Probe]) -> int:
    """Return the gate code without a bypass for text-layer PDFs."""
    if not probes["pymupdf"].ok:
        return 1
    return 0 if usable_caps(probes) else 4


def render(probes: Dict[str, Probe]) -> None:
    """Print the human report."""
    print(f"interpreter\n  {sys.executable}")
    print("  every command below must use THIS python, or the packages it "
          "found are not the ones\n  that will be imported at run time "
          "(an active venv shadows a global install).")

    print("\nrequired")
    for key in ("pymupdf", "lxml", "imaging", "docx"):
        _row(probes[key])

    print("\nfigure capabilities (need at least one)")
    for p in figure_caps(probes):
        _row(p)

    print("\noptional (PDF output only)")
    for key in ("pandoc", "browser"):
        _row(probes[key])


def _row(p: Probe) -> None:
    mark = "ok " if p.ok else "NO "
    line = f"  {mark} {p.label:<22} {p.detail}"
    print(line)
    if not p.ok and p.fix:
        print(f"      -> {p.fix}")


def verdict(probes: Dict[str, Probe]) -> int:
    """Print what this machine can and cannot do, and return the exit code."""
    sys.stdout.flush()          # keep the report above the STOP block
    code = decide(probes)
    if code == 1:
        print("\nSTOP: PyMuPDF missing. Nothing in this skill runs without "
              "it.\n    pip install pymupdf", file=sys.stderr)
        return 1

    caps = usable_caps(probes)
    if code == 4:
        print("\nSTOP: neither Acrobat Pro nor RapidOCR is usable.\n"
              "Obtain the user's explicit consent to download and install "
              "RapidOCR\nbefore running any installation command. If consent "
              "is refused or pending,\nstop; a text-layer PDF does not bypass "
              "this requirement.\n\n"
              f"After consent, use this interpreter:\n    {OCR_INSTALL}\n"
              "If cv2 is absent, also install opencv-python; do not replace "
              "an existing OpenCV package.\n\n"
              f"Installing into a different python than\n    {sys.executable}"
              "\nwill not help - check the interpreter line above.",
              file=sys.stderr)
        if probes["converter"].ok and not probes["lxml"].ok:
            print("\nNote: a converter IS installed, but lxml is not, so the "
                  "Word path cannot\nbe read. 'pip install lxml' alone "
                  "clears this stop.", file=sys.stderr)
        return 4

    print("\nverdict")
    print("  available : " + (", ".join(p.label for p in caps) or "none"))
    if probes["converter"].ok and not probes["lxml"].ok:
        print("  converter : found but UNUSABLE without lxml "
              "(docx_extract.py needs it) - pip install lxml")
    if not probes["imaging"].ok:
        print("  panels    : cannot split at all - Pillow/NumPy missing, "
              "figures stay whole")
    elif not probes["ocr"].ok:
        print("  panels    : keep whole figures; label cross-validation "
              "requires RapidOCR")
    if not probes["ocr"].ok:
        print("  fallback  : if Acrobat export fails, request consent "
              "to install RapidOCR before continuing")
    if not probes["converter"].ok:
        print("  figures   : positions are guessed by insert_figures.py "
              "(first mention), not taken from the source layout")
    print("  Word review: skip after successful Acrobat export; required "
          "after RapidOCR reconstruction")
    print("  images    : embed in Markdown/PDF; do not ask to save a collection")
    return 0


def main() -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Preflight the paper-translator environment.")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    args = ap.parse_args()

    probes = collect()
    if args.json:
        code = decide(probes)
        print(json.dumps({
            "interpreter": sys.executable,
            "probes": {k: asdict(v) for k, v in probes.items()},
            "figure_capabilities": [p.label for p in usable_caps(probes)],
            "requires_rapidocr_install_consent": code == 4,
            "exit": code,
        }, indent=2, ensure_ascii=False))
        return code

    render(probes)
    return verdict(probes)


if __name__ == "__main__":
    sys.exit(main())
