/* ── Industry Daily Digest Panel ── */

// digest cache & in-flight tracker (separate from state to avoid serialisation issues)
const _digestCache = {};       // { industry: { data, fetchedAt } | { error, fetchedAt } }
const _digestLoading = new Set();
const _digestTopic = {};       // { industry: topicName | null }  — active pill per industry
const _trendsCache = {};       // { industry: { data, fetchedAt } | { error, fetchedAt } }
const _trendsLoading = new Set();
const _panelView = {};         // { industry: "digest" | "trends" }

function renderIndustryPanel() {
  const panel = document.getElementById("industry-panel");
  const show = state.activeTab === "all" && !!state.activeIndustry;
  panel.style.display = show ? "" : "none";
  if (!show) return;
  _doRenderIndustryPanel(panel, state.activeIndustry);
}

function _doRenderIndustryPanel(panel, industry) {
  if ((_panelView[industry] ?? "digest") === "trends") {
    _doRenderTrendsPanel(panel, industry);
  } else {
    const cached = _digestCache[industry];
    if (cached?.data) {
      _renderDigestContent(panel, cached.data, industry);
    } else if (cached?.error) {
      panel.innerHTML = `<div class="daily-error">⚠ 無法載入日報：${escHtml(cached.error)}</div>`;
    } else if (_digestLoading.has(industry)) {
      if (!panel.querySelector(".daily-loading")) panel.innerHTML = _digestLoadingHtml(industry);
    } else {
      panel.innerHTML = _digestLoadingHtml(industry);
      _fetchDigest(industry, false);
    }
  }
}

function _doRenderTrendsPanel(panel, industry) {
  const cached = _trendsCache[industry];
  if (cached?.data) {
    _renderTrendsContent(panel, cached.data, industry);
  } else if (cached?.error) {
    panel.innerHTML = `<div class="daily-error">⚠ 無法載入趨勢：${escHtml(cached.error)}</div>`;
  } else if (_trendsLoading.has(industry)) {
    if (!panel.querySelector(".daily-loading")) panel.innerHTML = _trendsLoadingHtml(industry);
  } else {
    panel.innerHTML = _trendsLoadingHtml(industry);
    _fetchTrends(industry, false);
  }
}

function _viewTabsHtml(industry, activeView) {
  return `<div class="daily-view-tabs">
    <button class="daily-view-tab${activeView === "digest" ? " active" : ""}"
      onclick='switchPanelView(${JSON.stringify(industry)}, "digest")'>熱門日報</button>
    <button class="daily-view-tab${activeView === "trends" ? " active" : ""}"
      onclick='switchPanelView(${JSON.stringify(industry)}, "trends")'>本季趨勢</button>
  </div>`;
}

function _updateIndustryMapToolbarBtn() {
  const btn = document.getElementById("industry-map-toolbar-btn");
  if (!btn) return;
  // 只要在某個產業視角下就顯示（含「追蹤」tab 仍是該產業的子集）
  const show = !!state.activeIndustry && !state.activeLabel && !state.activeLabelGroup;
  btn.style.display = show ? "" : "none";
  if (show) {
    btn.onclick = () => openIndustryMap(state.activeIndustry);
    btn.title = `開啟「${state.activeIndustry}」的產業地圖`;
  }
}

async function _fetchDigest(industry, forceRefresh) {
  _digestLoading.add(industry);
  try {
    const qs = forceRefresh ? "?refresh=true" : "";
    const data = await api("GET", `/api/industries/${encodeURIComponent(industry)}/daily${qs}`);
    _digestCache[industry] = { data, fetchedAt: Date.now() };
  } catch (e) {
    _digestCache[industry] = { error: e.message, fetchedAt: Date.now() };
  } finally {
    _digestLoading.delete(industry);
    if (state.activeIndustry === industry && state.activeTab === "all") {
      const panel = document.getElementById("industry-panel");
      if (panel) _doRenderIndustryPanel(panel, industry);
    }
  }
}

function _digestLoadingHtml(industry) {
  return `<div class="daily-panel">
    <div class="daily-header">
      <span class="daily-header-icon">📰</span>
      <span class="daily-header-industry">${escHtml(industry)}</span>
      ${_viewTabsHtml(industry, "digest")}
      <span class="daily-header-date">載入中…</span>
    </div>
    <div class="daily-loading">
      <div class="ind-placeholder-line w80"></div>
      <div class="ind-placeholder-line w55"></div>
      <div class="ind-placeholder-line w70"></div>
      <div class="ind-placeholder-line w60"></div>
      <div class="ind-placeholder-line w75"></div>
    </div>
  </div>`;
}

function _trendsLoadingHtml(industry) {
  return `<div class="daily-panel">
    <div class="daily-header">
      <span class="daily-header-icon">📰</span>
      <span class="daily-header-industry">${escHtml(industry)}</span>
      ${_viewTabsHtml(industry, "trends")}
      <span class="daily-header-date">AI 分析中…</span>
      <button class="daily-refresh-btn trends-reanalyze-btn" disabled>↻ 分析中…</button>
    </div>
    <div class="daily-loading">
      <div class="ind-placeholder-line w80"></div>
      <div class="ind-placeholder-line w55"></div>
      <div class="ind-placeholder-line w70"></div>
    </div>
  </div>`;
}

function _renderDigestContent(panel, data, industry) {
  const displayDate = (data.date || "").replace(/-/g, "/");
  const topics = data.topics || [];
  const activeTopic = _digestTopic[industry] ?? null;

  // Collect articles for the active topic view
  let articles = [];
  if (activeTopic === null) {
    const seen = new Set();
    articles = topics.flatMap(t => t.articles).filter(a => {
      if (seen.has(a.url)) return false;
      seen.add(a.url);
      return true;
    });
  } else {
    articles = (topics.find(t => t.name === activeTopic)?.articles) || [];
  }

  const totalCount = data.article_count || 0;

  // Topic pills (watchlist topic gets a star icon + accent style)
  // Note: onclick uses single-quoted attribute so JSON.stringify's double-quoted
  // strings can be embedded safely without HTML attribute conflict.
  const pillsHtml = topics.length > 0 ? `
    <div class="daily-topics-bar">
      <button class="daily-topic-pill${activeTopic === null ? " active" : ""}"
        onclick='selectDigestTopic(${JSON.stringify(industry)}, null)'>全部 ${totalCount}</button>
      ${topics.map(t => {
        const isWatchlist = t.name === "感興趣名單";
        const cls = "daily-topic-pill" +
          (activeTopic === t.name ? " active" : "") +
          (isWatchlist ? " watchlist" : "");
        const label = isWatchlist ? `⭐ ${escHtml(t.name)}` : escHtml(t.name);
        return `<button class="${cls}"
          onclick='selectDigestTopic(${JSON.stringify(industry)}, ${JSON.stringify(t.name)})'>
          ${label} ${t.articles.length}
        </button>`;
      }).join("")}
    </div>` : "";

  // News list — show first 5 inline; collapse the rest into a <details> block
  const MAX_NEWS = 5;
  const renderItem = a => `
      <div class="daily-news-item">
        <span class="daily-news-bullet">•</span>
        <a class="daily-news-title" href="${escHtml(a.url)}" target="_blank" rel="noopener">${escHtml(a.title)}</a>
        <span class="daily-news-source">${escHtml(a.source)}</span>
        <a class="daily-news-ext" href="${escHtml(a.url)}" target="_blank" rel="noopener">↗</a>
        <button class="daily-news-dismiss" title="不想看這篇" onclick='dismissArticle(${JSON.stringify(a.url)},${JSON.stringify(a.title)},${JSON.stringify(a.source)},${JSON.stringify(a.source_url || "")},this)'>×</button>
      </div>`;
  // Filter out already-dismissed articles (in-session)
  articles = articles.filter(a => !_dismissedUrls.has(a.url));
  const visible = articles.slice(0, MAX_NEWS);
  const hidden = articles.slice(MAX_NEWS);
  let newsHtml;
  if (visible.length === 0) {
    newsHtml = `<div class="daily-empty">${totalCount === 0 ? "📭 今日尚無相關新聞" : "此分類暫無新聞"}</div>`;
  } else {
    newsHtml = visible.map(renderItem).join("");
    if (hidden.length > 0) {
      newsHtml += `<details class="daily-news-details">
        <summary class="daily-news-summary">
          <span class="when-closed">▼ 顯示其他 ${hidden.length} 則新聞</span>
          <span class="when-open">▲ 收起</span>
        </summary>
        ${hidden.map(renderItem).join("")}
      </details>`;
    }
  }

  panel.innerHTML = `<div class="daily-panel">
    <div class="daily-header">
      <span class="daily-header-icon">📰</span>
      <span class="daily-header-industry">${escHtml(industry)}</span>
      ${_viewTabsHtml(industry, "digest")}
      <span class="daily-header-date">${escHtml(displayDate)} · 更新 ${data.generated_at ? data.generated_at.slice(11, 16) : "--:--"}</span>
      <button class="daily-refresh-btn" onclick="refreshPanel()" title="重新整理">↻</button>
    </div>
    ${data.summary ? `
    <div class="daily-summary">
      <div class="daily-summary-bar"></div>
      <p class="daily-summary-text">${escHtml(data.summary)}</p>
    </div>` : ""}
    ${pillsHtml}
    <div class="daily-news-list">${newsHtml}</div>
  </div>`;
}

function selectDigestTopic(industry, topicName) {
  _digestTopic[industry] = topicName;
  const panel = document.getElementById("industry-panel");
  const cached = _digestCache[industry];
  if (panel && cached?.data) _renderDigestContent(panel, cached.data, industry);
}

async function _fetchTrends(industry, forceRefresh) {
  _trendsLoading.add(industry);
  try {
    const qs = forceRefresh ? "?refresh=true" : "";
    const data = await api("GET", `/api/industries/${encodeURIComponent(industry)}/trends${qs}`);
    _trendsCache[industry] = { data, fetchedAt: Date.now() };
  } catch (e) {
    _trendsCache[industry] = { error: e.message, fetchedAt: Date.now() };
  } finally {
    _trendsLoading.delete(industry);
    if (state.activeIndustry === industry && state.activeTab === "all" &&
        (_panelView[industry] ?? "digest") === "trends") {
      const panel = document.getElementById("industry-panel");
      if (panel) _doRenderTrendsPanel(panel, industry);
    }
  }
}

function _fmtGenAt(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const pad = v => String(v).padStart(2, "0");
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return t;
  return `${d.getFullYear()}/${pad(d.getMonth()+1)}/${pad(d.getDate())} ${t}`;
}

function _renderTrendsContent(panel, data, industry) {
  const from  = (data.date_range?.from || "").replace(/-/g, "/");
  const to    = (data.date_range?.to   || "").replace(/-/g, "/");
  const n     = data.days_analyzed || 0;
  const genAt = _fmtGenAt(data.generated_at);
  const trends = data.trends || [];
  const SIG = {
    rising:  { icon: "▲", cls: "rising"  },
    falling: { icon: "▼", cls: "falling" },
    stable:  { icon: "→", cls: "stable"  },
  };
  const trendsHtml = trends.length === 0
    ? `<div class="daily-empty">${escHtml(data.overview || "尚無足夠資料生成趨勢")}</div>`
    : trends.map(t => {
        const sig = SIG[t.signal] || SIG.stable;
        const tagsHtml = (t.representative_titles || [])
          .map(ti => {
            // ti can be a string (legacy cache) or {title, url, source} (new)
            if (typeof ti === "string") {
              return `<span class="trend-title-tag">${escHtml(ti)}</span>`;
            }
            const title = ti.title || "";
            const url = ti.url || "";
            const src = ti.source ? ` · ${ti.source}` : "";
            const tip = `${title}${src}`;
            if (url) {
              return `<a class="trend-title-tag trend-title-link" href="${escHtml(url)}" target="_blank" rel="noopener" title="${escHtml(tip)}">${escHtml(title)} ↗</a>`;
            }
            return `<span class="trend-title-tag" title="${escHtml(tip)}">${escHtml(title)}</span>`;
          }).join("");
        return `<div class="trend-card">
          <div class="trend-card-header">
            <span class="trend-signal ${sig.cls}">${sig.icon}</span>
            <span class="trend-name">${escHtml(t.name)}</span>
          </div>
          <p class="trend-insight">${escHtml(t.insight)}</p>
          ${tagsHtml ? `<div class="trend-titles">${tagsHtml}</div>` : ""}
        </div>`;
      }).join("");

  panel.innerHTML = `<div class="daily-panel">
    <div class="daily-header">
      <span class="daily-header-icon">📰</span>
      <span class="daily-header-industry">${escHtml(industry)}</span>
      ${_viewTabsHtml(industry, "trends")}
      <span class="daily-header-date">${from && to ? `${from} – ${to} · 近${n}天` : `近${n}天`}${genAt ? ` · 分析於 ${genAt}` : ""}</span>
      <button class="daily-refresh-btn trends-reanalyze-btn" onclick="refreshPanel()" title="重新用 AI 分析最新新聞（約需 30 秒）">↻ 重新分析</button>
    </div>
    ${data.overview && trends.length > 0 ? `
    <div class="daily-summary">
      <div class="daily-summary-bar" style="background:#7c3aed"></div>
      <p class="daily-summary-text">${escHtml(data.overview)}</p>
    </div>` : ""}
    <div class="trends-list">${trendsHtml}</div>
  </div>`;
}

function switchPanelView(industry, view) {
  _panelView[industry] = view;
  const panel = document.getElementById("industry-panel");
  if (panel) _doRenderIndustryPanel(panel, industry);
}

async function refreshPanel() {
  const industry = state.activeIndustry;
  if (!industry) return;
  const view = _panelView[industry] ?? "digest";
  const panel = document.getElementById("industry-panel");
  if (view === "trends") {
    if (_trendsLoading.has(industry)) return;
    delete _trendsCache[industry];
    if (panel) panel.innerHTML = _trendsLoadingHtml(industry);
    await _fetchTrends(industry, true);
  } else {
    if (_digestLoading.has(industry)) return;
    delete _digestCache[industry];
    if (panel) panel.innerHTML = _digestLoadingHtml(industry);
    await _fetchDigest(industry, true);
  }
}

/* ── Grid ── */
function renderGrid() {
  renderIndustryPanel();
  updateBulkEnrichButtonVisibility();
  _updateIndustryMapToolbarBtn();
  updateWatchCount();
  const grid = document.getElementById("company-grid");
  const title = document.getElementById("toolbar-title");

  // 1. 先套用 sidebar scope 過濾（產業 / 標籤 / 標籤群組）
  let companies = [...state.companies];
  let scopeTitle = "";

  if (state.activeLabelGroup) {
    const gLabels = (state.labelGroups[state.activeLabelGroup] || []).filter(l => state.pinnedItems.has(l));
    companies = companies.filter(c => (c.labels || []).some(l => gLabels.includes(l)));
    scopeTitle = `標籤群組：${state.activeLabelGroup}`;
  } else if (state.activeLabel) {
    companies = companies.filter(c => (c.labels || []).includes(state.activeLabel));
    if (state.activeLabelIndustry === "__none__") {
      companies = companies.filter(c => !(c.industries || []).length);
      scopeTitle = `${state.activeLabel} — 未分類`;
    } else if (state.activeLabelIndustry) {
      companies = companies.filter(c => (c.industries || []).includes(state.activeLabelIndustry));
      scopeTitle = `${state.activeLabel} — ${state.activeLabelIndustry}`;
    } else {
      scopeTitle = `標籤：${state.activeLabel}`;
    }
  } else if (state.activeIndustry) {
    const _fset = _industryFilterSet(state.activeIndustry);
    companies = companies.filter(c => (c.industries || []).some(i => _fset.has(i)));
    if (state.activeGroup === "__ungrouped__") {
      companies = companies.filter(c => !c.labels || c.labels.length === 0);
      scopeTitle = `${state.activeIndustry} — 未分組`;
    } else if (state.activeGroup) {
      companies = companies.filter(c => (c.labels || []).includes(state.activeGroup));
      scopeTitle = `${state.activeIndustry} — ${state.activeGroup}`;
    } else {
      scopeTitle = state.activeIndustry;
    }
  }

  // 2. 再套用「追蹤」tab 子集過濾
  if (state.activeTab === "watched") {
    companies = companies.filter(c => c.watched === true);
    title.textContent = scopeTitle ? `${scopeTitle} — ⭐ 追蹤` : "";
  } else {
    title.textContent = scopeTitle;
  }

  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    companies = companies.filter(c =>
      c.name.toLowerCase().includes(q) ||
      (c.representative || "").toLowerCase().includes(q)
    );
  }

  if (state.sortBy === "name") {
    companies.sort((a, b) => a.name.localeCompare(b.name, "zh-TW"));
  } else {
    const dir = state.sortDir === "asc" ? 1 : -1;
    companies.sort((a, b) => dir * ((a.capital || 0) - (b.capital || 0)));
  }

  if (companies.length === 0) {
    let emptyMsg;
    if (state.activeTab === "watched") {
      const scopeHint = scopeTitle ? `「${escHtml(scopeTitle)}」範圍內` : "";
      emptyMsg = `<div class="empty-icon">⭐</div><div>${scopeHint}尚無追蹤公司<br><small>將滑鼠移至公司卡片，點擊「+ 追蹤」即可收藏</small></div>`;
    } else {
      emptyMsg = `<div class="empty-icon">🏢</div><div>尚無公司資料<br><small>請上傳檔案以開始辨識</small></div>`;
    }
    grid.innerHTML = `<div id="empty-state">${emptyMsg}</div>`;
    return;
  }

  grid.innerHTML = companies.map(c => companyCardHtml(c)).join("");
  _wireGridDelegation(grid);
}

// 卡片點擊改事件委派：renderGrid 不再每次對幾百張卡逐一 addEventListener。
// 卡內按鈕/下拉的 inline onclick 都有 event.stopPropagation()，不會冒泡到這裡；
// closest 的 button/select/input 檢查是額外保險（例如簡介編輯的 textarea）。
function _wireGridDelegation(grid) {
  if (grid._delegated) return;
  grid._delegated = true;
  grid.addEventListener("click", ev => {
    const card = ev.target.closest(".company-card");
    if (!card) return;
    if (ev.target.closest("button, select, input, textarea")) return;
    openModal(card.dataset.id);
  });
}

// 單卡就地重繪：單筆操作（追蹤/標籤/簡介）不再全格重建幾百張卡。
// 操作可能讓卡片離開目前篩選範圍時（mustRefilter），退回完整 renderGrid。
function patchCard(id, mustRefilter = false) {
  if (mustRefilter) { renderGrid(); return; }
  const c = state.companies.find(x => x.id === id);
  const card = document.querySelector(`#company-grid .company-card[data-id="${id}"]`);
  if (!c || !card) { renderGrid(); return; }
  card.outerHTML = companyCardHtml(c);
}

// 目前的篩選是否吃「標籤」——是的話改標籤就可能讓卡片進出範圍，需要整格重繪
function _labelFilterActive() {
  return !!(state.activeLabelGroup || state.activeLabel || state.activeGroup);
}

function companyCardHtml(c) {
  const isEnriching = state.enrichingIds.has(c.id);
  const isDone = state.doneIds.has(c.id);
  const isFailed = !isEnriching && c.enrich_status === "failed";

  const isWatched = c.watched === true;
  const cardClass = (isEnriching ? " enriching" : isDone ? " enriching-done" : isFailed ? " enrich-failed" : "") + (!(c.industries || []).length ? " no-industry" : "") + (isWatched ? " watched" : "");
  const statusBadge = isEnriching
    ? '<span class="enriching-badge">● 生成中</span>'
    : isFailed
    ? `<button class="retry-badge" onclick="event.stopPropagation();retryEnrich('${c.id}')" title="重新生成公司簡介">⚠ 生成失敗 · 重試</button>`
    : isDone
    ? '<span class="done-badge">✓ 已完成</span>'
    : "";

  const groupBadge = c.group ? `<span class="group-badge">${escHtml(c.group)}</span>` : "";
  const labelChips = (c.labels || []).map(l =>
    `<span class="label-chip" title="${escHtml(l)}">${escHtml(truncLabel(l))}<button class="label-remove-btn" onclick="event.stopPropagation();removeLabel('${c.id}','${escAttr(l)}')" title="移除標籤">×</button></span>`
  ).join("");
  const addLabelBtn = `<button class="label-add-btn" onclick="event.stopPropagation();startAddLabel('${c.id}')" title="新增標籤">+</button>`;
  const badge = listingBadge(c.listing_status);
  const capital = c.capital ? `NT$${(c.capital / 1e6).toFixed(1)}M` : "—";
  const blurbText = cardBlurb(c);
  const summaryHtml = isEnriching
    ? `<div class="enriching-summary">正在為您生成公司簡介</div>`
    : isFailed
    ? `<div class="enrich-failed-summary">公司簡介生成失敗，點右上角「重試」重新生成</div>`
    : `<div class="card-summary-wrap">
        <span class="card-summary-text" id="blurb-text-${c.id}">${escHtml(blurbText)}</span>
        <button class="blurb-edit-btn" onclick="event.stopPropagation();startEditBlurb('${c.id}')" title="編輯簡介">✎</button>
      </div>`;

  const watchPillBtn = `<button class="watch-pill-btn${isWatched ? " is-watched" : ""}" onclick="event.stopPropagation();toggleWatch('${c.id}')">${isWatched ? "✓ 追蹤中" : "+ 追蹤"}</button>`;
  const nameRowPill = "";
  const labelRowPill = `<span class="watch-pill in-labels">${watchPillBtn}</span>`;
  const industryTag = (c.industries || []).length
    ? (c.industries || []).map(ind => {
        let parentHint = "";
        for (const [par, kids] of Object.entries(state.industryTree)) {
          if ((kids || []).includes(ind)) { parentHint = `<span class="card-ind-parent">${escHtml(par)} ›</span>`; break; }
        }
        return `<span class="card-industry-tag">${parentHint}${escHtml(ind)}</span>`;
      }).join("")
    : `<span class="card-industry-tag no-ind">未分類</span>`;

  return `
    <div class="company-card${cardClass}" data-id="${c.id}">
      <button class="card-delete-btn" onclick="event.stopPropagation();deleteCompany('${c.id}')" title="刪除">✕</button>
      <div class="card-name">
        <span class="card-name-text">${escHtml(shortName(c.name))}</span>
        ${badge}
        ${industryTag}
        ${nameRowPill}
        ${statusBadge}
      </div>
      <div class="card-labels" id="card-labels-${c.id}">${groupBadge}${labelChips}${addLabelBtn}${labelRowPill}</div>
      <div class="card-meta">
        <div><span>代表人：</span><strong>${escHtml(c.representative || "—")}</strong></div>
        <div><span>資本額：</span><strong>${capital}</strong></div>
      </div>
      ${summaryHtml}
    </div>`;
}

async function deleteCompany(id) {
  if (!confirm("確定要刪除這間公司嗎？")) return;
  try {
    await api("DELETE", `/api/companies/${id}`);
    state.companies = state.companies.filter(c => c.id !== id);
    computeGroups();
    renderSidebar();
    renderGrid();
  } catch (err) {
    toast(`刪除失敗：${err.message}`, true);
  }
}

/* ── Blurb inline edit ── */
function startEditBlurb(id) {
  const wrapEl = document.querySelector(`[data-id="${id}"] .card-summary-wrap`);
  if (!wrapEl) return;
  const c = state.companies.find(x => x.id === id);
  const current = c ? (c.blurb || "") : "";
  wrapEl.innerHTML = `
    <textarea class="blurb-edit-input" id="blurb-input-${id}" rows="1">${escHtml(current)}</textarea>
    <div class="blurb-edit-actions">
      <button class="blurb-save-btn" onclick="event.stopPropagation();saveBlurb('${id}')">✔</button>
      <button class="blurb-cancel-btn" onclick="event.stopPropagation();renderGrid()">✖</button>
    </div>`;
  const inp = document.getElementById(`blurb-input-${id}`);
  if (inp) { inp.focus(); inp.select(); }
}

async function saveBlurb(id) {
  const inp = document.getElementById(`blurb-input-${id}`);
  if (!inp) return;
  const blurb = inp.value.trim();
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { blurb });
    const idx = state.companies.findIndex(c => c.id === id);
    if (idx !== -1) state.companies[idx] = updated;
    patchCard(id);   // 簡介不影響篩選，單卡重繪即可
    toast("簡介已更新");
  } catch (err) {
    toast(`更新失敗：${err.message}`, true);
  }
}

/* ── Label inline edit on card ── */
async function removeLabel(id, label) {
  const c = state.companies.find(x => x.id === id);
  if (!c) return;
  const newLabels = (c.labels || []).filter(l => l !== label);
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { labels: newLabels });
    const idx = state.companies.findIndex(x => x.id === id);
    if (idx !== -1) state.companies[idx] = updated;
    patchCard(id, _labelFilterActive());   // 標籤篩選中改標籤可能讓卡片離開範圍
  } catch (err) {
    toast(`移除標籤失敗：${err.message}`, true);
  }
}

function startAddLabel(id) {
  const labelsEl = document.getElementById(`card-labels-${id}`);
  if (!labelsEl) return;
  const addBtn = labelsEl.querySelector(".label-add-btn");
  if (addBtn) addBtn.style.display = "none";

  const c = state.companies.find(x => x.id === id);
  const currentLabels = new Set(c?.labels || []);
  const allLabels = (state.labels || [])
    .filter(l => !currentLabels.has(l))
    .sort((a, b) => a.localeCompare(b, "zh-TW"));

  const wrap = document.createElement("div");
  wrap.className = "label-add-wrap";
  wrap.onclick = e => e.stopPropagation();

  const inp = document.createElement("input");
  inp.className = "label-add-input";
  inp.placeholder = "新增標籤";
  inp.maxLength = 20;

  const dropdown = document.createElement("div");
  dropdown.className = "label-add-dropdown";
  document.body.appendChild(dropdown);

  function positionDropdown() {
    const rect = inp.getBoundingClientRect();
    dropdown.style.top = `${rect.bottom + 4}px`;
    dropdown.style.left = `${rect.left}px`;
    dropdown.style.minWidth = `${Math.max(rect.width, 140)}px`;
  }

  function removeDropdown() {
    dropdown.remove();
  }

  function renderDropdown(filter) {
    const matches = filter
      ? allLabels.filter(l => l.includes(filter))
      : allLabels;
    dropdown.innerHTML = "";
    matches.forEach(label => {
      const item = document.createElement("div");
      item.className = "label-add-dropdown-item";
      item.textContent = label;
      item.onmousedown = e => { e.preventDefault(); confirmAddLabel(id, label); };
      dropdown.appendChild(item);
    });
    dropdown.style.display = matches.length ? "block" : "none";
    positionDropdown();
  }

  inp.oninput = () => renderDropdown(inp.value.trim());
  inp.onfocus = () => renderDropdown(inp.value.trim());
  inp.onblur = () => { setTimeout(() => { removeDropdown(); }, 150); };
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.stopPropagation(); removeDropdown(); confirmAddLabel(id, inp.value); }
    if (e.key === "Escape") { e.stopPropagation(); removeDropdown(); renderGrid(); }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const first = dropdown.querySelector(".label-add-dropdown-item");
      if (first) first.focus();
    }
  };

  dropdown.onkeydown = e => {
    const items = [...dropdown.querySelectorAll(".label-add-dropdown-item")];
    const idx = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[idx + 1]?.focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); idx > 0 ? items[idx - 1].focus() : inp.focus(); }
    if (e.key === "Enter") { e.preventDefault(); if (idx >= 0) { removeDropdown(); confirmAddLabel(id, items[idx].textContent); } }
    if (e.key === "Escape") { e.stopPropagation(); removeDropdown(); renderGrid(); }
  };

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "label-add-confirm-btn";
  confirmBtn.textContent = "✔";
  confirmBtn.onmousedown = e => { e.preventDefault(); removeDropdown(); confirmAddLabel(id, inp.value); };

  wrap.appendChild(inp);
  wrap.appendChild(confirmBtn);
  labelsEl.appendChild(wrap);
  inp.focus();
  renderDropdown("");
}

async function confirmAddLabel(id, label) {
  label = label.trim();
  if (!label) { renderGrid(); return; }
  const c = state.companies.find(x => x.id === id);
  if (!c) return;
  const newLabels = [...(c.labels || [])];
  if (!newLabels.includes(label)) newLabels.push(label);
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { labels: newLabels });
    const idx = state.companies.findIndex(x => x.id === id);
    if (idx !== -1) state.companies[idx] = updated;
    patchCard(id, _labelFilterActive());   // 同 removeLabel
  } catch (err) {
    toast(`新增標籤失敗：${err.message}`, true);
  }
}

function listingBadge(status) {
  if (!status) return "";
  const cls = `badge-${status.replace(/\s/g, "")}`;
  return `<span class="listing-badge ${cls}">${escHtml(status)}</span>`;
}

// Fallback for old company records not yet re-enriched: look up rep entity in local DB.
function _dirRepListingBadge(repName, repTaxId) {
  if (!repName) return "";
  const norm = n => n.replace(/股份有限公司$|有限公司$/, "").trim();
  const found = state.companies.find(c =>
    (repTaxId && c.tax_id === repTaxId) || norm(c.name) === norm(repName)
  );
  return (found?.listing_status && found.listing_status !== "非公發")
    ? listingBadge(found.listing_status)
    : "";
}

/* ── Watch list ── */
async function toggleWatch(id) {
  const c = state.companies.find(x => x.id === id);
  if (!c) return;
  const newVal = !(c.watched === true);
  try {
    await api("PUT", `/api/companies/${id}`, { watched: newVal });
    c.watched = newVal;
    updateWatchCount();
    // 「追蹤」tab 下取消追蹤要把卡片移出清單 → 整格重繪；其他情境單卡即可
    patchCard(id, state.activeTab === "watched");
  } catch (err) {
    toast(`操作失敗：${err.message}`, true);
  }
}

function _updateModalWatchBtn(c) {
  const btn = document.getElementById("modal-watch-btn");
  if (!btn) return;
  btn.title = c.watched ? "取消追蹤" : "追蹤";
  if (c.watched) {
    btn.classList.add("is-watched");
  } else {
    btn.classList.remove("is-watched");
  }
}

async function toggleModalWatch() {
  const c = state.companies.find(x => x.id === _modalCompanyId);
  if (!c) return;
  const newVal = !(c.watched === true);
  try {
    await api("PUT", `/api/companies/${c.id}`, { watched: newVal });
    c.watched = newVal;
    _updateModalWatchBtn(c);
    updateWatchCount();
    renderGrid();
    // refresh memo button visibility
    const supBtnHtml = `<button id="materials-open-btn" onclick="openMaterialsPanel()">➕ 補充資料</button>`;
    document.getElementById("modal-name").innerHTML =
      escHtml(shortName(c.name)) + listingBadge(c.listing_status) + supBtnHtml;
    _updateModalWatchBtn(c);
  } catch (err) {
    toast(`操作失敗：${err.message}`, true);
  }
}

/* ── Call Memo ── */
const MEMO_FIELDS = [
  ["deal_source",        "案件來源",                    false],
  ["interviewees",       "受訪人",                      false],
  ["paid_in_capital",    "實收資本額",                   false],
  ["address",            "地址",                        false],
  ["founding_date",      "設立日期",                    false],
  ["underwriter",        "承銷商",                      false],
  ["auditor",            "會計師事務所",                 false],
  ["chairman",           "董事長",                      false],
  ["general_manager",    "總經理",                      false],
  ["headcount",          "員工人數",                    false],
  ["ipo_timeline",       "公開發行及上市櫃時程/募資規劃", true],
  ["investment_terms",   "增資計畫或投資條件",           true],
  ["business_revenue",   "主要業務、產品營收比重",        true],
  ["financials",         "財務狀況",                    true],
  ["management_team",    "經營團隊背景",                 true],
  ["board_shareholding", "董監或主要股東持股情形",        true],
  ["recent_development", "公司發展近況",                 true],
  ["major_customers",    "主要銷貨客戶",                 true],
  ["major_suppliers",    "主要進貨廠商",                 true],
  ["factory_capacity",   "廠房及產能使用情形",           true],
  ["competitors",        "國內外主要競爭對手",           true],
  ["industry_trends",    "產業發展趨勢",                 true],
  ["risk_tracking",      "風險評估及追蹤事項",           true],
  ["conclusion",         "評估結論與建議",               true],
];

function _renderMemoFields(memo) {
  const container = document.getElementById("memo-fields");
  const today = new Date().toLocaleDateString("zh-TW", { year: "numeric", month: "2-digit", day: "2-digit" }).replace(/\//g, "/");
  const dateVal = (memo && memo.interview_date) ? memo.interview_date : today;

  const dateField = `
    <div class="memo-fields-grid" style="margin-bottom:10px">
      <div class="memo-field">
        <label>訪談日期</label>
        <input id="memo-interview_date" type="text" value="${escAttr(dateVal)}" placeholder="例：2025/01/01" />
      </div>
    </div>`;

  const fields = MEMO_FIELDS.map(([key, label, isLong]) => {
    const val = (memo && memo[key]) ? memo[key] : "";
    const cls = `memo-field${isLong ? " full" : ""}`;
    const input = isLong
      ? `<textarea id="memo-${key}" rows="3">${escHtml(val)}</textarea>`
      : `<input id="memo-${key}" type="text" value="${escAttr(val)}" />`;
    return `<div class="${cls}"><label>${escHtml(label)}</label>${input}</div>`;
  }).join("");

  container.innerHTML = dateField + `<div class="memo-fields-grid">${fields}</div>`;
}

async function loadMemo(id) {
  try {
    const memo = await api("GET", `/api/companies/${id}/memo`);
    _renderMemoFields(memo);
  } catch {
    _renderMemoFields({});
  }
}

// Memo lives inside the unified 補充資料 panel now. Kept as an alias in case
// anything still calls it.
function openMemoPanel() { openMaterialsPanel(); }

// Render the 訪談備忘錄 fields into the unified panel (from cache or backend).
function _loadMemoSection(id) {
  document.getElementById("memo-extract-status").textContent = "";
  const c = state.companies.find(x => x.id === id);
  if (c && c.call_memo && Object.keys(c.call_memo).length > 0) {
    _renderMemoFields(c.call_memo);
  } else {
    _renderMemoFields({});
    loadMemo(id);
  }
}

function _collectMemoData() {
  const data = { interview_date: (document.getElementById("memo-interview_date")?.value || "").trim() };
  for (const [key] of MEMO_FIELDS) {
    const el = document.getElementById(`memo-${key}`);
    data[key] = el ? el.value.trim() : "";
  }
  return data;
}

async function saveMemo(silent = false) {
  const id = _modalCompanyId;
  if (!id) return;
  const data = _collectMemoData();
  try {
    await api("PUT", `/api/companies/${id}/memo`, data);
    const idx = state.companies.findIndex(c => c.id === id);
    if (idx !== -1) state.companies[idx].call_memo = data;
    if (!silent) toast("Call Memo 已儲存");
  } catch (err) {
    if (!silent) toast(`儲存失敗：${err.message}`, true);
  }
}

function downloadMemo() {
  const id = _modalCompanyId;
  if (!id) return;
  window.location.href = `/api/companies/${id}/memo/download`;
}

document.getElementById("memo-file-input").addEventListener("change", async function() {
  const file = this.files[0];
  if (!file) return;
  this.value = "";
  const id = _modalCompanyId;
  if (!id) return;

  const status = document.getElementById("memo-extract-status");
  status.textContent = "⏳ Claude 正在分析逐字稿，約需 30–60 秒…";

  const fd = new FormData();
  fd.append("file", file);
  try {
    const fields = await api("POST", `/api/companies/${id}/memo/extract`, fd);
    _renderMemoFields(fields);
    status.textContent = "✅ 自動填寫完成，請確認後儲存";
    alertDone("(!) 逐字稿分析完成", "✅ 訪談備忘錄欄位已自動填寫，請確認後儲存");
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
  }
});

/* ── Audio transcription ── */
document.getElementById("memo-audio-input").addEventListener("change", async function() {
  const file = this.files[0];
  if (!file) return;
  this.value = "";
  const id = _modalCompanyId;
  if (!id) return;

  const status = document.getElementById("memo-audio-status");
  const transcriptBox = document.getElementById("memo-transcript-box");
  transcriptBox.style.display = "none";

  status.textContent = "⏳ Whisper 語音辨識中，依錄音長度約需 1–3 分鐘…";
  status.className = "memo-status-info";

  const fd = new FormData();
  fd.append("file", file);
  try {
    const result = await api("POST", `/api/companies/${id}/memo/transcribe-audio`, fd);
    document.getElementById("memo-transcript-text").value = result.transcript;
    transcriptBox.style.display = "";
    document.getElementById("memo-transcript-toggle").textContent = "▲";
    document.getElementById("memo-transcript-text").style.display = "";
    _renderMemoFields(result.fields);
    status.textContent = "✅ 語音辨識完成，欄位已自動填寫，請確認後儲存";
    status.className = "memo-status-ok";
    alertDone("(!) 語音辨識完成", "✅ 語音辨識完成，訪談備忘錄已自動填寫，請確認後儲存");
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "memo-status-error";
  }
});

function toggleTranscript() {
  const text = document.getElementById("memo-transcript-text");
  const toggle = document.getElementById("memo-transcript-toggle");
  const collapsed = text.style.display === "none";
  text.style.display = collapsed ? "" : "none";
  toggle.textContent = collapsed ? "▲" : "▼";
}

/* ── 補充資料 panel ── */
function openMaterialsPanel() {
  const id = _modalCompanyId;
  if (!id) return;
  const panel = document.getElementById("materials-panel");
  document.getElementById("materials-upload-status").textContent = "";
  document.getElementById("materials-gen-status").textContent = "";
  _setMatRegenStale(false);
  switchMatTab("files");
  panel.classList.add("open");
  loadMaterials(id);
  _loadMemoSection(id);
}

function switchMatTab(tab) {
  document.querySelectorAll("#materials-panel .mtab").forEach(t =>
    t.classList.toggle("on", t.dataset.tab === tab));
  document.getElementById("mtab-files").style.display = tab === "files" ? "" : "none";
  document.getElementById("mtab-memo").style.display = tab === "memo" ? "" : "none";
}

// Mark the generate button as「需要重新生成」(stale) after files change while a
// summary already exists — the generated summary no longer reflects all files.
function _setMatRegenStale(stale) {
  const btn = document.getElementById("materials-gen-btn");
  if (!btn || btn.disabled) return;  // don't fight the running state
  btn.classList.toggle("is-stale", stale);
  btn.textContent = stale
    ? "✦ 用 AI 重新生成（納入新資料）"
    : "✦ 用 AI 更新公司簡介";
}

async function closeMaterialsPanel() {
  const panel = document.getElementById("materials-panel");
  if (!panel.classList.contains("open")) return;
  await saveMemo(true);   // auto-save the 訪談備忘錄 fields
  panel.classList.remove("open");
}

async function loadMaterials(id) {
  try {
    const data = await api("GET", `/api/companies/${id}/materials`);
    _renderMaterialsList(data.materials || []);
    _renderMaterialsResult(data.materials_summary || "", data.materials_generated_at || "");
  } catch (err) {
    document.getElementById("materials-file-list").innerHTML =
      `<div class="materials-empty">載入失敗：${escHtml(err.message)}</div>`;
  }
}

const _MATERIALS_ICONS = { pdf: "📕", pptx: "📊", docx: "📘", xlsx: "📗", xls: "📗", txt: "📄" };
function _materialsIcon(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (["jpg","jpeg","png","gif","webp","tiff","tif","bmp"].includes(ext)) return "image";
  return _MATERIALS_ICONS[ext] || "📎";
}

function _renderMaterialsList(materials) {
  const wrap = document.getElementById("materials-file-list");
  const badge = document.getElementById("mtab-file-count");
  if (badge) {
    badge.textContent = materials.length || "";
    badge.style.display = materials.length ? "" : "none";
  }
  if (!materials.length) {
    wrap.innerHTML = `<div class="materials-empty">尚未上傳任何檔案</div>`;
    return;
  }
  wrap.innerHTML = materials.map(m => {
    const icon = _materialsIcon(m.filename);
    const thumb = icon === "image"
      ? `<img class="materials-thumb" src="${m.url}" alt="${escHtml(m.filename)}" />`
      : `<span class="materials-thumb materials-thumb-icon">${icon}</span>`;
    const kb = m.size ? `${(m.size / 1024).toFixed(0)} KB` : "";
    return `<div class="materials-file-item">
      <a class="materials-file-link" href="${m.url}" target="_blank" rel="noopener" title="點擊開啟">
        ${thumb}
        <span class="materials-file-name">${escHtml(m.filename)}</span>
        <span class="materials-file-size">${kb}</span>
      </a>
      <button class="materials-del-btn" onclick="deleteMaterial('${encodeURIComponent(m.stored_name)}')" title="刪除">✕</button>
    </div>`;
  }).join("");
}

// Store the last-generated materials summary so it can be re-reviewed
let _matLastSummary = "";

function _renderMaterialsResult(summary, generatedAt) {
  const wrap = document.getElementById("materials-result-wrap");
  const el = document.getElementById("materials-result");
  _matLastSummary = summary || "";
  if (!summary) {
    wrap.style.display = "none";
    el.innerHTML = "";
    return;
  }
  const when = formatTimestamp(generatedAt);
  el.innerHTML =
    `<div class="materials-redo-note">已生成更新版簡介${when ? `（${escHtml(when)}）` : ""}，` +
    `尚未套用到公司簡介的段落可重新審核。</div>` +
    `<button class="materials-redo-btn" onclick="openMatReview(_matLastSummary)">📝 重新審核並套用</button>`;
  wrap.style.display = "";
}

async function deleteMaterial(storedNameEnc) {
  const id = _modalCompanyId;
  if (!id) return;
  if (!confirm("確定刪除這個檔案？")) return;
  const hadSummary = !!_matLastSummary;
  try {
    const data = await api("DELETE", `/api/companies/${id}/materials/${storedNameEnc}`);
    _renderMaterialsList(data.materials || []);
    if (hadSummary && (data.materials || []).length) {
      _setMatRegenStale(true);
      const us = document.getElementById("materials-upload-status");
      us.textContent = "✅ 已移除檔案。內容已變動，建議按「✦ 用 AI 重新生成」更新簡介。";
      us.className = "memo-status-ok";
    }
  } catch (err) {
    toast(`刪除失敗：${err.message}`, true);
  }
}

let _matGenTimer = null;

async function generateFromMaterials() {
  const id = _modalCompanyId;
  if (!id) return;
  // Persist the 訪談備忘錄 fields first so the backend reads the latest content.
  await saveMemo(true);

  const status = document.getElementById("materials-gen-status");
  const btn = document.getElementById("materials-gen-btn");

  // Animated, reassuring "still working" indicator with an elapsed-time counter
  let elapsed = 0;
  const renderProgress = () => {
    status.innerHTML =
      `<span class="mat-spinner"></span>` +
      `<span>AI 正在閱讀所有補充資料並更新簡介` +
      `<span class="mat-dots"><i>.</i><i>.</i><i>.</i></span></span>` +
      `<span class="mat-elapsed">已 ${elapsed} 秒（約需 1–4 分鐘）</span>`;
  };
  status.className = "memo-status-info mat-gen-running";
  renderProgress();
  clearInterval(_matGenTimer);
  _matGenTimer = setInterval(() => { elapsed += 1; renderProgress(); }, 1000);
  btn.classList.remove("is-stale");  // acting on the stale state now
  btn.disabled = true;
  btn.classList.add("is-running");

  try {
    await api("POST", `/api/companies/${id}/materials/generate`);
    // 背景生成：輪詢 GET /materials 直到完成。關窗/切走也會在後端跑完並落地，
    // 下次開面板即見結果，不再因關窗而白跑一趟 Opus。
    let data = null;
    for (let i = 0; i < 200; i++) {          // 每 3 秒一次，最長約 10 分鐘
      await new Promise(r => setTimeout(r, 3000));
      if (_modalCompanyId !== id) return;    // 使用者切走：停止輪詢（後端仍會跑完落地）
      data = await api("GET", `/api/companies/${id}/materials`);
      if (!data.materials_generating) break;
    }
    if (data && data.materials_error) {
      status.textContent = `❌ ${data.materials_error}`;
      status.className = "memo-status-error";
      return;
    }
    const summary = (data && data.materials_summary) || "";
    const c = state.companies.find(x => x.id === id);
    if (c) Object.assign(c, {
      materials_summary: summary,
      materials_blurb: (data && data.materials_blurb) || "",
      materials_generated_at: (data && data.materials_generated_at) || "",
    });
    _renderMaterialsResult(summary, (data && data.materials_generated_at) || "");
    status.textContent = "✅ 已生成，請於審核框選取要套用的段落";
    status.className = "memo-status-ok";
    openMatReview(summary);
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "memo-status-error";
  } finally {
    clearInterval(_matGenTimer);
    btn.disabled = false;
    btn.classList.remove("is-running");
    _setMatRegenStale(false);  // reset label back to「生成公司簡介」
  }
}

/* ── 逐段審核：把簡報生成的段落套用進公司簡介 ── */
function _parseMdSections(md) {
  const sections = [];
  let cur = null;
  (md || "").split("\n").forEach(line => {
    const m = line.trim().match(/^##\s+(.+?)\s*$/);
    if (m) { cur = { heading: m[1].trim(), bodyLines: [] }; sections.push(cur); }
    else if (cur) cur.bodyLines.push(line);
  });
  return sections.map(s => ({ heading: s.heading, body: s.bodyLines.join("\n").trim() }));
}

function openMatReview(materialsSummary) {
  if (!materialsSummary || !materialsSummary.trim()) {
    toast("尚未生成簡報簡介", true);
    return;
  }
  const sections = _parseMdSections(materialsSummary);

  const PUBLIC_SECTIONS = new Set(["業務概況", "競業分析", "主要風險"]);
  const body = document.getElementById("mat-review-body");
  body.innerHTML = sections.map((s, i) => {
    // Public DD section → replaces it in place (修改); otherwise grouped under 營運綜覽
    const tag = PUBLIC_SECTIONS.has(s.heading)
      ? `<span class="mat-tag mat-tag-mod">修改</span>`
      : `<span class="mat-tag mat-tag-new">歸入營運綜覽</span>`;
    return `<label class="mat-review-row">
      <input type="checkbox" class="mat-sec-cb" data-heading="${escHtml(s.heading)}" checked />
      <div class="mat-review-sec">
        <div class="mat-review-sec-head">${tag}<strong>${escHtml(s.heading)}</strong></div>
        <div class="mat-review-sec-body">${renderSummary("## " + s.heading + "\n" + s.body).replace(/^<h3>[\s\S]*?<\/h3>/, "")}</div>
      </div>
    </label>`;
  }).join("");

  document.getElementById("mat-review-all").checked = true;
  openOverlay("mat-review-overlay");
}

function closeMatReview() {
  closeOverlay("mat-review-overlay");
}

function toggleMatReviewAll(cb) {
  document.querySelectorAll("#mat-review-body .mat-sec-cb").forEach(x => { x.checked = cb.checked; });
}

async function applyMatReview() {
  const id = _modalCompanyId;
  if (!id) return;
  const headings = Array.from(document.querySelectorAll("#mat-review-body .mat-sec-cb"))
    .filter(cb => cb.checked)
    .map(cb => cb.dataset.heading);
  if (!headings.length) { toast("請至少選取一個段落", true); return; }

  const applyBtn = document.getElementById("mat-review-apply");
  applyBtn.disabled = true;
  try {
    const fields = await api("POST", `/api/companies/${id}/materials/apply`, { headings });
    const c = state.companies.find(x => x.id === id);
    if (c) Object.assign(c, fields);
    _updateSummaryInModal(c);
    _expandSummarySection();
    closeMatReview();
    closeMaterialsPanel();
    toast(`已套用 ${headings.length} 個段落到公司簡介`);
  } catch (err) {
    toast(`套用失敗：${err.message}`, true);
  } finally {
    applyBtn.disabled = false;
  }
}

document.getElementById("materials-file-input").addEventListener("change", async function() {
  const files = Array.from(this.files || []);
  this.value = "";
  if (!files.length) return;
  const id = _modalCompanyId;
  if (!id) return;

  const status = document.getElementById("materials-upload-status");
  status.textContent = `⏳ 上傳中（${files.length} 個檔案）…`;
  status.className = "memo-status-info";

  const fd = new FormData();
  files.forEach(f => fd.append("files", f));
  try {
    const data = await api("POST", `/api/companies/${id}/materials`, fd);
    const hadSummary = !!_matLastSummary;
    _renderMaterialsList(data.materials || []);
    if (hadSummary) {
      // A summary already exists → it doesn't cover the just-added file(s) yet.
      status.textContent = `✅ 已新增 ${data.saved.length} 個檔案。內容已變動，請按下方「✦ 用 AI 重新生成」以納入新檔案（只按「重新審核」不會讀到新檔）。`;
      _setMatRegenStale(true);
    } else {
      status.textContent = `✅ 已上傳 ${data.saved.length} 個檔案，可點下方按鈕生成簡介`;
    }
    status.className = "memo-status-ok";
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "memo-status-error";
  }
});

