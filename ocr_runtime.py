"""Skill-local OCR paths, read-only checks and CLI interpreter selection."""

import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys

SKILL_ROOT = Path(__file__).resolve().parent
OCR_ROOT = SKILL_ROOT / "ocr"
VENV_ROOT = OCR_ROOT / ".venv"
OCR_PYTHON = VENV_ROOT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MODEL_ROOT = OCR_ROOT / "models"
INSTALL_HINT = f'python "{SKILL_ROOT / "setup_ocr.py"}" --install'

# RapidOCR 3.9.2 default_models.yaml: PP-OCRv6 small plus direction classifier.
MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx":
        "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx":
        "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx":
        "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}
OCR_MODULES = (
    "rapidocr", "onnxruntime", "cv2", "numpy", "PIL", "shapely",
    "pyclipper", "omegaconf", "colorlog", "requests", "tqdm", "six", "yaml",
)


def in_local_runtime():
    # Compare prefixes, not executable targets: POSIX venv Python is a symlink.
    return Path(sys.prefix).resolve() == VENV_ROOT.resolve()


def relaunch_in_local_runtime(script):
    """CLI-only: preserve arguments and status, never install or scan elsewhere."""
    if OCR_PYTHON.is_file() and not in_local_runtime():
        result = subprocess.run([
            str(OCR_PYTHON), "-E", "-s", str(Path(script).resolve()), *sys.argv[1:]
        ])
        raise SystemExit(result.returncode)


def valid_model(path, expected_hash):
    try:
        if not path.resolve().is_relative_to(MODEL_ROOT.resolve()):
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == expected_hash
    except OSError:
        return False


def check_local_ocr():
    """No engine initialization or network access; global packages never count."""
    if not OCR_PYTHON.is_file():
        return False, f"MISSING skill-local OCR interpreter: {OCR_PYTHON}"
    if not in_local_runtime():
        return False, f"wrong interpreter; use skill-local OCR: {OCR_PYTHON}"

    for name in OCR_MODULES:
        try:
            module = importlib.import_module(name)
            origin = getattr(module, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(VENV_ROOT.resolve()):
                return False, f"{name} is outside skill-local OCR: {VENV_ROOT}"
        except Exception as exc:
            return False, f"skill-local OCR dependency {name}: {type(exc).__name__}: {exc}"

    invalid = [
        name for name, digest in MODEL_HASHES.items()
        if not valid_model(MODEL_ROOT / name, digest)
    ]
    if invalid:
        return False, f"MISSING or corrupt OCR models in {MODEL_ROOT}: {', '.join(invalid)}"
    return True, f"local packages: {VENV_ROOT}; 3 verified models: {MODEL_ROOT}"
