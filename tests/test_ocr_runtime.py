import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_runtime as runtime
import setup_ocr


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.venv = root / "ocr" / ".venv"
        self.python = self.venv / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.models = root / "ocr" / "models"
        self.models.mkdir()
        self.model = self.models / "det.onnx"
        self.model.write_bytes(b"test model")
        self.digest = hashlib.sha256(b"test model").hexdigest()
        for name, value in {
            "VENV_ROOT": self.venv, "OCR_PYTHON": self.python,
            "MODEL_ROOT": self.models, "MODEL_HASHES": {"det.onnx": self.digest},
        }.items():
            p = patch.object(runtime, name, value)
            p.start()
            self.addCleanup(p.stop)

    @contextlib.contextmanager
    def local_packages(self):
        with patch.object(runtime.sys, "prefix", str(self.venv)), \
                patch.object(runtime.importlib, "import_module",
                             side_effect=lambda name: SimpleNamespace(
                                 __file__=str(self.venv / "site-packages" / name / "__init__.py")
                             )):
            yield

    def test_local_packages_and_valid_models_pass(self):
        with self.local_packages():
            ok, detail = runtime.check_local_ocr()
        self.assertTrue(ok, detail)
        self.assertIn(str(self.models), detail)

    def test_global_interpreter_is_rejected_even_with_local_files(self):
        with patch.object(runtime.sys, "prefix", str(self.venv.parent)):
            ok, detail = runtime.check_local_ocr()
        self.assertFalse(ok)
        self.assertIn("wrong interpreter", detail)
        self.assertIn(str(self.python), detail)

    def test_global_package_origin_is_rejected(self):
        with self.local_packages(), \
                patch.object(runtime.importlib, "import_module",
                             return_value=SimpleNamespace(__file__="C:/global/rapidocr.py")):
            ok, detail = runtime.check_local_ocr()
        self.assertFalse(ok)
        self.assertIn("outside skill-local OCR", detail)

    def test_missing_model_is_rejected_without_download(self):
        with self.local_packages(), patch.object(
                runtime, "MODEL_HASHES", {"missing.onnx": self.digest}):
            ok, detail = runtime.check_local_ocr()
        self.assertFalse(ok)
        self.assertIn("missing.onnx", detail)

    def test_corrupt_model_is_rejected(self):
        self.model.write_bytes(b"corrupt")
        with self.local_packages():
            ok, detail = runtime.check_local_ocr()
        self.assertFalse(ok)
        self.assertIn("corrupt", detail)

    def test_model_outside_local_root_is_rejected(self):
        outside = self.models.parent / "outside.onnx"
        outside.write_bytes(b"test model")
        self.assertFalse(runtime.valid_model(outside, self.digest))

    def test_broken_dependency_is_not_reported_as_missing_install(self):
        with self.local_packages(), patch.object(
                runtime.importlib, "import_module", side_effect=OSError("DLL load failed")):
            ok, detail = runtime.check_local_ocr()
        self.assertFalse(ok)
        self.assertIn("DLL load failed", detail)

    def test_launcher_preserves_arguments_and_exit_code(self):
        script = self.venv.parent / "preflight.py"
        with patch.object(runtime, "in_local_runtime", return_value=False), \
                patch.object(sys, "argv", [str(script), "--json"]), \
                patch.object(runtime.subprocess, "run",
                             return_value=subprocess.CompletedProcess([], 4)) as run:
            with self.assertRaises(SystemExit) as stopped:
                runtime.relaunch_in_local_runtime(script)
        self.assertEqual(stopped.exception.code, 4)
        self.assertEqual(run.call_args.args[0],
                         [str(self.python), "-E", "-s", str(script), "--json"])

    def test_launcher_does_not_recurse_in_local_runtime(self):
        with self.local_packages(), patch.object(
                runtime.subprocess, "run", side_effect=AssertionError("recursive launch")):
            runtime.relaunch_in_local_runtime("preflight.py")

    def test_missing_runtime_does_not_trigger_installation(self):
        with patch.object(runtime, "OCR_PYTHON", self.venv / "missing"), \
                patch.object(runtime.subprocess, "run",
                             side_effect=AssertionError("unexpected subprocess")):
            runtime.relaunch_in_local_runtime("preflight.py")
            self.assertFalse(runtime.check_local_ocr()[0])

    def test_setup_without_install_flag_has_no_side_effects(self):
        with patch.object(sys, "argv", ["setup_ocr.py"]), \
                patch.object(setup_ocr, "install",
                             side_effect=AssertionError("installation without consent")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = setup_ocr.main()
        self.assertEqual(code, 4)
        self.assertIn("consent", output.getvalue())

    def test_setup_refuses_non_venv_directory(self):
        with patch.object(setup_ocr, "cached_models", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                setup_ocr.install()


if __name__ == "__main__":
    unittest.main()
