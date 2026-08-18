#!/usr/bin/env python
"""Convert a paper PDF to DOCX, keeping the figures and the reading order.

Why this is the preferred first step: a PDF-to-DOCX converter has already
solved the two hardest problems of this whole skill - the figures come across
as embedded images, and they land in roughly the right place in the text. No
figure extraction, no panel splitting, no relocating captions. You then
translate the body paragraphs in place and the layout survives.

Acrobat Pro is the best converter available, and this script drives it end to
end with no clicking. Getting there took three separate fixes, because a
single misleading COM error ("not implemented") hid all of them:

  1. pywin32 invokes IDispatch methods with DISPATCH_METHOD|DISPATCH_PROPERTYGET.
     Acrobat's JSObject answers that combination with E_NOTIMPL for *every*
     method it owns - privileged or not, getPageNumWords() included. A bare
     DISPATCH_METHOD works. See call(). This, not the privilege model, is what
     made the whole route look impossible.
  2. doc.saveAs really is privileged, so it has to come from a trusted
     function in a folder-level script. Acrobat 25.x ignores the per-user
     JavaScripts folder; only the application-level one next to debugger.js is
     read, and writing there needs elevation - hence the UAC prompt in
     install_acrobat_js(). Acrobat itself runs unelevated as usual.
  3. While Protected Mode is on, saveAs neither returns nor raises: it hangs
     forever. protected_mode_off() switches it off around the export only, and
     puts it back afterwards.

Tested on Acrobat Pro 25.1 (Exchange-Pro), pywin32 311, Windows 11.

Layout mode is the opposite of what you would guess. "Retain Page Layout"
(iLayoutMode=1) pins every block to its visual position, which shreds running
text into hundreds of text boxes, writes each twice (DrawingML + VML) and
reorders sentences across block boundaries - on a real paper it produced
"weakly allowed due to|transitions22,23. Notably,|orbital angular momentum
mixing". "Retain Flowing Text" (iLayoutMode=0) keeps reading order, headings
and paragraph breaks, and still places figures inline, which is what
docx_extract.py wants. Flowing is the default; --layout page is there if you
ever need visual fidelity instead.

Word COM is the fallback when Acrobat is absent: no setup, but a worse
converter - on a two-column Elsevier paper it shattered running text into 58
text boxes, splitting words ("ScienceDirec" + "t").

Usage:
    python pdf_to_docx.py <pdf> [-o out.docx] [--engine acrobat|word|auto]
                                [--layout flowing|page]
    python pdf_to_docx.py --install-acrobat-js   # one time, prompts for UAC
    python pdf_to_docx.py --check

Exit codes:
    0  DOCX written
    2  no converter available
    1  error
"""

import argparse
import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import winreg

try:
    import pythoncom
    import win32com.client as win32
except ImportError:                                 # reported by check()
    pythoncom = win32 = None

DC = r"SOFTWARE\Adobe\Adobe Acrobat\DC"
PRIV_KEY = DC + r"\Privileged"
DOCX_SETTINGS = DC + r"\AVConversionFromPDF\cSettings\c1\cSettings"

JS_NAME = "paper_translator.js"
JS_VERSION = "paper-translator/4"
PM_STASH = os.path.join(tempfile.gettempdir(), "paper_translator_pm_restore")

ACROBAT_JS = '''// paper-translator skill: trusted PDF -> DOCX export.
//
// Acrobat grants doc.saveAs only to a trusted function declared in a
// folder-level script. Acrobat 25.x ignores the per-user JavaScripts folder,
// so this file belongs in the APPLICATION folder, next to debugger.js:
//   <Acrobat install dir>\\Javascripts\\
// Writing there needs elevation; pdf_to_docx.py --install-acrobat-js does it.
//
// Errors come back as a return string, not a throw: a JS exception surfaces
// over COM as an opaque "server threw an exception" with no detail at all.

// Non-privileged probe. If COM can call this, the folder script is loaded.
var tpVersion = function () { return "%s"; };

// Export the document the COM caller already has open. Reopening that same
// PDF from inside Acrobat deadlocks, so this must not call app.openDoc.
var tpExportThis = app.trustedFunction(function (outPath, convID) {
    try {
        app.beginPriv();
        this.saveAs({ cPath: outPath,
                      cConvID: convID || "com.adobe.acrobat.docx" });
        app.endPriv();
        return "ok";
    } catch (e) {
        try { app.endPriv(); } catch (ePriv) {}
        return "ERROR: " + e;
    }
});
''' % JS_VERSION

NO_CONVERTER = """
Neither Acrobat Pro nor Word is usable on this machine, so skip step 0 and go
to step 1 of the skill (extract_paper.py). If you do have Acrobat Pro, export
by hand instead: File -> Export To -> Microsoft Word -> Word Document, and in
that dialog Settings... -> Layout Settings -> "Retain Flowing Text".
"""


# --- registry ---------------------------------------------------------------

def reg_get(sub, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def reg_set(sub, name, value):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def restore_stashed_protected_mode():
    """Put Protected Mode back if an earlier run died mid-export."""
    if not os.path.exists(PM_STASH):
        return
    try:
        with open(PM_STASH) as f:
            value = int(f.read().strip())
        reg_set(PRIV_KEY, "bProtectedMode", value)
        print(f"note    : restored Protected Mode={value}, left off by an "
              f"earlier run")
    except (OSError, ValueError):
        pass
    with contextlib.suppress(OSError):
        os.remove(PM_STASH)


@contextlib.contextmanager
def protected_mode_off():
    """saveAs hangs forever with Protected Mode on - no error, no timeout.

    The original value is stashed on disk as well as in memory, so a killed
    process does not leave the sandbox switched off for good.
    """
    before = reg_get(PRIV_KEY, "bProtectedMode")
    if before in (None, 0):
        yield
        return
    with open(PM_STASH, "w") as f:
        f.write(str(before))
    reg_set(PRIV_KEY, "bProtectedMode", 0)
    try:
        yield
    finally:
        reg_set(PRIV_KEY, "bProtectedMode", before)
        with contextlib.suppress(OSError):
            os.remove(PM_STASH)


# --- the trusted script -----------------------------------------------------

def acrobat_exe():
    """Acrobat's install path, from its App Paths registry entry."""
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


def app_js_path():
    """The application-level folder script Acrobat 25.x actually reads."""
    exe = acrobat_exe()
    if not exe:
        return None
    return os.path.join(os.path.dirname(exe), "Javascripts", JS_NAME)


def js_current(path):
    """True if the installed script is byte-identical to what we ship."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read() == ACROBAT_JS
    except OSError:
        return False


def install_acrobat_js(quiet=False):
    """Copy the trusted-function script into Acrobat's install folder.

    That folder is under Program Files, so this needs elevation and raises a
    UAC prompt. Only the copy is elevated - Acrobat and this script keep
    running as the normal user, which is what COM needs.
    """
    dst = app_js_path()
    if not dst:
        print("ERROR: cannot locate Acrobat's install folder (Acrobat Pro "
              "installed?)", file=sys.stderr)
        return False
    if js_current(dst):
        if not quiet:
            print(f"already current: {dst}")
        return True

    src = os.path.join(tempfile.gettempdir(), JS_NAME)
    with open(src, "w", encoding="utf-8") as f:
        f.write(ACROBAT_JS)
    # Driving the copy from a .ps1 keeps these paths out of a nested-quoted
    # command line. Single quotes: PowerShell treats backslashes literally.
    ps1 = os.path.join(tempfile.gettempdir(), "paper_translator_install.ps1")
    with open(ps1, "w", encoding="utf-8") as f:
        f.write("$ErrorActionPreference = 'Stop'\n")
        f.write("New-Item -ItemType Directory -Force -Path '%s' | Out-Null\n"
                % os.path.dirname(dst))
        f.write("Copy-Item -LiteralPath '%s' -Destination '%s' -Force\n"
                % (src, dst))

    print("elevating: approve the UAC prompt so Acrobat can load the export")
    print("           script (one time; Acrobat itself stays unelevated)")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        '-NoProfile -ExecutionPolicy Bypass -File "%s"' % ps1, None, 0)
    if rc <= 32:
        print(f"ERROR: elevation refused or failed (ShellExecute {rc})",
              file=sys.stderr)
        return False
    for _ in range(90):             # ShellExecuteW returns before the copy
        if js_current(dst):
            print(f"installed: {dst}")
            return True
        time.sleep(1)
    print("ERROR: the elevated copy never completed", file=sys.stderr)
    return False


# --- COM plumbing -----------------------------------------------------------

def call(obj, name, *args):
    """Invoke a JSObject method with a bare DISPATCH_METHOD.

    pywin32's normal `obj.name(...)` adds DISPATCH_PROPERTYGET to the flags,
    and Acrobat answers that with E_NOTIMPL for every method it has. Reading
    properties works either way; only calls need this.
    """
    dispid = obj._oleobj_.GetIDsOfNames(0, name)
    return obj._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True,
                               *args)


def devpath(win_path):
    """C:\\a\\b.pdf -> /C/a/b.pdf, the only form Acrobat JS accepts."""
    p = os.path.abspath(win_path)
    return "/" + p[0] + p[2:].replace("\\", "/")


def kill_acrobat():
    for exe in ("Acrobat.exe", "AcroCEF.exe"):
        subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
    time.sleep(3)


def convert_acrobat(pdf, out, layout="flowing", install=True, restart=True):
    """Export through Acrobat Pro's own converter, unattended."""
    if win32 is None:
        print("  acrobat: pywin32 missing (pip install pywin32)")
        return False
    dst = app_js_path()
    if not dst:
        print("  acrobat: not installed")
        return False
    if not js_current(dst):
        if not install:
            print("  acrobat: export script not installed "
                  "(run --install-acrobat-js)")
            return False
        if not install_acrobat_js(quiet=True):
            return False

    reg_set(DOCX_SETTINGS, "iLayoutMode", 1 if layout == "page" else 0)
    reg_set(DOCX_SETTINGS, "bIncludeImages", 1)

    # Protected Mode is read at startup, so the restart has to happen inside
    # the context manager, not before it.
    with protected_mode_off():
        if restart:
            kill_acrobat()
        app = avdoc = None
        try:
            app = win32.DispatchEx("AcroExch.App")
            avdoc = win32.DispatchEx("AcroExch.AVDoc")
            if not avdoc.Open(os.path.abspath(pdf), "paper-translator"):
                print("  acrobat: could not open the PDF")
                return False
            jso = avdoc.GetPDDoc().GetJSObject()
            try:
                version = call(jso, "tpVersion")
            except Exception:                       # noqa: BLE001
                print("  acrobat: folder script not loaded - quit Acrobat "
                      "completely and rerun --install-acrobat-js")
                return False
            print(f"  acrobat: script {version}, layout {layout}")
            result = call(jso, "tpExportThis", devpath(out))
            if result != "ok":
                print(f"  acrobat: {result}")
                return False
            for _ in range(30):     # saveAs returns synchronously; be safe
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    return True
                time.sleep(1)
            print("  acrobat: saveAs reported ok but wrote no file")
            return False
        except Exception as e:                      # noqa: BLE001
            print(f"  acrobat: {type(e).__name__}: {e}")
            return False
        finally:
            with contextlib.suppress(Exception):
                avdoc.Close(True)
            with contextlib.suppress(Exception):
                app.Exit()


def convert_word(pdf, out):
    """Export through Word's own PDF importer. Automatic, lower fidelity."""
    if win32 is None:
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
        with contextlib.suppress(Exception):
            app.Quit()


def check():
    """Report whether the unattended Acrobat route is ready to run."""
    print("converter availability")
    if win32 is None:
        print("  pywin32          : MISSING (pip install pywin32)")
        return 2
    print("  pywin32          : ok")

    exe = acrobat_exe()
    if exe:
        print(f"  Acrobat Pro      : {exe}")
        dst = app_js_path()
        state = ("current" if js_current(dst) else
                 "STALE - will reinstall" if os.path.exists(dst) else
                 "not installed - will prompt for UAC")
        print(f"  export script    : {state}")
        print(f"                     {dst}")
    else:
        print("  Acrobat Pro      : no")

    pm = reg_get(PRIV_KEY, "bProtectedMode")
    print(f"  Protected Mode   : {pm} "
          f"{'(switched off during export, then restored)' if pm else ''}")
    layout = reg_get(DOCX_SETTINGS, "iLayoutMode")
    print(f"  iLayoutMode      : {layout} "
          f"({'page' if layout == 1 else 'flowing'}); set per export")

    try:
        w = win32.DispatchEx("Word.Application")
        ver = w.Version
        w.Quit()
        print(f"  Microsoft Word   : {ver} (fallback)")
    except Exception:                               # noqa: BLE001
        print("  Microsoft Word   : no")
    return 0


def main():
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--engine", choices=("auto", "acrobat", "word"),
                    default="auto")
    ap.add_argument("--layout", choices=("flowing", "page"), default="flowing",
                    help="flowing keeps reading order (default); page keeps "
                         "visual position but scrambles sentence order")
    ap.add_argument("--install-acrobat-js", action="store_true",
                    help="install the trusted export script (prompts for UAC)")
    ap.add_argument("--no-install", action="store_true",
                    help="fail instead of prompting for UAC when the export "
                         "script is missing")
    ap.add_argument("--check", action="store_true",
                    help="report which converters are available")
    ap.add_argument("--no-restart", action="store_true",
                    help="do not kill a running Acrobat before exporting")
    args = ap.parse_args()

    restore_stashed_protected_mode()

    if args.install_acrobat_js:
        return 0 if install_acrobat_js() else 1
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
        ok = (convert_acrobat(args.pdf, out, layout=args.layout,
                              install=not args.no_install,
                              restart=not args.no_restart)
              if engine == "acrobat" else convert_word(args.pdf, out))
        if ok:
            print(f"docx    : {out}  "
                  f"({os.path.getsize(out) / 1e6:.1f} MB, via {engine})")
            if engine == "word":
                print("\nNOTE: Word's converter fragments running text into "
                      "many small text boxes,\n      so the translation may "
                      "read choppy. Acrobat Pro's export is cleaner.")
            return 0
    print("\nno converter worked.")
    print(NO_CONVERTER)
    return 2


if __name__ == "__main__":
    sys.exit(main())
