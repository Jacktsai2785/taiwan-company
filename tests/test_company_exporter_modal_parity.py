from services.company_exporter import (
    _basic_info_rows,
    _dir_table_rows,
    _patent_table_rows,
    _shareholder_block,
)


def test_export_basic_info_matches_modal_fields():
    labels = [label for label, _ in _basic_info_rows({
        "tax_id": "12345678",
        "setup_date": "0990101",
    })]

    assert "設立日期" not in labels


def test_export_director_totals_match_modal_entity_deduplication():
    directors = [
        {"name": "甲", "representative_of": "同一法人股份有限公司",
         "shares": 100, "ratio": 0.1},
        {"name": "乙", "representative_of": "同一法人股份有限公司",
         "shares": 100, "ratio": 0.1},
        {"name": "丙", "representative_of": "", "shares": 50, "ratio": 0.05},
    ]

    rows, _ = _dir_table_rows(directors)

    assert rows[-1][-2:] == ["150", "15.00%"]
    assert "15.00%" in _shareholder_block({"directors": directors}, None)["alert"]


def test_export_patents_include_every_modal_information_field():
    rows = _patent_table_rows([{
        "patent_no": "I123456",
        "title": "測試專利",
        "app_date": "2026-01-01",
        "status": "核准",
        "applicant": "測試股份有限公司",
        "inventors": ["王小明"],
        "brief": "完整摘要",
    }])

    assert rows[0] == ["專利號", "名稱", "申請日", "狀態", "申請人／發明人", "摘要"]
    assert rows[1][-2] == "申請人：測試股份有限公司；發明人：王小明"
    assert rows[1][-1] == "完整摘要"
