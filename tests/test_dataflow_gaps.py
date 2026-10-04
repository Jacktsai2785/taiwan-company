"""Regression tests for the 2026-10 dataflow audit gaps."""
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import main
from routers import enrichment
from services import data_store

TZ = timezone(timedelta(hours=8))
REPO = Path(__file__).resolve().parent.parent


class DailySchedulerCatchUpTests(unittest.TestCase):
    def test_before_8am_is_not_due(self):
        self.assertFalse(main._daily_run_due(datetime(2026, 10, 4, 7, 59, tzinfo=TZ), "2026-10-03"))

    def test_after_8am_and_not_run_today_is_due(self):
        # The machine was off at 08:00; booting at 14:39 must catch up.
        self.assertTrue(main._daily_run_due(datetime(2026, 10, 4, 14, 39, tzinfo=TZ), "2026-10-02"))

    def test_already_run_today_is_not_due(self):
        self.assertFalse(main._daily_run_due(datetime(2026, 10, 4, 14, 39, tzinfo=TZ), "2026-10-04"))

    def test_never_run_is_due_after_8am(self):
        self.assertTrue(main._daily_run_due(datetime(2026, 10, 4, 9, 0, tzinfo=TZ), ""))

    def test_sleep_is_capped_at_one_hour(self):
        self.assertEqual(main._seconds_until_next_check(datetime(2026, 10, 4, 14, 0, tzinfo=TZ)), 3600)

    def test_sleep_wakes_just_after_8am(self):
        self.assertEqual(main._seconds_until_next_check(datetime(2026, 10, 4, 7, 59, 30, tzinfo=TZ)), 31)


class InterruptedJobResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(data_store, "COMPANIES_FILE", Path(self.tmp.name) / "companies.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, companies):
        data_store._write(data_store.COMPANIES_FILE, {"companies": companies})

    def test_orphans_are_reset_and_healthy_records_untouched(self):
        self._seed([
            {"id": "a", "enrich_status": "generating", "summary": "已有簡介"},
            {"id": "b", "enrich_status": "generating", "summary": ""},
            {"id": "c", "enrich_status": "ok", "summary": "x"},
            {"id": "d", "materials_generating": True},
        ])
        self.assertEqual(data_store.reset_interrupted_jobs(), {"enrich": 2, "materials": 1})
        by_id = {c["id"]: c for c in data_store.get_all_companies()}
        self.assertEqual(by_id["a"]["enrich_status"], "")
        self.assertEqual(by_id["b"]["enrich_status"], "failed")
        self.assertIn("重試", by_id["b"]["enrich_error"])
        self.assertEqual(by_id["c"]["enrich_status"], "ok")
        self.assertFalse(by_id["d"]["materials_generating"])

    def test_nothing_to_reset_does_not_rewrite_file(self):
        self._seed([{"id": "c", "enrich_status": "ok", "summary": "x"}])
        before = data_store.COMPANIES_FILE.stat().st_mtime_ns
        self.assertEqual(data_store.reset_interrupted_jobs(), {"enrich": 0, "materials": 0})
        self.assertEqual(data_store.COMPANIES_FILE.stat().st_mtime_ns, before)


class EnrichWarningTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, gcis):
        saved = {}
        company = {"id": "c1", "name": "測試公司", "tax_id": "12345678"}
        ev = MagicMock()
        session = MagicMock()
        session.__enter__.return_value = ev
        session.__exit__.return_value = False

        def capture(_cid, fields):
            saved.update(fields)
            return fields

        with patch.object(enrichment._enrich_channel, "session", return_value=session), \
             patch.object(enrichment.data_store, "get_company", return_value=company), \
             patch.object(enrichment.data_store, "update_company", side_effect=capture), \
             patch.object(enrichment.gcis_client, "fetch_company_data_by_tax_id", new=gcis), \
             patch.object(enrichment.competitor_service, "gather_competitor_context",
                          return_value={"direct": [], "extended": []}), \
             patch.object(enrichment.report_generator, "generate_summary",
                          new=AsyncMock(return_value={"summary": "s", "blurb": "b"})), \
             patch.object(enrichment.competitor_service, "resolve_competitor_ids", return_value=[]), \
             patch.object(enrichment, "_export_to_jk_nb", create=True):
            await enrichment._enrich_company("c1")
        return saved

    async def test_gcis_exception_leaves_a_warning_even_though_status_is_ok(self):
        saved = await self._run(AsyncMock(side_effect=RuntimeError("boom")))
        self.assertEqual(saved["enrich_status"], "ok")
        self.assertIn("未更新", saved["enrich_warning"])

    async def test_gcis_success_clears_the_warning(self):
        saved = await self._run(AsyncMock(return_value={"capital": 1, "representative": "王"}))
        self.assertEqual(saved["enrich_warning"], "")


class BackupCoverageTests(unittest.TestCase):
    def test_industry_maps_is_backed_up(self):
        files = re.search(r"^FILES=\((.*?)\)", (REPO / "scripts/backup_data.sh").read_text(), re.M).group(1)
        self.assertIn("industry_maps.json", files.split())


if __name__ == "__main__":
    unittest.main()


class EnrichFailureKeepsWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_failure_still_records_gcis_warning(self):
        saved = {}
        company = {"id": "c1", "name": "測試公司", "tax_id": "12345678"}
        ev = MagicMock()
        session = MagicMock()
        session.__enter__.return_value = ev
        session.__exit__.return_value = False
        with patch.object(enrichment._enrich_channel, "session", return_value=session), \
             patch.object(enrichment.data_store, "get_company", return_value=company), \
             patch.object(enrichment.data_store, "update_company", side_effect=lambda _c, f: saved.update(f)), \
             patch.object(enrichment.gcis_client, "fetch_company_data_by_tax_id",
                          new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(enrichment.competitor_service, "gather_competitor_context",
                          return_value={"direct": [], "extended": []}), \
             patch.object(enrichment.report_generator, "generate_summary",
                          new=AsyncMock(side_effect=RuntimeError("ai down"))):
            await enrichment._enrich_company("c1")
        self.assertEqual(saved["enrich_status"], "failed")
        self.assertIn("未更新", saved["enrich_warning"])
