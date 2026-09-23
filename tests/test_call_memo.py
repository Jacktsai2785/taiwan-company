import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from routers.call_memo import (
    _write_memo_source_file,
    download_memo,
    extract_memo,
    create_memo,
    list_memos,
    delete_memo,
)
from services.memo_extractor import infer_date_from_filename, prepare_transcript


class _FakeStore:
    """Minimal stand-in for services.data_store: one company, deepcopy semantics
    matching the real get_company/update_company so tests don't over-specify
    how many times internal helpers happen to call them."""

    def __init__(self, company: dict):
        self.company = company

    def get_company(self, company_id: str):
        if company_id != self.company.get("id"):
            return None
        return copy.deepcopy(self.company)

    def update_company(self, company_id: str, updates: dict):
        if company_id != self.company.get("id"):
            return None
        self.company.update(updates)
        return copy.deepcopy(self.company)


def _company_with_memo(**memo_overrides):
    memo = {"id": "memo-1", "interview_date": "", "label": ""}
    memo.update(memo_overrides)
    return {"id": "company-id", "name": "測試公司", "call_memos": [memo]}


class CallMemoDownloadTests(unittest.TestCase):
    @patch("routers.call_memo.memo_extractor.fill_template", return_value=b"docx")
    @patch("routers.call_memo.data_store")
    def test_chinese_filename_is_header_safe(self, data_store, _fill_template):
        store = _FakeStore({
            "id": "company-id",
            "name": "誠佳科紡",
            "call_memos": [{"id": "memo-1", "interview_date": "2026/07/29"}],
        })
        data_store.get_company.side_effect = store.get_company
        data_store.update_company.side_effect = store.update_company

        response = download_memo("company-id", "memo-1")
        disposition = response.headers["content-disposition"]

        disposition.encode("latin-1")
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E8%AA%A0%E4%BD%B3%E7%A7%91%E7%B4%A1", disposition)
        self.assertIn("callmemo_20260729.docx", disposition)

    @patch("routers.call_memo.data_store")
    def test_unknown_memo_id_is_404(self, data_store):
        store = _FakeStore(_company_with_memo())
        data_store.get_company.side_effect = store.get_company

        with self.assertRaises(HTTPException) as ctx:
            download_memo("company-id", "no-such-memo")
        self.assertEqual(ctx.exception.status_code, 404)


class CallMemoCrudTests(unittest.TestCase):
    @patch("routers.call_memo.data_store")
    def test_create_and_list_memos(self, data_store):
        store = _FakeStore({"id": "company-id", "name": "測試公司", "call_memos": []})
        data_store.get_company.side_effect = store.get_company
        data_store.update_company.side_effect = store.update_company

        created = create_memo("company-id")
        self.assertTrue(created["id"])
        self.assertEqual(created["interview_date"], "")

        memos = list_memos("company-id")
        self.assertEqual(len(memos), 1)
        self.assertEqual(memos[0]["id"], created["id"])

    @patch("routers.call_memo.data_store")
    def test_list_memos_migrates_legacy_single_call_memo(self, data_store):
        legacy_company = {
            "id": "company-id",
            "name": "測試公司",
            "call_memo": {"interview_date": "2026/07/29", "deal_source": "自行開發"},
            "call_memo_source": {"filename": "x.txt", "stored_name": "memo_source_x.txt"},
        }
        store = _FakeStore(legacy_company)
        data_store.get_company.side_effect = store.get_company
        data_store.update_company.side_effect = store.update_company

        memos = list_memos("company-id")

        self.assertEqual(len(memos), 1)
        self.assertEqual(memos[0]["interview_date"], "2026/07/29")
        self.assertEqual(memos[0]["deal_source"], "自行開發")
        self.assertEqual(memos[0]["source"]["filename"], "x.txt")
        # migration is persisted so the next read doesn't redo the wrap
        self.assertEqual(len(store.company["call_memos"]), 1)

    @patch("routers.call_memo.data_store")
    def test_delete_memo_removes_it_and_returns_remaining(self, data_store):
        company = {
            "id": "company-id",
            "name": "測試公司",
            "call_memos": [
                {"id": "memo-1", "interview_date": "2026/07/01"},
                {"id": "memo-2", "interview_date": "2026/07/02"},
            ],
        }
        store = _FakeStore(company)
        data_store.get_company.side_effect = store.get_company
        data_store.update_company.side_effect = store.update_company

        result = delete_memo("company-id", "memo-1")

        self.assertEqual([m["id"] for m in result["call_memos"]], ["memo-2"])


class CallMemoExtractTests(unittest.IsolatedAsyncioTestCase):
    @patch("routers.call_memo.data_store")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    @patch("routers.call_memo.extract_text")
    async def test_markdown_is_decoded_as_plain_text(
        self, extract_text, extract_with_audit, data_store
    ):
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp) / "uploads"), \
                 patch("routers.call_memo._MEMO_RUNS_DIR", Path(tmp) / "memo_runs"), \
                 patch("routers.call_memo.data_store.DATA_DIR", Path(tmp)):
                store = _FakeStore(_company_with_memo())
                data_store.get_company.side_effect = store.get_company
                data_store.update_company.side_effect = store.update_company

                extract_with_audit.return_value = (
                    {"interview_date": "2026/08/04", "deal_source": "自行開發"},
                    {"evidence": {}, "coverage": {}},
                )
                transcript = "# Podcast 逐字稿\n\n營收為新台幣一億元。"
                upload = Mock(filename="podcast.md")
                upload.read = AsyncMock(return_value=transcript.encode("utf-8"))

                result = await extract_memo("company-id", "memo-1", upload, {"engine": "claude"})

                extract_text.assert_not_called()
                extract_with_audit.assert_awaited_once_with(
                    "測試公司", transcript, source_filename="podcast.md",
                    native_file_path="", engine="claude"
                )
                self.assertEqual(result["interview_date"], "2026/08/04")
                saved_memo = store.company["call_memos"][0]
                self.assertEqual(saved_memo["deal_source"], "自行開發")
                self.assertEqual(saved_memo["source"]["filename"], "podcast.md")
                self.assertEqual(len(saved_memo["runs"]), 1)

    @patch("routers.call_memo.data_store")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    @patch("routers.call_memo.extract_text")
    async def test_pdf_with_empty_text_layer_still_extracts_via_native_path(
        self, extract_text, extract_with_audit, data_store
    ):
        """PDF 頁面若整頁是設計排版圖片，get_text() 抓出的文字層可能是空字串（見
        services/file_parser.py 的 NATIVE_EXTS）。這種情況不該直接 422，應該把原始
        PDF 路徑也交給模型讀圖（native_file_path），而不是只靠已抽出的空文字。"""
        extract_text.return_value = ""  # 模擬整份 PDF 都是圖片頁，文字層全空
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp) / "uploads"), \
                 patch("routers.call_memo._MEMO_RUNS_DIR", Path(tmp) / "memo_runs"), \
                 patch("routers.call_memo.data_store.DATA_DIR", Path(tmp)):
                store = _FakeStore(_company_with_memo())
                data_store.get_company.side_effect = store.get_company
                data_store.update_company.side_effect = store.update_company

                extract_with_audit.return_value = (
                    {"interview_date": "", "headcount": "13人"},
                    {"evidence": {}, "coverage": {}},
                )
                upload = Mock(filename="bp.pdf")
                upload.read = AsyncMock(return_value=b"%PDF-fake-bytes")

                result = await extract_memo("company-id", "memo-1", upload, {"engine": "claude"})

                self.assertEqual(result["headcount"], "13人")
                _, kwargs = extract_with_audit.await_args
                self.assertTrue(kwargs["native_file_path"])
                self.assertTrue(kwargs["native_file_path"].endswith(".pdf"))
                self.assertTrue(Path(kwargs["native_file_path"]).is_file())

    @patch("routers.call_memo.data_store")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    async def test_missing_interview_date_is_not_replaced_with_today(
        self, extract_with_audit, data_store
    ):
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp) / "uploads"), \
                 patch("routers.call_memo._MEMO_RUNS_DIR", Path(tmp) / "memo_runs"), \
                 patch("routers.call_memo.data_store.DATA_DIR", Path(tmp)):
                store = _FakeStore(_company_with_memo())
                data_store.get_company.side_effect = store.get_company
                data_store.update_company.side_effect = store.update_company

                extract_with_audit.return_value = (
                    {"interview_date": ""}, {"evidence": {}, "coverage": {}}
                )
                upload = Mock(filename="podcast.txt")
                upload.read = AsyncMock(return_value="逐字稿".encode("utf-8"))

                result = await extract_memo("company-id", "memo-1", upload, {"engine": "claude"})

                self.assertEqual(result["interview_date"], "")

    @patch("routers.call_memo.data_store")
    @patch("routers.call_memo.memo_extractor.extract_with_audit")
    async def test_failed_extraction_does_not_overwrite_previous_source(
        self, extract_with_audit, data_store
    ):
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp) / "uploads"), \
                 patch("routers.call_memo._MEMO_RUNS_DIR", Path(tmp) / "memo_runs"), \
                 patch("routers.call_memo.data_store.DATA_DIR", Path(tmp)):
                store = _FakeStore(_company_with_memo(source={"filename": "good.txt", "stored_name": "memo_source_good.txt"}))
                data_store.get_company.side_effect = store.get_company
                data_store.update_company.side_effect = store.update_company

                extract_with_audit.side_effect = ValueError("AI 無法完成逐字稿分段抽取")
                upload = Mock(filename="bad.txt")
                upload.read = AsyncMock(return_value="逐字稿".encode("utf-8"))

                with self.assertRaises(HTTPException) as ctx:
                    await extract_memo("company-id", "memo-1", upload, {"engine": "claude"})

                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(
                    store.company["call_memos"][0]["source"]["filename"], "good.txt"
                )


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

    def test_source_file_is_persisted(self):
        with TemporaryDirectory() as tmp:
            with patch("routers.call_memo._MEMO_SOURCES_DIR", Path(tmp)):
                source = _write_memo_source_file(
                    "company-id", "joinx podcast 20260708.md", b"transcript"
                )

            stored = Path(tmp) / "company-id" / source["stored_name"]
            self.assertEqual(stored.read_bytes(), b"transcript")
            self.assertEqual(source["filename"], "joinx podcast 20260708.md")
            self.assertEqual(source["size"], 10)


if __name__ == "__main__":
    unittest.main()
