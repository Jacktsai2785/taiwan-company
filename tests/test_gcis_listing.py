from datetime import datetime

import pytest

from services import gcis_client


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "tables": [{
                "data": [[
                    1, "7942", "東佑達", "115/08/06", "新台幣 10.0000元",
                ]]
            }]
        }


class _Client:
    async def get(self, url, **kwargs):
        assert url == gcis_client._UPCOMING_EMERGING_URL
        assert kwargs["params"] == {"date": str(datetime.now().year)}
        return _Response()


@pytest.mark.asyncio
async def test_upcoming_emerging_company_is_resolved_before_first_trading_day():
    gcis_client._by_abbrev.clear()

    await gcis_client._load_upcoming_emerging(_Client())

    assert (
        gcis_client._resolve_listing_status(
            "70740963", "東佑達自動化科技股份有限公司"
        )
        == "興櫃"
    )
