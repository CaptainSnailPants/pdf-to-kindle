import re
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

try:  # Prefer an installed PyMuPDF over the macOS-only vendored copy.
    import pymupdf  # noqa: F401
except ImportError:
    pass

import kindle_converter as converter

BODY = ("This is ordinary body text from a digital PDF. It wraps over several lines so that "
        "the text layer contains plenty of real words for the converter to use. ")


def word(left, top, width, height, text, block=1, paragraph=1, line=1):
    return {"text": text, "left": left, "top": top, "width": width, "height": height,
            "block": block, "paragraph": paragraph, "line": line}


@unittest.skipUnless(converter.fitz is not None, "PyMuPDF is not available")
class TextLayerConversionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        patcher = mock.patch.object(converter, "ensure_ocr_language", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def make_pdf(self, pages):
        """pages: list of 'text', 'scan' or 'table'."""
        document = converter.fitz.open()
        for number, kind in enumerate(pages, 1):
            page = document.new_page(width=420, height=595)
            if kind == "text":
                words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
                unique = " ".join(words[(number + i) % len(words)] for i in range(40))
                page.insert_textbox(converter.fitz.Rect(40, 40, 380, 560),
                                    f"Digital page {number}. {unique}. " + BODY * 3, fontsize=10)
            elif kind == "table":
                page.insert_textbox(converter.fitz.Rect(40, 40, 380, 150), BODY * 2, fontsize=10)
                left, top, cell_w, cell_h = 60, 200, 100, 24
                cells = [["Name", "Pages", "Year"], ["Alpha", "120", "1999"], ["Beta", "340", "2004"]]
                for row, values in enumerate(cells):
                    for column, value in enumerate(values):
                        rect = converter.fitz.Rect(left + column * cell_w, top + row * cell_h,
                                                   left + (column + 1) * cell_w, top + (row + 1) * cell_h)
                        page.draw_rect(rect, color=(0, 0, 0), width=0.8)
                        page.insert_text((rect.x0 + 6, rect.y0 + 16), value, fontsize=10)
                page.insert_textbox(converter.fitz.Rect(40, 300, 380, 560), BODY * 2, fontsize=10)
            else:
                image = Image.new("RGB", (840, 1190), "white")
                draw = ImageDraw.Draw(image)
                for line in range(25):
                    draw.text((80, 80 + line * 40), f"Scanned line {line}", fill="black")
                scan = self.root / f"scan-{number}.png"
                image.save(scan)
                page.insert_image(page.rect, filename=str(scan))
        path = self.root / "book.pdf"
        document.save(path)
        document.close()
        return path

    def convert(self, pdf, ocr, **options):
        output = self.root / "book.epub"
        with mock.patch.object(converter, "_ocr_layout", side_effect=ocr):
            converter.convert_pdf_to_epub(pdf, output, converter.ConversionOptions(
                "Book", preserve_visual_pages=False, **options))
        with zipfile.ZipFile(output) as archive:
            return "".join(archive.read(name).decode() for name in sorted(archive.namelist())
                           if name.startswith("EPUB/text/"))

    @staticmethod
    def fake_ocr(calls, delay=0.0):
        lock = threading.Lock()

        def ocr(image, language, cancel_event=None):
            number = int(re.search(r"page-(\d+)", image.name).group(1))
            with lock:
                calls.append(number)
            time.sleep(delay)
            return [word(100, 100 + line * 40, 600, 24, f"OCRPAGE{number}", paragraph=line)
                    for line in range(3)]
        return ocr

    def test_digital_pages_use_text_layer_and_scans_use_ocr(self):
        pdf = self.make_pdf(["text", "scan", "text"])
        calls = []
        text = self.convert(pdf, self.fake_ocr(calls))
        self.assertEqual(calls, [2])
        self.assertIn("Digital page 1.", text)
        self.assertIn("Digital page 3.", text)
        self.assertIn("OCRPAGE2", text)
        self.assertLess(text.index("Digital page 1."), text.index("OCRPAGE2"))
        self.assertLess(text.index("OCRPAGE2"), text.index("Digital page 3."))

    def test_text_layer_can_be_turned_off(self):
        pdf = self.make_pdf(["text", "text"])
        calls = []
        text = self.convert(pdf, self.fake_ocr(calls), use_text_layer=False)
        self.assertEqual(sorted(calls), [1, 2])
        self.assertNotIn("Digital page", text)

    def test_parallel_ocr_keeps_page_order(self):
        pdf = self.make_pdf(["scan"] * 6)
        calls = []
        text = self.convert(pdf, self.fake_ocr(calls, delay=0.2))
        self.assertEqual(sorted(calls), list(range(1, 7)))
        positions = [text.index(f"OCRPAGE{number}") for number in range(1, 7)]
        self.assertEqual(positions, sorted(positions))

    def test_cancel_stops_parallel_ocr(self):
        pdf = self.make_pdf(["scan"] * 6)
        cancel = threading.Event()
        calls = []

        def ocr(image, language, cancel_event=None):
            calls.append(image.name)
            cancel.set()
            raise converter.ConversionCancelled("The conversion was cancelled.")

        with mock.patch.object(converter, "_ocr_layout", side_effect=ocr):
            with self.assertRaises(converter.ConversionCancelled):
                converter.convert_pdf_to_epub(pdf, self.root / "out.epub",
                                              converter.ConversionOptions("Book"), cancel_event=cancel)
        self.assertFalse((self.root / "out.epub").exists())

    def test_table_becomes_epub_table(self):
        pdf = self.make_pdf(["table"])
        text = self.convert(pdf, self.fake_ocr([]))
        self.assertIn("<table>", text)
        self.assertIn("<th>Name</th>", text)
        self.assertIn("<td>Beta</td>", text)
        # Table words are not repeated as loose paragraphs.
        self.assertEqual(text.count("Alpha"), 1)


class LayoutHelperTests(unittest.TestCase):
    def test_garbled_text_layer_is_rejected(self):
        good = [(0, 0, 10, 10, "word", 0, 0, i) for i in range(30)]
        garbled = [(0, 0, 10, 10, "��", 0, 0, i) for i in range(30)]
        too_little = [(0, 0, 10, 10, "word", 0, 0, i) for i in range(5)]
        self.assertTrue(converter._text_layer_is_usable(good))
        self.assertFalse(converter._text_layer_is_usable(garbled))
        self.assertFalse(converter._text_layer_is_usable(too_little))

    def test_two_columns_are_read_left_then_right(self):
        block = converter.ContentBlock
        blocks = [
            block("heading", 50, text="Title", bottom=90, x1=100, x2=900),
            block("paragraph", 120, text="R1", bottom=300, x1=520, x2=900),
            block("paragraph", 120, text="L1", bottom=300, x1=100, x2=480),
            block("paragraph", 320, text="R2", bottom=500, x1=520, x2=900),
            block("paragraph", 320, text="L2", bottom=500, x1=100, x2=480),
        ]
        ordered = [item.text for item in converter._order_columns(blocks, 1000)]
        self.assertEqual(ordered, ["Title", "L1", "L2", "R1", "R2"])

    def test_single_column_order_is_unchanged(self):
        block = converter.ContentBlock
        blocks = [block("paragraph", 300, text="B", x1=100, x2=900),
                  block("paragraph", 100, text="A", x1=100, x2=900)]
        self.assertEqual([b.text for b in converter._order_columns(blocks, 1000)], ["B", "A"])

    def test_small_numbered_text_at_page_bottom_is_a_footnote(self):
        words = [word(100, 100 + line * 40, 700, 24, "Body text line", block=1, line=line)
                 for line in range(20)]
        words.append(word(100, 1300, 500, 16, "1 A footnote.", block=2))
        blocks = converter._text_blocks(words, 1000, 1400)
        self.assertEqual(blocks[-1].css_class, "footnote")
        self.assertNotIn("footnote", [b.css_class for b in blocks[:-1]])

    def test_table_markup_escapes_cells(self):
        markup, plain = converter._table_markup([["A&B", "<x>"], ["1", None]])
        self.assertEqual(markup, "<table><tr><th>A&amp;B</th><th>&lt;x&gt;</th></tr>"
                                 "<tr><td>1</td><td></td></tr></table>")
        self.assertEqual(plain, "A&B | <x>\n1 | ")

    def test_paragraphs_inside_one_pdf_block_are_split(self):
        words = []
        for line, (left, right) in enumerate([(100, 900), (100, 900), (100, 500), (140, 900), (100, 900)]):
            words.append(word(left, 100 + line * 30, right - left, 24, f"w{line}", block=0, line=line))
        converter._split_native_paragraphs(words)
        self.assertEqual([w["paragraph"] for w in words], [0, 0, 0, 1, 1])


if __name__ == "__main__":
    unittest.main()
