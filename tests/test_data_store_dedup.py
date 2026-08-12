import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

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


def test_merge_company_records_keeps_explicit_false_over_donor_true():
    # no_par_value=False is a confirmed "has par value" state, not a blank —
    # bool is a subclass of int so False == 0, which used to fall into the
    # empty-field backfill branch and get silently replaced by a donor's True.
    primary = {"id": "keep", "name": "公司", "tax_id": "12345678", "no_par_value": False}
    donor = {"id": "drop", "name": "公司", "tax_id": "12345678", "no_par_value": True}

    merged = data_store._merge_company_records(primary, [primary, donor])

    assert merged["no_par_value"] is False


def test_merge_company_records_backfills_false_into_missing_field():
    primary = {"id": "keep", "name": "公司", "tax_id": "12345678"}
    donor = {"id": "drop", "name": "公司", "tax_id": "12345678", "no_par_value": False}

    merged = data_store._merge_company_records(primary, [primary, donor])

    assert merged["no_par_value"] is False


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


class CompanyMergeTransactionTests(unittest.TestCase):
    def _fixture(self, root: Path):
        companies_file = root / "companies.json"
        old_store = {
            "companies": [
                {"id": "keep", "name": "保留公司", "materials": [{"url": "/uploads/keep/keep.txt"}]},
                {"id": "drop", "name": "重複公司", "materials": [{"url": "/uploads/drop/drop.txt"}]},
            ]
        }
        companies_file.write_text(json.dumps(old_store, ensure_ascii=False), encoding="utf-8")
        refs_file = root / "refs.json"
        refs_file.write_text(json.dumps({"company_id": "drop", "url": "/uploads/drop/drop.txt"}), encoding="utf-8")
        for company_id, filename, content in (
            ("keep", "keep.txt", "keep"),
            ("drop", "drop.txt", "drop"),
        ):
            directory = root / "uploads" / company_id
            directory.mkdir(parents=True)
            (directory / filename).write_text(content, encoding="utf-8")
        new_store = deepcopy(old_store)
        new_store["companies"] = [{
            "id": "keep",
            "name": "保留公司",
            "materials": [
                {"url": "/uploads/keep/keep.txt"},
                {"url": "/uploads/keep/drop.txt"},
            ],
        }]
        return companies_file, refs_file, old_store, new_store

    def test_transaction_rolls_back_json_and_uploads_after_partial_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            companies_file, refs_file, _, new_store = self._fixture(root)
            before_companies = companies_file.read_bytes()
            before_refs = refs_file.read_bytes()
            real_merge = data_store._merge_upload_dirs

            def fail_after_move(id_map):
                real_merge(id_map)
                raise RuntimeError("simulated move failure")

            data_store._FILE_CACHE.clear()
            with patch.object(data_store, "DATA_DIR", root), \
                 patch.object(data_store, "COMPANIES_FILE", companies_file), \
                 patch.object(data_store, "_merge_upload_dirs", side_effect=fail_after_move):
                with self.assertRaisesRegex(RuntimeError, "simulated move failure"):
                    data_store._commit_company_merge_transaction(new_store, {"drop": "keep"})

            self.assertEqual(companies_file.read_bytes(), before_companies)
            self.assertEqual(refs_file.read_bytes(), before_refs)
            self.assertEqual((root / "uploads" / "keep" / "keep.txt").read_text(), "keep")
            self.assertEqual((root / "uploads" / "drop" / "drop.txt").read_text(), "drop")
            self.assertFalse(list(root.glob(".company-merge-*")))

    def test_transaction_commits_all_references_and_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            companies_file, refs_file, _, new_store = self._fixture(root)
            data_store._FILE_CACHE.clear()
            with patch.object(data_store, "DATA_DIR", root), \
                 patch.object(data_store, "COMPANIES_FILE", companies_file):
                result = data_store._commit_company_merge_transaction(new_store, {"drop": "keep"})

            saved = json.loads(companies_file.read_text(encoding="utf-8"))
            refs = json.loads(refs_file.read_text(encoding="utf-8"))
            self.assertEqual([company["id"] for company in saved["companies"]], ["keep"])
            self.assertEqual(refs["company_id"], "keep")
            self.assertEqual(refs["url"], "/uploads/keep/drop.txt")
            self.assertTrue((root / "uploads" / "keep" / "keep.txt").exists())
            self.assertTrue((root / "uploads" / "keep" / "drop.txt").exists())
            self.assertFalse((root / "uploads" / "drop").exists())
            self.assertEqual(result["external_refs_rewritten"], 2)

    def test_transaction_rewrites_metadata_for_colliding_filenames(self):
        # Both companies uploaded a file named the same thing; the physical
        # merge must rename one on collision, and the JSON URL that pointed
        # at it must be rewritten to match — not left pointing at a name
        # that no longer exists on disk.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            companies_file = root / "companies.json"
            store = {
                "companies": [
                    {
                        "id": "keep",
                        "name": "保留公司",
                        "materials": [{"url": "/uploads/keep/deck.pdf"}],
                    },
                    {
                        "id": "drop",
                        "name": "重複公司",
                        "materials": [{"url": "/uploads/drop/deck.pdf"}],
                    },
                ]
            }
            companies_file.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
            for company_id, content in (("keep", "keep-deck"), ("drop", "drop-deck")):
                directory = root / "uploads" / company_id
                directory.mkdir(parents=True)
                (directory / "deck.pdf").write_text(content, encoding="utf-8")

            new_store = deepcopy(store)
            new_store["companies"] = [{
                "id": "keep",
                "name": "保留公司",
                "materials": [
                    {"url": "/uploads/keep/deck.pdf"},
                    {"url": "/uploads/drop/deck.pdf"},
                ],
            }]

            data_store._FILE_CACHE.clear()
            with patch.object(data_store, "DATA_DIR", root), \
                 patch.object(data_store, "COMPANIES_FILE", companies_file):
                data_store._commit_company_merge_transaction(new_store, {"drop": "keep"})

            saved = json.loads(companies_file.read_text(encoding="utf-8"))
            urls = {m["url"] for m in saved["companies"][0]["materials"]}
            for url in urls:
                relative = url.removeprefix("/uploads/")
                self.assertTrue((root / "uploads" / relative).is_file(), url)
            self.assertIn("/uploads/keep/deck.pdf", urls)
            self.assertEqual((root / "uploads" / "keep" / "deck.pdf").read_text(), "keep-deck")

    def test_transaction_rejects_upload_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            companies_file, _, _, new_store = self._fixture(root)
            with patch.object(data_store, "DATA_DIR", root), \
                 patch.object(data_store, "COMPANIES_FILE", companies_file):
                with self.assertRaises(ValueError):
                    data_store._commit_company_merge_transaction(new_store, {"../drop": "keep"})
