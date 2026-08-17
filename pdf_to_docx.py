#!/usr/bin/env python
"""Convert a paper PDF to DOCX, keeping the page layout and the figures.

Why this is the preferred first step: a PDF-to-DOCX converter has already
solved the two hardest problems of this whole skill - the figures come across
as embedded images, and they land in roughly the right place in the text. No
figure extraction, no panel splitting, no relocating captions. You then
translate the body paragraphs in place and the layout survives.

Acrobat Pro's export is the best of the converters, and it is what this script
tries to use. Two routes, in order:

  1. Acrobat Pro over COM. Needs a one-time trusted-function install
     (--install-acrobat-js), because Acrobat refuses `doc.saveAs` from an
     unprivileged caller - the raw COM call fails with "not implemented".
     Recent Acrobat builds (tested 25.001) may still refuse to load the
     user-level folder script, in which case route 2 or a manual export.
  2. Microsoft Word over COM. Fully automatic, no setup, but a worse
     converter: on a two-column Elsevier paper it shattered running text into
     58 text boxes, splitting words ("ScienceDirec" + "t"). Usable, not ideal.

If both are unavailable, do it by hand in Acrobat - four clicks, best quality:
    File -> Export To -> Microsoft Word -> Word Document
    In that dialog: Settings... -> Layout Settings -> "Retain Page Layout"

Usage:
    python pdf_to_docx.py <pdf> [-o out.docx] [--engine acrobat|word|auto]
    python pdf_to_docx.py --install-acrobat-js
    python pdf_to_docx.py --check

Exit codes:
    0  DOCX written
    2  no converter available - export by hand, instructions printed
    1  error
"""

import argparse
import os
import subprocess
import sys
import time

ACROBAT_JS = """// installed by the translating-papers skill.
// Acrobat blocks doc.saveAs from an unprivileged caller, so the export is
// wrapped in a trusted function that COM is allowed to invoke.
var tpPdfToDocx = app.trustedFunction(function (inPath, outPath) {
    app.beginPriv();
    var d = app.openDoc({ cPath: inPath });
    d.saveAs({ cPath: outPath, cConvID: "com.adobe.acrobat.docx" });
    d.closeDoc(true);
    app.endPriv();
    return "ok";
});
"""

MANUAL = """
Export it by hand in Acrobat Pro - the highest-quality route regardless:

    1. open the PDF in Acrobat Pro
    2. File -> Export To -> Microsoft Word -> Word Document
    3. in the save dialog click "Settings..."
    4. Layout Settings: choose "Retain Page Layout"   <-- important
       ("Retain Flowing Text" reflows the page and moves the figures)
    5. save it next to the PDF, then carry on with that .docx
"""


def acrobat_js_path():
    base = os.path.join(os.environ.get("APPDATA", ""), "Adobe", "Acrobat")
    if not os.path.isdir(base):
        return None
    versions = [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d)) and d[:1].isalnum()]
    # Prefer the numbered/continuous track folder Acrobat actually uses.
    for pref in ("DC", "2020", "2017"):
        if pref in versions:
            return os.path.join(base, pref, "JavaScripts", "translating_papers.js")
    return os.path.join(base, versions[0], "JavaScripts",
                        "translating_papers.js") if versions else None


def install_acrobat_js():
    path = acrobat_js_path()
    if not path:
        print("ERROR: no Acrobat user folder under %APPDATA%\\Adobe\\Acrobat",
              file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(ACROBAT_JS)
    print(f"installed : {path}")
    print("next      : quit Acrobat completely, then re-run with --check.")
    print("            Acrobat also needs JavaScript enabled "
          "(Preferences -> JavaScript).")
    print("            Some 2024+ builds refuse user-level folder scripts; if "
          "--check still\n            says unavailable, use --engine word or "
          "export by hand.")
    return 0


def devpath(win_path):
    """C:\\a\\b.pdf -> /C/a/b.pdf, the only form Acrobat JS accepts."""
    p = os.path.abspath(win_path)
    return "/" + p[0] + p[2:].replace("\\", "/")


def kill_acrobat():
    for exe in ("Acrobat.exe", "AcroCEF.exe"):
        subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
    time.sleep(3)


def convert_acrobat(pdf, out, restart=True):
    """Export through Acrobat Pro's own converter. Returns True on success."""
    try:
        import win32com.client as win32
    except ImportError:
        print("  acrobat: pywin32 missing (pip install pywin32)")
        return False
    if restart:
        kill_acrobat()
    app = avdoc = None
    try:
        app = win32.DispatchEx("AcroExch.App")
        avdoc = win32.DispatchEx("AcroExch.AVDoc")
        if not avdoc.Open(os.path.abspath(pdf), "convert"):
            print("  acrobat: could not open the PDF")
            return False
        jso = avdoc.GetPDDoc().GetJSObject()
        try:
            jso.tpPdfToDocx(devpath(pdf), devpath(out))
        except Exception as e:                      # noqa: BLE001
            print(f"  acrobat: trusted function unavailable ({e}); "
                  f"run --install-acrobat-js, or export by hand")
            return False
        for _ in range(120):
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return True
            time.sleep(1)
        print("  acrobat: export produced no file within 120 s")
        return False
    except Exception as e:                          # noqa: BLE001
        print(f"  acrobat: {type(e).__name__}: {e}")
        return False
    finally:
        for obj, meth in ((avdoc, "Close"), (app, "Exit")):
            try:
                getattr(obj, meth)(True) if meth == "Close" else obj.Exit()
            except Exception:
                pass


def convert_word(pdf, out):
    """Export through Word's own PDF importer. Automatic, lower fidelity."""
    try:
        import win32com.client as win32
    except ImportError:
        print("  word: pywin32 missing (pip install pywin32)")
        return False
    app = None
    try:
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(os.path.abspath(pdf), ConfirmConversions=False,
                                 ReadOnly=False)
        doc.SaveAs2(os.path.abspath(out), FileFormat=16)   # wdFormatDocx
        print(f"  word: {doc.Paragraphs.Count} paragraphs, "
              f"{doc.InlineShapes.Count + doc.Shapes.Count} images")
        doc.Close(False)
        return os.path.exists(out)
    except Exception as e:                          # noqa: BLE001
        print(f"  word: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            app.Quit()
        except Exception:
            pass


def check():
    """Report which converters this machine can actually use."""
    print("converter availability")
    try:
        import win32com.client as win32
    except ImportError:
        print("  pywin32          : MISSING (pip install pywin32)")
        return 2
    print("  pywin32          : ok")
    try:
        app = win32.DispatchEx("AcroExch.App")
        try:
            app.Exit()
        except Exception:
            pass
        print("  Acrobat Pro      : installed")
    except Exception:
        print("  Acrobat Pro      : no")
    js = acrobat_js_path()
    print(f"  trusted JS file  : "
          f"{'present' if js and os.path.exists(js) else 'not installed'}")
    try:
        w = win32.DispatchEx("Word.Application")
        ver = w.Version
        w.Quit()
        print(f"  Microsoft Word   : {ver}")
    except Exception:
        print("  Microsoft Word   : no")
    print(MANUAL)
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--engine", choices=("auto", "acrobat", "word"),
                    default="auto")
    ap.add_argument("--install-acrobat-js", action="store_true",
                    help="one-time setup so Acrobat allows the COM export")
    ap.add_argument("--check", action="store_true",
                    help="report which converters are available")
    ap.add_argument("--no-restart", action="store_true",
                    help="do not kill a running Acrobat before exporting")
    args = ap.parse_args()

    if args.install_acrobat_js:
        return install_acrobat_js()
    if args.check:
        return check()
    if not args.pdf:
        ap.error("give a PDF, or use --check / --install-acrobat-js")
    if not os.path.isfile(args.pdf):
        print(f"ERROR: no such file: {args.pdf}", file=sys.stderr)
        return 1

    out = args.out or os.path.splitext(os.path.abspath(args.pdf))[0] + ".docx"
    order = {"auto": ("acrobat", "word"),
             "acrobat": ("acrobat",), "word": ("word",)}[args.engine]

    print(f"pdf     : {args.pdf}")
    for engine in order:
        print(f"trying  : {engine}")
        ok = (convert_acrobat(args.pdf, out, restart=not args.no_restart)
              if engine == "acrobat" else convert_word(args.pdf, out))
        if ok:
            print(f"docx    : {out}  "
                  f"({os.path.getsize(out) / 1e6:.1f} MB, via {engine})")
            if engine == "word":
                print("\nNOTE: Word's converter fragments running text into "
                      "many small text boxes.\n      Acrobat Pro's export is "
                      "cleaner - see the manual steps below if the\n      "
                      "translation comes out choppy.")
                print(MANUAL)
            return 0
    print("\nno converter worked.")
    print(MANUAL)
    return 2


if __name__ == "__main__":
    sys.exit(main())


