from services import industry_map


def _map_card(name, *, tax_id=None):
    return {
        "sections": [{
            "subgroups": [{
                "companies": [{
                    "name": name,
                    "tax_id": tax_id,
                    "in_db": False,
                    "company_id": None,
                }]
            }]
        }],
        "stats": {"in_db_count": 0},
    }


def test_reconcile_finds_company_assigned_to_another_industry(monkeypatch):
    company = {
        "id": "mobagel-new",
        "name": "行動貝果有限公司",
        "tax_id": "53550539",
        "industries": ["企業 AI Agent 與流程自動化平台"],
        "blurb": "企業智能決策平台",
    }
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [company])

    result = industry_map.reconcile_company_ids(
        _map_card("行動貝果股份有限公司"),
        {"in_db_companies": [], "expansion_pool": []},
    )

    card = result["sections"][0]["subgroups"][0]["companies"][0]
    assert card["in_db"] is True
    assert card["company_id"] == "mobagel-new"
    assert card["tax_id"] == "53550539"
    assert result["stats"]["in_db_count"] == 1


def test_reconcile_prefers_tax_id_over_different_name(monkeypatch):
    company = {
        "id": "company-by-tax",
        "name": "更新後正式名稱有限公司",
        "tax_id": "12345678",
    }
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [company])

    result = industry_map.reconcile_company_ids(
        _map_card("地圖上的舊名稱股份有限公司", tax_id="12345678")
    )

    card = result["sections"][0]["subgroups"][0]["companies"][0]
    assert card["in_db"] is True
    assert card["company_id"] == "company-by-tax"


def test_reconcile_keeps_unknown_company_uncollected(monkeypatch):
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [])

    result = industry_map.reconcile_company_ids(_map_card("尚未收錄股份有限公司"))

    card = result["sections"][0]["subgroups"][0]["companies"][0]
    assert card["in_db"] is False
    assert card["company_id"] is None
    assert result["stats"]["in_db_count"] == 0


def test_vertical_guard_rejects_digital_twin_company_from_textile(monkeypatch):
    company = {
        "id": "metai",
        "name": "宇見智能科技",
        "tax_id": "90090880",
        "blurb": "工業數位孿生合成資料",
        "summary": "## 業務概況\nMetGen 服務倉儲機器人、半導體設備與資料中心。",
    }
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [company])
    map_data = {
        "sections": [{
            "id": "textile",
            "title": "紡織垂直應用：數位孿生與材料科技",
            "order": 0,
            "subgroups": [{
                "title": "布料數位孿生與合成資料",
                "companies": [{"name": "宇見智能科技", "in_db": True, "company_id": "metai"}],
            }],
        }],
        "stats": {},
    }

    result = industry_map.reconcile_company_ids(map_data)

    textile_cards = result["sections"][0]["subgroups"][0]["companies"]
    assert textile_cards == []
    pending = next(s for s in result["sections"] if s.get("classification_pending"))
    card = pending["subgroups"][0]["companies"][0]
    assert card["company_id"] == "metai"
    assert "未找到「紡織」業務證據" in card["placement_warning"]
    assert pending["promotable"] is False


def test_vertical_guard_keeps_company_with_textile_evidence(monkeypatch):
    company = {
        "id": "fabric-ai",
        "name": "聯覺科技",
        "blurb": "AI 布料數位孿生掃描雲端平台",
    }
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [company])
    map_data = {
        "sections": [{
            "title": "紡織垂直應用",
            "order": 0,
            "subgroups": [{
                "title": "布料數位孿生",
                "companies": [{"name": "聯覺科技", "in_db": True, "company_id": "fabric-ai"}],
            }],
        }],
    }

    result = industry_map.reconcile_company_ids(map_data)

    cards = result["sections"][0]["subgroups"][0]["companies"]
    assert [c["company_id"] for c in cards] == ["fabric-ai"]
    assert not any(s.get("classification_pending") for s in result["sections"])


def test_pending_section_cannot_be_promoted_to_subindustry():
    sections = [{
        "title": "跨領域技術／待確認",
        "classification_pending": True,
        "promotable": False,
        "subgroups": [{"companies": [{"name": "宇見智能", "in_db": True, "company_id": "metai"}]}],
    }]

    assert industry_map.groups_from_sections(sections) == []


def test_write_boundary_rejects_unsupported_vertical_group(monkeypatch):
    company = {
        "id": "metai",
        "name": "宇見智能科技",
        "blurb": "工業數位孿生合成資料",
        "summary": "服務倉儲機器人、半導體設備與資料中心",
    }
    monkeypatch.setattr(industry_map.data_store, "get_all_companies", lambda: [company])

    groups, warnings = industry_map.validate_subdivision_groups([
        {"name": "紡織數位孿生", "company_ids": ["metai"]}
    ])

    assert groups == []
    assert warnings[0]["company"] == "宇見智能科技"
    assert warnings[0]["missing_evidence"] == ["紡織"]
