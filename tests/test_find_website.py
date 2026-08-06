import unittest
from unittest.mock import AsyncMock, patch

from routers import enrichment


class FindWebsiteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company = {"id": "company-1", "name": "測試", "tax_id": "12345678"}

    async def _call(self):
        return await enrichment.find_website("company-1", {"engine": "claude"})

    async def test_normal_empty_answer_is_not_found(self):
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(return_value="")):
            self.assertEqual(await self._call(), {"website": "", "status": "not_found"})

    async def test_unreachable_candidate_is_distinct_from_not_found(self):
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(return_value="https://example.invalid")), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=False)):
            self.assertEqual(
                await self._call(),
                {"website": "", "status": "candidate_unreachable", "candidate_url": "https://example.invalid"},
            )

    async def test_max_turns_retries_once_then_succeeds(self):
        ask = AsyncMock(side_effect=[RuntimeError("Error: Reached max turns (6)"), "https://example.com"])
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=ask), \
             patch.object(enrichment, "_ssrf_safe_reachable", new=AsyncMock(return_value=True)):
            self.assertEqual(await self._call(), {"website": "https://example.com", "status": "found"})
            self.assertEqual(ask.await_count, 2)

    async def test_max_turns_after_retry_has_search_limit_status(self):
        ask = AsyncMock(side_effect=RuntimeError("Error: Reached max turns (6)"))
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=ask):
            result = await self._call()
            self.assertEqual(result["status"], "search_limit")
            self.assertTrue(result["engine_error"])
            self.assertEqual(ask.await_count, 2)

    async def test_auth_error_has_auth_status(self):
        with patch.object(enrichment.data_store, "get_company", return_value=self.company), \
             patch.object(enrichment.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("claude CLI 尚未登入授權"))):
            result = await self._call()
            self.assertEqual(result["status"], "auth_error")


if __name__ == "__main__":
    unittest.main()
