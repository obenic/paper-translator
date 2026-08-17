#!/usr/bin/env python
"""Render a translated Markdown paper to PDF.

Pipeline: Markdown --pandoc--> self-contained HTML --Chrome/Edge--> PDF

Images are embedded as data URIs by pandoc, so the PDF never depends on
the figures folder staying next to the Markdown. Chrome supplies the CJK
font rendering and page breaking, so no LaTeX install is required.

Usage:
    python md_to_pdf.py <input.md> [-o out.pdf] [--font serif|sans]

Exit codes:
    0  PDF written and verified
    1  error (missing pandoc / no browser / render failed)
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BROWSERS = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/microsoft-edge",
]

# Latin faces come first so ASCII uses a real text face; per-character
# fallback then hands CJK to the Han fonts. Putting SimSun first would
# render Latin with SimSun's poor typewriter-like glyphs.
FONTS = {
    "serif": 'Georgia, "Times New Roman", "Source Han Serif SC", '
             '"Noto Serif CJK SC", SimSun, serif',
    "sans": '"Segoe UI", "Microsoft YaHei", "Source Han Sans SC", '
            '"Noto Sans CJK SC", sans-serif',
}

CSS = """
@page {{ size: A4; margin: 20mm 18mm; }}
body {{
  font-family: {body_font};
  font-size: 10.5pt; line-height: 1.75; color: #1a1a1a;
  max-width: none; margin: 0;
  text-align: justify;
}}
h1 {{ font-family: {head_font}; font-size: 19pt; line-height: 1.4;
     margin: 0 0 1.2em; padding-bottom: .4em; border-bottom: 2px solid #333; }}
h2 {{ font-family: {head_font}; font-size: 14pt; margin: 1.8em 0 .7em;
     padding-left: .5em; border-left: 4px solid #333; break-after: avoid; }}
h3 {{ font-family: {head_font}; font-size: 11.5pt; margin: 1.3em 0 .5em;
     break-after: avoid; }}
p {{ margin: 0 0 .8em; }}

/* The Markdown supplies its own H1; pandoc's metadata title would duplicate it */
h1.title, header#title-block-header {{ display: none; }}

/* implicit_figures turns "![caption](img)" into figure+figcaption, which is
   the only reliable way to stop a page break landing between a figure and
   its caption - and a caption printed under the wrong figure is worse than
   no caption at all. */
figure {{ break-inside: avoid; page-break-inside: avoid; margin: 1.4em 0; }}
/* Cap the image height so image + caption still fit one page. break-inside is
   only honoured while the block fits; a tall portrait figure (a 951x1373 plot
   plus a five-line caption) overflows the page box, Chrome gives up, and the
   caption lands alone on the next page under nothing. */
figure img {{ margin: 0 auto .5em; max-height: 72vh; width: auto; }}
figcaption {{ font-size: 9pt; line-height: 1.65; color: #2a2a2a;
             text-align: left; }}
img {{ max-width: 100%; height: auto; display: block; margin: .8em auto; }}

blockquote {{
  background: #f6f7f9; border-left: 3px solid #8a8f98;
  margin: 0 0 1.4em; padding: .8em 1em; font-size: 9.5pt;
  break-inside: avoid; text-align: left;
}}
blockquote p {{ margin: .25em 0; }}

table {{ border-collapse: collapse; width: 100%; font-size: 9pt;
        margin: 1em 0; break-inside: avoid; }}
th, td {{ border: 1px solid #c8ccd2; padding: .4em .6em; text-align: left; }}
th {{ background: #eef0f3; font-family: {head_font}; }}

code {{ font-family: Consolas, "Courier New", monospace; font-size: 9pt;
       background: #f0f1f3; padding: .1em .35em; border-radius: 3px; }}
pre {{ background: #f6f7f9; padding: .8em; overflow-x: auto;
      border-radius: 4px; break-inside: avoid; }}
pre code {{ background: none; padding: 0; }}

/* Reference lists: dense, left-aligned, English */
ol {{ padding-left: 1.6em; }}
ol li {{ margin-bottom: .3em; font-size: 9.5pt; text-align: left; }}

hr {{ border: none; border-top: 1px solid #d5d8dd; margin: 2em 0; }}
a {{ color: #1a4d8f; text-decoration: none; word-break: break-all; }}
em {{ color: #555; }}
"""


def find_browser():
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    for name in ("chrome", "google-chrome", "chromium", "msedge",
                 "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("-o", "--out")
    ap.add_argument("--font", choices=("serif", "sans"), default="serif",
                    help="body typeface; headings always use the sans stack")
    ap.add_argument("--keep-html", action="store_true",
                    help="keep the intermediate HTML for debugging")
    args = ap.parse_args()

    md = Path(args.md).resolve()
    if not md.exists():
        print(f"ERROR: not found: {md}", file=sys.stderr)
        return 1

    out = Path(args.out).resolve() if args.out else md.with_suffix(".pdf")

    if not shutil.which("pandoc"):
        print("ERROR: pandoc not found. https://pandoc.org/installing.html",
              file=sys.stderr)
        return 1
    browser = find_browser()
    if not browser:
        print("ERROR: no Chrome/Edge found for PDF rendering.", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="md2pdf_"))
    css_file = tmp / "style.css"
    css_file.write_text(
        CSS.format(body_font=FONTS[args.font], head_font=FONTS["sans"]),
        encoding="utf-8")
    html = (md.with_suffix(".html") if args.keep_html else tmp / "doc.html")

    # Markdown -> self-contained HTML (images become data URIs)
    cmd = [
        "pandoc", str(md), "-f", "gfm+implicit_figures", "-t", "html5",
        "--standalone", "--embed-resources", "--css", str(css_file),
        "--metadata", f"title={md.stem}",
        "--resource-path", str(md.parent),
        "-o", str(html),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print(f"ERROR: pandoc failed:\n{r.stderr}", file=sys.stderr)
        return 1

    # HTML -> PDF. virtual-time-budget lets large embedded images finish
    # decoding before the page is printed.
    out.parent.mkdir(parents=True, exist_ok=True)
    for headless in ("--headless=new", "--headless"):
        r = subprocess.run([
            browser, headless, "--disable-gpu", "--no-sandbox",
            "--no-pdf-header-footer", "--virtual-time-budget=20000",
            f"--print-to-pdf={out}", html.as_uri(),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        if out.exists():
            break

    if not out.exists():
        print("ERROR: browser produced no PDF.", file=sys.stderr)
        print((r.stderr or "")[-800:], file=sys.stderr)
        return 1

    pages = "?"
    try:
        import fitz
        with fitz.open(out) as d:
            pages = len(d)
    except Exception:
        pass

    if not args.keep_html:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"pdf    : {out}")
    print(f"size   : {out.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"pages  : {pages}")
    print(f"font   : {args.font}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
