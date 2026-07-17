/* ── 自動抓取每股金額（生成完後靜默執行）── */
let _autoFetchQueue    = [];   // 待抓取的 company IDs
let _autoFetchRunning  = false;
let _autoFetchSessionId = null;
let _autoFetchCfSettle  = null;

function _needsAutoFetch(c) {
  if (!c || !c.tax_id) return false;
  if (c.par_value || c.no_par_value) return false;
  return true;
}

function _enqueueAutoFetch(companyId) {
  if (_autoFetchQueue.includes(companyId)) return;
  _autoFetchQueue.push(companyId);
  _processAutoFetchQueue();
}

async function _processAutoFetchQueue() {
  if (_autoFetchRunning || _autoFetchQueue.length === 0) return;
  _autoFetchRunning = true;
  while (_autoFetchQueue.length > 0) {
    const id = _autoFetchQueue.shift();
    const c  = state.companies.find(x => x.id === id);
    if (c && _needsAutoFetch(c)) await _runAutoFetch(c);
  }
  _autoFetchRunning = false;
}

async function _runAutoFetch(c) {
  try {
    const res = await fetch(`/api/findbiz/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ company_id: c.id, tax_id: c.tax_id }),
    });
    if (!res.ok) throw new Error(await res.text());
    const { session_id } = await res.json();
    _autoFetchSessionId = session_id;
    await _listenAutoFetchStream(session_id, c.id, c.name);
  } catch (err) {
    toast(`⚠️ ${c.name} 自動抓取失敗：${err.message}`, true);
  } finally {
    _autoFetchSessionId = null;
  }
}

function _listenAutoFetchStream(sessionId, companyId, companyName) {
  return new Promise(resolve => {
    const es = new EventSource(`/api/findbiz/stream/${sessionId}`);
    let settled = false;
    const settle = () => {
      if (!settled) {
        settled = true;
        _hideAutoFetchBanner();
        es.close();
        resolve();
      }
    };

    es.onmessage = async e => {
      const msg = JSON.parse(e.data);
      if (msg.type === "heartbeat") return;

      if (msg.type === "browser_ready") {
        _showAutoFetchBanner(companyName, settle);
      } else if (msg.type === "done") {
        settle();
        toast(`✅ 已自動抓取 ${companyName} 的每股金額`);
        try { await loadCompanies(); renderGrid(); } catch (_) {}
      } else if (msg.type === "error") {
        settle();
        toast(`⚠️ ${companyName} 自動抓取失敗：${msg.message}`, true);
      }
    };

    es.onerror = () => settle();
  });
}

function _showAutoFetchBanner(companyName, onSkip) {
  const banner = document.getElementById("auto-fetch-cf-banner");
  const msg    = document.getElementById("auto-fetch-cf-msg");
  if (!banner || !msg) return;
  msg.textContent = `🔍 自動抓取中：${companyName} — 請在已開啟的瀏覽器中完成 Cloudflare 驗證後點擊確認`;
  _autoFetchCfSettle = onSkip;
  banner.classList.remove("hidden");
}

function _hideAutoFetchBanner() {
  const banner = document.getElementById("auto-fetch-cf-banner");
  if (banner) banner.classList.add("hidden");
  _autoFetchCfSettle = null;
}

function confirmAutoFetchCloudflare() {
  if (!_autoFetchSessionId) return;
  fetch(`/api/findbiz/confirm/${_autoFetchSessionId}`, { method: "POST" }).catch(() => {});
}

function skipAutoFetchCloudflare() {
  if (_autoFetchCfSettle) _autoFetchCfSettle();
}

function refreshGcis() {
  const id = _modalCompanyId;
  if (!id) return;
  toast("正在重新拉取基本資料…");
  const btn = document.querySelector(".gcis-refresh-btn");
  if (btn) btn.disabled = true;

  const sseUrl = `/api/companies/${id}/refresh-gcis`;
  const es = new EventSource(sseUrl);

  es.onmessage = async e => {
    const event = JSON.parse(e.data);
    if (event.type === "data") {
      const company = state.companies.find(c => c.id === id);
      if (company) {
        Object.assign(company, event.fields);
        renderGrid();
        if (_modalCompanyId === id && document.getElementById("modal-overlay").classList.contains("open")) {
          openModal(id);
        }
      }
    } else if (event.type === "progress") {
      toast(event.message);
    } else if (event.type === "done") {
      es.close();
      const b = document.querySelector(".gcis-refresh-btn");
      if (b) b.disabled = false;
      try { await loadCompanies(); renderGrid(); if (_modalCompanyId === id) openModal(id); } catch (_) {}
    }
  };
  es.onerror = () => {
    es.close();
    const b = document.querySelector(".gcis-refresh-btn");
    if (b) b.disabled = false;
  };
}

// 共用：呼叫 find-website API，回傳 { website } | { website: "" }。
// _showBatchWebsitePrompt（多筆並行）與 _showWebsitePrompt（單筆）都用這個，
// 差異只在拿到結果後怎麼更新各自的 UI。
async function _fetchCompanyWebsite(companyId) {
  const findUrl = `/api/companies/${companyId}/find-website?engine=${encodeURIComponent(getAiEngine())}`;
  const r = await fetch(findUrl);
  return r.json();
}

function _showBatchWebsitePrompt(companyIds) {
  return new Promise(resolve => {
    const overlay   = document.getElementById("batch-wp-overlay");
    const body      = document.getElementById("batch-wp-body");
    const skipBtn   = document.getElementById("batch-wp-skip");
    const confirmBtn = document.getElementById("batch-wp-confirm");

    // Build rows
    body.innerHTML = companyIds.map((id, i) => {
      const c = state.companies.find(x => x.id === id);
      const name = c?.name || id;
      const existing = (c?.website || "").trim();
      return `
        <div class="bwp-row">
          <div class="bwp-row-header">
            <span class="bwp-name">${escHtml(name)}</span>
            <span class="bwp-status${existing ? "" : " searching"}" id="bwp-status-${i}">${existing ? "已知官網" : "搜尋中…"}</span>
          </div>
          <input class="bwp-input" type="url" id="bwp-input-${i}"
            placeholder="${existing ? "https://example.com" : "搜尋中…"}"
            value="${escAttr(existing)}"
            ${existing ? "" : "disabled"}
            autocomplete="off" />
        </div>`;
    }).join("");

    // Disable confirm until all searches finish; skip is always available
    const needSearch = companyIds.filter((id, i) => !state.companies.find(x => x.id === id)?.website);
    let pending = needSearch.length;
    const _updateConfirmBtn = () => {
      confirmBtn.disabled = pending > 0;
    };
    _updateConfirmBtn();

    overlay.classList.add("open");

    // Fire parallel searches for companies without a stored website
    companyIds.forEach((id, i) => {
      const c = state.companies.find(x => x.id === id);
      if (c?.website) return; // already known
      _fetchCompanyWebsite(id)
        .then(data => {
          if (!overlay.classList.contains("open")) return;
          const input  = document.getElementById(`bwp-input-${i}`);
          const status = document.getElementById(`bwp-status-${i}`);
          if (!input) return;
          input.disabled = false;
          if (data.website) {
            input.value = data.website;
            input.placeholder = "https://example.com";
            if (status) { status.textContent = "找到官網 ✓"; status.className = "bwp-status found"; }
          } else if (data.engine_error) {
            input.placeholder = "AI 引擎未就緒，請重試或換引擎";
            if (status) { status.textContent = "引擎未就緒（非查無）"; status.className = "bwp-status missing"; }
          } else {
            input.placeholder = "找不到，請手動填入";
            if (status) { status.textContent = "找不到官網"; status.className = "bwp-status missing"; }
          }
        })
        .catch(() => {
          if (!overlay.classList.contains("open")) return;
          const input = document.getElementById(`bwp-input-${i}`);
          const status = document.getElementById(`bwp-status-${i}`);
          if (input) { input.disabled = false; input.placeholder = "https://example.com"; }
          if (status) { status.className = "bwp-status missing"; status.textContent = "搜尋失敗"; }
        })
        .finally(() => {
          pending = Math.max(0, pending - 1);
          _updateConfirmBtn();
        });
    });

    const close = (result) => {
      overlay.classList.remove("open");
      skipBtn.onclick = null;
      confirmBtn.onclick = null;
      resolve(result);
    };

    skipBtn.onclick = () => close(null); // null = 略過，不儲存任何網址
    confirmBtn.onclick = () => {
      const map = {};
      companyIds.forEach((id, i) => {
        const input = document.getElementById(`bwp-input-${i}`);
        map[id] = input ? input.value.trim() : "";
      });
      close(map);
    };
  });
}

function _showWebsitePrompt(companyId, opts = {}) {
  return new Promise(resolve => {
    const c = state.companies.find(x => x.id === companyId);
    const overlay    = document.getElementById("website-prompt-overlay");
    const titleEl    = document.getElementById("website-prompt-title");
    const nameEl     = document.getElementById("website-prompt-company-name");
    const input      = document.getElementById("website-prompt-input");
    const skipBtn    = document.getElementById("website-prompt-skip");
    const confirmBtn = document.getElementById("website-prompt-confirm");
    const hintEl     = document.getElementById("website-prompt-hint");
    const progressEl   = document.getElementById("website-prompt-progress");
    const progressFill = document.getElementById("website-prompt-progress-fill");
    const progressPctEl= document.getElementById("website-prompt-progress-pct");

    if (!overlay || !nameEl || !input || !skipBtn || !confirmBtn) {
      resolve(undefined);
      return;
    }

    // opts: { autoSearch=true, title, confirmText, skipText, prefillValue }
    const autoSearch  = opts.autoSearch !== false;
    const existing    = opts.prefillValue !== undefined ? opts.prefillValue : (c?.website || "");
    if (titleEl) titleEl.textContent = opts.title || "生成公司簡介";
    skipBtn.textContent    = opts.skipText || "略過，直接生成";
    confirmBtn.textContent = opts.confirmText || "確認並生成";

    nameEl.textContent = c?.name || "";
    if (hintEl) hintEl.textContent = "提供官網可讓 AI 直接擷取業務資訊，生成更準確的簡介。若無官網可略過。";
    overlay.classList.add("open");

    let dotTimer = null;
    let progressTimer = null;
    let progressPct = 0;

    const _setProgress = (pct) => {
      progressPct = pct;
      const r = Math.round(pct);
      if (progressFill)  progressFill.style.width = r + "%";
      if (progressPctEl) progressPctEl.textContent = r + "%";
    };

    const close = (website) => {
      clearInterval(dotTimer);
      clearInterval(progressTimer);
      input.classList.remove("searching");
      if (progressEl) {
        progressEl.classList.remove("active");
        _setProgress(0);
      }
      if (hintEl) hintEl.classList.remove("searching");
      overlay.classList.remove("open");
      skipBtn.onclick = null;
      confirmBtn.onclick = null;
      input.onkeydown = null;
      resolve(website);
    };

    skipBtn.onclick    = () => close(undefined);
    confirmBtn.onclick = () => close(input.value.trim());
    input.onkeydown    = e => { if (e.key === "Enter") close(input.value.trim()); };

    if (existing) {
      input.value = existing;
      input.disabled = false;
      confirmBtn.disabled = false;
      input.placeholder = "https://example.com";
      setTimeout(() => input.focus(), 50);
    } else if (!autoSearch) {
      // 手動填入模式：不自動搜尋，給空輸入框讓使用者自行輸入
      input.value = "";
      input.disabled = false;
      confirmBtn.disabled = false;
      input.placeholder = "https://example.com";
      if (hintEl) hintEl.textContent = "請手動填入公司官網（或留空後按略過）。";
      setTimeout(() => input.focus(), 50);
    } else {
      // 搜尋期間：input 與確認按鈕均 disabled，shimmer 動畫 + 動態省略號告知使用者等待中
      input.value = "";
      input.disabled = true;
      confirmBtn.disabled = true;
      input.placeholder = "搜尋中…";
      if (progressEl) { _setProgress(0); progressEl.classList.add("active"); }
      if (hintEl) {
        hintEl.textContent = "AI 正在自動搜尋官網，請稍候片刻…";
        hintEl.classList.add("searching");
      }

      // 模擬進度：每 400ms 往 85% 慢速逼近，讓使用者感受到搜尋需要時間
      // 約 10s → 46%，20s → 71%，30s → 81%，不會快速衝到頂
      _setProgress(0);
      progressTimer = setInterval(() => {
        _setProgress(progressPct + (85 - progressPct) * 0.025);
      }, 400);

      const _endSearchingUi = () => {
        clearInterval(dotTimer);
        clearInterval(progressTimer);
        dotTimer = null;
        progressTimer = null;
        // 進度跳到 100%，短暫停留後隱藏
        _setProgress(100);
        setTimeout(() => {
          if (progressEl) progressEl.classList.remove("active");
          _setProgress(0);
        }, 450);
        input.classList.remove("searching");
        if (hintEl) hintEl.classList.remove("searching");
        input.disabled = false;
        confirmBtn.disabled = false;
        input.placeholder = "https://example.com";
      };

      _fetchCompanyWebsite(companyId)
        .then(data => {
          if (!overlay.classList.contains("open")) { _endSearchingUi(); return; }
          _endSearchingUi();
          if (data.website) {
            input.value = data.website;
            if (hintEl) hintEl.textContent = "提供官網可讓 AI 直接擷取業務資訊，生成更準確的簡介。若無官網可略過。";
          } else if (data.engine_error) {
            if (hintEl) hintEl.textContent = "AI 引擎未就緒或逾時（不代表沒有官網），請重試或到 ⚙ 換引擎；也可手動填入。";
          } else {
            if (hintEl) hintEl.textContent = "找不到官網，若您知道請手動填入（或留空略過）。";
          }
          input.focus();
        })
        .catch(() => {
          if (!overlay.classList.contains("open")) { _endSearchingUi(); return; }
          _endSearchingUi();
          if (hintEl) hintEl.textContent = "搜尋失敗，若您知道官網請手動填入（或留空略過）。";
          input.focus();
        });
    }
  });
}

// 沒官網時先問使用者：搜尋 / 填入 / 略過。resolve 'search' | 'manual' | 'skip' | null（取消）
function _showWebsiteChoice(c) {
  return new Promise(resolve => {
    const overlay   = document.getElementById("website-choice-overlay");
    const nameEl    = document.getElementById("website-choice-company-name");
    const searchBtn = document.getElementById("website-choice-search");
    const manualBtn = document.getElementById("website-choice-manual");
    const skipBtn   = document.getElementById("website-choice-skip");
    const cancelBtn = document.getElementById("website-choice-cancel");

    if (!overlay || !nameEl || !searchBtn || !manualBtn || !skipBtn || !cancelBtn) {
      resolve(null);
      return;
    }

    nameEl.textContent = c?.name || "";
    overlay.classList.add("open");

    const close = (choice) => {
      overlay.classList.remove("open");
      searchBtn.onclick = manualBtn.onclick = skipBtn.onclick = cancelBtn.onclick = null;
      overlay.onclick = null;
      resolve(choice);
    };
    searchBtn.onclick = () => close("search");
    manualBtn.onclick = () => close("manual");
    skipBtn.onclick   = () => close("skip");
    cancelBtn.onclick = () => close(null);
    overlay.onclick   = (e) => { if (e.target === overlay) close(null); };
  });
}

// 存官網到後端並同步 state + modal 基本資訊
async function _persistWebsite(id, website) {
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { website });
    const idx = state.companies.findIndex(x => x.id === id);
    if (idx !== -1) {
      state.companies[idx] = updated;
      const infoEl = document.getElementById("modal-info");
      if (infoEl) infoEl.innerHTML = _buildModalInfoHTML(updated);
    }
    return updated;
  } catch (_) { return null; }
}

// modal 內「編輯/新增官網」按鈕：只更新官網，不觸發重新生成
async function editWebsite() {
  const id = _modalCompanyId;
  if (!id) return;
  const c = state.companies.find(x => x.id === id);
  const website = await _showWebsitePrompt(id, {
    autoSearch: false,
    title: c?.website ? "編輯官方網站" : "新增官方網站",
    confirmText: "儲存",
    skipText: "取消",
    prefillValue: c?.website || "",
  });
  if (website === undefined) return;            // 取消
  if (website === (c?.website || "")) return;   // 沒變動
  const updated = await _persistWebsite(id, website);
  if (updated) toast(website ? "已更新官方網站" : "已清除官方網站");
}

// 重新生成公開簡介會整段重建、清掉逐段審核合併進去的簡報/訪談補充。
// 有已套用的補充段落時先警告（生成後可在「📎 補充資料」面板重新套用）。
function _confirmMaterialsOverwrite(c) {
  const applied = (c && c.materials_applied_headings) || [];
  if (!applied.length) return true;
  return confirm(
    `此公司的簡介已合併過補充資料（${applied.join("、")}）。\n\n` +
    `重新生成公開簡介會覆蓋這些段落。生成完成後可在「📎 補充資料」面板按一次「套用」重新併回。\n\n` +
    `確定要重新生成嗎？`
  );
}

async function regenSummary() {
  const id = _modalCompanyId;
  if (!id) return;
  const c = state.companies.find(x => x.id === id);
  if (!_confirmMaterialsOverwrite(c)) return;

  // 有官網 → 直接生成（官網可在 modal 用「✏️ 編輯」隨時調整）
  // 沒官網 → 先問使用者：搜尋 / 填入 / 略過
  if (!c?.website) {
    const choice = await _showWebsiteChoice(c);
    if (choice === null) return;   // 使用者取消，中止整個流程
    if (choice === "search" || choice === "manual") {
      const website = await _showWebsitePrompt(id, { autoSearch: choice === "search" });
      // undefined = 略過；"" = 清空；"https://..." = 填入
      if (website !== undefined && website !== (c?.website || "")) {
        await _persistWebsite(id, website);
      }
    }
    // skip → 不動官網，直接生成
  }

  const summaryEl = document.getElementById("modal-summary");
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">⏳ 重新生成中，請稍候（約 3–7 分鐘）…</p>";
  _expandSummarySection();
  _subscribeSummarize(id, true);
}

function deepEnrich() {
  const id = _modalCompanyId;
  if (!id) return;
  const c = state.companies.find(x => x.id === id);
  if (!_confirmMaterialsOverwrite(c)) return;
  let force = false;
  if (c && c.deep_enriched_at) {
    const when = formatTimestamp(c.deep_enriched_at);
    if (!confirm(`這間公司已於 ${when} 做過深度生成。\n\n再次深度生成會覆蓋現有的簡介與競業分析（約 4–8 分鐘）。\n確定要重新生成嗎？`)) return;
    force = true;   // 使用者確認覆蓋 → 帶給後端防重守衛
  }
  const summaryEl = document.getElementById("modal-summary");
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">🔍 深度搜尋媒體報導中，請稍候（約 4–8 分鐘）…</p>";
  _expandSummarySection();
  const btn = document.getElementById("modal-gen-btn");
  if (btn) btn.disabled = true;
  _subscribeDeepEnrich(id, force).finally(() => {
    if (btn) btn.disabled = false;
  });
}

function patentGen() {
  const id = _modalCompanyId;
  if (!id) return;
  showModalSection("patents");   // 切到專利頁，進度與結果都在這裡
  const status = document.getElementById("modal-patents-status");
  const table  = document.getElementById("modal-patents-table");
  if (table)  table.style.display = "none";
  if (status) status.innerHTML = '<p class="summary-placeholder">📋 連接 TIPO 系統中，請稍候…</p>';
  _subscribePatent(id);
}

const _PATENT_FOLD = 3;

// 儲存專利資料供 modal 使用
const _patentBriefData = {};

function _formatBriefHtml(rawText) {
  const text = rawText.replace(/^﻿/, '').replace(/  +/g, ' ').trim();

  const claimsIdx = text.search(/專利範圍|申請專利範圍/);
  const refIdx    = text.search(/參考文獻/);
  const firstBreak = [claimsIdx, refIdx].filter(i => i !== -1).reduce((a, b) => Math.min(a, b), Infinity);

  const abstract = (firstBreak < Infinity ? text.slice(0, firstBreak) : text).trim();
  const rest     = firstBreak < Infinity ? text.slice(firstBreak) : '';

  const html = [];

  // 摘要：每 2 句一段
  if (abstract) {
    const parts = abstract.split('。').filter(s => s.trim());
    const withDot = parts.map((s, i) => s.trim() + (i < parts.length - 1 || abstract.endsWith('。') ? '。' : ''));
    for (let i = 0; i < withDot.length; i += 2) {
      const chunk = withDot.slice(i, i + 2).join('');
      if (chunk.trim()) html.push(`<p>${escHtml(chunk)}</p>`);
    }
  }

  if (!rest) return html.join('') || `<p>${escHtml(text)}</p>`;

  // 專利範圍
  const claimsMatch = rest.match(/^(?:申請專利範圍|專利範圍)([\s\S]*?)(?=參考文獻|$)/);
  const refMatch    = rest.match(/參考文獻([\s\S]*)$/);

  if (claimsMatch) {
    const claimsText = claimsMatch[1]
      .replace(/^\s*\d+:\d+\s*/, '')
      .replace(/^(?:申請專利範圍|專利範圍)\s*/, '')
      .trim();
    if (claimsText) {
      html.push('<h4>專利範圍</h4>');
      // 嘗試按編號分段 "1.一種..." "2.如..."
      const items = claimsText.split(/(?=\d+\.\s*[一-鿿＀-￯])/).filter(s => s.trim());
      if (items.length > 1) {
        items.forEach(item => html.push(`<p class="patent-claim-item">${escHtml(item.trim())}</p>`));
      } else {
        const parts = claimsText.split('。').filter(s => s.trim());
        for (let i = 0; i < parts.length; i += 2) {
          const chunk = parts.slice(i, i + 2).map((s, j) => s + (i + j < parts.length - 1 ? '。' : '')).join('');
          if (chunk.trim()) html.push(`<p>${escHtml(chunk)}</p>`);
        }
      }
    }
  }

  // 參考文獻
  if (refMatch) {
    const refsText = refMatch[1].replace(/^引用專利\s*/, '').trim();
    if (refsText) {
      html.push('<h4>參考文獻</h4>');
      html.push(`<p class="patent-ref-text">${escHtml(refsText)}</p>`);
    }
  }

  return html.join('') || `<p>${escHtml(text)}</p>`;
}

function openBriefModal(idx) {
  const d = _patentBriefData[idx];
  if (!d) return;
  document.getElementById("patent-brief-title").textContent = d.title || "簡要說明";
  document.getElementById("patent-brief-text").innerHTML = _formatBriefHtml(d.brief);
  openOverlay("patent-brief-overlay");
}

function closeBriefModal(e) {
  if (e && e.target !== document.getElementById("patent-brief-overlay")) return;
  closeOverlay("patent-brief-overlay");
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeOverlay("patent-brief-overlay");
});

function togglePatentRows() {
  const rows = document.querySelectorAll(".patent-row-extra");
  const foldRow = document.getElementById("patent-fold-row");
  const btn = foldRow && foldRow.querySelector("button");
  const isHidden = rows.length && rows[0].style.display === "none";
  rows.forEach(r => r.style.display = isHidden ? "" : "none");
  if (btn) btn.textContent = isHidden ? "▲ 收合" : `▼ 展開全部（剩餘 ${rows.length} 筆）`;
}

function togglePatentSection() {
  const h4 = document.querySelector(".patent-section-h4");
  const table = document.getElementById("modal-patents-table");
  const isOpen = h4 && h4.classList.contains("is-open");
  if (h4) h4.classList.toggle("is-open", !isOpen);
  if (table) table.style.display = isOpen ? "none" : "";
}

function toggleShareholderSection() {
  const section = document.getElementById("modal-shareholders-section");
  const h4 = section && section.querySelector(".collapsible-h4");
  const content = document.getElementById("modal-shareholder-content");
  const isOpen = h4 && h4.classList.contains("is-open");
  if (h4) h4.classList.toggle("is-open", !isOpen);
  if (content) content.style.display = isOpen ? "none" : "";
}

function toggleSummarySection() {
  const summaryEl = document.getElementById("modal-summary");
  const h4 = summaryEl && summaryEl.closest(".modal-section")?.querySelector(".collapsible-h4");
  const isOpen = h4 && h4.classList.contains("is-open");
  if (h4) h4.classList.toggle("is-open", !isOpen);
  if (summaryEl) summaryEl.style.display = isOpen ? "none" : "";
}

function _expandSummarySection() {
  const summaryEl = document.getElementById("modal-summary");
  const h4 = summaryEl && summaryEl.closest(".modal-section")?.querySelector(".collapsible-h4");
  if (h4) h4.classList.add("is-open");
  if (summaryEl) summaryEl.style.display = "";
}

function _subscribePatent(companyId) {
  const es = trackModalES(new EventSource(`/api/companies/${companyId}/patents`));
  const status = document.getElementById("modal-patents-status");
  const hint   = document.getElementById("modal-patents-hint");

  es.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if (d.type === "progress") {
      if (status) status.innerHTML = `<p class="summary-placeholder">${escHtml(d.message)}</p>`;
    } else if (d.type === "done") {
      es.close();
      if (status) status.innerHTML = "";
      const c = state.companies.find(x => x.id === companyId);
      if (c && d.patents) c.patents = d.patents;
      _renderPatents(companyId, true);
    } else if (d.type === "error") {
      es.close();
      if (status) status.innerHTML = `<p class="summary-placeholder" style="color:var(--danger)">⚠ ${escHtml(d.message)}</p>`;
    }
  };
  es.onerror = () => {
    es.close();
    if (status) status.innerHTML = '<p class="summary-placeholder" style="color:var(--danger)">⚠ 連線中斷</p>';
  };
}

function _renderPatents(companyId, autoShow = false) {
  const c = state.companies.find(x => x.id === companyId);
  const patents = c && c.patents;
  const hint    = document.getElementById("modal-patents-hint");
  const table   = document.getElementById("modal-patents-table");
  const tbody   = document.getElementById("modal-patents-body");
  if (!patents || !patents.length) {
    if (hint) hint.textContent = "未找到專利資料";
    return;
  }
  if (hint) hint.textContent = `共 ${patents.length} 筆（更新：${patents[0]?.fetched_at || ""}）`;
  const makeRow = (p, idx) => {
    const applicantPart = p.applicant
      ? `<span class="inv-applicant">申請人：${escHtml(p.applicant)}</span>` : "";
    const inventorPart = (p.inventors || []).length
      ? `<span class="inv-inventor">發明人：${escHtml((p.inventors || []).join("、"))}</span>` : "";
    const inventorsHtml = [applicantPart, inventorPart].filter(Boolean).join("") || "—";

    const brief = p.brief || "";
    const briefPreview = brief.length > 30 ? brief.slice(0, 30) + "…" : brief;
    const hasMore = brief.length > 30;
    if (brief) _patentBriefData[idx] = { title: p.title, brief };
    const briefHtml = brief
      ? `<span class="brief-preview">${escHtml(briefPreview)}</span>`
        + (hasMore
          ? `<button class="brief-toggle" onclick="openBriefModal(${idx})">展開</button>`
          : "")
      : "—";

    const hidden = idx >= _PATENT_FOLD ? ' class="patent-row-extra" style="display:none"' : '';
    return `<tr${hidden}>
      <td class="patent-no">${escHtml(p.patent_no || "—")}</td>
      <td class="patent-title">${escHtml(p.title || "—")}</td>
      <td class="patent-date">${escHtml(p.app_date || "—")}</td>
      <td class="patent-status ${p.status === "核准" ? "status-granted" : ""}">${escHtml(p.status || "—")}</td>
      <td class="patent-inventors">${inventorsHtml}</td>
      <td class="patent-brief">${briefHtml}</td>
    </tr>`;
  };

  let rows = patents.map(makeRow).join("");
  if (patents.length > _PATENT_FOLD) {
    const extra = patents.length - _PATENT_FOLD;
    rows += `<tr id="patent-fold-row">
      <td colspan="6" style="text-align:center;padding:6px 0">
        <button class="brief-toggle" style="font-size:11px;padding:2px 12px"
          onclick="togglePatentRows()">▼ 展開全部（剩餘 ${extra} 筆）</button>
      </td>
    </tr>`;
  }
  tbody.innerHTML = rows;
  if (table) table.style.display = "";
  _refreshModalBookmarks();          // enable 專利 bookmark + badge
  if (autoShow) showModalSection("patents");
}

function _updateSummaryInModal(company) {
  if (_modalCompanyId !== company.id) return;
  if (!document.getElementById("modal-overlay").classList.contains("open")) return;
  const summaryEl = document.getElementById("modal-summary");
  if (!summaryEl) return;
  summaryEl.innerHTML = company.summary
    ? renderSummary(company.summary, company.materials_applied_headings)
    : "<p class=\"summary-placeholder\">（公司簡介資料補充中，請稍後重整）</p>";
  summaryEl.style.display = "";
  applySummaryTabs(summaryEl);   // rebuild 公司簡介 sub-tabs
  showModalSection("summary");   // jump to 公司簡介 page to show the result
}

function _subscribeSummarize(companyId, reset = false) {
  const params = new URLSearchParams();
  if (reset) params.set("reset", "true");
  params.set("engine", getAiEngine());
  const sseUrl = `/api/companies/${companyId}/summarize?${params}`;

  state.enrichingIds.add(companyId);
  renderGrid();

  return new Promise(resolve => {
    const es = new EventSource(sseUrl);
    let settled = false;
    const settle = () => { if (!settled) { settled = true; resolve(); } };

    es.onmessage = async e => {
      const event = JSON.parse(e.data);
      if (event.type === "data") {
        const company = state.companies.find(c => c.id === companyId);
        if (company) {
          Object.assign(company, event.fields);
          renderGrid();
          _updateSummaryInModal(company);
        }
      } else if (event.type === "progress") {
        toast(event.message);
      } else if (event.type === "done") {
        es.close();
        settle();
        state.enrichingIds.delete(companyId);
        state.doneIds.add(companyId);
        try { await loadCompanies(); computeGroups(); renderSidebar(); renderGrid(); } catch (_) {}
        setTimeout(() => { state.doneIds.delete(companyId); renderGrid(); }, 3000);
      }
    };
    es.onerror = () => {
      es.close();
      state.enrichingIds.delete(companyId);
      renderGrid();
      settle();
    };
  });
}

function _subscribeDeepEnrich(companyId, force = false) {
  const sseUrl = `/api/companies/${companyId}/deep-enrich?engine=${encodeURIComponent(getAiEngine())}`
    + (force ? "&force=true" : "");

  state.enrichingIds.add(companyId);
  renderGrid();

  return new Promise(resolve => {
    const es = new EventSource(sseUrl);
    let settled = false;
    const settle = () => { if (!settled) { settled = true; resolve(); } };

    es.onmessage = async e => {
      const event = JSON.parse(e.data);
      if (event.type === "data") {
        const company = state.companies.find(c => c.id === companyId);
        if (company) {
          Object.assign(company, event.fields);
          renderGrid();
          _updateSummaryInModal(company);
        }
      } else if (event.type === "progress") {
        toast(event.message);
      } else if (event.type === "done") {
        es.close();
        settle();
        state.enrichingIds.delete(companyId);
        state.doneIds.add(companyId);
        try { await loadCompanies(); computeGroups(); renderSidebar(); renderGrid(); } catch (_) {}
        setTimeout(() => { state.doneIds.delete(companyId); renderGrid(); }, 3000);
      }
    };
    es.onerror = () => {
      es.close();
      state.enrichingIds.delete(companyId);
      renderGrid();
      settle();
    };
  });
}

function _closeDetailModal() {
  closeOverlay("modal-overlay");
  document.body.classList.remove("detail-open");
  closeMaterialsPanel();
}
document.getElementById("modal-close").addEventListener("click", _closeDetailModal);
document.getElementById("modal-overlay").addEventListener("click", e => {
  // In side-by-side mode the overlay is pointer-events:none, so this only fires when alone
  if (e.target === document.getElementById("modal-overlay")) _closeDetailModal();
});

/* ── Upload ── */
const fileInput = document.getElementById("file-input");
const uploadProgress = document.getElementById("upload-progress");

// 開啟/關閉「新增公司」子選單
const _addMenuBtn = document.getElementById("add-company-menu-btn");
const _addSubmenu = document.getElementById("add-company-submenu");
const _addArrow = document.getElementById("add-company-arrow");
_addMenuBtn.addEventListener("click", () => {
  const open = _addSubmenu.style.display === "none" || !_addSubmenu.style.display;
  _addSubmenu.style.display = open ? "block" : "none";
  _addArrow.style.transform = open ? "rotate(180deg)" : "";
});

// 上傳檔案觸發
document.getElementById("upload-trigger-btn").addEventListener("click", () => {
  _addSubmenu.style.display = "none";
  _addArrow.style.transform = "";
  fileInput.click();
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) handleUpload(fileInput.files[0]);
});

// 全部公司 / 面板入口（合一）
document.getElementById("sb-main-btn").addEventListener("click", openSidePanel);

// Panel 開關
document.getElementById("open-side-panel-btn")?.addEventListener("click", openSidePanel);
document.getElementById("close-side-panel-btn").addEventListener("click", closeSidePanel);
document.getElementById("main").addEventListener("click", e => {
  if (document.getElementById("side-panel").classList.contains("open")) closeSidePanel();
});

// Panel 分頁切換
document.querySelectorAll(".sp-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".sp-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    state.sidePanelTab = tab.dataset.tab;
    _renderSidePanelToolbar();
    renderSidePanel();
  });
});
document.getElementById("sp-search").addEventListener("input", e => {
  state.sidePanelSearch = e.target.value;
  renderSidePanel();
});
document.getElementById("sp-sort").addEventListener("change", e => {
  state.sidePanelSort = e.target.value;
  renderSidePanel();
});

async function handleUpload(file) {
  uploadProgress.textContent = "⏳ 正在解析檔案…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const result = await api("POST", "/api/upload", fd);

    if (result.ocr_failed) {
      uploadProgress.textContent = `⚠️ 無法從圖片讀出公司名稱，請手動輸入`;
      setTimeout(() => uploadProgress.textContent = "", 5000);
      openManualDialog(result.suggested_label);
      return;
    }

    const validCount = (result.valid || []).length;
    const uncertainCount = (result.uncertain || []).length;
    const excludedCount = (result.excluded || []).length;

    if (validCount === 0 && uncertainCount === 0) {
      let msg = "✅ 解析完成，未找到股份有限公司";
      if (excludedCount > 0) msg += `（已排除 ${excludedCount} 間有限公司）`;
      uploadProgress.textContent = msg;
      setTimeout(() => uploadProgress.textContent = "", 5000);
      return;
    }

    uploadProgress.textContent =
      `✅ 找到 ${validCount} 間股份有限公司` +
      (uncertainCount ? `，${uncertainCount} 間待確認` : "") +
      (excludedCount ? `，排除 ${excludedCount} 間有限公司` : "");
    setTimeout(() => uploadProgress.textContent = "", 5000);

    alertDone("(!) 辨識完成 — 請確認", `✅ 找到 ${validCount} 間公司，請確認辨識結果`);
    openNameReviewDialog(result.valid || [], result.uncertain || [], result.excluded || [], result.suggested_label);
  } catch (err) {
    uploadProgress.textContent = `❌ ${err.message}`;
    setTimeout(() => uploadProgress.textContent = "", 6000);
  } finally {
    fileInput.value = "";
  }
}

