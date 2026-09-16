import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preflight
import ocr_runtime


class PreflightTests(unittest.TestCase):
    def probes(self, acrobat=False, rapidocr=False):
        flags = {
            "pymupdf": True, "lxml": True, "imaging": True, "docx": True,
            "converter": acrobat, "ocr": rapidocr, "vision": False,
            "pandoc": True, "browser": True,
        }
        return {
            key: preflight.Probe(key, key, ok, "test")
            for key, ok in flags.items()
        }

    def test_only_acrobat_and_rapidocr_are_capabilities(self):
        probes = self.probes(acrobat=True, rapidocr=True)
        self.assertEqual(
            [probe.key for probe in preflight.figure_caps(probes)],
            ["converter", "ocr"],
        )

    def test_each_supported_capability_can_pass(self):
        for acrobat, rapidocr in ((True, False), (False, True), (True, True)):
            with self.subTest(acrobat=acrobat, rapidocr=rapidocr):
                self.assertEqual(preflight.decide(self.probes(acrobat, rapidocr)), 0)

    def test_neither_capability_stops(self):
        self.assertEqual(preflight.decide(self.probes()), 4)

    def test_missing_pdf_reader_stops(self):
        probes = self.probes(True, True)
        probes["pymupdf"] = preflight.Probe("pymupdf", "PyMuPDF", False, "missing")
        self.assertEqual(preflight.decide(probes), 1)

    def test_word_installation_does_not_count_as_acrobat(self):
        with patch.object(preflight.sys, "platform", "win32"), \
                patch.object(preflight, "_spec", return_value=True), \
                patch.object(preflight, "_acrobat_exe", return_value=None), \
                patch.object(preflight, "_progid_registered", return_value=True):
            self.assertFalse(preflight.probe_converter().ok)

    def test_unrelated_packages_do_not_count_as_local_rapidocr(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ocr_runtime, "OCR_PYTHON", Path(tmp) / "missing"), \
                patch.object(preflight, "_spec", return_value=True), \
                patch("ocr_engine.available", return_value=["unrelated-engine"]):
            self.assertFalse(preflight.probe_ocr().ok)

    def test_rapidocr_requires_runtime_dependencies(self):
        with patch.object(ocr_runtime, "in_local_runtime", return_value=True), \
                patch.object(ocr_runtime.Path, "is_file", return_value=True), \
                patch.object(ocr_runtime.importlib, "import_module",
                             side_effect=ImportError("onnxruntime missing")):
            self.assertFalse(preflight.probe_ocr().ok)

    def test_global_packages_alone_do_not_pass_local_ocr_check(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ocr_runtime, "OCR_PYTHON", Path(tmp) / "missing"), \
                patch.object(preflight, "_spec", return_value=True):
            probe = preflight.probe_ocr()
        self.assertFalse(probe.ok)
        self.assertIn("ocr", probe.detail.lower())

    def test_install_hint_targets_skill_setup_not_global_pip(self):
        self.assertIn("setup_ocr.py", preflight.OCR_INSTALL)
        self.assertIn("--install", preflight.OCR_INSTALL)

    def test_stop_requires_explicit_install_consent(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(preflight.verdict(self.probes()), 4)
        message = out.getvalue() + err.getvalue()
        self.assertIn("consent", message.lower())
        self.assertIn("RapidOCR", message)
        self.assertIn(preflight.OCR_INSTALL, message)

    def test_obsolete_flags_are_rejected(self):
        for flag in ("--multimodal", "--allow-unverified"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [sys.executable, str(Path(preflight.__file__)), flag],
                    capture_output=True, text=True, encoding="utf-8",
                )
                self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
