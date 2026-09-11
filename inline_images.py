#!/usr/bin/env python
"""Replace local Markdown image links with self-contained data URIs.

This is the final packaging step for paper-translator. It runs after figure
blocks have been moved to their first in-text mention, so the filename is no
longer needed for placement. The resulting Markdown can be delivered without
the temporary ``_figs`` or ``panels`` directory.

Exit codes:
    0  all local images embedded
    1  invalid input or an unreadable image
    3  one or more image destinations could not be embedded
"""

import argparse
import base64
import mimetypes
import shutil
import sys
from pathlib import Path


def image_spans(line: str):
    """Yield ``(dest_start, dest_end)`` for each Markdown image on a line."""
    i = 0
    while True:
        i = line.find("![", i)
        if i < 0:
            return
        j, depth = i + 2, 1
        while j < len(line) and depth:
            if line[j] == "[":
                depth += 1
            elif line[j] == "]":
                depth -= 1
            j += 1
        if depth or j >= len(line) or line[j] != "(":
            i += 2
            continue
        start = j + 1
        k, depth = start, 1
        while k < len(line) and depth:
            if line[k] == "(":
                depth += 1
            elif line[k] == ")":
                depth -= 1
            k += 1
        if depth:
            k = line.rfind(")") + 1
            if k <= start:
                i = start
                continue
        yield start, k - 1
        i = k


def destination_path(destination: str) -> str:
    """Remove optional angle brackets and a Markdown title suffix."""
    value = destination.strip()
    if value.startswith("<") and ">" in value:
        value = value[1:value.find(">")]
    else:
        value = value.split(" \"")[0].split(" '")[0].strip()
    return value


def mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def embed(text: str, base_dir: Path):
    """Return rewritten text, embedded count, and error messages."""
    lines = []
    embedded = 0
    errors = []
    for line_no, line in enumerate(text.split("\n"), 1):
        for start, end in reversed(list(image_spans(line))):
            raw = line[start:end]
            dest = destination_path(raw)
            if dest.startswith("data:"):
                continue
            if dest.startswith(("http://", "https://")):
                errors.append(f"line {line_no}: remote image is not self-contained: {dest}")
                continue
            path = (base_dir / dest).resolve()
            if not path.is_file():
                errors.append(f"line {line_no}: missing image: {dest}")
                continue
            try:
                payload = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                errors.append(f"line {line_no}: cannot read {dest}: {exc}")
                continue
            uri = f"data:{mime_for(path)};base64,{payload}"
            line = f"{line[:start]}{uri}{line[end:]}"
            embedded += 1
        lines.append(line)
    return "\n".join(lines), embedded, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("md", help="Markdown file containing local image links")
    ap.add_argument("-o", "--out", help="output Markdown; default rewrites in place")
    ap.add_argument("--no-backup", action="store_true",
                    help="do not keep a .bak when rewriting in place")
    args = ap.parse_args()

    source = Path(args.md).resolve()
    if not source.is_file():
        print(f"ERROR: not found: {source}", file=sys.stderr)
        return 1
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {source}: {exc}", file=sys.stderr)
        return 1

    rewritten, embedded, errors = embed(text, source.parent)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 3

    target = Path(args.out).resolve() if args.out else source
    target.parent.mkdir(parents=True, exist_ok=True)
    if target == source and not args.no_backup:
        backup = source.with_suffix(source.suffix + ".bak")
        shutil.copy2(source, backup)
    try:
        target.write_text(rewritten, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"ERROR: cannot write {target}: {exc}", file=sys.stderr)
        return 1

    print(f"markdown : {target}")
    print(f"images   : {embedded} embedded as data URI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
