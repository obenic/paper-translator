import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr_engine


class RapidEngine:
    __module__ = "rapidocr.test"

    def __init__(self, return_value):
        self.call = Mock(return_value=return_value)
        self.predict = Mock(side_effect=AssertionError("unexpected alternate API"))

    def __call__(self, rgb):
        return self.call(rgb)


class RapidOnlyTests(unittest.TestCase):
    def test_available_only_reports_local_rapidocr(self):
        with patch.object(ocr_engine.ocr_runtime, "check_local_ocr",
                          return_value=(True, "local")), \
                patch.object(importlib.util, "find_spec", return_value=object()):
            self.assertEqual(ocr_engine.available(), ["RapidOCR"])

    def test_missing_local_runtime_never_discovers_another_engine(self):
        with patch.object(ocr_engine.ocr_runtime, "check_local_ocr",
                          return_value=(False, "missing local runtime")), \
                patch.object(importlib.util, "find_spec", return_value=object()):
            self.assertEqual(ocr_engine.available(), [])

    def test_missing_runtime_reports_local_repair_and_consent(self):
        with patch.object(ocr_engine.ocr_runtime, "check_local_ocr",
                          return_value=(False, "missing local runtime")), \
                patch.object(ocr_engine, "available", return_value=[]), \
                patch.object(ocr_engine, "_build_rapid") as build:
            engine, backend, note = ocr_engine.make_engine()
        self.assertIsNone(engine)
        self.assertIsNone(backend)
        self.assertIn("missing local runtime", note)
        self.assertIn("user consent", note)
        self.assertIn(ocr_engine.INSTALL_RAPID, note)
        build.assert_not_called()

    def test_unsupported_engine_is_rejected(self):
        with patch.object(ocr_engine, "_build_rapid") as build:
            engine, backend, note = ocr_engine.make_engine(prefer="unsupported")
        self.assertIsNone(engine)
        self.assertIsNone(backend)
        self.assertIn("Only RapidOCR", note)
        build.assert_not_called()

    def test_default_and_explicit_calls_build_rapidocr(self):
        sentinel = object()
        with patch.object(ocr_engine.ocr_runtime, "check_local_ocr",
                          return_value=(True, "local")), \
                patch.object(ocr_engine, "_build_rapid", return_value=sentinel):
            for prefer in (None, "RapidOCR"):
                with self.subTest(prefer=prefer):
                    self.assertEqual(
                        ocr_engine.make_engine(prefer=prefer),
                        (sentinel, "RapidOCR", None),
                    )

    def test_initialization_failure_stops_with_local_repair_hint(self):
        with patch.object(ocr_engine.ocr_runtime, "check_local_ocr",
                          return_value=(True, "local")), \
                patch.object(ocr_engine, "_build_rapid",
                             side_effect=RuntimeError("invalid model")):
            engine, backend, note = ocr_engine.make_engine()
        self.assertIsNone(engine)
        self.assertIsNone(backend)
        self.assertIn("invalid model", note)
        self.assertIn(ocr_engine.INSTALL_RAPID, note)

    def test_read_uses_rapidocr_result_and_rgb_input(self):
        rgb = np.zeros((8, 8, 3), dtype=np.uint8)
        engine = RapidEngine(return_value=SimpleNamespace(
            txts=("sample",),
            boxes=np.array([[[1, 2], [5, 2], [5, 6], [1, 6]]]),
            scores=(0.9,),
        ))
        rows = ocr_engine.read(engine, rgb)
        self.assertIs(engine.call.call_args.args[0], rgb)
        engine.predict.assert_not_called()
        self.assertEqual(rows, [{"text": "sample", "box": (1, 2, 6, 7), "score": 0.9}])

    def test_empty_rapidocr_result_is_empty(self):
        engine = RapidEngine(return_value=SimpleNamespace(txts=None, boxes=None, scores=None))
        self.assertEqual(ocr_engine.read(engine, np.zeros((8, 8, 3), dtype=np.uint8)), [])

    def test_cli_initialization_failure_is_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.png"
            Image.new("RGB", (8, 8), "white").save(image)
            with patch.object(sys, "argv", ["ocr_engine.py", str(image)]), \
                    patch.object(ocr_engine, "available", return_value=["RapidOCR"]), \
                    patch.object(ocr_engine, "make_engine",
                                 return_value=(None, None, "invalid model")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(ocr_engine.main(), 0)


if __name__ == "__main__":
    unittest.main()
