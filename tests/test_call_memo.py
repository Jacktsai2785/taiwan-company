import unittest
from unittest.mock import patch

from routers.call_memo import download_memo


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


if __name__ == "__main__":
    unittest.main()
