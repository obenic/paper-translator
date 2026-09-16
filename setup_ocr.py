#!/usr/bin/env python
"""Install OCR under this skill only, after explicit user consent."""

import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import ocr_runtime as runtime


def cached_models():
    """Reuse only the invoking interpreter's known cache during explicit setup."""
    spec = importlib.util.find_spec("rapidocr")
    if spec is not None and spec.origin:
        return Path(spec.origin).parent / "models"
    return None


def install():
    source = cached_models()
    if runtime.VENV_ROOT.exists() and not (runtime.VENV_ROOT / "pyvenv.cfg").is_file():
        raise RuntimeError(f"Refusing to overwrite a non-venv directory: {runtime.VENV_ROOT}")
    if not runtime.OCR_PYTHON.is_file():
        venv.EnvBuilder(with_pip=True).create(runtime.VENV_ROOT)

    command = [str(runtime.OCR_PYTHON), "-E", "-s"]
    # One OpenCV distribution in an isolated venv; global packages are untouched.
    subprocess.run([
        *command, "-m", "pip", "install", "--disable-pip-version-check",
        "-r", str(runtime.SKILL_ROOT / "requirements-ocr.txt"),
    ], check=True)
    runtime.MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    if source and source.is_dir():
        for name in runtime.MODEL_HASHES:
            target = runtime.MODEL_ROOT / name
            if not target.exists() and (source / name).is_file():
                shutil.copy2(source / name, target)
                print(f"copied cached model: {target}", flush=True)

    # Initialization downloads only missing/invalid weights to the local model root.
    subprocess.run([
        *command, "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from rapidocr import RapidOCR; import ocr_runtime as r; "
        "from rapidocr.utils.typings import ModelType; "
        "RapidOCR(params={'Global.model_root_dir': str(r.MODEL_ROOT), "
        "'Global.log_level': 'error', 'Det.model_type': ModelType.SMALL, "
        "'Rec.model_type': ModelType.SMALL})",
        str(runtime.SKILL_ROOT),
    ], check=True)
    # Do not let a working Acrobat hide a failed OCR installation.
    subprocess.run([
        *command, "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import ocr_runtime as r; "
        "ok, detail = r.check_local_ocr(); print(detail); sys.exit(0 if ok else 4)",
        str(runtime.SKILL_ROOT),
    ], check=True)
    subprocess.run([*command, str(runtime.SKILL_ROOT / "preflight.py")], check=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true",
                        help="use ONLY after user consent to download/install local OCR")
    args = parser.parse_args()
    if not args.install:
        print(f"Obtain user consent before installing RapidOCR into {runtime.OCR_ROOT}.")
        print(f"After consent: {runtime.INSTALL_HINT}")
        return 4
    try:
        return install()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"OCR setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
