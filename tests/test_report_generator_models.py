import unittest
from unittest.mock import patch

from services import competitor_service
from services.report_generator import (
    _build_deep_prompt,
    _build_prompt,
    _claude_only_model,
    _parse_competitor_table,
)


class ClaudeOnlyModelTests(unittest.TestCase):
    def test_keeps_claude_model_aliases_for_claude(self):
        self.assertEqual(_claude_only_model("claude", "opus"), "opus")
        self.assertEqual(
            _claude_only_model("anthropic", "claude-sonnet-4-6"),
            "claude-sonnet-4-6",
        )

    def test_other_engines_use_their_own_default_model(self):
        for engine in ("codex", "gemini", "ollama"):
            with self.subTest(engine=engine):
                self.assertEqual(_claude_only_model(engine, "claude-sonnet-4-6"), "")


class CompetitorFormatPromptTests(unittest.TestCase):
    def test_initial_prompt_requires_canonical_competitor_analysis_groups(self):
        prompt = _build_prompt({"name": "測試公司"})
        self.assertIn("**本案相對優勢：**", prompt)
        self.assertIn("**相對劣勢或挑戰：**", prompt)
        self.assertIn("禁止在每一點重複", prompt)
        self.assertIn("[正式登記名稱](可直接佐證該公司的官網或可信來源 URL)", prompt)
        self.assertIn("重要可驗證事實", prompt)

    def test_deep_prompt_preserves_canonical_competitor_analysis_groups(self):
        prompt = _build_deep_prompt({"name": "測試公司", "summary": "## 業務概況\n內容"})
        self.assertIn("固定依序輸出一次 `**本案相對優勢：**`", prompt)
        self.assertIn("一次 `**相對劣勢或挑戰：**`", prompt)
        self.assertIn("所有競業公司名稱必須優先寫成", prompt)

    def test_competitor_parser_extracts_structured_source_url(self):
        summary = """## 競業分析

| 公司名稱 | 核心業務 | 主要差異化特點 | 上市狀態 | 競業類型 |
|---|---|---|---|---|
| 本案股份有限公司（本案） | 本案 | — | 非公發 | — |
| [競業股份有限公司](https://example.com/about) | 產品 | 差異 | 上市 | 正面競業 |

## 主要風險
"""
        competitors = _parse_competitor_table(summary)
        self.assertEqual(len(competitors), 1)
        self.assertEqual(competitors[0]["name"], "競業股份有限公司")
        self.assertEqual(competitors[0]["source_url"], "https://example.com/about")

    def test_competitor_parser_never_infers_missing_or_unsafe_url(self):
        summary = """## 競業分析
| 公司名稱 | 核心業務 | 主要差異化特點 | 上市狀態 | 競業類型 |
|---|---|---|---|---|
| 純文字有限公司 | 產品 | 差異 | 非公發 | 替代路徑 |
| [不安全有限公司](javascript:alert(1)) | 產品 | 差異 | 非公發 | 正面競業 |
## 主要風險
"""
        competitors = _parse_competitor_table(summary)
        self.assertEqual(competitors[0]["source_url"], "")
        self.assertEqual(competitors[1]["source_url"], "")

    @patch("services.competitor_service.data_store.get_all_companies")
    def test_resolver_uses_saved_official_website_without_guessing(self, get_all):
        get_all.return_value = [{
            "id": "known-id",
            "name": "已知競業股份有限公司",
            "tax_id": "12345678",
            "website": "https://known.example.com",
        }]
        result = competitor_service.resolve_competitor_ids([{
            "name": "已知競業股份有限公司",
            "tax_id": None,
            "source_url": "",
        }])
        self.assertEqual(result[0]["company_id"], "known-id")
        self.assertEqual(result[0]["source_url"], "https://known.example.com")


if __name__ == "__main__":
    unittest.main()
