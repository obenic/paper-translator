import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract_paper


class ExtractionGateTests(unittest.TestCase):
    def run_scan(self, with_ocr=False):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scan.pdf"
            with fitz.open() as text_pdf:
                page = text_pdf.new_page()
                page.insert_text((72, 72), "Scanned text for extraction gate test.")
                raster = page.get_pixmap()
                with fitz.open() as scan:
                    scan.new_page().insert_image(page.rect, pixmap=raster)
                    scan.save(source)
            argv = ["extract_paper.py", str(source), "-o", str(Path(tmp) / "out")]
            if with_ocr:
                argv.append("--ocr")
            output = io.StringIO()
            with patch.object(sys, "argv", argv), \
                    patch.object(extract_paper, "make_ocr_engine",
                                 return_value=(object(), "RapidOCR")), \
                    patch.object(extract_paper, "ocr_pixmap", return_value=""), \
                    contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(output):
                return extract_paper.main(), output.getvalue()

    def test_scan_without_ocr_cannot_report_success(self):
        code, message = self.run_scan()
        self.assertEqual(code, 4)
        self.assertIn("--ocr", message)

    def test_failed_ocr_cannot_report_success(self):
        code, message = self.run_scan(with_ocr=True)
        self.assertEqual(code, 1)
        self.assertIn("OCR", message)


if __name__ == "__main__":
    unittest.main()
