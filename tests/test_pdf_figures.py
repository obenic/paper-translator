import sys
import unittest
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract_paper


class PdfFigureTests(unittest.TestCase):
    def test_dense_vector_plot_with_small_marks_is_detected(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            for row in range(16):
                for column in range(25):
                    page.draw_circle(
                        (80 + column * 8, 100 + row * 8), 0.5,
                        color=(0, 0, 0), fill=(0, 0, 0),
                    )
            regions = extract_paper.figure_regions(page)
            self.assertEqual(len(regions), 1)
            self.assertLessEqual(regions[0].x0, 80)
            self.assertGreaterEqual(regions[0].x1, 272)
            self.assertGreaterEqual(regions[0].y1, 220)

    def test_caption_continues_across_adjacent_pdf_text_blocks(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            lines = [
                "Figure 1. First caption line.",
                "Second caption line with panel descriptions.",
                "Third caption line with the final explanation.",
            ]
            for row, text in enumerate(lines):
                page.insert_text((60, 300 + row * 19), text, fontsize=11)
            page.insert_text((60, 377), "2. Methods", fontsize=12)
            captions = extract_paper.figure_captions(doc)
            self.assertEqual(len(captions), 1)
            self.assertEqual(captions[0]["text"], " ".join(lines))
            self.assertNotIn("Methods", captions[0]["text"])

    def test_thin_vector_plot_is_detected_but_header_rule_is_not(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.draw_line((60, 40), (540, 40), width=0.5)
            page.draw_line((80, 100), (80, 250), width=0.5)
            page.draw_line((80, 250), (280, 250), width=0.5)
            regions = extract_paper.figure_regions(page)
            self.assertEqual(len(regions), 1)
            self.assertGreater(regions[0].y0, 90)
            self.assertGreaterEqual(regions[0].y1, 250)

    def test_captions_do_not_consume_other_columns_or_next_caption(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((60, 300), "Figure 1. First figure.", fontsize=10)
            page.insert_text((330, 300), "Body in another column.", fontsize=10)
            page.insert_text((60, 316), "Continuation of the first caption.", fontsize=10)
            page.insert_text((60, 332), "Figure 2. Second figure.", fontsize=10)
            captions = extract_paper.figure_captions(doc)
            self.assertEqual([item["num"] for item in captions], [1, 2])
            self.assertEqual(captions[0]["text"],
                             "Figure 1. First figure. Continuation of the first caption.")
            self.assertEqual(captions[1]["text"], "Figure 2. Second figure.")

    def test_long_caption_is_not_truncated(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            lines = ["Figure 1. Detailed measurements."] + [
                f"Panel {i} shows synthesis parameters and measured diamond characteristics."
                for i in range(20)
            ]
            for row, text in enumerate(lines):
                page.insert_text((60, 250 + row * 16), text, fontsize=10)
            captions = extract_paper.figure_captions(doc)
            self.assertEqual(captions[0]["text"], " ".join(lines))

    def test_trailing_space_font_does_not_end_caption(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            first = "Figure 1. First caption line."
            last = "Continuation with a differently sized trailing space."
            page.insert_text((60, 300), first, fontsize=11)
            page.insert_text((60, 319), last, fontsize=11)
            end = 60 + fitz.get_text_length(last, fontsize=11)
            page.insert_text((end, 319), " ", fontsize=12)
            captions = extract_paper.figure_captions(doc)
            self.assertEqual(captions[0]["text"], first + " " + last)

    def test_small_header_logo_is_not_merged_with_first_figure(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            logo = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 33), False)
            logo.clear_with(100)
            page.insert_image(fitz.Rect(400, 35, 520, 68), pixmap=logo)
            page.draw_rect(fitz.Rect(80, 79, 520, 270), width=0.5)
            regions = extract_paper.figure_regions(page)
            self.assertEqual(len(regions), 1)
            self.assertGreaterEqual(regions[0].y0, 79)

    def test_short_body_text_with_citation_is_not_an_axis_label(self):
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.draw_rect(fitz.Rect(80, 150, 500, 370), width=0.5)
            page.insert_text((80, 143), "structures.[27,100,101]", fontsize=11)
            regions = extract_paper.figure_regions(page)
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].y0, 150)


if __name__ == "__main__":
    unittest.main()
