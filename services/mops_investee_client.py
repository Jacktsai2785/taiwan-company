import os
import httpx

MOPS_BASE = os.getenv("MOPS_INVESTEE_URL", "http://localhost:8085").rstrip("/")
MOPS_KEY  = os.getenv("MOPS_INVESTEE_API_KEY", "")


async def reverse_lookup(
    name: str,
    tax_id: str | None = None,
    fuzzy: bool = False,
) -> list[dict]:
    headers = {"X-API-Key": MOPS_KEY} if MOPS_KEY else {}
    params: dict = {"name": name}
    if tax_id:
        params["tax_id"] = tax_id
    if fuzzy:
        params["fuzzy"] = "true"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{MOPS_BASE}/reverse-lookup/investee",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("results", [])
