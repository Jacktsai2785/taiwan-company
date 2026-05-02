/* ── State ── */
const state = {
  companies: [],
  industries: [],
  groups: {},                    // {industry: [group, ...]}
  expandedIndustries: new Set(),
  activeIndustry: null,          // null = all
  activeGroup: null,             // null = all groups, "__ungrouped__" = no group
  activeTab: "all",              // "all" | "watched"
  sortBy: "capital",
  searchQuery: "",
  pendingCandidates: [],
  pendingUncertain: [],
  pendingLabel: "",
  enrichingIds: new Set(),       // currently enriching company ids
  doneIds: new Set(),            // briefly green after enrichment completes
};

let _modalCompanyId = null;

/* ── AI settings (localStorage) ── */
function getAiKey()      { return localStorage.getItem("ai_api_key") || ""; }
function getAiProvider() { return localStorage.getItem("ai_provider") || "local"; }
function isLocalMode()   { return getAiProvider() === "local"; }

function _updateAiModeLabel() {
  const el = document.getElementById("ai-mode-label");
  if (!el) return;
  const prov = getAiProvider();
  const labels = { local: "本機 Claude", anthropic: "Claude API", openai: "OpenAI", gemini: "Gemini" };
  el.textContent = labels[prov] || prov;
}

function openSettings() {
  const prov = getAiProvider();
  const key  = getAiKey();
  const radio = document.querySelector(`input[name="ai-provider"][value="${prov}"]`);
  if (radio) radio.checked = true;
  const keyInp = document.getElementById("settings-api-key");
  if (keyInp) keyInp.value = key;
  document.getElementById("settings-key-section").style.display = prov === "local" ? "none" : "";
  document.getElementById("settings-error").textContent = "";
  document.getElementById("settings-overlay").classList.add("open");
}

function toggleSettingsKeyVisibility() {
  const inp = document.getElementById("settings-api-key");
  inp.type = inp.type === "password" ? "text" : "password";
}

// Show / hide API key input when provider changes
document.querySelectorAll('input[name="ai-provider"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const needsKey = radio.value !== "local";
    document.getElementById("settings-key-section").style.display = needsKey ? "" : "none";
    document.getElementById("settings-error").textContent = "";
  });
});

document.getElementById("settings-save").addEventListener("click", () => {
  const prov = document.querySelector('input[name="ai-provider"]:checked')?.value || "local";
  if (prov !== "local") {
    const key = document.getElementById("settings-api-key").value.trim();
    if (!key) {
      document.getElementById("settings-error").textContent = "請輸入 API Key";
      return;
    }
    localStorage.setItem("ai_api_key", key);
  } else {
    localStorage.removeItem("ai_api_key");
  }
  localStorage.setItem("ai_provider", prov);
  document.getElementById("settings-overlay").classList.remove("open");
  _updateAiModeLabel();
  toast(prov === "local" ? "已切換為本機 Claude 模式" : "API Key 已儲存");
});

document.getElementById("settings-skip").addEventListener("click", () => {
  document.getElementById("settings-overlay").classList.remove("open");
});

/* ── API helpers ── */
async function api(method, path, body) {
  const opts = { method, headers: {} };
  const key = getAiKey();
  if (key) {
    opts.headers["X-API-Key"]      = key;
    opts.headers["X-AI-Provider"]  = getAiProvider();
  }
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

/* ── Boot ── */
async function boot() {
  await Promise.all([loadIndustries(), loadCompanies()]);
  computeGroups();
  renderSidebar();
  renderGrid();
  _updateAiModeLabel();
  // Show settings only on very first visit (localStorage never set)
  if (localStorage.getItem("ai_provider") === null) openSettings();
}

async function loadIndustries() {
  state.industries = await api("GET", "/api/config/industries");
}

async function loadCompanies() {
  state.companies = await api("GET", "/api/companies");
  updateWatchCount();
}

function updateWatchCount() {
  const n = state.companies.filter(c => c.watched).length;
  const el = document.getElementById("watch-count");
  if (el) el.textContent = n > 0 ? ` (${n})` : "";
}

function computeGroups() {
  const g = {};
  for (const c of state.companies) {
    const ind = c.industry || "";
    if (!g[ind]) g[ind] = [];
    for (const label of (c.labels || [])) {
      if (!g[ind].includes(label)) g[ind].push(label);
    }
  }
  state.groups = g;
}

/* ── Sidebar ── */
function renderSidebar() {
  const list = document.getElementById("industry-list");
  list.innerHTML = "";

  // "All" entry
  const allDiv = document.createElement("div");
  allDiv.className = "industry-item" + (state.activeIndustry === null ? " active" : "");
  allDiv.innerHTML = `
    <span class="chevron">›</span>
    <span class="ind-label">全部公司</span>
    <span class="industry-badge">${state.companies.length}</span>
  `;
  allDiv.addEventListener("click", () => {
    state.activeIndustry = null;
    state.activeGroup = null;
    renderSidebar();
    renderGrid();
  });
  list.appendChild(allDiv);

  for (const ind of state.industries) {
    const indCount = state.companies.filter(c => c.industry === ind).length;
    const isExpanded = state.expandedIndustries.has(ind);
    const isActive = state.activeIndustry === ind && state.activeGroup === null;

    const div = document.createElement("div");
    div.className = "industry-item" + (isActive ? " active" : "") + (isExpanded ? " open" : "");
    div.innerHTML = `
      <span class="chevron">›</span>
      <span class="ind-label">${escHtml(ind)}</span>
      <span class="industry-badge">${indCount}</span>
      <span class="industry-actions">
        <button class="rename-ind" title="重新命名">✏️</button>
        <button class="delete-ind" title="刪除">🗑</button>
      </span>
    `;

    div.addEventListener("click", e => {
      if (e.target.closest(".industry-actions")) return;
      if (state.expandedIndustries.has(ind)) {
        state.expandedIndustries.delete(ind);
      } else {
        state.expandedIndustries.add(ind);
      }
      state.activeIndustry = ind;
      state.activeGroup = null;
      renderSidebar();
      renderGrid();
    });

    div.querySelector(".rename-ind").addEventListener("click", e => {
      e.stopPropagation();
      startRenameIndustry(div, ind);
    });
    div.querySelector(".delete-ind").addEventListener("click", async e => {
      e.stopPropagation();
      if (!confirm(`確定要刪除產業別「${ind}」嗎？`)) return;
      await api("DELETE", `/api/config/industries/${encodeURIComponent(ind)}`);
      if (state.activeIndustry === ind) { state.activeIndustry = null; state.activeGroup = null; }
      state.expandedIndustries.delete(ind);
      await loadIndustries();
      renderSidebar();
      renderGrid();
    });

    list.appendChild(div);

    // Group sub-items (only when expanded)
    if (isExpanded) {
      const companiesInInd = state.companies.filter(c => c.industry === ind);
      const groups = state.groups[ind] || [];

      for (const grp of groups) {
        const grpCount = companiesInInd.filter(c => (c.labels || []).includes(grp)).length;
        const isGrpActive = state.activeIndustry === ind && state.activeGroup === grp;
        const grpDiv = document.createElement("div");
        grpDiv.className = "group-item" + (isGrpActive ? " active" : "");
        grpDiv.innerHTML = `<span>${escHtml(grp)}</span><span class="industry-badge">${grpCount}</span>`;
        grpDiv.addEventListener("click", () => {
          state.activeIndustry = ind;
          state.activeGroup = grp;
          renderSidebar();
          renderGrid();
        });
        list.appendChild(grpDiv);
      }

      const ungroupedCount = companiesInInd.filter(c => !c.labels || c.labels.length === 0).length;
      if (ungroupedCount > 0) {
        const isUngroupedActive = state.activeIndustry === ind && state.activeGroup === "__ungrouped__";
        const ungroupedDiv = document.createElement("div");
        ungroupedDiv.className = "group-item" + (isUngroupedActive ? " active" : "");
        ungroupedDiv.innerHTML = `<span>未分組</span><span class="industry-badge">${ungroupedCount}</span>`;
        ungroupedDiv.addEventListener("click", () => {
          state.activeIndustry = ind;
          state.activeGroup = "__ungrouped__";
          renderSidebar();
          renderGrid();
        });
        list.appendChild(ungroupedDiv);
      }
    }
  }
}

function startRenameIndustry(div, oldName) {
  const labelSpan = div.querySelector(".ind-label");
  const input = document.createElement("input");
  input.className = "industry-edit-input";
  input.value = oldName;
  labelSpan.replaceWith(input);
  input.focus();
  input.select();

  const commit = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      await api("PUT", "/api/config/industries", { old_name: oldName, new_name: newName });
      if (state.activeIndustry === oldName) state.activeIndustry = newName;
      if (state.expandedIndustries.has(oldName)) {
        state.expandedIndustries.delete(oldName);
        state.expandedIndustries.add(newName);
      }
      await Promise.all([loadIndustries(), loadCompanies()]);
      computeGroups();
    }
    renderSidebar();
    renderGrid();
  };

  input.addEventListener("blur", commit);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = oldName; input.blur(); }
  });
}

document.getElementById("add-industry-btn").addEventListener("click", async () => {
  const name = prompt("請輸入新產業別名稱：");
  if (!name || !name.trim()) return;
  const indName = name.trim();

  // Show suggest dialog in loading state
  const overlay = document.getElementById("ind-suggest-overlay");
  document.getElementById("ind-suggest-title").textContent = `新增「${indName}」`;
  document.getElementById("ind-suggest-subtitle").textContent = "Claude 正在比對現有公司…";
  document.getElementById("ind-suggest-loading").style.display = "flex";
  document.getElementById("ind-suggest-rows").innerHTML = "";
  document.getElementById("ind-suggest-ok").disabled = true;
  overlay.classList.add("open");

  let matchedIds = [];
  try {
    const res = await api("POST", "/api/config/industries/suggest", { name: indName });
    matchedIds = res.matched_ids || [];
  } catch (e) {
    toast(`比對失敗：${e.message}，可手動勾選`, true);
  }

  // Render results
  document.getElementById("ind-suggest-loading").style.display = "none";
  document.getElementById("ind-suggest-title").textContent = `新增「${indName}」`;
  const matchSet = new Set(matchedIds);
  const rows = document.getElementById("ind-suggest-rows");

  if (state.companies.length === 0) {
    rows.innerHTML = `<p class="suggest-empty">目前沒有公司資料</p>`;
  } else {
    document.getElementById("ind-suggest-subtitle").textContent =
      matchedIds.length > 0
        ? `Claude 建議以下 ${matchedIds.length} 間公司歸入此產業別（可調整勾選）`
        : "Claude 未找到符合的公司，你可以手動勾選";
    rows.innerHTML = state.companies.map(c => `
      <label class="suggest-row${matchSet.has(c.id) ? " suggested" : ""}">
        <input type="checkbox" value="${c.id}" ${matchSet.has(c.id) ? "checked" : ""} />
        <span class="suggest-name">${escHtml(c.name.replace(/股份有限公司$/, ""))}</span>
        <span class="suggest-blurb">${escHtml(c.blurb || "—")}</span>
        ${c.industry ? `<span class="suggest-ind-badge">${escHtml(c.industry)}</span>` : ""}
      </label>`).join("");
  }
  document.getElementById("ind-suggest-ok").disabled = false;

  // Footer buttons
  document.getElementById("ind-suggest-cancel").onclick = () => overlay.classList.remove("open");
  document.getElementById("ind-suggest-ok").onclick = async () => {
    const checked = [...rows.querySelectorAll("input[type=checkbox]:checked")].map(el => el.value);
    overlay.classList.remove("open");

    await api("POST", "/api/config/industries", { name: indName });

    if (checked.length > 0) {
      await Promise.all(checked.map(id => api("PUT", `/api/companies/${id}`, { industry: indName })));
      checked.forEach(id => {
        const idx = state.companies.findIndex(c => c.id === id);
        if (idx !== -1) state.companies[idx].industry = indName;
      });
    }

    await loadIndustries();
    computeGroups();
    renderSidebar();
    renderGrid();
    toast(`產業別「${indName}」已新增${checked.length > 0 ? `，${checked.length} 間公司已歸入` : ""}`);
  };
});

/* ── Industry Panel ── */
function renderIndustryPanel() {
  const panel = document.getElementById("industry-panel");
  const show = state.activeTab === "all" && !!state.activeIndustry;
  panel.style.display = show ? "" : "none";
  if (!show) return;

  const today = new Date();
  const fmt = d => `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,"0")}/${String(d.getDate()).padStart(2,"0")}`;
  const quarterStart = new Date(today);
  quarterStart.setMonth(today.getMonth() - 3);
  document.getElementById("ind-daily-date").textContent = fmt(today);
  document.getElementById("ind-discuss-date").textContent =
    `${fmt(quarterStart)} – ${fmt(today)}`;
}

/* ── Grid ── */
function renderGrid() {
  renderIndustryPanel();
  const grid = document.getElementById("company-grid");
  const title = document.getElementById("toolbar-title");

  let companies = [...state.companies];

  if (state.activeTab === "watched") {
    companies = companies.filter(c => c.watched === true);
    title.textContent = "";
  } else {
    if (state.activeIndustry) {
      companies = companies.filter(c => c.industry === state.activeIndustry);
      if (state.activeGroup) {
        if (state.activeGroup === "__ungrouped__") {
          companies = companies.filter(c => !c.labels || c.labels.length === 0);
          title.textContent = `${state.activeIndustry} — 未分組`;
        } else {
          companies = companies.filter(c => (c.labels || []).includes(state.activeGroup));
          title.textContent = `${state.activeIndustry} — ${state.activeGroup}`;
        }
      } else {
        title.textContent = state.activeIndustry;
      }
    } else {
      title.textContent = "";
    }
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
    companies.sort((a, b) => (b.capital || 0) - (a.capital || 0));
  }

  if (companies.length === 0) {
    const emptyMsg = state.activeTab === "watched"
      ? `<div class="empty-icon">⭐</div><div>尚無追蹤公司<br><small>將滑鼠移至公司卡片，點擊「+ 追蹤」即可收藏</small></div>`
      : `<div class="empty-icon">🏢</div><div>尚無公司資料<br><small>請上傳檔案以開始辨識</small></div>`;
    grid.innerHTML = `<div id="empty-state">${emptyMsg}</div>`;
    return;
  }

  grid.innerHTML = companies.map(c => companyCardHtml(c)).join("");
  grid.querySelectorAll(".company-card").forEach(card => {
    card.addEventListener("click", () => openModal(card.dataset.id));
  });
}

function companyCardHtml(c) {
  const isEnriching = state.enrichingIds.has(c.id);
  const isDone = state.doneIds.has(c.id);

  const isWatched = c.watched === true;
  const cardClass = (isEnriching ? " enriching" : isDone ? " enriching-done" : "") + (!c.industry ? " no-industry" : "") + (isWatched ? " watched" : "");
  const statusBadge = isEnriching
    ? '<span class="enriching-badge">● 生成中</span>'
    : isDone
    ? '<span class="done-badge">✓ 已完成</span>'
    : "";

  const groupBadge = c.group ? `<span class="group-badge">${escHtml(c.group)}</span>` : "";
  const labelChips = (c.labels || []).map(l =>
    `<span class="label-chip">${escHtml(l)}<button class="label-remove-btn" onclick="event.stopPropagation();removeLabel('${c.id}','${escAttr(l)}')" title="移除標籤">×</button></span>`
  ).join("");
  const addLabelBtn = `<button class="label-add-btn" onclick="event.stopPropagation();startAddLabel('${c.id}')" title="新增標籤">+</button>`;
  const badge = listingBadge(c.listing_status);
  const capital = c.capital ? `NT$${(c.capital / 1e6).toFixed(1)}M` : "—";
  const blurbText = cardBlurb(c);
  const summaryHtml = isEnriching
    ? `<div class="enriching-summary">正在為您生成公司簡介</div>`
    : `<div class="card-summary-wrap">
        <span class="card-summary-text" id="blurb-text-${c.id}">${escHtml(blurbText)}</span>
        <button class="blurb-edit-btn" onclick="event.stopPropagation();startEditBlurb('${c.id}')" title="編輯簡介">✎</button>
      </div>`;

  const watchPill = `<button class="watch-pill-btn${isWatched ? " is-watched" : ""}" onclick="event.stopPropagation();toggleWatch('${c.id}')">${isWatched ? "✓ 追蹤中" : "+ 追蹤"}</button>`;

  return `
    <div class="company-card${cardClass}" data-id="${c.id}">
      <button class="card-delete-btn" onclick="event.stopPropagation();deleteCompany('${c.id}')" title="刪除">✕</button>
      <div class="card-name">
        <span class="card-name-text">${escHtml(shortName(c.name))}</span>
        ${badge}
        <span class="watch-pill">${watchPill}</span>
        ${statusBadge}
      </div>
      <div class="card-labels" id="card-labels-${c.id}">${groupBadge}${labelChips}${addLabelBtn}</div>
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
    renderGrid();
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
    renderGrid();
  } catch (err) {
    toast(`移除標籤失敗：${err.message}`, true);
  }
}

function startAddLabel(id) {
  const labelsEl = document.getElementById(`card-labels-${id}`);
  if (!labelsEl) return;
  const addBtn = labelsEl.querySelector(".label-add-btn");
  if (addBtn) addBtn.style.display = "none";
  const inp = document.createElement("input");
  inp.className = "label-add-input";
  inp.placeholder = "新增標籤";
  inp.maxLength = 20;
  inp.onclick = e => e.stopPropagation();
  inp.onkeydown = e => {
    if (e.key === "Enter") { e.stopPropagation(); confirmAddLabel(id, inp.value); }
    if (e.key === "Escape") { e.stopPropagation(); renderGrid(); }
  };
  const confirmBtn = document.createElement("button");
  confirmBtn.className = "label-add-confirm-btn";
  confirmBtn.textContent = "✔";
  confirmBtn.onclick = e => { e.stopPropagation(); confirmAddLabel(id, inp.value); };
  labelsEl.appendChild(inp);
  labelsEl.appendChild(confirmBtn);
  inp.focus();
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
    renderGrid();
  } catch (err) {
    toast(`新增標籤失敗：${err.message}`, true);
  }
}

function listingBadge(status) {
  if (!status) return "";
  const cls = `badge-${status.replace(/\s/g, "")}`;
  return `<span class="listing-badge ${cls}">${escHtml(status)}</span>`;
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
    renderGrid();
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

function openMemoPanel() {
  const id = _modalCompanyId;
  if (!id) return;
  const panel = document.getElementById("memo-panel");
  document.getElementById("memo-extract-status").textContent = "";
  panel.classList.add("open");

  const c = state.companies.find(x => x.id === id);
  if (c && c.call_memo && Object.keys(c.call_memo).length > 0) {
    _renderMemoFields(c.call_memo);
  } else {
    _renderMemoFields({});
    loadMemo(id);
  }
}

async function closeMemoPanel() {
  const panel = document.getElementById("memo-panel");
  if (!panel.classList.contains("open")) return;
  await saveMemo(true);
  panel.classList.remove("open");
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
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
  }
});

document.getElementById("memo-drop-label").addEventListener("click", () => {
  document.getElementById("memo-file-input").click();
});

/* ── Tabs ── */
document.getElementById("tab-group").addEventListener("click", e => {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  state.activeTab = btn.dataset.tab;
  document.querySelectorAll(".tab-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === state.activeTab)
  );
  renderGrid();
});

/* ── Search ── */
document.getElementById("search-box").addEventListener("input", e => {
  state.searchQuery = e.target.value.trim();
  renderGrid();
});

/* ── Sort ── */
document.getElementById("sort-group").addEventListener("click", e => {
  const btn = e.target.closest(".sort-btn");
  if (!btn) return;
  state.sortBy = btn.dataset.sort;
  document.querySelectorAll(".sort-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  renderGrid();
});

/* ── Modal ── */
function openModal(id) {
  _modalCompanyId = id;
  const c = state.companies.find(x => x.id === id);
  if (!c) return;

  const memoBtnHtml = c.watched
    ? `<button id="memo-open-btn" onclick="openMemoPanel()">📋 訪談備忘錄</button>`
    : "";
  document.getElementById("modal-name").innerHTML =
    escHtml(shortName(c.name)) + listingBadge(c.listing_status) + memoBtnHtml;

  document.getElementById("modal-labels").innerHTML =
    (c.labels || []).map(l => `<span class="label-chip">${escHtml(l)}</span>`).join("") || "（無標籤）";

  const fmt = n => n ? `NT$ ${Number(n).toLocaleString()} 元` : "—";
  document.getElementById("modal-info").innerHTML = `
    <span class="info-label">統一編號</span><span class="info-value">${escHtml(c.tax_id || "—")}</span>
    <span class="info-label">公司代表人</span><span class="info-value">${escHtml(c.representative || "—")}</span>
    <span class="info-label">資本總額</span><span class="info-value">${fmt(c.authorized_capital)}</span>
    <span class="info-label">實收資本額</span><span class="info-value">${fmt(c.capital)}</span>
    <span class="info-label">每股金額</span><span class="info-value">${c.par_value ? `NT$ ${c.par_value} 元` : "—"}</span>
    <span class="info-label">股份總數</span><span class="info-value">${c.total_shares ? Number(c.total_shares).toLocaleString() + " 股" : "—"}</span>
    <span class="info-label">公司所在地</span><span class="info-value">${escHtml(c.address || "—")}</span>
    <span class="info-label">產業別</span>
    <span class="info-value modal-industry-wrap">
      <select id="modal-industry-select" onchange="saveModalIndustry()">
        <option value="">— 未指定 —</option>
        ${state.industries.map(ind => `<option value="${escHtml(ind)}"${ind === (c.industry || "") ? " selected" : ""}>${escHtml(ind)}</option>`).join("")}
      </select>
    </span>
  `;

  const directors = c.directors || [];
  const tbody = document.getElementById("modal-directors");
  if (directors.length) {
    // Deduplicate by representative_of: same 法人 counts once
    const seenEntity = new Set();
    let totalShares = 0, totalRatio = 0;
    const hasShares = directors.some(d => d.shares);
    const hasRatio = directors.some(d => d.ratio != null);
    for (const d of directors) {
      const entity = (d.representative_of || "").trim();
      const key = entity || `__individual__${d.name}`;
      if (!seenEntity.has(key)) {
        seenEntity.add(key);
        totalShares += d.shares || 0;
        totalRatio  += d.ratio != null ? d.ratio : 0;
      }
    }
    tbody.innerHTML = directors.map(d => `
      <tr>
        <td>${escHtml(d.title || "—")}</td>
        <td>${escHtml(d.name || "—")}</td>
        <td>${escHtml(d.representative_of || "—")}</td>
        <td>${d.shares ? Number(d.shares).toLocaleString() : "—"}</td>
        <td>${d.ratio != null ? (d.ratio * 100).toFixed(2) + "%" : "—"}</td>
      </tr>`).join("") + `
      <tr class="director-total-row">
        <td colspan="3">合計</td>
        <td>${hasShares ? Number(totalShares).toLocaleString() : "—"}</td>
        <td>${hasRatio ? (totalRatio * 100).toFixed(2) + "%" : "—"}</td>
      </tr>`;
    document.getElementById("modal-directors-section").style.display = "";
  } else {
    document.getElementById("modal-directors-section").style.display = "none";
  }

  const summaryEl = document.getElementById("modal-summary");
  summaryEl.innerHTML =
    c.summary ? renderSummary(c.summary) : "<p class=\"summary-placeholder\">（公司簡介資料補充中，請稍後重整）</p>";
  applyCollapsible(summaryEl);

  document.getElementById("modal-overlay").classList.add("open");
}


async function saveModalIndustry() {
  const id = _modalCompanyId;
  if (!id) return;
  const sel = document.getElementById("modal-industry-select");
  const industry = sel ? sel.value : "";
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { industry });
    const idx = state.companies.findIndex(c => c.id === id);
    if (idx !== -1) state.companies[idx] = updated;
    computeGroups();
    renderSidebar();
    renderGrid();
    toast("產業別已更新");
  } catch (err) {
    toast(`更新失敗：${err.message}`, true);
  }
}

function regenSummary() {
  const id = _modalCompanyId;
  if (!id) return;
  document.getElementById("modal-overlay").classList.remove("open");
  subscribeEnrichment(id);
}

document.getElementById("modal-close").addEventListener("click", () => {
  document.getElementById("modal-overlay").classList.remove("open");
  closeMemoPanel();
});
document.getElementById("modal-overlay").addEventListener("click", e => {
  if (e.target === document.getElementById("modal-overlay")) {
    document.getElementById("modal-overlay").classList.remove("open");
    closeMemoPanel();
  }
});

/* ── Upload ── */
const dropTarget = document.getElementById("drop-target");
const fileInput = document.getElementById("file-input");
const uploadProgress = document.getElementById("upload-progress");

dropTarget.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) handleUpload(fileInput.files[0]);
});
dropTarget.addEventListener("dragover", e => {
  e.preventDefault();
  dropTarget.classList.add("drag-over");
});
dropTarget.addEventListener("dragleave", () => dropTarget.classList.remove("drag-over"));
dropTarget.addEventListener("drop", e => {
  e.preventDefault();
  dropTarget.classList.remove("drag-over");
  if (e.dataTransfer.files[0]) handleUpload(e.dataTransfer.files[0]);
});

async function handleUpload(file) {
  uploadProgress.textContent = "⏳ 正在解析檔案…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const result = await api("POST", "/api/upload", fd);

    if (result.ocr_failed) {
      uploadProgress.textContent = `⚠️ 圖片辨識失敗，請手動輸入公司名稱`;
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

    openNameReviewDialog(result.valid || [], result.uncertain || [], result.excluded || [], result.suggested_label);
  } catch (err) {
    uploadProgress.textContent = `❌ ${err.message}`;
    setTimeout(() => uploadProgress.textContent = "", 6000);
  } finally {
    fileInput.value = "";
  }
}

/* ── Manual Input Dialog ── */
document.getElementById("manual-input-btn").addEventListener("click", () => openManualDialog());
document.getElementById("manual-cancel").addEventListener("click", () =>
  document.getElementById("manual-overlay").classList.remove("open"));
document.getElementById("manual-overlay").addEventListener("click", e => {
  if (e.target === document.getElementById("manual-overlay"))
    document.getElementById("manual-overlay").classList.remove("open");
});

function openManualDialog(suggestedLabel = "") {
  const sel = document.getElementById("manual-industry");
  sel.innerHTML = state.industries.map(ind =>
    `<option value="${escHtml(ind)}">${escHtml(ind)}</option>`
  ).join("");
  document.getElementById("manual-names").value = "";
  document.getElementById("manual-label").value = suggestedLabel;
  document.getElementById("manual-overlay").classList.add("open");
  setTimeout(() => document.getElementById("manual-names").focus(), 50);
}

document.getElementById("manual-ok").addEventListener("click", () => {
  const rawText = document.getElementById("manual-names").value;
  const label = document.getElementById("manual-label").value.trim();
  const industry = document.getElementById("manual-industry").value;

  const names = rawText.split("\n").map(n => n.trim()).filter(n => n.length > 0);
  if (names.length === 0) { toast("請輸入至少一個公司名稱", true); return; }

  document.getElementById("manual-overlay").classList.remove("open");

  const valid = [], uncertain = [];
  for (const name of names) {
    const existing = state.companies.find(c => c.name === name);
    const candidate = {
      name,
      suggested_label: label,
      suggested_industry: industry,
      is_new: !existing,
      existing_id: existing ? existing.id : null,
      existing_labels: existing ? (existing.labels || []) : [],
    };
    if (name.endsWith("股份有限公司")) {
      valid.push(candidate);
    } else {
      uncertain.push(candidate);
    }
  }

  openConfirmDialog(valid, uncertain, [], label);
});

/* ── Name Review Dialog ── */
let _nameReviewMeta = null;

function openNameReviewDialog(valid, uncertain, excluded, suggestedLabel) {
  // If nothing identified, skip straight to confirm (which will show an appropriate message)
  if (valid.length === 0 && uncertain.length === 0) {
    openConfirmDialog([], [], excluded, suggestedLabel);
    return;
  }

  _nameReviewMeta = { excluded, suggestedLabel };

  const rows = [
    ...valid.map(c => ({ name: c.name, kind: "valid" })),
    ...uncertain.map(c => ({ name: c.name, kind: "uncertain" })),
  ];

  document.getElementById("name-review-rows").innerHTML = rows.map((c, i) => `
    <div class="name-review-row" id="nr-row-${i}">
      <span class="nr-kind ${c.kind === "valid" ? "nr-valid" : "nr-uncertain"}"
            title="${c.kind === "valid" ? "含股份有限公司" : "名稱待確認"}">
        ${c.kind === "valid" ? "✔" : "?"}
      </span>
      <input class="name-review-input" id="nr-input-${i}" value="${escHtml(c.name)}" placeholder="公司名稱" />
      <button class="nr-delete" onclick="document.getElementById('nr-row-${i}').remove()">✕</button>
    </div>
  `).join("");

  document.getElementById("name-review-overlay").classList.add("open");
  // Focus first input
  setTimeout(() => document.querySelector(".name-review-input")?.focus(), 50);
}

document.getElementById("name-review-cancel").addEventListener("click", () =>
  document.getElementById("name-review-overlay").classList.remove("open"));

document.getElementById("name-review-ok").addEventListener("click", () => {
  const inputs = document.querySelectorAll(".name-review-input");
  if (inputs.length === 0) {
    toast("未保留任何公司名稱", true);
    return;
  }

  const newValid = [], newUncertain = [];
  const { suggestedLabel } = _nameReviewMeta;

  inputs.forEach(input => {
    const name = input.value.trim();
    if (!name) return;
    if (name.includes("股份有限公司")) {
      const existing = state.companies.find(c => c.name === name);
      newValid.push({
        name,
        is_new: !existing,
        existing_id: existing ? existing.id : null,
        existing_labels: existing ? (existing.labels || []) : [],
        suggested_label: suggestedLabel,
        suggested_industry: existing ? existing.industry : (state.industries[0] || ""),
      });
    } else {
      newUncertain.push({
        name,
        suggested_label: suggestedLabel,
        suggested_industry: state.industries[0] || "",
      });
    }
  });

  document.getElementById("name-review-overlay").classList.remove("open");
  openConfirmDialog(newValid, newUncertain, _nameReviewMeta.excluded, suggestedLabel);
});

/* ── Confirm Dialog ── */
function openConfirmDialog(valid, uncertain, excluded, suggestedLabel) {
  state.pendingCandidates = valid;
  state.pendingUncertain = uncertain;
  state.pendingExcluded  = excluded;
  state.pendingLabel = suggestedLabel;

  let subtitle = `辨識到 ${valid.length} 間股份有限公司`;
  if (uncertain.length) subtitle += `，${uncertain.length} 間名稱待確認`;
  if (excluded.length)  subtitle += `，${excluded.length} 間有限公司（可確認是否升格）`;
  document.getElementById("confirm-subtitle").textContent = subtitle;

  const bulkBar = `
    <div id="confirm-bulk-bar">
      <span class="bulk-bar-label">全部套用標籤</span>
      <input id="bulk-label-input" type="text" value="${escHtml(suggestedLabel)}" placeholder="標籤（留空不變）" />
      <button class="bulk-apply-btn" onclick="applyBulkEdit()">套用 →</button>
    </div>`;

  // ── Section 1: valid (股份有限公司) ──
  const validHtml = valid.length ? `
    <div class="confirm-section-title">✅ 股份有限公司</div>
    ${valid.map((c, i) => {
      const badge = c.is_new
        ? `<span class="new-badge">新增</span>`
        : `<span class="update-badge">既有</span>`;
      const existingLabels = c.existing_labels?.length
        ? `<div class="existing-labels">現有標籤：${c.existing_labels.join("、")}</div>`
        : "";
      return `
        <div class="confirm-row">
          <div class="company-name-col">${escHtml(c.name)}${badge}${existingLabels}</div>
          <input type="text" id="label-v${i}" value="${escHtml(c.suggested_label)}" placeholder="標籤名稱" />
        </div>`;
    }).join("")}` : "";

  // ── Section 2: uncertain (neither suffix) ──
  const uncertainHtml = uncertain.length ? `
    <div class="confirm-section-title uncertain-title">❓ 不含標準公司結尾，是否視為股份有限公司？</div>
    ${uncertain.map((c, i) => `
      <div class="confirm-row uncertain-row" id="uncertain-row-${i}">
        <div class="company-name-col uncertain-name">${escHtml(c.name)}</div>
        <div class="uncertain-actions">
          <button class="unc-btn unc-yes" onclick="toggleUncertain(${i}, true)">✔ 是，納入</button>
          <button class="unc-btn unc-no active" onclick="toggleUncertain(${i}, false)">✘ 否，略過</button>
        </div>
        <div class="uncertain-fields" id="uncertain-fields-${i}" style="display:none; grid-column:1/-1;">
          <div class="confirm-row" style="border:none;padding:4px 0;">
            <div></div>
            <input type="text" id="label-u${i}" value="${escHtml(c.suggested_label || suggestedLabel)}" placeholder="標籤名稱" />
          </div>
        </div>
      </div>`).join("")}` : "";

  // ── Section 3: excluded (有限公司 only) — allow rescue if OCR misread ──
  const excludedHtml = excluded.length ? `
    <div class="confirm-section-title excluded-title">⚠️ 僅含「有限公司」（可能是 OCR 誤讀，確認是否為股份有限公司？）</div>
    ${excluded.map((c, i) => `
      <div class="confirm-row excluded-row" id="excluded-row-${i}">
        <div class="company-name-col excluded-name">${escHtml(c.name)}</div>
        <div class="uncertain-actions">
          <button class="unc-btn unc-yes" onclick="toggleExcluded(${i}, true)">✔ 是股份有限公司，納入</button>
          <button class="unc-btn unc-no active" onclick="toggleExcluded(${i}, false)">✘ 確實排除</button>
        </div>
        <div class="uncertain-fields" id="excluded-fields-${i}" style="display:none; grid-column:1/-1;">
          <div class="confirm-row" style="border:none;padding:4px 0;">
            <div></div>
            <input type="text" id="label-e${i}" value="${escHtml(suggestedLabel)}" placeholder="標籤名稱" />
          </div>
        </div>
      </div>`).join("")}` : "";

  document.getElementById("confirm-rows").innerHTML = bulkBar + validHtml + uncertainHtml + excludedHtml;
  document.getElementById("confirm-overlay").classList.add("open");
}

function applyBulkEdit() {
  const labelVal = document.getElementById("bulk-label-input").value.trim();
  if (!labelVal) return;
  document.querySelectorAll('#confirm-rows input[id^="label-"]').forEach(el => {
    el.value = labelVal;
  });
}

function toggleUncertain(i, accept) {
  const row = document.getElementById(`uncertain-row-${i}`);
  const fields = document.getElementById(`uncertain-fields-${i}`);
  row.querySelectorAll(".unc-btn").forEach(b => b.classList.remove("active"));
  if (accept) {
    row.querySelector(".unc-yes").classList.add("active");
    fields.style.display = "";
  } else {
    row.querySelector(".unc-no").classList.add("active");
    fields.style.display = "none";
  }
  row.dataset.accepted = accept ? "1" : "0";
}

function toggleExcluded(i, accept) {
  const row = document.getElementById(`excluded-row-${i}`);
  const fields = document.getElementById(`excluded-fields-${i}`);
  row.querySelectorAll(".unc-btn").forEach(b => b.classList.remove("active"));
  if (accept) {
    row.querySelector(".unc-yes").classList.add("active");
    fields.style.display = "";
  } else {
    row.querySelector(".unc-no").classList.add("active");
    fields.style.display = "none";
  }
  row.dataset.accepted = accept ? "1" : "0";
}

document.getElementById("confirm-cancel").addEventListener("click", () =>
  document.getElementById("confirm-overlay").classList.remove("open"));

document.getElementById("confirm-ok").addEventListener("click", async () => {
  // Collect valid candidates
  const companies = state.pendingCandidates.map((c, i) => ({
    name: c.name,
    label: document.getElementById(`label-v${i}`)?.value.trim() ?? state.pendingLabel,
    is_new: c.is_new,
    existing_id: c.existing_id ?? null,
  }));

  // Collect accepted uncertain candidates
  (state.pendingUncertain || []).forEach((c, i) => {
    const row = document.getElementById(`uncertain-row-${i}`);
    if (row?.dataset.accepted === "1") {
      companies.push({
        name: c.name,
        label: document.getElementById(`label-u${i}`)?.value.trim() ?? state.pendingLabel,
        is_new: true,
        existing_id: null,
      });
    }
  });

  // Collect rescued excluded candidates (user confirmed they are 股份有限公司)
  (state.pendingExcluded || []).forEach((c, i) => {
    const row = document.getElementById(`excluded-row-${i}`);
    if (row?.dataset.accepted === "1") {
      companies.push({
        name: c.name,
        label: document.getElementById(`label-e${i}`)?.value.trim() ?? state.pendingLabel,
        is_new: true,
        existing_id: null,
      });
    }
  });

  document.getElementById("confirm-overlay").classList.remove("open");

  if (companies.length === 0) {
    toast("未選擇任何公司，已取消");
    return;
  }

  try {
    const result = await api("POST", "/api/companies/confirm", { companies });
    toast(`已儲存 ${result.saved} 筆公司資料`);
    for (const id of (result.enriching || [])) {
      subscribeEnrichment(id);
    }
    await loadCompanies();
    computeGroups();
    renderSidebar();
    renderGrid();
  } catch (err) {
    toast(`儲存失敗：${err.message}`, true);
  }
});

/* ── SSE Enrichment ── */
let _enrichPollTimer = null;
function _startEnrichPoll() {
  if (_enrichPollTimer) return;
  _enrichPollTimer = setInterval(async () => {
    if (state.enrichingIds.size === 0) {
      clearInterval(_enrichPollTimer);
      _enrichPollTimer = null;
      return;
    }
    await loadCompanies();
    renderGrid();
    if (_modalCompanyId && state.enrichingIds.has(_modalCompanyId)) openModal(_modalCompanyId);
  }, 30000);
}

function subscribeEnrichment(companyId) {
  state.enrichingIds.add(companyId);
  _startEnrichPoll();
  renderGrid();

  const key = getAiKey();
  const sseUrl = key
    ? `/api/companies/enrich/${companyId}?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}`
    : `/api/companies/enrich/${companyId}`;
  const es = new EventSource(sseUrl);
  es.onmessage = async e => {
    const event = JSON.parse(e.data);

    if (event.type === "data") {
      const company = state.companies.find(c => c.id === companyId);
      if (company) {
        Object.assign(company, event.fields);
        renderGrid();
        if (_modalCompanyId === companyId) openModal(companyId);
      }

    } else if (event.type === "progress") {
      toast(event.message);

    } else if (event.type === "done") {
      es.close();
      state.enrichingIds.delete(companyId);
      state.doneIds.add(companyId);
      await loadCompanies();
      computeGroups();
      renderSidebar();
      renderGrid();
      if (_modalCompanyId === companyId) openModal(companyId);
      setTimeout(() => {
        state.doneIds.delete(companyId);
        renderGrid();
      }, 2000);
    }
  };
  es.onerror = () => {
    es.close();
    state.enrichingIds.delete(companyId);
    renderGrid();
  };
}

/* ── Toast ── */
function toast(message, isError = false) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  if (isError) el.style.background = "#991b1b";
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ── Collapsible summary sections ── */
const COLLAPSIBLE_SECTIONS = new Set(["業務概況", "競業分析", "主要風險"]);

function applyCollapsible(container) {
  for (const h3 of [...container.querySelectorAll("h3")]) {
    if (!COLLAPSIBLE_SECTIONS.has(h3.textContent.trim())) continue;

    const body = document.createElement("div");
    body.className = "collapsible-body";
    let next = h3.nextElementSibling;
    while (next && next.tagName !== "H3") {
      const tmp = next.nextElementSibling;
      body.appendChild(next);
      next = tmp;
    }
    h3.after(body);
    h3.classList.add("collapsible-h3");
    h3.addEventListener("click", () => {
      const open = body.classList.toggle("open");
      h3.classList.toggle("open", open);
    });
  }
}

/* ── Markdown summary renderer ── */
function renderSummary(raw) {
  // Drop any preamble before the first ## heading (e.g. Claude status messages)
  // Also drop "## 公司名稱 公司簡介" opening title if present
  let text = raw.replace(/^##\s+.+公司簡介[^\n]*\n+/, "");
  const firstHeading = text.indexOf("\n##");
  const hasLeadingJunk = !text.trimStart().startsWith("##") && firstHeading !== -1;
  if (hasLeadingJunk) text = text.slice(firstHeading + 1);
  text = text.trimStart();

  const lines = text.split("\n");
  const out = [];
  let inList = false;
  let inTable = false;
  let tableRows = [];

  const flushTable = () => {
    if (!tableRows.length) return;
    // first row = header, second row = separator (skip), rest = body
    const [header, , ...body] = tableRows;
    const ths = (header || "").split("|").filter((_,i,a) => i>0 && i<a.length-1)
      .map(c => `<th>${inlineMarkdown(c.trim())}</th>`).join("");
    const trs = body.map(row =>
      "<tr>" + row.split("|").filter((_,i,a) => i>0 && i<a.length-1)
        .map(c => `<td>${inlineMarkdown(c.trim())}</td>`).join("") + "</tr>"
    ).join("");
    out.push(`<table class="summary-table"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`);
    tableRows = [];
    inTable = false;
  };

  let inOList = false;

  const flushLists = () => {
    if (inList)  { out.push("</ul>");  inList  = false; }
    if (inOList) { out.push("</ol>"); inOList = false; }
  };

  for (let i = 0; i < lines.length; i++) {
    const raw  = lines[i];
    const line = raw.trim();

    // Table row
    if (line.startsWith("|") && line.endsWith("|")) {
      flushLists();
      inTable = true;
      tableRows.push(line);
      continue;
    }
    if (inTable) { flushTable(); }

    // Horizontal rule
    if (/^---+$/.test(line)) {
      flushLists();
      out.push("<hr>");
      continue;
    }

    // Headings ## / ###
    const h2 = line.match(/^##\s+(.+)/);
    const h3 = line.match(/^###\s+(.+)/);
    if (h2 || h3) {
      flushLists();
      const tag = h3 ? "h4" : "h3";
      out.push(`<${tag}>${inlineMarkdown(h2 ? h2[1] : h3[1])}</${tag}>`);
      continue;
    }

    // Unordered list item (-, *, •)
    const ul = line.match(/^[-*•]\s+(.+)/);
    if (ul) {
      if (inOList) { out.push("</ol>"); inOList = false; }
      if (!inList)  { out.push("<ul>");  inList  = true;  }
      out.push(`<li>${inlineMarkdown(ul[1])}</li>`);
      continue;
    }

    // Ordered list item (1. 2. 3.) — render as bullet for visual consistency
    const ol = line.match(/^\d+[.)]\s+(.+)/);
    if (ol) {
      if (inOList) { out.push("</ol>"); inOList = false; }
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inlineMarkdown(ol[1])}</li>`);
      continue;
    }

    if (line === "") {
      // Don't break a list when blank lines appear between items — peek ahead
      if (inOList || inList) {
        const next = lines.slice(i + 1).find(l => l.trim() !== "");
        if (inOList && next && /^\d+[.)]\s+/.test(next.trim())) continue;
        if (inList  && next && /^[-*•]\s+/.test(next.trim()))  continue;
      }
      flushLists();
      out.push("");
      continue;
    }

    // Close lists before normal content
    flushLists();

    out.push(inlineMarkdown(line));
  }

  flushLists();
  if (inTable) flushTable();

  // Group consecutive non-empty lines into <p> blocks
  const html = [];
  let para = [];
  const BLOCK = s => s.startsWith("<h") || s.startsWith("<ul") || s.startsWith("</ul")
    || s.startsWith("<ol") || s.startsWith("</ol")
    || s.startsWith("<li")
    || s.startsWith("<table") || s === "<hr>";
  for (const l of out) {
    if (l === "") {
      if (para.length) { html.push(`<p>${para.join(" ")}</p>`); para = []; }
    } else if (BLOCK(l)) {
      if (para.length) { html.push(`<p>${para.join(" ")}</p>`); para = []; }
      html.push(l);
    } else {
      para.push(l);
    }
  }
  if (para.length) html.push(`<p>${para.join(" ")}</p>`);

  return html.join("\n");
}

function inlineMarkdown(str) {
  return escHtml(str)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

/* ── Util ── */
function cardBlurb(c) {
  return c.blurb || "（資料補充中）";
}

function shortName(name) {
  return (name || "").replace(/股份有限公司$/, "").trim();
}

function escHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escAttr(str) {
  return String(str || "").replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

/* ── Init ── */
boot();
