import json
import unittest
from unittest.mock import patch

from services import company_extractor


class CompanyExtractorBatchTests(unittest.TestCase):
    def test_chunk_text_preserves_all_content_with_8000_char_limit(self):
        text = ("A" * 7990) + "\n" + ("B" * 200) + "\n公司名稱"

        chunks = company_extractor._chunk_text(text)

        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 8000 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    @patch("services.company_extractor.claude_client.ask")
    def test_extracts_every_batch_and_deduplicates_in_original_order(self, ask):
        text = ("第一批內容\n" * 1000) + ("第二批內容\n" * 1000) + "第三批內容"
        expected_chunks = company_extractor._chunk_text(text)
        responses = []
        for index in range(len(expected_chunks)):
            names = [f"第{index + 1}批股份有限公司"]
            if index > 0:
                names.insert(0, "第1批股份有限公司")
            responses.append(json.dumps(names, ensure_ascii=False))
        ask.side_effect = responses

        result = company_extractor._ask_claude(text, engine="codex")

        self.assertEqual(ask.call_count, len(expected_chunks))
        sent_chunks = [
            call.args[0].split("文字內容：\n", 1)[1]
            for call in ask.call_args_list
        ]
        self.assertEqual("".join(sent_chunks), text)
        self.assertTrue(all(len(chunk) <= 8000 for chunk in sent_chunks))
        self.assertEqual(
            result,
            [f"第{index + 1}批股份有限公司" for index in range(len(expected_chunks))],
        )
        self.assertTrue(all(call.kwargs["engine"] == "codex" for call in ask.call_args_list))


if __name__ == "__main__":
    unittest.main()
