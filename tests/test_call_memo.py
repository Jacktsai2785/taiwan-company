import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from routers.call_memo import _save_memo_source, download_memo, extract_memo
from services.memo_extractor import infer_date_from_filename, prepare_transcript


class CallMemoDownloadTests(unittest.TestCase):
    @patch("routers.call_memo.memo_extractor.fill_template", return_value=b"docx")
    @patch("routers.call_memo.data_store.get_company")
    def test_chinese_filename_is_header_safe(self, get_company, _fill_template):
        get_company.return_value = {
            "name": "誠佳科紡",
            "call_memo": {"interview_date": "2026/07/29"},
        }

        response = download_memo("company-id")
        disposition = response.headers["content-disposition"]

        disposition.encode("latin-1")
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E8%AA%A0%E4%BD%B3%E7%A7%91%E7%B4%A1", disposition)


class CallMemoExtractTests(unittest.IsolatedAsyncioTestCase):
    @patch("routers.call_memo._record_memo_run")
    @patch("routers.call_memo._save_memo_source")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    @patch("routers.call_memo.extract_text")
    @patch("routers.call_memo.data_store.get_company")
    async def test_markdown_is_decoded_as_plain_text(
        self, get_company, extract_text, extract_with_audit, save_source, record_run
    ):
        get_company.return_value = {"name": "測試公司"}
        extract_with_audit.return_value = (
            {"interview_date": "2026/08/04"}, {"evidence": {}, "coverage": {}}
        )
        transcript = "# Podcast 逐字稿\n\n營收為新台幣一億元。"
        upload = Mock(filename="podcast.md")
        upload.read = AsyncMock(return_value=transcript.encode("utf-8"))

        result = await extract_memo("company-id", upload, {"engine": "claude"})

        extract_text.assert_not_called()
        save_source.assert_called_once_with(
            "company-id", "podcast.md", transcript.encode("utf-8")
        )
        extract_with_audit.assert_awaited_once_with(
            "測試公司", transcript, source_filename="podcast.md", engine="claude"
        )
        self.assertEqual(result["interview_date"], "2026/08/04")
        record_run.assert_called_once()

    @patch("routers.call_memo._record_memo_run")
    @patch("routers.call_memo._save_memo_source")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    @patch("routers.call_memo.data_store.get_company")
    async def test_missing_interview_date_is_not_replaced_with_today(
        self, get_company, extract_with_audit, _save_source, _record_run
    ):
        get_company.return_value = {"name": "測試公司"}
        extract_with_audit.return_value = (
            {"interview_date": ""}, {"evidence": {}, "coverage": {}}
        )
        upload = Mock(filename="podcast.txt")
        upload.read = AsyncMock(return_value="逐字稿".encode("utf-8"))

        result = await extract_memo("company-id", upload, {"engine": "claude"})

        self.assertEqual(result["interview_date"], "")


class CallMemoTranscriptPreparationTests(unittest.TestCase):
    def test_markdown_uses_only_raw_transcript_section(self):
        content = """---
generated_at: 2026-08-04
---
## 重點摘要
AI 先前產生的摘要
## 逐字稿
[00:00:01] 原始訪談內容
"""

        self.assertEqual(
            prepare_transcript(content, "joinx podcast 20260708.md"),
            "[00:00:01] 原始訪談內容\n",
        )

    def test_date_is_inferred_from_filename(self):
        self.assertEqual(
            infer_date_from_filename("joinx podcast 20260708.md"), "2026/07/08"
        )

    def test_invalid_filename_date_is_ignored(self):
        self.assertEqual(infer_date_from_filename("podcast 20261340.md"), "")

    @patch("routers.call_memo.data_store.update_company")
    def test_source_file_and_metadata_are_persisted(self, update_company):
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp)):
                source = _save_memo_source(
                    "company-id", "joinx podcast 20260708.md", b"transcript"
                )

            stored = Path(tmp) / "company-id" / source["stored_name"]
            self.assertEqual(stored.read_bytes(), b"transcript")
            self.assertEqual(source["filename"], "joinx podcast 20260708.md")
            self.assertEqual(source["size"], 10)
            update_company.assert_called_once_with(
                "company-id", {"call_memo_source": source}
            )


if __name__ == "__main__":
    unittest.main()
