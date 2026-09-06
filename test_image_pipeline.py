import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import zipfile

from PIL import Image, ImageDraw

import kindle_converter as converter


def word(left, top, width, height, text="text"):
    return {
        "left": left, "top": top, "width": width, "height": height,
        "text": text, "confidence": 95, "block": 1, "paragraph": 1, "line": 1,
    }


class ImagePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, image, name="page.png"):
        path = self.root / name
        image.save(path)
        return path

    def test_text_only_produces_no_local_candidate_and_no_vision_call(self):
        image = Image.new("RGB", (800, 1200), "white")
        draw = ImageDraw.Draw(image)
        words = []
        for row in range(20):
            for column in range(3):
                x, y = 90 + column * 180, 90 + row * 45
                draw.rectangle((x, y, x + 130, y + 12), fill="black")
                words.append(word(x, y, 130, 12))
        candidates = converter._local_illustration_candidates(self.save(image), words, 1)
        self.assertEqual(candidates, [])
        with mock.patch.object(converter, "_ai_verify_candidates") as vision:
            accepted, usage, fallback = converter._verify_candidates_or_fallback(
                self.root / "page.png", [], converter.ConversionOptions("Book"), None)
        vision.assert_not_called()
        self.assertEqual((accepted, usage, fallback), ([], {}, False))

    def test_ai_text_only_skips_entire_image_pipeline(self):
        text_only = converter.ConversionOptions(
            "Book", preserve_visual_pages=True, ai_cleanup=True, ai_illustrations=False)
        text_and_images = converter.ConversionOptions(
            "Book", preserve_visual_pages=True, ai_cleanup=True, ai_illustrations=True)
        local_only = converter.ConversionOptions(
            "Book", preserve_visual_pages=True, ai_cleanup=False, ai_illustrations=False)
        self.assertFalse(converter._should_process_images(text_only))
        self.assertTrue(converter._should_process_images(text_and_images))
        self.assertTrue(converter._should_process_images(local_only))

    def test_frozen_app_uses_user_writable_tessdata_directory(self):
        with mock.patch.object(converter.sys, "frozen", True, create=True), \
                mock.patch.object(converter.Path, "home", return_value=self.root):
            self.assertEqual(
                converter._local_tessdata_dir(),
                self.root / "Library" / "Application Support" / "PDF til Kindle" / "tessdata",
            )

    def test_bundled_tool_has_priority_in_frozen_resource_directory(self):
        tools = self.root / "tools"
        tools.mkdir()
        bundled = tools / "tesseract"
        bundled.write_text("test")
        with mock.patch.object(converter.sys, "_MEIPASS", str(self.root), create=True):
            self.assertEqual(converter._tool("tesseract"), str(bundled))

    @unittest.skipIf(converter.fitz is None, "PyMuPDF is not available")
    def test_jpeg_ocr_page_render_uses_pymupdf_without_poppler(self):
        pdf_path = self.root / "render.pdf"
        document = converter.fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_text((40, 80), "Standalone render")
        document.save(pdf_path)
        document.close()
        with mock.patch.object(converter, "_run", side_effect=AssertionError("Poppler was called")):
            rendered = converter._render_page(pdf_path, 1, self.root / "rendered", 144, None, "jpeg")
        self.assertTrue(rendered.is_file())
        with Image.open(rendered) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (600, 800))

    def test_small_black_and_white_drawing_is_detected(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((90, 210, 255, 315), outline="black", width=5)
        draw.ellipse((120, 230, 175, 285), outline="black", width=5)
        draw.line((180, 285, 235, 230), fill="black", width=5)
        candidates = converter._local_illustration_candidates(self.save(image), [], 1)
        self.assertTrue(candidates)
        self.assertTrue(any((c.bbox[2]-c.bbox[0]) < 500 for c in candidates))

    def test_full_page_illustration_is_not_rejected(self):
        image = Image.new("RGB", (500, 700), "white")
        draw = ImageDraw.Draw(image)
        for x in range(15, 485, 24):
            draw.line((x, 20, 500-x//3, 680), fill="black", width=7)
        candidates = converter._local_illustration_candidates(self.save(image), [], 1)
        self.assertTrue(candidates)
        self.assertTrue(any((c.bbox[2]-c.bbox[0]) * (c.bbox[3]-c.bbox[1]) > 500_000
                            for c in candidates))

    def test_scanned_text_page_is_not_a_full_page_illustration(self):
        image = Image.new("RGB", (700, 1000), "white")
        draw = ImageDraw.Draw(image)
        words = []
        for row in range(25):
            for column in range(4):
                x, y = 55 + column * 145, 40 + row * 35
                draw.rectangle((x, y, x + 105, y + 10), fill="black")
                words.append(word(x, y, 105, 10))
        candidates = converter._local_illustration_candidates(self.save(image), words, 1)
        self.assertFalse(any(c.bbox == (0, 0, 1000, 1000) for c in candidates))
        self.assertEqual(candidates, [])

    def test_scanned_page_photo_becomes_a_candidate(self):
        image = Image.new("RGB", (700, 1000), "white")
        draw = ImageDraw.Draw(image)
        words = []
        for row in range(10):
            x, y = 60, 55 + row * 35
            draw.rectangle((x, y, x + 560, y + 10), fill="black")
            words.append(word(x, y, 560, 10))
        for y in range(470, 810, 8):
            shade = 35 + (y % 110)
            draw.rectangle((130, y, 570, y + 8), fill=(shade, shade + 15, shade + 25))
        candidates = converter._local_illustration_candidates(self.save(image), words, 1)
        self.assertTrue(any(c.bbox[1] > 350 and c.bbox[3] > 700 for c in candidates))

    def test_refinement_keeps_diagram_labels_inside_candidate(self):
        image = Image.new("RGB", (1000, 1000), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((205, 205, 795, 795), outline="black", width=5)
        draw.text((225, 225), "Y AXIS", fill="black")
        draw.text((680, 750), "X AXIS", fill="black")
        refined = converter._refine_illustration_bbox(image, (180, 180, 820, 820))
        self.assertLessEqual(refined[0], 225)
        self.assertLessEqual(refined[1], 225)
        self.assertGreaterEqual(refined[2], 730)
        self.assertGreaterEqual(refined[3], 780)

    def test_refinement_removes_uniform_beige_scan_margin(self):
        image = Image.new("RGB", (800, 1000), (198, 189, 170))
        draw = ImageDraw.Draw(image)
        for y in range(35, 700, 8):
            shade = 25 + y % 160
            draw.rectangle((35, y, 765, y + 8), fill=(shade, max(0, shade-10), max(0, shade-18)))
        refined = converter._refine_illustration_bbox(image, (0, 0, 1000, 1000))
        self.assertLess(refined[3], 780, refined)
        self.assertGreater(refined[3], 650, refined)

    def test_caption_below_is_recognized_without_entering_crop(self):
        blocks = [converter.ContentBlock("paragraph", 610, "Figur 2. En maskine", bottom=632,
                                         x1=210, x2=790)]
        match = converter._caption_for_bbox(blocks, (200, 200, 800, 600), 1000, 1000)
        self.assertEqual(match[2:], (0, "Figur 2. En maskine"))

    def test_caption_above_is_recognized(self):
        blocks = [converter.ContentBlock("paragraph", 160, "Figure 4: Assembly", bottom=190,
                                         x1=210, x2=790)]
        match = converter._caption_for_bbox(blocks, (200, 200, 800, 600), 1000, 1000)
        self.assertEqual(match[2], 0)

    def test_two_column_anchor_uses_horizontal_geometry(self):
        blocks = [
            converter.ContentBlock("paragraph", 100, "left", bottom=280, x1=40, x2=460),
            converter.ContentBlock("paragraph", 230, "right before", bottom=295, x1=540, x2=960),
            converter.ContentBlock("paragraph", 520, "left later", bottom=590, x1=40, x2=460),
            converter.ContentBlock("paragraph", 540, "right later", bottom=610, x1=540, x2=960),
        ]
        anchor = converter._find_illustration_anchor(blocks, (580, 310, 930, 510), 1000, 1000)
        self.assertEqual(anchor, 1)

    def test_separate_images_stay_separate_and_overlap_prefers_native(self):
        first = converter.IllustrationCandidate(0, (100, 100, 350, 350), 1, .8, [], "local")
        separate = converter.IllustrationCandidate(1, (600, 100, 850, 350), 1, .7, [], "local")
        native = converter.IllustrationCandidate(2, (105, 105, 345, 345), 1, .9, [], "native")
        result = converter._deduplicate_candidates([first, separate, native])
        self.assertEqual(len(result), 2)
        self.assertIn("native", [item.source for item in result])

    def test_multiple_images_at_same_anchor_keep_top_to_bottom_order(self):
        text = [converter.ContentBlock("paragraph", 20, "before", bottom=40, x1=0, x2=500)]
        lower = converter.ContentBlock("image", 300, text="lower", anchor=0)
        upper = converter.ContentBlock("image", 100, text="upper", anchor=0)
        merged = converter._merge_illustrations(text, [lower, upper])
        self.assertEqual([block.text for block in merged], ["before", "upper", "lower"])

    @unittest.skipIf(converter.fitz is None, "PyMuPDF is not available")
    def test_native_photo_is_extracted_with_placement(self):
        photo = Image.new("RGB", (120, 80), (40, 120, 180))
        buffer = io.BytesIO(); photo.save(buffer, "JPEG")
        pdf_path = self.root / "native.pdf"
        document = converter.fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_text((60, 80), "A short paragraph")
        page.insert_image(converter.fitz.Rect(100, 180, 500, 480), stream=buffer.getvalue())
        document.save(pdf_path); document.close()
        result = converter._native_image_candidates(pdf_path, [3], self.root / "native")
        self.assertEqual(len(result[1]), 1)
        candidate = result[1][0]
        self.assertEqual(candidate.source, "native")
        self.assertTrue(candidate.asset_path.exists())
        self.assertGreater(candidate.bbox[2]-candidate.bbox[0], 500)

    @unittest.skipIf(converter.fitz is None, "PyMuPDF is not available")
    def test_full_page_native_raster_is_deferred_even_when_ocr_is_poor(self):
        scan = Image.new("RGB", (600, 800), (202, 194, 177))
        draw = ImageDraw.Draw(scan)
        for y in range(80, 500, 45):
            draw.rectangle((70, y, 520, y + 5), fill=(190, 183, 169))
        buffer = io.BytesIO(); scan.save(buffer, "JPEG")
        pdf_path = self.root / "scan.pdf"
        document = converter.fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_image(page.rect, stream=buffer.getvalue())
        document.save(pdf_path); document.close()
        result = converter._native_image_candidates(pdf_path, [0], self.root / "native-scan")
        self.assertEqual(result, {})

    def test_low_contrast_ghost_crop_is_rejected(self):
        ghost = Image.new("RGB", (800, 1200), (202, 194, 177))
        draw = ImageDraw.Draw(ghost)
        draw.rectangle((25, 690, 775, 1120), fill=(194, 188, 175))
        for y in range(90, 620, 55):
            draw.rectangle((80, y, 690, y + 4), fill=(197, 190, 176))
        path = self.save(ghost, "ghost.png")
        candidate = converter.IllustrationCandidate(
            0, (0, 0, 1000, 1000), 1, .8, [], asset_path=path)
        meaningful, metrics = converter._visual_content_quality(candidate)
        self.assertFalse(meaningful, metrics)

    def test_photo_and_sparse_line_drawing_pass_visual_quality_check(self):
        photo = Image.new("RGB", (500, 350), (220, 225, 230))
        draw = ImageDraw.Draw(photo)
        for y in range(0, 350, 10):
            draw.rectangle((0, y, 500, y + 10), fill=(20 + y % 180, 60, 130))
        photo_path = self.save(photo, "photo.png")
        line = Image.new("RGB", (500, 350), "white")
        line_draw = ImageDraw.Draw(line)
        line_draw.rectangle((50, 40, 450, 310), outline="black", width=5)
        line_draw.line((70, 280, 240, 90, 430, 250), fill="black", width=5)
        line_path = self.save(line, "line.png")
        for path in (photo_path, line_path):
            candidate = converter.IllustrationCandidate(
                0, (0, 0, 1000, 1000), 1, .8, [], asset_path=path)
            meaningful, metrics = converter._visual_content_quality(candidate)
            self.assertTrue(meaningful, (path.name, metrics))

    def test_ai_failure_falls_back_only_to_supplied_page_candidates(self):
        candidate = converter.IllustrationCandidate(0, (100, 100, 500, 500), 42, .8, [])
        with mock.patch.object(converter, "_ai_verify_candidates",
                               side_effect=converter.AIServiceError("offline")):
            accepted, usage, fallback = converter._verify_candidates_or_fallback(
                self.root / "missing.jpg", [candidate], converter.ConversionOptions("Book"), None)
        self.assertEqual(accepted, [candidate])
        self.assertEqual(usage, {})
        self.assertTrue(fallback)

    def test_same_page_candidates_are_batched_in_one_low_detail_call(self):
        page = self.save(Image.new("RGB", (800, 1000), "white"))
        candidates = [
            converter.IllustrationCandidate(0, (50, 100, 400, 450), 1, .7, []),
            converter.IllustrationCandidate(1, (550, 500, 950, 900), 1, .7, []),
        ]
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"figures": []}'},
        ]}], "usage": {"input_tokens": 100, "output_tokens": 5}}
        with mock.patch.object(converter, "_openai_response", return_value=response) as request:
            accepted, usage = converter._ai_verify_candidates(
                page, candidates, converter.ConversionOptions("Book", api_key="x"), None)
        self.assertEqual(accepted, [])
        self.assertEqual(request.call_count, 1)
        content = request.call_args.args[0]["input"][0]["content"]
        self.assertEqual(sum(item.get("type") == "input_image" for item in content), 2)
        self.assertTrue(all(item.get("detail") == "low" for item in content
                            if item.get("type") == "input_image"))
        self.assertEqual(usage["input_tokens"], 100)

    def test_invalid_low_detail_output_escalates_once_to_high_detail(self):
        page = self.save(Image.new("RGB", (800, 1000), "white"))
        candidate = converter.IllustrationCandidate(0, (100, 100, 700, 700), 1, .7, [])
        invalid = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "not json"},
        ]}], "usage": {"input_tokens": 10, "output_tokens": 2}}
        valid = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"figures": []}'},
        ]}], "usage": {"input_tokens": 20, "output_tokens": 3}}
        with mock.patch.object(converter, "_openai_response", side_effect=[invalid, valid]) as request:
            accepted, usage = converter._ai_verify_candidates(
                page, [candidate], converter.ConversionOptions("Book", api_key="x"), None)
        self.assertEqual(accepted, [])
        self.assertEqual(request.call_count, 2)
        high_content = request.call_args_list[1].args[0]["input"][0]["content"]
        self.assertEqual(next(item["detail"] for item in high_content
                              if item.get("type") == "input_image"), "high")
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["high_detail_calls"], 1)

    def test_openai_wait_status_advances_and_clears_on_response(self):
        statuses = []
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return b'{"output": []}'
        def delayed_response(*_args, **_kwargs):
            time.sleep(2.15)
            return FakeResponse()
        with mock.patch.object(converter.urllib.request, "urlopen", side_effect=delayed_response):
            converter._openai_response(
                {"model": "test", "input": "test"},
                converter.ConversionOptions("Book", api_key="x", ai_model="test"),
                status=statuses.append, max_attempts=1)
        self.assertTrue(any("· 1 sek. gået" in message for message in statuses))
        self.assertTrue(any("· 2 sek. gået" in message for message in statuses))
        self.assertEqual(statuses[-1], "OpenAI-svar modtaget")

    def test_openai_reaches_delayed_recovery_attempt_with_same_idempotency_key(self):
        class FakeResponse:
            headers = {"x-request-id": "req-recovered"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return b'{"output": []}'
        failures = [TimeoutError("temporary timeout") for _ in range(4)]
        with mock.patch.object(
                converter.urllib.request, "urlopen",
                side_effect=[*failures, FakeResponse()]) as request:
            payload = converter._openai_response(
                {"model": "test", "input": "test"},
                converter.ConversionOptions("Book", api_key="x", ai_model="test"),
                max_attempts=5, attempt_timeout=10, retry_delays=(0, 0, 0, 0))
        self.assertEqual(payload, {"output": []})
        self.assertEqual(request.call_count, 5)
        keys = {
            call.args[0].get_header("Idempotency-key")
            for call in request.call_args_list
        }
        self.assertEqual(len(keys), 1)
        self.assertNotIn(None, keys)

    def test_openai_does_not_retry_permanent_authentication_error(self):
        error = converter.urllib.error.HTTPError(
            "https://api.openai.com/v1/responses", 401, "Unauthorized", {},
            io.BytesIO(b'{"error":{"type":"invalid_api_key","message":"Invalid key"}}'),
        )
        with mock.patch.object(converter.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(converter.AIServiceError) as raised:
                converter._openai_response(
                    {"model": "test", "input": "test"},
                    converter.ConversionOptions("Book", api_key="x", ai_model="test"),
                    max_attempts=6, attempt_timeout=10, retry_delays=(0,))
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(request.call_count, 1)

    def test_response_text_joins_all_output_text_parts(self):
        payload = {
            "id": "resp_test", "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": '{"items":['},
                {"type": "output_text", "text": "]}"},
            ]}],
        }
        self.assertEqual(converter._response_text(payload), '{"items":[]}')

    def test_response_text_reports_incomplete_reason(self):
        payload = {
            "id": "resp_test", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"}, "output": [],
        }
        with self.assertRaisesRegex(converter.AIStructuredOutputError, "max_output_tokens"):
            converter._response_text(payload)

    def test_ai_request_retries_invalid_structured_response_once(self):
        invalid = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "not json"},
        ]}], "usage": {"input_tokens": 10, "output_tokens": 2}}
        valid = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"items":[{"id":0,"text":"Rettet"}]}'},
        ]}], "usage": {"input_tokens": 11, "output_tokens": 3}}
        statuses = []
        with mock.patch.object(converter, "_openai_response", side_effect=[invalid, valid]) as request:
            corrected, usage = converter._ai_request(
                [{"id": 0, "text": "Rettat"}],
                converter.ConversionOptions("Book", api_key="x", ai_model="test"),
                None, statuses.append)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(corrected, {0: "Rettet"})
        self.assertEqual(usage, {"input_tokens": 21, "output_tokens": 5})
        self.assertTrue(any("prøver tekstportionen igen" in message for message in statuses))

    def test_ai_cleanup_preserves_successful_batches_if_later_batch_is_invalid(self):
        first = converter.ContentBlock("paragraph", 0, "A" * 3000)
        second = converter.ContentBlock("paragraph", 10, "B" * 3000)
        warnings = []
        with mock.patch.object(
                converter, "_ai_request",
                side_effect=[({0: "Første portion rettet"}, {"input_tokens": 10, "output_tokens": 5}),
                             converter.AIStructuredOutputError("invalid")]):
            result = converter._ai_cleanup(
                [[first, second]], converter.ConversionOptions("Book", ai_model="test"),
                lambda *_: None, None, warning=warnings.append)
        self.assertEqual(first.text, "Første portion rettet")
        self.assertEqual(second.text, "B" * 3000)
        self.assertEqual(result, {"completed_batches": 1, "failed_batches": 1, "total_batches": 2})
        self.assertEqual(len(warnings), 1)

    def test_epub_deduplicates_asset_but_keeps_occurrences_and_caption(self):
        asset = self.save(Image.new("RGB", (80, 60), "navy"), "shared.png")
        pages = [[
            converter.ContentBlock("paragraph", 10, "Before", bottom=20, x1=0, x2=500),
            converter.ContentBlock("image", 30, image=asset, caption="Figur 1"),
            converter.ContentBlock("image", 80, image=asset, caption="Figur 1 igen", caption_above=True),
        ]]
        epub = self.root / "book.epub"
        converter._write_epub(epub, pages, converter.ConversionOptions("Book"))
        converter.validate_epub(epub)
        with zipfile.ZipFile(epub) as archive:
            images = [name for name in archive.namelist() if name.startswith("EPUB/images/")]
            xhtml = "".join(archive.read(name).decode() for name in archive.namelist()
                            if name.endswith(".xhtml") and "/text/" in name)
        self.assertEqual(len(images), 1)
        self.assertEqual(xhtml.count("<img "), 2)
        self.assertEqual(xhtml.count("<figcaption>"), 2)
        self.assertIn("<figcaption>Figur 1 igen</figcaption><img", xhtml)


if __name__ == "__main__":
    unittest.main()
