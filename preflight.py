#!/usr/bin/env python
"""Check whether this machine can deliver a figure-complete translation.

Runs before anything else. Three capabilities can carry the figures through
the pipeline, and at least ONE of them must be present:

  A  PDF -> Word converter   Acrobat Pro or Word over COM (Windows only).
                             Puts every figure back where it belongs in the
                             body text, so nothing has to be guessed.
  B  PaddleOCR               Reads scanned pages, and validates a panel split
                             by matching the a/b/c labels it reads back.
  C  a multimodal model       Reads the rendered figures directly. No script
                             can detect this, so the caller declares it with
                             --multimodal.

With none of the three, scanned PDFs are impossible outright and nothing is
left that can catch a wrong panel split or a swapped caption - the two
failure modes this skill exists to prevent. So the run stops here.

PaddleOCR is the cheapest way out: ~1GB and one pip command. Installing
Acrobat Pro just to translate one paper is not a reasonable ask.

Usage:
    python preflight.py                  # report + verdict
    python preflight.py --multimodal     # the calling model can read images
    python preflight.py --json           # machine-readable report

Exit codes:
    0  at least one figure capability is available
    1  PyMuPDF missing - nothing in this skill runs without it
    4  no figure capability at all - install PaddleOCR, or stop
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

CAP_CONVERTER = "PDF -> Word converter"
CAP_OCR = "PaddleOCR"
CAP_VISION = "multimodal model"

OCR_INSTALL = "pip install paddlepaddle paddleocr"


@dataclass(frozen=True)
class Probe:
    """One capability check: what it is, whether it is there, how to fix it."""

    key: str
    label: str
    ok: bool
    detail: str
    fix: str = ""


def _spec(name: str) -> bool:
    """True if a module is importable, without paying to import it.

    PaddleOCR pulls in paddle and takes seconds to import, so a preflight
    must never import it just to answer yes/no.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _acrobat_exe() -> Optional[str]:
    """Acrobat's install path from its App Paths entry, or None.

    Duplicated from pdf_to_docx.py on purpose: that module imports winreg at
    top level, so importing it here would break every non-Windows run.
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

    pdf_to_docx.py --check starts Word to find out, which costs seconds and
    pops a process. Reading the registration is enough for a preflight.
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
    """Capability A - Acrobat Pro or Word reachable over COM."""
    if sys.platform != "win32":
        return Probe("converter", CAP_CONVERTER, False,
                     f"no - COM is Windows only (this is {sys.platform})")
    if not _spec("win32com"):
        return Probe("converter", CAP_CONVERTER, False,
                     "no - pywin32 MISSING", "pip install pywin32")

    engines = []
    exe = _acrobat_exe()
    if exe:
        engines.append("Acrobat Pro")
    if _progid_registered("Word.Application"):
        engines.append("Word")
    if not engines:
        return Probe("converter", CAP_CONVERTER, False,
                     "no - neither Acrobat Pro nor Word is installed")
    return Probe("converter", CAP_CONVERTER, True, " + ".join(engines))


def probe_ocr() -> Probe:
    """Capability B - PaddleOCR (needs both paddleocr and paddle)."""
    return probe_module("ocr", CAP_OCR, ["paddleocr", "paddle"], OCR_INSTALL)


def probe_vision(declared: bool) -> Probe:
    """Capability C - caller-declared, because no probe can see the model."""
    if declared:
        return Probe("vision", CAP_VISION, True, "declared by caller")
    return Probe("vision", CAP_VISION, False,
                 "not declared", "pass --multimodal if the model reads images")


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


def collect(multimodal: bool) -> Dict[str, Probe]:
    """Run every probe once, in report order."""
    probes = [
        probe_pymupdf(),
        probe_module("lxml", "lxml", ["lxml"], "pip install lxml"),
        probe_module("imaging", "Pillow + NumPy", ["PIL", "numpy"],
                     "pip install pillow numpy"),
        probe_converter(),
        probe_ocr(),
        probe_vision(multimodal),
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
    """The three capabilities the gate is about, in preference order."""
    return [probes["converter"], probes["ocr"], probes["vision"]]


def usable_caps(probes: Dict[str, Probe]) -> List[Probe]:
    """Of the three, the ones whose own dependencies are also satisfied.

    A converter without lxml is worthless here: docx_extract.py parses the
    exported .docx with lxml, so the Word path dies one step later. Counting
    it would let the gate pass a machine that cannot finish the job.
    """
    out = []
    if probes["converter"].ok and probes["lxml"].ok:
        out.append(probes["converter"])
    for key in ("ocr", "vision"):
        if probes[key].ok:
            out.append(probes[key])
    return out


def decide(probes: Dict[str, Probe]) -> int:
    """The exit code, from one place, so report and --json never disagree."""
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
    for key in ("pymupdf", "lxml", "imaging"):
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
        print("\nSTOP: no way to verify any figure work on this machine.\n\n"
              "Scanned PDFs are impossible, and nothing is left that could "
              "catch a\nwrong panel split or a caption pinned to the wrong "
              "figure - the two\nfailures this skill exists to prevent.\n\n"
              f"Cheapest fix, about 1GB, one command:\n    {OCR_INSTALL}\n\n"
              "Installing Acrobat Pro for one paper is not reasonable; a "
              "multimodal\nmodel works too (rerun with --multimodal). "
              "Otherwise translate this\npaper with something else.\n\n"
              f"Installing into a different python than\n    {sys.executable}"
              "\nwill not help - check the interpreter line above.",
              file=sys.stderr)
        if probes["converter"].ok and not probes["lxml"].ok:
            print("\nNote: a converter IS installed, but lxml is not, so the "
                  "Word path cannot\nbe read. 'pip install lxml' alone "
                  "clears this stop.", file=sys.stderr)
        return 4

    print("\nverdict")
    print("  available : " + ", ".join(p.label for p in caps))

    if probes["converter"].ok and not probes["lxml"].ok:
        print("  converter : found but UNUSABLE without lxml "
              "(docx_extract.py needs it) - pip install lxml")
    if not probes["imaging"].ok:
        print("  panels    : cannot split at all - Pillow/NumPy missing, "
              "figures stay whole")
    elif not (probes["ocr"].ok or probes["vision"].ok):
        print("  panels    : geometry only - run panel_split.py --no-ocr, "
              "and expect no label cross-check")
    elif not probes["ocr"].ok:
        print("  panels    : no OCR label check - the multimodal model has "
              "to confirm each split by eye")
    if not (probes["ocr"].ok or probes["vision"].ok):
        print("  scanned   : NO - a scanned PDF cannot be read at all "
              "(needs PaddleOCR or a multimodal model)")
    if not probes["converter"].ok:
        print("  figures   : positions are guessed by insert_figures.py "
              "(first mention), not taken from the source layout")
    if not probes["vision"].ok:
        print("  captions  : no visual pairing check - verify with the fitz "
              "one-liner in SKILL.md step 5")
    return 0


def main() -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Preflight the paper-translator environment.")
    ap.add_argument("--multimodal", action="store_true",
                    help="declare that the calling model can read images")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    args = ap.parse_args()

    probes = collect(args.multimodal)
    if args.json:
        code = decide(probes)
        print(json.dumps({
            "interpreter": sys.executable,
            "probes": {k: asdict(v) for k, v in probes.items()},
            "figure_capabilities": [p.label for p in usable_caps(probes)],
            "exit": code,
        }, indent=2, ensure_ascii=False))
        return code

    render(probes)
    return verdict(probes)


if __name__ == "__main__":
    sys.exit(main())
