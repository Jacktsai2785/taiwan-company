"""
Extract and classify company names from text using Claude CLI.

Classification rules:
  valid     — contains 股份有限公司 → keep
  excluded  — contains 有限公司 but NOT 股份有限公司 → silently drop
  uncertain — neither suffix → ask user
"""
import asyncio
import json
import logging

from . import claude_client, data_store

log = logging.getLogger("extractor")


def _classify(name: str) -> str:
    if "股份有限公司" in name:
        return "valid"
    if "有限公司" in name:
        return "excluded"
    return "uncertain"


def extract_companies_from_text(text: str, source_label: str, api_key: str = "", provider: str = "anthropic") -> dict:
    """
    Returns:
    {
        "valid":     [ candidate_dict, ... ],
        "excluded":  [ {"name": str}, ... ],
        "uncertain": [ {"name": str}, ... ],
    }
    candidate_dict keys: name, is_new, existing_id, existing_labels, suggested_label
    """
    all_names = _ask_claude(text, api_key=api_key, provider=provider)
    log.info("Total unique names found: %d (claude=%d)", len(all_names), len(all_names))

    result: dict[str, list] = {"valid": [], "excluded": [], "uncertain": []}

    for name in all_names:
        kind = _classify(name)
        if kind == "excluded":
            result["excluded"].append({"name": name})
        elif kind == "uncertain":
            result["uncertain"].append({"name": name})
        else:
            existing = data_store.find_company_by_name_or_tax_id(name)
            result["valid"].append({
                "name": name,
                "is_new": existing is None,
                "existing_id": existing["id"] if existing else None,
                "existing_labels": existing["labels"] if existing else [],
                "suggested_label": source_label,
            })

    return result


async def extract_companies_from_image(image_content: bytes, image_ext: str, source_label: str, api_key: str = "", provider: str = "anthropic") -> dict:
    """
    Pass image to Claude CLI for visual recognition.
    If the image contains a 統一編號 column, use GCIS to get the authoritative company name
    so that rare/misread Chinese characters are corrected.
    """
    prompt = (
        "請讀取圖片，找出其中的台灣公司資料。\n\n"
        "【輸出規則】\n"
        "1. 若圖片有「統一編號」欄位，請輸出 JSON 陣列，每筆格式為 "
        "{\"name\": \"公司名稱\", \"tax_id\": \"統一編號\"}\n"
        "2. 若圖片沒有統一編號，請輸出純字串陣列 [\"公司名稱A\", \"公司名稱B\"]\n"
        "3. 統一編號為8位數字，請完整抓取，不要省略\n"
        "4. 只抓公司或機構名稱，不要列人名、地址、電話、欄位標題\n"
        "5. 只輸出 JSON，不要任何說明文字"
    )

    raw_items: list[dict] = []
    try:
        raw = claude_client.ask_with_image(prompt, image_content, image_ext, timeout=120, api_key=api_key, provider=provider)
        log.info("Claude image raw response:\n%s", raw)
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            parsed = json.loads(raw[start:end + 1])
            for item in parsed:
                if isinstance(item, str) and len(item) >= 3:
                    raw_items.append({"name": item, "tax_id": ""})
                elif isinstance(item, dict):
                    name = (item.get("name") or item.get("募資企業名稱")
                            or item.get("公司名稱", ""))
                    tax_id = str(item.get("tax_id") or item.get("統一編號", "")).strip()
                    if name and len(name) >= 3:
                        raw_items.append({"name": name, "tax_id": tax_id})
            log.info("Parsed %d candidates from image", len(raw_items))
        else:
            log.warning("Claude response had no valid JSON array")
    except Exception as e:
        log.error("Claude image extraction failed: %s", e)

    # Resolve names via GCIS when tax_id is available (fixes rare character misreads)
    resolved = await _resolve_names(raw_items)

    result: dict[str, list] = {"valid": [], "excluded": [], "uncertain": []}
    for name in resolved:
        kind = _classify(name)
        if kind == "excluded":
            result["excluded"].append({"name": name})
        elif kind == "uncertain":
            result["uncertain"].append({"name": name})
        else:
            existing = data_store.find_company_by_name_or_tax_id(name)
            result["valid"].append({
                "name": name,
                "is_new": existing is None,
                "existing_id": existing["id"] if existing else None,
                "existing_labels": existing["labels"] if existing else [],
                "suggested_label": source_label,
            })
    return result


async def _resolve_names(items: list[dict]) -> list[str]:
    """For items with a valid 8-digit tax_id, fetch the official name from GCIS."""
    from . import gcis_client

    async def resolve_one(item: dict) -> str:
        tax_id = item.get("tax_id", "").strip()
        if tax_id and len(tax_id) == 8 and tax_id.isdigit():
            official = await gcis_client.fetch_company_name_by_tax_id(tax_id)
            if official:
                log.info("GCIS resolved %s → %s", item["name"], official)
                return official
        return item["name"]

    return list(await asyncio.gather(*[resolve_one(i) for i in items]))


async def suggest_companies_for_industry(industry: str, companies: list[dict], api_key: str = "", provider: str = "anthropic") -> list[str]:
    """Ask Claude which companies belong to the given industry. Returns list of matching company IDs."""
    if not companies:
        return []

    lines = []
    for c in companies:
        blurb = c.get("blurb") or c.get("name", "")
        lines.append(f'{c["id"]}: {c["name"]} — {blurb}')

    prompt = (
        f"以下是公司清單（格式：ID: 公司名稱 — 業務簡介）。\n"
        f"請找出哪些公司適合歸類為「{industry}」產業別。\n"
        f"只回傳符合的公司 ID，格式為 JSON 陣列，例如：[\"id1\", \"id2\"]\n"
        f"若沒有符合的公司，回傳 []\n"
        f"不要任何說明文字，只輸出 JSON 陣列。\n\n"
        f"公司清單：\n" + "\n".join(lines)
    )

    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 60, None, api_key, provider)
        log.info("Industry suggest raw: %s", raw[:200])
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            ids = json.loads(raw[start:end + 1])
            valid_ids = {c["id"] for c in companies}
            return [i for i in ids if isinstance(i, str) and i in valid_ids]
    except Exception as e:
        log.error("Industry suggest failed: %s", e)
    return []


async def suggest_industries_for_companies(
    companies: list[dict],
    industries: list[str],
    api_key: str = "",
    provider: str = "anthropic",
) -> dict[str, str]:
    """For each company, pick the best-fit industry from the given list.

    Returns {company_id: industry_name}. Companies with no good match are omitted.
    """
    if not companies or not industries:
        return {}

    lines = []
    for c in companies:
        name = c.get("name", "")
        blurb = (c.get("blurb") or "").strip()
        summary = (c.get("summary") or "").strip()
        parts = [f'{c["id"]}: {name}']
        if blurb:
            # blurb is ≤10 chars and sufficient for classification
            parts.append(f"一句話：{blurb}")
        elif summary:
            # fallback: only the first 100 chars to keep prompt compact
            parts.append(f"摘要：{summary[:100].replace(chr(10), ' ')}")
        lines.append(" | ".join(parts))

    industry_list = "\n".join(f"- {i}" for i in industries)
    prompt = (
        f"以下是既有的產業別清單與公司清單。請為每家公司從產業別清單中選一個最適合的，"
        f"判斷依據是公司業務描述（一句話與摘要）。\n"
        f"若沒有任何產業別合適，回傳空字串。\n"
        f"只輸出 JSON 物件，鍵為公司 ID、值為產業別名稱（必須是清單內的名稱或空字串），不要任何其他文字。\n"
        f"範例：{{\"id1\": \"前瞻科技\", \"id2\": \"\"}}\n\n"
        f"產業別清單：\n{industry_list}\n\n"
        f"公司清單：\n" + "\n".join(lines)
    )

    valid_ids = {c["id"] for c in companies}
    valid_inds = set(industries)
    try:
        raw = await asyncio.to_thread(claude_client.ask, prompt, 180, None, api_key, provider)
        log.info("Industry classify raw: %s", raw[:300])
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            obj = json.loads(raw[start:end + 1])
            return {
                cid: ind for cid, ind in obj.items()
                if isinstance(cid, str) and cid in valid_ids
                and isinstance(ind, str) and ind in valid_inds
            }
    except Exception as e:
        log.error("Industry classify failed: %s", e)
    return {}


def build_candidate(name: str, source_label: str) -> dict:
    """Turn an uncertain name (confirmed by user) into a valid candidate dict."""
    existing = data_store.find_company_by_name_or_tax_id(name)
    return {
        "name": name,
        "is_new": existing is None,
        "existing_id": existing["id"] if existing else None,
        "existing_labels": existing["labels"] if existing else [],
        "suggested_label": source_label,
    }


def _ask_claude(text: str, api_key: str = "", provider: str = "anthropic") -> list[str]:
    """Ask Claude to extract all company-like names from text."""
    if not text or len(text.strip()) < 5:
        log.info("Claude skipped: text too short")
        return []

    truncated = text[:8000]
    prompt = (
        "請從以下文字中找出所有疑似公司或機構的名稱。\n"
        "規則：\n"
        "1. 包含「股份有限公司」或「有限公司」的請完整列出\n"
        "2. 看起來是公司或組織但沒有標準結尾的名稱也請列出\n"
        "3. 不要捏造不存在於文字中的名稱\n"
        "4. 只回傳 JSON 陣列，例如：[\"AA科技股份有限公司\", \"BB有限公司\", \"CC創新\"]\n"
        "5. 如果文字中沒有任何公司名稱，回傳空陣列 []\n"
        "6. 不要有任何其他說明文字，只輸出 JSON 陣列\n\n"
        f"文字內容：\n{truncated}"
    )

    try:
        raw = claude_client.ask(prompt, timeout=90, api_key=api_key, provider=provider)
        log.info("Claude response: %s", raw[:300])

        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                names = json.loads(raw[start:end + 1])
                names = [n for n in names if isinstance(n, str) and len(n) >= 3]
                log.info("Claude extracted %d names: %s", len(names), names)
                return names
            except json.JSONDecodeError as e:
                log.warning("JSON parse failed: %s", e)
        log.warning("Claude response had no valid JSON array")
    except Exception as e:
        log.error("Claude call failed: %s", e)

    return []
