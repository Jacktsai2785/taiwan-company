import unittest
from unittest.mock import AsyncMock, patch

from services import memo_extractor


class MemoEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_material_candidate_scan_keeps_numeric_and_governance_context(self):
        transcript = "\n".join([
            "[00:00:01] 普通開場",
            "[00:00:02] 訂單從8000元提高到8萬元",
            "[00:00:03] 這是成長轉折",
            "[00:00:04] 普通過場",
            "[00:00:05] 未來要設計股權與表決權",
        ])

        candidates = memo_extractor._material_candidate_text(transcript)

        self.assertIn("訂單從8000元提高到8萬元", candidates)
        self.assertIn("未來要設計股權與表決權", candidates)

    def test_deterministic_safety_net_preserves_high_signal_verbatim(self):
        transcript = "\n".join([
            "[00:00:01] 客戶訂單從8000元",
            "[00:00:02] 提高到至少8萬元",
            "[00:00:03] 未來要設計股權與表決權",
        ])

        evidence = memo_extractor._deterministic_material_evidence(transcript)

        self.assertTrue(any("8000元" in item["quote"] for item in evidence["financials"]))
        self.assertTrue(any("股權" in item["quote"] for item in evidence["investment_terms"]))
        self.assertEqual(evidence["financials"][0]["fact"], "客戶訂單從8000元")

    def test_generic_signals_cover_saas_manufacturing_and_biotech(self):
        transcript = "\n".join([
            "[00:00:01] 我們ARR為1,200萬元，年增35%",
            "[00:00:02] 目前團隊42人",
            "[00:00:03] 廠房月產10萬件，良率99.2%",
            "[00:00:04] 預計2028年完成三期臨床",
            "[00:00:05] 本輪募資US$5 million，釋股12%",
        ])

        evidence = memo_extractor._deterministic_material_evidence(transcript)

        self.assertTrue(evidence["financials"])
        self.assertTrue(evidence["headcount"])
        self.assertTrue(evidence["factory_capacity"])
        self.assertTrue(evidence["recent_development"])
        self.assertTrue(evidence["investment_terms"])

    def test_generic_safety_net_excludes_questions_and_unrelated_examples(self):
        transcript = "\n".join([
            "[00:00:01] 你們營收有沒有1億元",
            "[00:00:02] 今年營收是3億元嗎？",
            "[00:00:03] 假設做到Uber要幾十億美元",
            "[00:00:04] 這是別人的案例",
        ])

        evidence = memo_extractor._deterministic_material_evidence(transcript)

        self.assertEqual(evidence["financials"], [])

    async def test_dual_extraction_keeps_success_when_other_path_times_out(self):
        successful = {key: [] for key in memo_extractor._EXTRACT_KEYS}
        successful["financials"] = [
            {"fact": "營收成長", "quote": "營收成長", "timestamp": ""}
        ]
        with patch(
            "services.memo_extractor._extract_evidence_once",
            new=AsyncMock(side_effect=RuntimeError("timeout")),
        ), patch(
            "services.memo_extractor._extract_material_facts_once",
            new=AsyncMock(return_value=successful),
        ):
            parts = await memo_extractor._extract_chunk_dual(
                "測試公司", "營收成長", "codex"
            )

        self.assertEqual(parts, [successful])

    def test_rejects_fact_when_quote_is_not_in_transcript(self):
        claimed = {
            "financials": [{
                "fact": "營收十億元",
                "quote": "營收十億元",
                "timestamp": "",
            }]
        }

        evidence = memo_extractor._validated_evidence(
            claimed, "逐字稿只說產品已經上市。"
        )

        self.assertEqual(evidence["financials"], [])

    async def test_all_chunks_are_aggregated_without_synthesis_loss(self):
        transcript = "甲" * memo_extractor._CHUNK_CHARS + "乙" * 600
        first = {key: [] for key in memo_extractor._EXTRACT_KEYS}
        second = {key: [] for key in memo_extractor._EXTRACT_KEYS}
        first["business_revenue"] = [
            {"fact": "提供甲產品", "quote": "甲", "timestamp": ""}
        ]
        second["business_revenue"] = [
            {"fact": "提供乙服務", "quote": "乙", "timestamp": ""}
        ]
        synthesized = {key: "" for key in memo_extractor._EXTRACT_KEYS}
        synthesized["business_revenue"] = "提供甲產品，並提供乙服務。"
        coverage = {
            key: {"evidence_count": 0, "used_count": 0, "synthesized": False}
            for key in memo_extractor._EXTRACT_KEYS
        }
        coverage["business_revenue"] = {
            "evidence_count": 2, "used_count": 2, "synthesized": True,
        }
        with patch(
            "services.memo_extractor._extract_evidence_once",
            new=AsyncMock(side_effect=[first, second]),
        ), patch(
            "services.memo_extractor._extract_material_facts_once",
            new=AsyncMock(side_effect=[
                {key: [] for key in memo_extractor._EXTRACT_KEYS},
                {key: [] for key in memo_extractor._EXTRACT_KEYS},
            ]),
        ), patch(
            "services.memo_extractor._synthesize_fields_from_evidence",
            new=AsyncMock(return_value=(synthesized, coverage)),
        ):
            result, audit = await memo_extractor.extract_with_audit(
                "測試公司", transcript, engine="codex"
            )

        evidence = audit["evidence"]
        self.assertEqual(len(evidence["business_revenue"]), 2)
        self.assertEqual(result["business_revenue"], "提供甲產品，並提供乙服務。")
        self.assertTrue(audit["coverage"]["business_revenue"]["synthesized"])
        self.assertEqual(
            [item["id"] for item in evidence["business_revenue"]],
            ["business_revenue:1", "business_revenue:2"],
        )

    def test_safety_net_is_audit_only_when_model_evidence_exists(self):
        items = [
            {"id": "headcount:1", "fact": "公司目前約80人。", "quote": "80人"},
            {
                "id": "headcount:2",
                "fact": "八十个人开口等你养",
                "quote": "八十个人开口等你养",
                "source": "deterministic_safety_net",
            },
        ]

        selected = memo_extractor._deduplicated_facts(items)

        self.assertEqual([item["id"] for item in selected], ["headcount:1"])

    def test_safety_net_never_becomes_published_prose_by_itself(self):
        items = [{
            "id": "factory_capacity:1",
            "fact": "其他半導體公司的產能有限",
            "quote": "其他半導體公司的產能有限",
            "source": "deterministic_safety_net",
        }]

        self.assertEqual(memo_extractor._deduplicated_facts(items), [])
        self.assertEqual(memo_extractor._fallback_field_text(items), "")

    def test_prose_normalization_removes_broken_punctuation(self):
        self.assertEqual(
            memo_extractor._normalize_memo_prose("第一句。；第二句；。第三句。。"),
            "第一句。第二句。第三句。",
        )

    async def test_synthesis_requires_valid_evidence_ids(self):
        evidence = {key: [] for key in memo_extractor._EXTRACT_KEYS}
        evidence["business_revenue"] = [
            {
                "id": "business_revenue:1",
                "fact": "公司提供企業軟體開發服務。",
                "quote": "提供企業軟體開發服務",
                "timestamp": "00:01:00",
            }
        ]
        response = {
            "business_revenue": {
                "text": "公司提供企業軟體開發服務。",
                "evidence_ids": ["business_revenue:1"],
            }
        }
        with patch(
            "services.memo_extractor.claude_client.ask",
            return_value=__import__("json").dumps(response, ensure_ascii=False),
        ):
            result = await memo_extractor._synthesize_field_group(
                "測試公司", ("business_revenue",), evidence, "codex"
            )

        self.assertEqual(result, response)


if __name__ == "__main__":
    unittest.main()
