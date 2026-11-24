
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
    def assert_attempts(self, http_status, expected_attempts):
        attempts = []

        def respond(request):
            attempts.append(request.method)
            return http.Response(
                http_status,
                json={
                    "error": {
                        "code": http_status,
                        "message": "test response",
                    }
                },
            )

        sdk_client = process_pdf.genai.Client

        def create_client(**kwargs):
            kwargs["http_options"].httpx_client = http.Client(
                transport=http.MockTransport(respond), trust_env=False
            )
            return sdk_client(**kwargs)

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "unused-test-key"}),
            patch.object(process_pdf.genai, "Client", side_effect=create_client),
            patch("tenacity.nap.time.sleep"),
            self.assertRaises(process_pdf.errors.APIError) as raised,
        ):
            process_pdf.call_gemini_with_retry(
                ["synthetic input"], process_pdf.types.GenerateContentConfig()
            )

        self.assertEqual(raised.exception.code, http_status)
        self.assertEqual(attempts, ["POST"] * expected_attempts)

    def test_503_has_three_total_http_attempts(self):
        self.assert_attempts(503, 3)

    def test_429_is_not_retried(self):
        self.assert_attempts(429, 1)


if __name__ == "__main__":
    unittest.main()
