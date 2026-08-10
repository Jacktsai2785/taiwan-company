from services import data_store


def test_merge_company_records_preserves_user_work_and_unions_taxonomy():
    primary = {
        "id": "keep",
        "name": "公司有限公司",
        "tax_id": "12345678",
        "labels": ["A"],
        "industries": ["產業甲"],
        "summary": "new summary",
        "materials": [{"stored_name": "deck.pdf", "url": "/uploads/keep/deck.pdf"}],
        "watched": False,
    }
    donor = {
        "id": "drop",
        "name": "公司",
        "tax_id": "12345678",
        "labels": ["B"],
        "industries": ["產業乙"],
        "summary": "old summary",
        "patents": [{"patent_no": "I123"}],
        "watched": True,
    }

    merged = data_store._merge_company_records(primary, [primary, donor])

    assert merged["id"] == "keep"
    assert merged["summary"] == "new summary"
    assert merged["labels"] == ["A", "B"]
    assert merged["industries"] == ["產業甲", "產業乙"]
    assert merged["patents"] == [{"patent_no": "I123"}]
    assert merged["watched"] is True
    assert merged["merged_from_ids"] == ["drop"]


def test_rewrite_company_id_refs_handles_ids_and_upload_urls():
    value = {
        "company_id": "drop",
        "url": "/uploads/drop/deck.pdf",
        "nested": ["drop", "unchanged"],
        "merged_from_ids": ["drop"],
    }

    rewritten, count = data_store._rewrite_company_id_refs(value, {"drop": "keep"})

    assert rewritten == {
        "company_id": "keep",
        "url": "/uploads/keep/deck.pdf",
        "nested": ["keep", "unchanged"],
        "merged_from_ids": ["drop"],
    }
    assert count == 3


def test_repair_competitor_company_refs_requires_exact_identity_match():
    companies = [
        {"id": "keep", "name": "甲公司股份有限公司", "tax_id": "12345678"},
        {
            "id": "owner",
            "name": "持有公司",
            "competitors": [
                {"name": "甲公司", "company_id": "stale"},
                {"name": "查無此公司", "company_id": "unknown"},
            ],
        },
    ]

    changed = data_store._repair_competitor_company_refs(companies)

    assert changed == 1
    assert companies[1]["competitors"][0]["company_id"] == "keep"
    assert companies[1]["competitors"][1]["company_id"] == "unknown"


def test_create_and_upsert_reuse_existing_legal_entity(monkeypatch, tmp_path):
    monkeypatch.setattr(data_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(data_store, "COMPANIES_FILE", tmp_path / "companies.json")
    data_store._FILE_CACHE.clear()

    first = data_store.create_company("測試科技有限公司", "標籤甲", "產業甲", "12345678")
    second = data_store.create_company("測試科技", "標籤乙", "產業乙", "12345678")

    assert second["id"] == first["id"]
    assert len(data_store.get_all_companies()) == 1
    assert second["labels"] == ["標籤甲", "標籤乙"]
    assert second["industries"] == ["產業甲", "產業乙"]

    incoming = dict(second, id="new-id", summary="補充資料")
    merged = data_store.upsert_company(incoming)

    assert merged["id"] == first["id"]
    assert merged["summary"] == "補充資料"
    assert len(data_store.get_all_companies()) == 1
