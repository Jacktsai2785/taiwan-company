import unittest
from unittest.mock import AsyncMock, patch

from routers import enrichment


class FindWebsiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company = {"id": "company-1", "name": "測試", "tax_id": "12345678"}

    async def _call(self):
        return await enrichment.find_website("company-1", {"engine": "claude"})

    def _no_fast_path(self):
        # find_website tries the DuckDuckGo fast path before falling back to the AI
        # WebSearch path these tests exercise. Without this it makes a real network
        # call per test and its (unmocked, non-deterministic) result can short-circuit
        # the assertions below, which are specifically about the AI-path behavior.
        return patch.object(enrichment, "_find_website_via_search", new=AsyncMock(return_value=None))

    async def test_normal_empty_answer_is_not_found(self):
        with self._no_fast_path(), \
             patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(return_value="")):
            self.assertEqual(await self._call(), {"website": "", "status": "not_found"})

    async def test_unreachable_candidate_is_distinct_from_not_found(self):
        with self._no_fast_path(), \
             patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(return_value="https://example.invalid")), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=False)):
            self.assertEqual(
                await self._call(),
                {"website": "", "status": "candidate_unreachable", "candidate_url": "https://example.invalid"},
            )

    async def test_max_turns_retries_once_then_succeeds(self):
        ask = AsyncMock(side_effect=[RuntimeError("Error: Reached max turns (6)"), "https://example.com"])
        with self._no_fast_path(), \
             patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=ask), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            self.assertEqual(await self._call(), {"website": "https://example.com", "status": "found"})
            self.assertEqual(ask.await_count, 2)

    async def test_max_turns_after_retry_has_search_limit_status(self):
        ask = AsyncMock(side_effect=RuntimeError("Error: Reached max turns (6)"))
        with self._no_fast_path(), \
             patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=ask):
            result = await self._call()
            self.assertEqual(result["status"], "search_limit")
            self.assertTrue(result["engine_error"])
            self.assertEqual(ask.await_count, 2)

    async def test_auth_error_has_auth_status(self):
        with self._no_fast_path(), \
             patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("claude CLI 尚未登入授權"))):
            result = await self._call()
            self.assertEqual(result["status"], "auth_error")

    async def test_fast_path_found_skips_ai_call(self):
        ask = AsyncMock()
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment, "_find_website_via_search",
                           new=AsyncMock(return_value={"website": "https://example.com", "status": "found"})), \
             patch.object(enrichment.asyncio, "to_thread", new=ask):
            self.assertEqual(await self._call(), {"website": "https://example.com", "status": "found"})
            ask.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class RegistryListingFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_mirror_is_skipped_so_ai_path_can_take_over(self):
        # Regression: biz.now.to (registry mirror, not in the domain blocklist) was
        # picked as the "official site" for 冠青能源 because the search results held
        # only registry/job-board pages.
        results = [
            {"url": "https://biz.now.to/abc", "title": "冠青能源科技股份有限公司 - 負責人賈中漢 - 資本額4億", "snippet": ""},
            {"url": "https://53583258.ec66.tw/", "title": "冠青能源科技股份有限公司", "snippet": ""},
        ]
        with patch.object(enrichment.google_search, "is_available", return_value=False), \
             patch.object(enrichment, "_search_duckduckgo", new=AsyncMock(return_value=results)), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            self.assertIsNone(await enrichment._find_website_via_search(
                "冠青能源科技股份有限公司", "冠青能源科技", "冠青能源科技股份有限公司"))

    async def test_real_homepage_still_accepted(self):
        results = [{"url": "http://www.wtoenergy.com/home", "title": "冠青能源-首頁", "snippet": "冠青能源科技股份有限公司成立於民國100年"}]
        with patch.object(enrichment.google_search, "is_available", return_value=False), \
             patch.object(enrichment, "_search_duckduckgo", new=AsyncMock(return_value=results)), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            got = await enrichment._find_website_via_search(
                "冠青能源科技股份有限公司", "冠青能源科技", "冠青能源科技股份有限公司")
            self.assertEqual(got["website"], "http://www.wtoenergy.com/home")


class GoogleFirstTests(unittest.IsolatedAsyncioTestCase):
    NAMES = ("冠青能源科技股份有限公司", "冠青能源科技", "冠青能源科技股份有限公司")

    async def test_google_result_wins_over_duckduckgo(self):
        google = [{"url": "http://www.wtoenergy.com/home", "title": "冠青能源-首頁", "snippet": ""}]
        ddg = AsyncMock(return_value=[])
        with patch.object(enrichment.google_search, "is_available", return_value=True), \
             patch.object(enrichment.google_search, "search_google", new=AsyncMock(return_value=google)), \
             patch.object(enrichment, "_search_duckduckgo", new=ddg), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            got = await enrichment._find_website_via_search(*self.NAMES)
        self.assertEqual(got["website"], "http://www.wtoenergy.com/home")
        ddg.assert_not_awaited()

    async def test_blocked_google_falls_back_to_duckduckgo(self):
        ddg = [{"url": "https://example.com/", "title": "冠青能源科技股份有限公司", "snippet": ""}]
        with patch.object(enrichment.google_search, "is_available", return_value=True), \
             patch.object(enrichment.google_search, "search_google", new=AsyncMock(return_value=[])), \
             patch.object(enrichment, "_search_duckduckgo", new=AsyncMock(return_value=ddg)), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            got = await enrichment._find_website_via_search(*self.NAMES)
        self.assertEqual(got["website"], "https://example.com/")
