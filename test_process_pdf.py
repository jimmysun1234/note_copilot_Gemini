
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import httpx2 as http
except ImportError:
    import httpx as http

import process_pdf


class PdfQueueTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, previous)
        self.old = Path("原始檔案", "電力電子", "2026-10-05.pdf")
        self.old.parent.mkdir(parents=True)
        self.old.write_bytes(b"old pdf")
        notes = Path("notes", "電力電子")
        notes.mkdir(parents=True)
        (notes / "2026-10-05_筆記.md").write_text(
            f"> 原始檔案：`{self.old.as_posix()}`\n", encoding="utf-8"
        )

    @patch.object(process_pdf, "is_valid_pdf", return_value=True)
    @patch.object(process_pdf, "process_single_pdf")
    def test_all_existing_notes_make_zero_generation_calls(self, process_one, _):
        process_pdf.process_all_unprocessed_pdfs()
        process_one.assert_not_called()

    @patch.object(process_pdf, "is_valid_pdf", return_value=True)
    @patch.object(process_pdf, "process_single_pdf")
    def test_only_unprocessed_pdf_is_generated(self, process_one, _):
        new = self.old.with_name("2026-10-12.pdf")
        new.write_bytes(b"new pdf")
        process_pdf.process_all_unprocessed_pdfs()
        process_one.assert_called_once_with(str(new))


    @patch.object(process_pdf, "is_valid_pdf", return_value=True)
    @patch.object(process_pdf, "process_single_pdf")
    def test_app_upload_is_found_and_old_copy_is_skipped(self, process_one, _):
        new = Path("incoming", "電力電子", "2026-11-24.pdf")
        new.parent.mkdir(parents=True)
        new.write_bytes(b"new app pdf")
        Path("incoming", "電力電子_板書.pdf").write_bytes(self.old.read_bytes())

        self.assertEqual(process_pdf.main(), 0)
        process_one.assert_called_once_with(str(new))


class GeminiRetryTest(unittest.TestCase):
    def assert_attempts(self, statuses, expected_models):
        attempts = []
        responses = iter(statuses)
        fake_note = (
            "===TOPIC===測試標題\n===SUMMARY===測試摘要\n"
            "===NOTES===測試正文\n===DIAGRAMS===NONE\n===QUESTION===NONE"
        )

        def respond(request):
            attempts.append(request.url.path.rsplit("/", 1)[-1].split(":", 1)[0])
            code = next(responses)
            if code == 200:
                return http.Response(
                    code,
                    json={"candidates": [{
                        "content": {"role": "model", "parts": [{"text": fake_note}]},
                        "finishReason": "STOP",
                    }]},
                )
            return http.Response(
                code,
                json={"error": {"code": code, "message": "test response"}},
            )

        sdk_client = process_pdf.genai.Client

        def create_client(**kwargs):
            kwargs["http_options"].httpx_client = http.Client(
                transport=http.MockTransport(respond), trust_env=False
            )
            return sdk_client(**kwargs)

        def generate():
            return process_pdf.call_gemini_with_retry(
                ["synthetic input"], process_pdf.types.GenerateContentConfig()
            )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unused-test-key"}),
            patch.object(process_pdf.genai, "Client", side_effect=create_client),
            patch("tenacity.nap.time.sleep"),
        ):
            if statuses[-1] == 200:
                self.assertEqual(process_pdf.parse_tagged_response(generate().text)["notes"], "測試正文")
            else:
                with self.assertRaises(process_pdf.errors.APIError) as raised:
                    generate()
                self.assertEqual(raised.exception.code, statuses[-1])

        self.assertEqual(attempts, expected_models)

    def test_503_has_three_total_http_attempts(self):
        self.assert_attempts(
            [503, 503, 503],
            ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )

    def test_alternate_model_can_generate_the_note(self):
        self.assert_attempts(
            [503, 503, 200],
            ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
        )

    def test_primary_success_does_not_call_the_alternate_model(self):
        self.assert_attempts([200], ["gemini-3.8-flash"])

    def test_429_is_not_retried(self):
        self.assert_attempts([429], ["gemini-3.8-flash"])

    def test_incomplete_model_response_does_not_become_a_note(self):
        with self.assertRaisesRegex(ValueError, "缺少標題、摘要或正文"):
            process_pdf.parse_tagged_response("===TOPIC===只有標題")


if __name__ == "__main__":
    unittest.main()
