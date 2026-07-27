"""AI enrichment endpoints: GCIS lookup + summary/blurb generation for companies.

Split out of routers/companies.py (which still owns plain CRUD + graphs). Two
CRUD-flavored endpoints in companies.py — confirm_companies and
add_company_from_graph — still need to kick off enrichment after creating a
company; they call the public `start_enrichment()` below instead of reaching
into this module's private `_enrich_company`/`_enrich_channel.running`/`_spawn`
(companies.py → enrichment.py, one-way; this module never imports from
companies.py)."""
import asyncio
import ipaddress
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services import claude_client, company_extractor, competitor_service, data_store, gcis_client, report_generator
from services.ai_deps import ai_from_headers, ai_from_query
from services.task_progress import ProgressChannel, spawn_background as _spawn

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/companies", tags=["enrichment"])

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _host_is_public(host: str) -> bool:
    """host 解析出的所有 IP 都是公開位址才回 True；任一私有/loopback/link-local/保留 → False。
    擋掉 AI 生成的 URL 把 httpx 導向 127.0.0.1 / 169.254.x / 內網（含本機其他 port）造成 SSRF。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


async def _ssrf_safe_reachable(url: str, *, max_redirects: int = 3) -> bool:
    """驗證 URL 可達，且每一跳都指向公開位址（手動跟隨 redirect 以便逐跳檢查 host）。"""
    current = url
    async with httpx.AsyncClient(
        timeout=8.0, follow_redirects=False, headers={"User-Agent": _UA},
    ) as vc:
        for _ in range(max_redirects + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return False
            if not _host_is_public(parts.hostname):
                log.info("find-website blocked non-public host: %s", parts.hostname)
                return False
            try:
                resp = await vc.head(current)
                if resp.status_code >= 400:
                    resp = await vc.get(current)
            except Exception:
                return False
            if resp.is_redirect and resp.headers.get("location"):
                current = str(httpx.URL(current).join(resp.headers["location"]))
                continue
            return resp.status_code < 400
    return False


# 四個獨立的 SSE 進度追蹤 channel（key = company_id），一種 kind 一個。
# routers/companies.py 另有 2 組同構的（build-relationship 的 rel、patents 的
# patent），一起收斂進 ProgressChannel（見 services/task_progress.py）——取代
# 原本 6 處各自手刻的 events/push/push_data/push_error + try/finally discard。
_enrich_channel = ProgressChannel()
_deep_channel = ProgressChannel()
_gcis_channel = ProgressChannel()
_summarize_channel = ProgressChannel()


def start_enrichment(company_id: str, engine: str = "claude") -> bool:
    """公開 API：啟動（或略過重複啟動）某公司的 AI 補全背景任務。回傳是否真的
    啟動了新任務——False 代表該公司已在跑，呼叫端不用另外處理。取代原本
    routers/companies.py 直接 import _enrich_company/_running/_spawn 三個私有
    識別字（router 之間直接互相耦合成 application service）。"""
    if company_id in _enrich_channel.running:
        return False
    _enrich_channel.running.add(company_id)
    _spawn(_enrich_company(company_id, engine=engine))
    return True


def _nonempty_fields(d: dict) -> dict:
    """只保留非空/非零欄位。外部 API（g0v/GCIS）全失敗時 gcis 回的是全預設骨架，
    用它整包寫回會洗掉既有的董監事/資本額/代表人等好資料。"""
    return {k: v for k, v in d.items() if v not in ("", 0, [], None)}


class EnrichBatchRequest(BaseModel):
    company_ids: list[str]


class SuggestIndustriesRequest(BaseModel):
    company_ids: list[str] | None = None


# suggest-industries 單次送 AI 的公司數上限（prompt 大小保護）
_SUGGEST_MAX = 80


@router.post("/suggest-industries")
async def suggest_industries(req: SuggestIndustriesRequest, ai: dict = Depends(ai_from_headers)):
    """Use AI to assign each given company an industry from the existing list.

    If `company_ids` is omitted, defaults to all companies missing an industry.
    Does not write changes — caller applies via PUT.
    """
    industries = data_store.get_industries()
    if not industries:
        raise HTTPException(status_code=422, detail="尚未建立任何產業別，請先新增至少一個產業別")

    all_companies = data_store.get_all_companies()
    if req.company_ids:
        wanted = set(req.company_ids)
        targets = [c for c in all_companies if c["id"] in wanted]
    else:
        targets = [c for c in all_companies if not (data_store.company_industries(c))]

    if not targets:
        return {"suggestions": {}, "industries": industries, "targets": []}

    # Cap：未分類公司會全部塞進單一 prompt，隨資料成長遲早撐爆（數百家＝數十 K 字）。
    # 一次最多送 _SUGGEST_MAX 家；truncated 回給前端提示「再跑一次處理剩餘的」。
    truncated = max(0, len(targets) - _SUGGEST_MAX)
    if truncated:
        targets = targets[:_SUGGEST_MAX]

    suggestions = await company_extractor.suggest_industries_for_companies(
        targets, industries, **ai
    )
    return {
        "suggestions": suggestions,
        "industries": industries,
        "targets": [{"id": c["id"], "name": c["name"], "blurb": c.get("blurb") or ""} for c in targets],
        "truncated": truncated,   # 還有幾家沒送 AI（0 = 全部處理完）
    }


@router.post("/enrich-batch")
async def enrich_batch(req: EnrichBatchRequest, ai: dict = Depends(ai_from_headers)):
    """Spawn enrichment tasks for the given company IDs (skips ones already running)."""
    known = {c["id"] for c in data_store.get_all_companies()}
    started = [cid for cid in req.company_ids if cid in known and start_enrichment(cid, **ai)]
    return {"started": started}


@router.get("/enrich/{company_id}")
async def enrich_stream(company_id: str, ai: dict = Depends(ai_from_query)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return StreamingResponse(
        _enrich_channel.stream(company_id, lambda: _spawn(_enrich_company(company_id, **ai))),
        media_type="text/event-stream",
    )


@router.get("/{company_id}/deep-enrich")
async def deep_enrich_stream(company_id: str, force: bool = False, ai: dict = Depends(ai_from_query)):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    # 防重：深度生成是全平台最貴的呼叫（Opus、~8 分鐘）。已做過的公司必須帶
    # force=true（前端在使用者確認覆蓋後才帶）才准重跑，避免誤觸重複燒額度。
    if company.get("deep_enriched_at") and not force:
        raise HTTPException(
            status_code=409,
            detail=f"此公司已於 {company['deep_enriched_at'][:10]} 深度生成過，重跑請帶 force=true",
        )

    return StreamingResponse(
        _deep_channel.stream(company_id, lambda: _spawn(_deep_enrich_company(company_id, **ai))),
        media_type="text/event-stream",
    )


async def _summarize_company(company_id: str, engine: str = "claude", reset: bool = False) -> None:
    """Summary-only enrichment: skips GCIS fetch, only runs AI summary generation.
    reset=True clears existing summary/competitors and ignores known competitor context,
    producing a true from-scratch rewrite."""
    with _summarize_channel.session(company_id) as ev:
        company = data_store.get_company(company_id)
        if not company:
            ev.done(True)
            return

        if reset:
            data_store.update_company(company_id, {"summary": "", "blurb": "", "competitors": []})
            company = data_store.get_company(company_id)
            ev.progress("已清除舊資料，從零重新生成（約 3–7 分鐘）…")
            ctx = None
        else:
            ev.progress("正在生成公司簡介（約 3–7 分鐘）…")
            ctx = competitor_service.gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                ev.progress(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")

        try:
            result = await report_generator.generate_summary(
                company, engine=engine, competitor_context=ctx
            )
            saved = _save_summary_result(company_id, result, extra={
                "enrich_status": "ok",
                "enriched_at": datetime.now(timezone.utc).isoformat(),
                "enrich_error": "",
            })
            ev.data({"summary": saved["summary"], "blurb": saved["blurb"]})
            ev.progress("公司簡介已生成完成")
            ev.done(True)
        except Exception as e:
            log.warning("summarize failed for %s: %s", company_id, e)
            data_store.update_company(company_id, {
                "enrich_status": "failed",
                "enrich_error": "公司簡介生成失敗，可在卡片點『重試』重新生成",
            })
            ev.error("公司簡介生成失敗，可在卡片點『重試』重新生成", code=claude_client.classify_ai_error(e))
            ev.done(False)


@router.get("/{company_id}/summarize")
async def summarize_stream(company_id: str, reset: bool = False, ai: dict = Depends(ai_from_query)):
    """SSE: regenerate AI summary only, without re-fetching GCIS data.
    reset=true clears existing data and skips injecting known competitors."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return StreamingResponse(
        _summarize_channel.stream(company_id, lambda: _spawn(_summarize_company(company_id, **ai, reset=reset))),
        media_type="text/event-stream",
    )


@router.get("/{company_id}/find-website")
async def find_website(company_id: str, ai: dict = Depends(ai_from_query)):
    """Quick WebSearch to find the company's official website URL."""
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    name = company.get("name", "")
    tax_id = company.get("tax_id", "")
    full = name if any(name.endswith(s) for s in ("股份有限公司", "有限公司")) else name + "股份有限公司"

    prompt = (
        f"請用 WebSearch 搜尋「{full}」（統編：{tax_id}）的官方網站。\n"
        f"只輸出最可能的官方網站 URL（含 https://），禁止任何其他說明文字。\n"
        f"若找不到官方網站，輸出空字串。\n"
        f"範例輸出：https://example.com"
    )
    try:
        result = await asyncio.to_thread(
            claude_client.ask,
            prompt, 60, ["WebSearch"],
            ai.get("engine", "claude"), 6,
        )
        url = result.strip().split("\n")[0].strip()
        if not url.startswith("http"):
            return {"website": ""}

        # 驗證可達性，並擋掉指向私網/本機的 SSRF（逐跳檢查 host）
        if not await _ssrf_safe_reachable(url):
            log.info("find-website URL unreachable or blocked for %s: %s", company_id, url)
            return {"website": ""}

        return {"website": url}
    except Exception as exc:
        # 引擎未就緒/逾時 ≠ 確實查無官網，回 engine_error 讓前端顯示可行動訊息
        log.warning("find-website failed for %s: %s", company_id, exc)
        return {"website": "", "engine_error": True}


@router.get("/{company_id}/refresh-gcis")
async def refresh_gcis_stream(company_id: str):
    company = data_store.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return StreamingResponse(
        _gcis_channel.stream(company_id, lambda: _spawn(_refresh_gcis_only(company_id)), max_ticks=120),
        media_type="text/event-stream",
    )


async def _refresh_gcis_only(company_id: str) -> None:
    with _gcis_channel.session(company_id) as ev:
        company = data_store.get_company(company_id)
        if not company:
            ev.done(True)
            return

        name = company["name"]
        stored_tax_id = company.get("tax_id", "")
        ev.progress(f"正在重新拉取 GCIS 資料：{name}")

        try:
            if stored_tax_id:
                enrichment = await gcis_client.fetch_company_data_by_tax_id(stored_tax_id)
            else:
                enrichment = await gcis_client.fetch_company_data(name)
            matched_name: str = enrichment.pop("matched_name", "")
            clean = _nonempty_fields(enrichment)
            if matched_name and matched_name != name:
                clean["name"] = matched_name
            if clean:
                data_store.update_company(company_id, clean)
                directors_count = len(clean.get("directors", []))
                ev.data(clean)
                ev.progress(f"基本資料已更新（資本額、代表人、董監事 {directors_count} 人）")
            else:
                ev.progress("政府登記資料暫時查無，保留既有資料")
        except Exception as e:
            log.warning("GCIS refresh failed for %s: %s", name, e)
            ev.progress("政府登記資料查詢暫時失敗")

        # GCIS 查詢失敗一律降級為「保留既有資料」，不是使用者可感知的失敗，done 恆為 ok。
        ev.done(True)


def _save_summary_result(company_id: str, result: dict, extra: dict | None = None) -> dict:
    """Persist summary/blurb/competitors from a generation result. `extra` folds
    additional fields into the same write (avoids a second full-file rewrite).
    Returns the fields saved."""
    fields: dict = {
        "summary": result.get("summary", ""),
        "blurb":   result.get("blurb", ""),
        # A full public-data regen replaces the whole summary, so any
        # previously-applied 簡報 section markers no longer apply.
        "materials_applied_headings": [],
    }
    if "competitors" in result:
        fields["competitors"] = competitor_service.resolve_competitor_ids(result["competitors"])
    if extra:
        fields.update(extra)
    data_store.update_company(company_id, fields)
    return fields


async def _enrich_company(company_id: str, engine: str = "claude") -> None:
    data_store.update_company(company_id, {"enrich_status": "generating"})

    with _enrich_channel.session(company_id) as ev:
        failed = False
        company = data_store.get_company(company_id)
        if not company:
            ev.done(True)
            return

        name = company["name"]
        stored_tax_id = company.get("tax_id", "")
        ev.progress(f"步驟 1/2：查詢政府登記資料（{name}）…")

        try:
            if stored_tax_id:
                enrichment = await gcis_client.fetch_company_data_by_tax_id(stored_tax_id)
            else:
                enrichment = await gcis_client.fetch_company_data(name)
            matched_name: str = enrichment.pop("matched_name", "")
            clean = _nonempty_fields(enrichment)
            # 修正公司名為 API 短名（去法律尾綴），併進同一次寫入（省一次全檔重寫）
            if matched_name:
                short = matched_name
                for sfx in ("股份有限公司", "有限公司"):
                    if short.endswith(sfx):
                        short = short[:-len(sfx)]
                        break
                if short and short != name:
                    clean["name"] = short
            if clean:
                data_store.update_company(company_id, clean)
                directors_count = len(clean.get("directors", []))
                ev.data(clean)
                ev.progress(f"基本資料已更新（資本額、代表人、董監事 {directors_count} 人）")
                if clean.get("name"):
                    ev.progress(f"公司名稱更新為：{clean['name']}")
            else:
                ev.progress("政府登記資料暫時查無，保留既有資料")
        except Exception as e:
            log.warning("GCIS enrichment failed for %s: %s", name, e)
            ev.progress("政府登記資料查詢暫時失敗，將僅生成簡介")

        ev.progress("步驟 2/2：生成公司簡介（約 3–7 分鐘）…")
        company = data_store.get_company(company_id)
        if not company:
            ev.done(True)
            return
        try:
            ctx = competitor_service.gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                ev.progress(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")
            result = await report_generator.generate_summary(
                company, engine=engine, competitor_context=ctx or None
            )
            saved = _save_summary_result(company_id, result, extra={
                "enrich_status": "ok",
                "enriched_at": datetime.now(timezone.utc).isoformat(),
                "enrich_error": "",
            })
            ev.data({"summary": saved["summary"], "blurb": saved["blurb"]})
            ev.progress("公司簡介已生成完成")
            # Reverse link: if this company appears in other companies' competitor lists, fill company_id
            competitor_service.backlink_competitor(company_id, company["name"])
        except Exception as e:
            failed = True
            log.warning("summary generation failed for %s: %s", name, e)
            data_store.update_company(company_id, {
                "enrich_status": "failed",
                "enrich_error": "公司簡介生成失敗，可在卡片點『重試』重新生成",
            })
            ev.error("公司簡介生成失敗，可在卡片點『重試』重新生成", code=claude_client.classify_ai_error(e))

        try:
            from services.jk_nb_exporter import export_company_to_jk_nb
            export_company_to_jk_nb(data_store.get_company(company_id) or {})
        except Exception:
            log.exception("jk_nb export failed for company %s (non-fatal)", company_id)

        ev.done(not failed)


async def _deep_enrich_company(company_id: str, engine: str = "claude") -> None:
    with _deep_channel.session(company_id) as ev:
        failed = False
        company = data_store.get_company(company_id)
        if not company:
            ev.done(True)
            return

        ev.progress("正在深度搜尋媒體報導與新聞（約 4–8 分鐘）…")
        try:
            ctx = competitor_service.gather_competitor_context(company_id, company.get("name", ""))
            if ctx["direct"]:
                ev.progress(f"偵測到 {len(ctx['direct'])} 家直接競業、{len(ctx['extended'])} 家延伸競業，將一併納入分析…")
            result = await report_generator.deep_enrich_summary(
                company, engine=engine, competitor_context=ctx or None
            )
            # Mark that a deep enrich has completed, so the UI can warn before
            # re-running it (distinct from last_updated, which any update touches).
            deep_at = datetime.now(timezone.utc).isoformat()
            saved = _save_summary_result(company_id, result, extra={"deep_enriched_at": deep_at})
            ev.data({"summary": saved["summary"], "blurb": saved["blurb"], "deep_enriched_at": deep_at})
            ev.progress("深度生成完成")
        except Exception as e:
            failed = True
            log.warning("deep enrich failed for %s: %s", company_id, e)
            ev.error(f"深度生成失敗：{e}", code=claude_client.classify_ai_error(e))

        try:
            from services.jk_nb_exporter import export_company_to_jk_nb
            export_company_to_jk_nb(data_store.get_company(company_id) or {})
        except Exception:
            log.exception("jk_nb export failed for company %s (non-fatal)", company_id)

        # 若上面意外拋出未捕捉的例外（非預期中止），session() 的 finally 安全網
        # 會自動補一個 done(ok=False)，不需要在這裡自己再判斷一次。
        ev.done(not failed)
