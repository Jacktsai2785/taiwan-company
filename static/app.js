/* ── State ── */
const state = {
  companies: [],
  industries: [],
  labels: [],
  groups: {},                    // {industry: [group, ...]}
  expandedIndustries: new Set(),
  activeIndustry: null,          // null = all
  activeGroup: null,             // null = all groups, "__ungrouped__" = no group
  activeLabel: null,             // cross-industry label filter
  activeLabelIndustry: null,     // industry drill-down within a label
  expandedLabels: new Set(),
  activeTab: "all",              // "all" | "watched"
  sortBy: "capital",
  sortDir: "desc",
  searchQuery: "",
  pendingCandidates: [],
  pendingUncertain: [],
  pendingLabel: "",
  enrichingIds: new Set(),       // currently enriching company ids
  doneIds: new Set(),            // briefly green after enrichment completes
};

let _modalCompanyId = null;

/* ── AI settings (localStorage) ── */
let _deployMode = "local";   // "local" | "cloud" — set by boot() from /api/config/deploy-mode

function getAiKey()      { return localStorage.getItem("ai_api_key") || ""; }
function getAiProvider() {
  const stored = localStorage.getItem("ai_provider");
  if (stored) return stored;
  return _deployMode === "cloud" ? "gemini" : "local";
}
function isLocalMode()   { return getAiProvider() === "local"; }
function isCloudDeploy() { return _deployMode === "cloud"; }

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
  // In cloud deploy, prevent re-selecting the local mode (binary not on server)
  const localRadio = document.querySelector('input[name="ai-provider"][value="local"]');
  if (localRadio) localRadio.disabled = isCloudDeploy();
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

function onAiProviderChange(radio) {
  const needsKey = radio.value !== "local";
  document.getElementById("settings-key-section").style.display = needsKey ? "" : "none";
  document.getElementById("settings-error").textContent = "";
}

function saveSettings() {
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
}

function closeSettings() {
  document.getElementById("settings-overlay").classList.remove("open");
}

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
  // Detect deploy mode FIRST so getAiProvider() default + UI reflect it.
  try {
    const r = await fetch("/api/config/deploy-mode");
    if (r.ok) _deployMode = (await r.json()).mode || "local";
  } catch (_) { /* default stays "local" on network error */ }
  if (_deployMode === "cloud") {
    document.body.classList.add("deploy-cloud");
    const sub = document.getElementById("settings-subtitle");
    if (sub) sub.innerHTML = "雲端版推薦使用 <strong>Gemini</strong>，可申請免費 Key。<br>「本機 Claude」僅在自行下載專案到本機執行時可用。";
    document.querySelectorAll(".badge-free").forEach(el => el.hidden = false);
    document.querySelectorAll(".badge-local-only").forEach(el => el.hidden = false);
  }

  await Promise.all([loadIndustries(), loadCompanies(), loadLabels()]);
  computeGroups();
  renderSidebar();
  renderGrid();
  _updateAiModeLabel();
  // Request notification permission early (must be from a page-load context, not a background task)
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }
  // Auto-stop title flash when user returns to the tab (for non-modal notifications)
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && _titleFlashTimer && !document.getElementById("batch-overlay").classList.contains("open")) {
      stopTitleFlash();
    }
  });
  // Show settings only on very first visit (localStorage never set)
  if (localStorage.getItem("ai_provider") === null) openSettings();
}

/* ── Notify helper ── */
// Send OS notification + start title flash when the page is not in the foreground.
// Call after any long-running AI task completes.
function alertDone(flashMsg, notifBody) {
  if (document.visibilityState !== "visible") {
    startTitleFlash(flashMsg);
    notifyUser("台灣產業商情平台", notifBody);
  }
}

function notifyUser(title, body) {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(title, { body, icon: "/static/favicon.ico" });
  }
}

let _titleFlashTimer = null;
let _titleFlashOriginal = null;

function startTitleFlash(msg) {
  if (_titleFlashTimer) return; // already flashing
  _titleFlashOriginal = document.title;
  let flip = false;
  _titleFlashTimer = setInterval(() => {
    document.title = (flip = !flip) ? msg : _titleFlashOriginal;
  }, 1000);
}

function stopTitleFlash() {
  if (_titleFlashTimer) { clearInterval(_titleFlashTimer); _titleFlashTimer = null; }
  if (_titleFlashOriginal !== null) { document.title = _titleFlashOriginal; _titleFlashOriginal = null; }
}

async function loadIndustries() {
  state.industries = await api("GET", "/api/config/industries");
}

async function loadLabels() {
  state.labels = await api("GET", "/api/config/labels");
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
    state.activeLabel = null;
    state.activeLabelIndustry = null;
    renderSidebar();
    renderGrid();
  });
  list.appendChild(allDiv);

  // Unclassified badge — only shown when companies exist without an industry
  const unclassifiedCount = state.companies.filter(c => !c.industry).length;
  if (unclassifiedCount > 0) {
    const badgeDiv = document.createElement("div");
    badgeDiv.id = "unclassified-badge";
    badgeDiv.innerHTML = `
      <span class="unclassified-dot"></span>
      <span class="unclassified-label">${unclassifiedCount} 間未分類</span>
      <button class="unclassified-classify-btn" title="AI 自動分類">✨ 自動分類</button>
    `;
    badgeDiv.querySelector(".unclassified-classify-btn").addEventListener("click", e => {
      e.stopPropagation();
      runClassify();
    });
    list.appendChild(badgeDiv);
  }

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
      state.activeLabel = null;
      state.activeLabelIndustry = null;
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
        grpDiv.innerHTML = `<span title="${escHtml(grp)}">${escHtml(grp)}</span><span class="industry-badge">${grpCount}</span>`;
        grpDiv.addEventListener("click", () => {
          state.activeIndustry = ind;
          state.activeGroup = grp;
          state.activeLabel = null;
          state.activeLabelIndustry = null;
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
          state.activeLabel = null;
          state.activeLabelIndustry = null;
          renderSidebar();
          renderGrid();
        });
        list.appendChild(ungroupedDiv);
      }
    }
  }

  // ── 標籤區塊（cross-industry，可展開看產業分布）──
  const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))].sort((a, b) => a.localeCompare(b, "zh-TW"));
  if (allLabels.length > 0) {
    const divider = document.createElement("div");
    divider.className = "sidebar-section-divider";
    divider.textContent = "標籤";
    list.appendChild(divider);

    for (const lbl of allLabels) {
      const companiesWithLbl = state.companies.filter(c => (c.labels || []).includes(lbl));
      const count = companiesWithLbl.length;
      const isLblActive = state.activeLabel === lbl;
      const isExpanded = state.expandedLabels.has(lbl);

      const lblDiv = document.createElement("div");
      lblDiv.className = "label-nav-item" + (isLblActive && !state.activeLabelIndustry ? " active" : "") + (isExpanded ? " open" : "");
      lblDiv.innerHTML = `
        <span class="label-chevron">›</span>
        <span title="${escHtml(lbl)}">${escHtml(lbl)}</span>
        <span class="industry-badge">${count}</span>`;
      lblDiv.addEventListener("click", () => {
        if (state.expandedLabels.has(lbl)) {
          state.expandedLabels.delete(lbl);
        } else {
          state.expandedLabels.add(lbl);
        }
        state.activeLabel = lbl;
        state.activeLabelIndustry = null;
        state.activeIndustry = null;
        state.activeGroup = null;
        state.activeTab = "all";
        document.querySelectorAll(".tab-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.tab === "all")
        );
        renderSidebar();
        renderGrid();
      });
      list.appendChild(lblDiv);

      if (isExpanded) {
        const industriesInLbl = [...new Set(companiesWithLbl.map(c => c.industry).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-TW"));

        for (const ind of industriesInLbl) {
          const indCount = companiesWithLbl.filter(c => c.industry === ind).length;
          const isIndActive = isLblActive && state.activeLabelIndustry === ind;
          const subDiv = document.createElement("div");
          subDiv.className = "label-ind-item" + (isIndActive ? " active" : "");
          subDiv.innerHTML = `<span title="${escHtml(ind)}">${escHtml(truncLabel(ind))}</span><span class="industry-badge">${indCount}</span>`;
          subDiv.addEventListener("click", e => {
            e.stopPropagation();
            state.activeLabel = lbl;
            state.activeLabelIndustry = isIndActive ? null : ind;
            state.activeIndustry = null;
            state.activeGroup = null;
            renderSidebar();
            renderGrid();
          });
          list.appendChild(subDiv);
        }

        const unclassifiedInLbl = companiesWithLbl.filter(c => !c.industry).length;
        if (unclassifiedInLbl > 0) {
          const isUncActive = isLblActive && state.activeLabelIndustry === "__none__";
          const uncDiv = document.createElement("div");
          uncDiv.className = "label-ind-item" + (isUncActive ? " active" : "");
          uncDiv.innerHTML = `<span>未分類</span><span class="industry-badge">${unclassifiedInLbl}</span>`;
          uncDiv.addEventListener("click", e => {
            e.stopPropagation();
            state.activeLabel = lbl;
            state.activeLabelIndustry = isUncActive ? null : "__none__";
            state.activeIndustry = null;
            state.activeGroup = null;
            renderSidebar();
            renderGrid();
          });
          list.appendChild(uncDiv);
        }
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

    try {
      await api("POST", "/api/config/industries", { name: indName });

      if (checked.length > 0) {
        await api("PUT", "/api/companies/batch-industry", {
          updates: checked.map(id => ({ id, industry: indName })),
        });
        checked.forEach(id => {
          const idx = state.companies.findIndex(c => c.id === id);
          if (idx !== -1) state.companies[idx].industry = indName;
        });
      }
      toast(`產業別「${indName}」已新增${checked.length > 0 ? `，${checked.length} 間公司已歸入` : ""}`);
    } catch (e) {
      toast(`新增失敗：${e.message}`, true);
    } finally {
      await loadIndustries();
      computeGroups();
      renderSidebar();
      renderGrid();
    }
  };
});

/* ── AI Auto-classify Industry ── */
async function runClassify() {
  if (state.industries.length === 0) {
    toast("請先新增至少一個產業別", true);
    return;
  }

  const overlay = document.getElementById("classify-overlay");
  const subtitle = document.getElementById("classify-subtitle");
  const loading = document.getElementById("classify-loading");
  const rows = document.getElementById("classify-rows");
  const okBtn = document.getElementById("classify-ok");

  subtitle.textContent = "Claude 正在比對既有產業別清單…";
  loading.style.display = "flex";
  rows.innerHTML = "";
  okBtn.disabled = true;
  overlay.classList.add("open");

  let result;
  try {
    result = await api("POST", "/api/companies/suggest-industries", { company_ids: null });
  } catch (e) {
    overlay.classList.remove("open");
    toast(`分類失敗：${e.message}`, true);
    return;
  }

  loading.style.display = "none";
  const targets = result.targets || [];
  const suggestions = result.suggestions || {};
  const industries = result.industries || [];

  alertDone("(!) 分類完成 — 請確認", `✅ AI 自動分類完成，請確認並套用`);

  if (targets.length === 0) {
    subtitle.textContent = "目前沒有缺漏產業別的公司";
    rows.innerHTML = `<p class="suggest-empty">所有公司皆已分類</p>`;
    okBtn.disabled = true;
    return;
  }

  const matched = targets.filter(t => suggestions[t.id]).length;
  subtitle.textContent = `共 ${targets.length} 家未分類，Claude 建議了 ${matched} 家（可逐家調整或取消勾選）`;

  rows.innerHTML = targets.map(t => {
    const suggested = suggestions[t.id] || "";
    const options = [`<option value="">— 不分類 —</option>`]
      .concat(industries.map(i => `<option value="${escHtml(i)}"${i === suggested ? " selected" : ""}>${escHtml(i)}</option>`))
      .join("");
    return `
      <label class="classify-row${suggested ? " suggested" : ""}">
        <input type="checkbox" data-id="${t.id}" ${suggested ? "checked" : ""} />
        <div class="classify-info">
          <span class="classify-name">${escHtml(shortName(t.name))}</span>
          <span class="classify-blurb">${escHtml(t.blurb || "—")}</span>
        </div>
        <select data-id="${t.id}">${options}</select>
      </label>`;
  }).join("");

  okBtn.disabled = false;

  document.getElementById("classify-cancel").onclick = () => overlay.classList.remove("open");
  okBtn.onclick = async () => {
    const checks = [...rows.querySelectorAll("input[type=checkbox]:checked")];
    const updates = checks.map(cb => {
      const id = cb.dataset.id;
      const sel = rows.querySelector(`select[data-id="${id}"]`);
      return { id, industry: sel ? sel.value : "" };
    }).filter(u => u.industry);

    overlay.classList.remove("open");
    if (updates.length === 0) {
      toast("未套用任何分類");
      return;
    }

    try {
      await api("PUT", "/api/companies/batch-industry", { updates });
      updates.forEach(u => {
        const idx = state.companies.findIndex(c => c.id === u.id);
        if (idx !== -1) state.companies[idx].industry = u.industry;
      });
      computeGroups();
      renderSidebar();
      renderGrid();
      toast(`已套用 ${updates.length} 家公司的分類`);
    } catch (e) {
      toast(`套用失敗：${e.message}`, true);
    }
  };
}

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

async function _fetchDigest(industry, forceRefresh) {
  // News digests need AI to summarise. On cloud without a key, surface a
  // friendly message instead of leaving the loading spinner forever.
  if (isCloudDeploy() && !getAiKey()) {
    _digestCache[industry] = {
      error: "尚未設定 AI。請點右上角 ⚙ 選擇 AI 提供者並輸入 API Key。",
      fetchedAt: Date.now(),
    };
    if (state.activeIndustry === industry && state.activeTab === "all") {
      const panel = document.getElementById("industry-panel");
      if (panel) _doRenderIndustryPanel(panel, industry);
    }
    return;
  }
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
      </div>`;
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
  const grid = document.getElementById("company-grid");
  const title = document.getElementById("toolbar-title");

  let companies = [...state.companies];

  if (state.activeTab === "watched") {
    companies = companies.filter(c => c.watched === true);
    title.textContent = "";
  } else if (state.activeLabel) {
    companies = companies.filter(c => (c.labels || []).includes(state.activeLabel));
    if (state.activeLabelIndustry === "__none__") {
      companies = companies.filter(c => !c.industry);
      title.textContent = `${state.activeLabel} — 未分類`;
    } else if (state.activeLabelIndustry) {
      companies = companies.filter(c => c.industry === state.activeLabelIndustry);
      title.textContent = `${state.activeLabel} — ${state.activeLabelIndustry}`;
    } else {
      title.textContent = `標籤：${state.activeLabel}`;
    }
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
    const dir = state.sortDir === "asc" ? 1 : -1;
    companies.sort((a, b) => dir * ((a.capital || 0) - (b.capital || 0)));
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
    `<span class="label-chip" title="${escHtml(l)}">${escHtml(truncLabel(l))}<button class="label-remove-btn" onclick="event.stopPropagation();removeLabel('${c.id}','${escAttr(l)}')" title="移除標籤">×</button></span>`
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

  const watchPillBtn = `<button class="watch-pill-btn${isWatched ? " is-watched" : ""}" onclick="event.stopPropagation();toggleWatch('${c.id}')">${isWatched ? "✓ 追蹤中" : "+ 追蹤"}</button>`;
  const nameRowPill = "";
  const labelRowPill = `<span class="watch-pill in-labels">${watchPillBtn}</span>`;
  const industryTag = c.industry
    ? `<span class="card-industry-tag">${escHtml(c.industry)}</span>`
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
    const memoBtnHtml = c.watched
      ? `<button id="memo-open-btn" onclick="openMemoPanel()">📋 訪談備忘錄</button>`
      : "";
    document.getElementById("modal-name").innerHTML =
      escHtml(shortName(c.name)) + listingBadge(c.listing_status) + memoBtnHtml;
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

/* ── Tabs ── */
document.getElementById("tab-group").addEventListener("click", e => {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  state.activeTab = btn.dataset.tab;
  state.activeLabel = null;
  state.activeLabelIndustry = null;
  document.querySelectorAll(".tab-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === state.activeTab)
  );
  renderSidebar();
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
  if (btn.dataset.sort === "capital" && state.sortBy === "capital") {
    state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
  } else {
    state.sortBy = btn.dataset.sort;
    state.sortDir = "desc";
  }
  document.querySelectorAll(".sort-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  document.querySelector('.sort-btn[data-sort="capital"]').textContent =
    `資本額 ${state.sortDir === "desc" ? "▼" : "▲"}`;
  renderGrid();
});

/* ── Modal ── */
function _buildModalInfoHTML(c) {
  const fmt = n => n ? `NT$ ${Number(n).toLocaleString()} 元` : "—";
  const websiteRow = c.website
    ? `<span class="info-label">官方網站</span>
       <span class="info-value"><a href="${escAttr(c.website)}" target="_blank" rel="noopener noreferrer">${escHtml(c.website.replace(/\/$/, ""))}</a></span>`
    : "";
  return `
    <span class="info-label">統一編號</span><span class="info-value">${escHtml(c.tax_id || "—")}</span>
    <span class="info-label">公司代表人</span><span class="info-value">${escHtml(c.representative || "—")}</span>
    <span class="info-label">資本總額</span><span class="info-value">${fmt(c.authorized_capital)}</span>
    <span class="info-label">實收資本額</span><span class="info-value">${fmt(c.capital)}</span>
    <span class="info-label">每股金額</span><span class="info-value">${
      c.par_value
        ? `NT$ ${c.par_value} 元`
        : c.no_par_value
          ? `<span class="no-par-badge">無票面金額</span>`
          : c.is_corp && c.tax_id
            ? `— <button class="fetch-par-btn" onclick="fetchParValue()" title="從 findbiz 抓取每股金額">🔍 抓取</button>`
            : "—"
    }</span>
    <span class="info-label">股份總數</span><span class="info-value">${c.total_shares ? Number(c.total_shares).toLocaleString() + " 股（" + Math.floor(c.total_shares / 1000).toLocaleString() + " 張）" : "—"}</span>
    <span class="info-label">公司所在地</span><span class="info-value">${escHtml(c.address || "—")}</span>
    <span class="info-label">產業別</span>
    <span class="info-value modal-industry-wrap">
      <select id="modal-industry-select" onchange="saveModalIndustry()">
        <option value="">— 未指定 —</option>
        ${state.industries.map(ind => `<option value="${escHtml(ind)}"${ind === (c.industry || "") ? " selected" : ""}>${escHtml(ind)}</option>`).join("")}
      </select>
    </span>
    ${websiteRow}
  `;
}

function openModal(id) {
  _modalCompanyId = id;
  const c = state.companies.find(x => x.id === id);
  if (!c) return;

  const memoBtnHtml = c.watched
    ? `<button id="memo-open-btn" onclick="openMemoPanel()">📋 訪談備忘錄</button>`
    : "";
  document.getElementById("modal-name").innerHTML =
    escHtml(shortName(c.name)) + listingBadge(c.listing_status) + memoBtnHtml;

  _updateModalWatchBtn(c);

  document.getElementById("modal-labels").innerHTML =
    (c.labels || []).map(l => `<span class="label-chip" title="${escHtml(l)}">${escHtml(l)}</span>`).join("") || "（無標籤）";

  document.getElementById("modal-info").innerHTML = _buildModalInfoHTML(c);

  const directors = c.directors || [];
  const tbody = document.getElementById("modal-directors");
  let totalRatio = 0, hasRatio = false;
  if (directors.length) {
    // Deduplicate by representative_of: same 法人 counts once
    const seenEntity = new Set();
    let totalShares = 0;
    const hasShares = directors.some(d => d.shares);
    hasRatio = directors.some(d => d.ratio != null);
    for (const d of directors) {
      const entity = (d.representative_of || "").trim();
      const key = entity || `__individual__${d.name}`;
      if (!seenEntity.has(key)) {
        seenEntity.add(key);
        totalShares += d.shares || 0;
        totalRatio  += d.ratio != null ? d.ratio : 0;
      }
    }
    // Determine which director would be auto-picked (largest with representative_of)
    let autoIdx = -1, autoMaxRatio = -1;
    directors.forEach((d, i) => {
      if ((d.representative_of || "").trim() && (d.ratio || 0) > autoMaxRatio) {
        autoMaxRatio = d.ratio || 0;
        autoIdx = i;
      }
    });
    // Active anchor (from existing relationship_graph if present, else auto)
    const rel = c.relationship_graph;
    let activeIdx = autoIdx;
    if (rel?.parent) {
      // Prefer the stored director_index when available — avoids re-derivation bugs
      // where a legal entity appears as d.name (not d.representative_of).
      if (Number.isInteger(rel.director_index) && rel.director_index >= 0 && rel.director_index < directors.length) {
        activeIdx = rel.director_index;
      } else {
        directors.forEach((d, i) => {
          if (rel.parent.kind === "person" && d.name === rel.parent.name && !d.representative_of) {
            activeIdx = i;
          } else if (rel.parent.kind === "legal_entity" && (
            d.representative_of === rel.parent.name ||
            (d.name === rel.parent.name && !d.representative_of)
          )) {
            activeIdx = i;
          }
        });
      }
    }

    tbody.innerHTML = directors.map((d, i) => {
      const isLegal = !!(d.representative_of || "").trim();
      const isActive = i === activeIdx;
      const isAuto = i === autoIdx;
      const cls = isActive ? "anchor-btn anchor-active" : "anchor-btn";
      const tip = isLegal
        ? `將「${escAttr(d.representative_of)}」設為母法人錨點`
        : `將「${escAttr(d.name)}」（自然人）設為錨點，查找其任職的所有公司`;
      const badge = isAuto && !isActive ? `<span class="anchor-auto-tag" title="預設選擇">●</span>` : "";
      const entityName = (d.representative_of || "").trim() || (_isLegalEntityName(d.name) ? d.name : "");
      const loadingRow = entityName
        ? `<tr class="director-parent-row" id="parent-row-${i}" style="display:none"><td colspan="6" class="director-parent-cell no-parent">查詢母公司中…</td></tr>`
        : "";
      return `
      <tr${isActive ? ' class="director-row-active"' : ""} data-dir-idx="${i}">
        <td>${escHtml(d.title || "—")}</td>
        <td>${escHtml(d.name || "—")}${badge}</td>
        <td>${escHtml(d.representative_of || "—")}</td>
        <td>${d.shares ? Number(d.shares).toLocaleString() : "—"}</td>
        <td>${d.ratio != null ? (d.ratio * 100).toFixed(2) + "%" : "—"}</td>
        <td><button class="${cls}" title="${tip}" onclick="setAnchorDirector(${i})">${isActive ? "✓" : "⊕"}</button></td>
      </tr>${loadingRow}`;
    }).join("") + `
      <tr class="director-total-row">
        <td colspan="3">合計</td>
        <td>${hasShares ? Number(totalShares).toLocaleString() : "—"}</td>
        <td>${hasRatio ? (totalRatio * 100).toFixed(2) + "%" : "—"}</td>
        <td></td>
      </tr>`;
    document.getElementById("modal-directors-section").style.display = "";
  } else {
    document.getElementById("modal-directors-section").style.display = "none";
  }

  const collapseBtn = document.getElementById("collapse-parent-rows-btn");
  if (collapseBtn) {
    const hasLegalEntities = directors.some(d =>
      (d.representative_of || "").trim() || _isLegalEntityName(d.name));
    collapseBtn.style.display = hasLegalEntities ? "" : "none";
    collapseBtn.dataset.collapsed = "1";
    collapseBtn.textContent = "法人溯源";
  }
  directors.forEach((d, i) => {
    const entity = (d.representative_of || "").trim() || (_isLegalEntityName(d.name) ? d.name : "");
    if (entity) _autoFillParentRow(i, entity, _modalCompanyId);
  });
  _renderShareholderSection(totalRatio, hasRatio);

  const summaryEl = document.getElementById("modal-summary");
  summaryEl.innerHTML =
    c.summary ? renderSummary(c.summary) : "<p class=\"summary-placeholder\">（公司簡介資料補充中，請稍後重整）</p>";
  summaryEl.style.display = "none";
  const summaryH4 = summaryEl.closest(".modal-section")?.querySelector(".collapsible-h4");
  if (summaryH4) summaryH4.classList.remove("is-open");
  applyCollapsible(summaryEl);

  // Patents: show section if data exists, hide if not
  const patentSection = document.getElementById("modal-patents-section");
  const patentStatus  = document.getElementById("modal-patents-status");
  if (c.patents && c.patents.length) {
    if (patentSection) patentSection.style.display = "";
    if (patentStatus)  patentStatus.innerHTML = "";
    // Reset to collapsed state each time modal opens
    const patH4 = document.querySelector(".patent-section-h4");
    if (patH4) patH4.classList.remove("is-open");
    const patTable = document.getElementById("modal-patents-table");
    if (patTable) patTable.style.display = "none";
    _renderPatents(id);
  } else {
    if (patentSection) patentSection.style.display = "none";
  }

  document.getElementById("modal-overlay").classList.add("open");
  document.body.classList.add("detail-open");
}


/* ── 董監法人母公司溯源 ── */
function _isLegalEntityName(name) {
  return /公司|合夥|基金|集團|Corp\.|Ltd\.|Inc\.|L\.P\.|LLC/i.test(name || "");
}

async function _autoFillParentRow(idx, entityName, companyId) {
  const row = document.getElementById(`parent-row-${idx}`);
  if (!row) return;
  try {
    const data = await api("GET", `/api/companies/investee-lookup?name=${encodeURIComponent(entityName)}`);
    if (_modalCompanyId !== companyId) return;
    if (data.count === 0) {
      row.innerHTML = `<td colspan="6" class="director-parent-cell no-parent">查無公發母公司記錄</td>`;
    } else {
      const badges = data.results.map(r =>
        `<span class="parent-company-badge">${escHtml(r.holder_name)}<small>${escHtml(r.holder_id)}</small></span>`
      ).join("");
      row.innerHTML = `<td colspan="6" class="director-parent-cell">公發母公司：${badges}</td>`;
    }
  } catch {
    if (_modalCompanyId !== companyId) return;
    row.innerHTML = `<td colspan="6" class="director-parent-cell no-parent">查詢失敗</td>`;
  }
}

function toggleParentRows(btn) {
  const rows = document.querySelectorAll("#modal-directors .director-parent-row");
  const isCollapsed = btn.dataset.collapsed === "1";
  rows.forEach(r => r.style.display = isCollapsed ? "" : "none");
  btn.dataset.collapsed = isCollapsed ? "0" : "1";
  btn.textContent = isCollapsed ? "法人溯源" : "法人溯源";
}

function _collapseParentRows() {
  document.querySelectorAll("#modal-directors .director-parent-row")
    .forEach(r => r.style.display = "none");
  const btn = document.getElementById("collapse-parent-rows-btn");
  if (btn && btn.style.display !== "none") {
    btn.dataset.collapsed = "1";
    btn.textContent = "法人溯源";
  }
}

function _expandParentRows() {
  document.querySelectorAll("#modal-directors .director-parent-row")
    .forEach(r => r.style.display = "");
  const btn = document.getElementById("collapse-parent-rows-btn");
  if (btn && btn.style.display !== "none") {
    btn.dataset.collapsed = "0";
    btn.textContent = "法人溯源";
  }
}

async function lookupDirectorParent(entityName, btn) {
  btn.disabled = true;
  btn.textContent = "…";
  const row = btn.closest("tr");
  // 移除舊子列（若存在）
  const old = row.nextElementSibling;
  if (old && old.classList.contains("director-parent-row")) old.remove();

  try {
    const data = await api("GET", `/api/companies/investee-lookup?name=${encodeURIComponent(entityName)}`);
    const subRow = document.createElement("tr");
    subRow.className = "director-parent-row";
    if (data.count === 0) {
      subRow.innerHTML = `<td colspan="6" class="director-parent-cell no-parent">查無公發母公司記錄（${escHtml(entityName)}）</td>`;
    } else {
      const badges = data.results.map(r =>
        `<span class="parent-company-badge" title="${escHtml(r.category || "")}">` +
        `${escHtml(r.holder_name)}<small>${escHtml(r.holder_id)}</small></span>`
      ).join("");
      subRow.innerHTML = `<td colspan="6" class="director-parent-cell">公發母公司：${badges}</td>`;
    }
    row.after(subRow);
    btn.textContent = "🔗";
    btn.disabled = false;
  } catch (err) {
    btn.textContent = "🔗";
    btn.disabled = false;
    const subRow = document.createElement("tr");
    subRow.className = "director-parent-row";
    subRow.innerHTML = `<td colspan="6" class="director-parent-cell no-parent">查詢失敗：${escHtml(err.message)}</td>`;
    row.after(subRow);
  }
}

/* ── 大股東板塊 ── */
function _renderShareholderSection(totalRatio, hasRatio) {
  const section = document.getElementById("modal-shareholders-section");
  const content = document.getElementById("modal-shareholder-content");
  if (!hasRatio) { section.style.display = "none"; return; }
  section.style.display = "";
  const h4 = section.querySelector(".collapsible-h4");
  if (h4) h4.classList.remove("is-open");
  content.style.display = "none";
  const pct = (totalRatio * 100).toFixed(2);
  const isIncomplete = totalRatio < 0.999;
  if (isIncomplete) {
    const missing = (100 - totalRatio * 100).toFixed(2);
    content.innerHTML = `
      <div class="hidden-holder-alert">
        <span class="alert-icon">⚠</span>
        <p>董監事持股合計 <b>${pct}%</b>，尚有 <b>${missing}%</b> 股份未在董監事名單中揭露，可能由其他股東持有</p>
      </div>
      <div id="modal-investee-holders"><p class="no-holders-hint">正在查詢公發公司持股資料…</p></div>`;
  } else {
    content.innerHTML = `<p class="no-holders-hint">董監事持股合計 ${pct}%，持股已完整揭露</p>`;
    return;
  }
  findPublicHolders();
}

async function findPublicHolders() {
  const id = _modalCompanyId;
  const resultEl = document.getElementById("modal-investee-holders");
  if (!resultEl) return;
  try {
    const data = await api("GET", `/api/companies/${id}/investee-holders`);
    if (data.count === 0) {
      resultEl.innerHTML = "<p class='no-holders-hint'>查無公發公司揭露持有此公司股份</p>";
    } else {
      const categoryLabel = {
        subsidiary:      "子公司",
        associate:       "關聯企業",
        fvoci_noncurrent:"FVOCI股權投資（非流動）",
        fvoci_current:   "FVOCI股權投資（流動）",
        fvoci_equity:    "FVOCI股權投資",
        mainland_china:  "大陸投資",
        other_lt_equity: "其他長期股權",
      };
      const catClass = { subsidiary: "holder-category-subsidiary", associate: "holder-category-associate" };
      resultEl.innerHTML = `
        <p class="holders-found-hint">找到 <b>${data.count}</b> 家公發公司揭露持有此公司股份：</p>
        <table class="investee-holders-table">
          <thead><tr><th>持有公司</th><th>代號</th><th>持股張數/比例</th><th>資料日期</th><th>類型</th></tr></thead>
          <tbody>${data.results.map(r => {
            let pctDisplay;
            const sharesNum = r.shares_nt != null ? Number(r.shares_nt) : null;
            const sharesStr = sharesNum != null ? sharesNum.toLocaleString() + "張" : null;
            let ratio = r.pct != null ? r.pct
                      : (sharesNum != null && data.total_shares > 0 ? sharesNum * 1000 / data.total_shares : null);
            const ratioStr = ratio != null ? "(" + (ratio * 100).toFixed(2) + "%)" : null;
            if (sharesStr && ratioStr) {
              pctDisplay = `${sharesStr} ${ratioStr}`;
            } else if (sharesStr) {
              pctDisplay = sharesStr;
            } else if (ratioStr) {
              pctDisplay = ratioStr;
            } else {
              pctDisplay = "—";
            }
            return `
            <tr>
              <td>${escHtml(r.holder_name || "—")}</td>
              <td>${escHtml(r.holder_id || "—")}</td>
              <td>${pctDisplay}</td>
              <td>${escHtml(r.as_of_date || "—")}</td>
              <td class="${catClass[r.category] || ""}">${categoryLabel[r.category] || escHtml(r.category || "—")}</td>
            </tr>`;
          }).join("")}</tbody>
        </table>`;
    }
  } catch (err) {
    resultEl.innerHTML = `<p class='no-holders-hint' style="color:#dc2626">查詢失敗：${escHtml(err.message)}</p>`;
  }
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

function toggleExportDropdown() {
  const toggle = document.getElementById("modal-export-btn");
  const menu   = document.getElementById("export-dropdown-menu");
  const isOpen = menu.classList.contains("open");
  if (isOpen) {
    toggle.classList.remove("open");
    menu.classList.remove("open");
  } else {
    toggle.classList.add("open");
    menu.classList.add("open");
  }
}

function closeExportDropdown() {
  document.getElementById("modal-export-btn")?.classList.remove("open");
  document.getElementById("export-dropdown-menu")?.classList.remove("open");
}

document.addEventListener("click", e => {
  const wrap = document.getElementById("export-dropdown-wrap");
  if (wrap && !wrap.contains(e.target)) closeExportDropdown();
});

function exportCompany(format) {
  const id = _modalCompanyId;
  if (!id) return;
  const a = document.createElement("a");
  a.href = `/api/companies/${id}/export?format=${format}`;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function toggleGenDropdown() {
  const toggle = document.getElementById("modal-gen-btn");
  const menu   = document.getElementById("gen-dropdown-menu");
  const isOpen = menu.classList.contains("open");
  if (isOpen) {
    _closeGenDropdownMenu(toggle, menu);
  } else {
    toggle.classList.add("open");
    const rect = toggle.getBoundingClientRect();
    menu.style.position = "fixed";
    menu.style.right = (window.innerWidth - rect.right) + "px";
    menu.style.left  = "";
    // Open upward if insufficient space below (menu ~150px tall)
    if (rect.bottom + 160 > window.innerHeight) {
      menu.style.top    = "auto";
      menu.style.bottom = (window.innerHeight - rect.top + 6) + "px";
    } else {
      menu.style.top    = (rect.bottom + 6) + "px";
      menu.style.bottom = "auto";
    }
    menu.classList.add("open");
  }
}

function _closeGenDropdownMenu(toggle, menu) {
  (toggle || document.getElementById("modal-gen-btn"))?.classList.remove("open");
  if (!menu) menu = document.getElementById("gen-dropdown-menu");
  if (menu) {
    menu.classList.remove("open");
    menu.style.position = "";
    menu.style.top = "";
    menu.style.bottom = "";
    menu.style.right = "";
  }
}

function closeGenDropdown() {
  _closeGenDropdownMenu();
}

document.addEventListener("click", e => {
  const wrap = document.getElementById("gen-dropdown-wrap");
  if (wrap && !wrap.contains(e.target)) closeGenDropdown();
});

/* ── FindBiz 每股金額抓取 ── */
let _findBizSessionId = null;

function fetchParValue() {
  const id = _modalCompanyId;
  const c  = state.companies.find(x => x.id === id);
  if (!id || !c?.tax_id) return;

  // 開啟 dialog，顯示 step 1 pending
  document.getElementById("findbiz-overlay").classList.add("open");
  document.getElementById("findbiz-subtitle").textContent = `公司：${c.name}（${c.tax_id}）`;
  document.getElementById("findbiz-s1-status").textContent = "⏳";
  document.getElementById("findbiz-s2-status").textContent = "";
  document.getElementById("findbiz-s3-status").textContent = "";
  document.getElementById("findbiz-status-msg").textContent = "正在啟動瀏覽器…";
  document.getElementById("findbiz-confirm-btn").style.display = "none";
  document.getElementById("findbiz-close-btn").textContent = "取消";

  fetch(`/api/findbiz/scrape`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ company_id: id, tax_id: c.tax_id }),
  })
    .then(r => r.json())
    .then(({ session_id }) => {
      _findBizSessionId = session_id;
      _listenFindBizStream(session_id, id);
    })
    .catch(err => {
      document.getElementById("findbiz-status-msg").textContent = `啟動失敗：${err}`;
    });
}

function _listenFindBizStream(sessionId, companyId) {
  const es = new EventSource(`/api/findbiz/stream/${sessionId}`);

  es.onmessage = async e => {
    const msg = JSON.parse(e.data);
    if (msg.type === "heartbeat") return;

    const statusEl = document.getElementById("findbiz-status-msg");

    if (msg.type === "browser_ready") {
      document.getElementById("findbiz-s1-status").textContent = "✅";
      document.getElementById("findbiz-s2-status").textContent = "⏳";
      statusEl.textContent = msg.message;
      document.getElementById("findbiz-confirm-btn").style.display = "";
    } else if (msg.type === "progress") {
      document.getElementById("findbiz-s2-status").textContent = "✅";
      document.getElementById("findbiz-s3-status").textContent = "⏳";
      statusEl.textContent = msg.message;
      document.getElementById("findbiz-confirm-btn").style.display = "none";
    } else if (msg.type === "done") {
      es.close();
      document.getElementById("findbiz-s2-status").textContent = "✅";
      document.getElementById("findbiz-s3-status").textContent = "✅";
      statusEl.innerHTML = `<span style="color:var(--success)">✅ ${msg.message}</span>`;
      document.getElementById("findbiz-close-btn").textContent = "關閉";
      document.getElementById("findbiz-confirm-btn").style.display = "none";
      // 更新本地 state 並重繪 modal
      try {
        await loadCompanies();
        renderGrid();
        if (_modalCompanyId === companyId && document.getElementById("modal-overlay").classList.contains("open")) {
          openModal(companyId);
        }
      } catch (_) {}
    } else if (msg.type === "error") {
      es.close();
      document.getElementById("findbiz-s1-status").textContent = "";
      document.getElementById("findbiz-s2-status").textContent = "";
      document.getElementById("findbiz-s3-status").textContent = "";
      statusEl.innerHTML = `<span style="color:var(--danger)">❌ ${msg.message}</span>`;
      document.getElementById("findbiz-close-btn").textContent = "關閉";
      document.getElementById("findbiz-confirm-btn").style.display = "none";
    }
  };

  es.onerror = () => {
    es.close();
    const statusEl = document.getElementById("findbiz-status-msg");
    if (statusEl && !statusEl.textContent.startsWith("✅") && !statusEl.textContent.startsWith("❌")) {
      statusEl.innerHTML = `<span style="color:var(--danger)">❌ 連線中斷，請重試</span>`;
    }
    document.getElementById("findbiz-close-btn").textContent = "關閉";
  };
}

function confirmCloudflare() {
  if (!_findBizSessionId) return;
  const btn = document.getElementById("findbiz-confirm-btn");
  if (btn) btn.disabled = true;
  fetch(`/api/findbiz/confirm/${_findBizSessionId}`, { method: "POST" })
    .then(() => {
      document.getElementById("findbiz-status-msg").textContent = "已通知後台，正在搜尋…";
    })
    .catch(() => {
      if (btn) btn.disabled = false;
    });
}

function closeFindBizDialog() {
  document.getElementById("findbiz-overlay").classList.remove("open");
  _findBizSessionId = null;
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
            <span class="bwp-status" id="bwp-status-${i}">${existing ? "已知官網" : "搜尋中…"}</span>
          </div>
          <input class="bwp-input" type="url" id="bwp-input-${i}"
            placeholder="${existing ? "https://example.com" : "搜尋中…"}"
            value="${escAttr(existing)}"
            ${existing ? "" : "disabled"}
            autocomplete="off" />
        </div>`;
    }).join("");

    overlay.classList.add("open");

    // Fire parallel searches for companies without a stored website
    companyIds.forEach((id, i) => {
      const c = state.companies.find(x => x.id === id);
      if (c?.website) return; // already known
      const key = getAiKey();
      const findUrl = `/api/companies/${id}/find-website` +
        (key ? `?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}` : "");
      fetch(findUrl)
        .then(r => r.json())
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
          } else {
            input.placeholder = "找不到，請手動填入";
            if (status) { status.textContent = "找不到官網"; status.className = "bwp-status missing"; }
          }
        })
        .catch(() => {
          if (!overlay.classList.contains("open")) return;
          const input = document.getElementById(`bwp-input-${i}`);
          if (input) { input.disabled = false; input.placeholder = "https://example.com"; }
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

function _showWebsitePrompt(companyId) {
  return new Promise(resolve => {
    const c = state.companies.find(x => x.id === companyId);
    const overlay    = document.getElementById("website-prompt-overlay");
    const nameEl     = document.getElementById("website-prompt-company-name");
    const input      = document.getElementById("website-prompt-input");
    const skipBtn    = document.getElementById("website-prompt-skip");
    const confirmBtn = document.getElementById("website-prompt-confirm");
    const hintEl     = document.getElementById("website-prompt-hint");

    nameEl.textContent = c?.name || "";
    if (hintEl) hintEl.textContent = "提供官網可讓 AI 直接擷取業務資訊，生成更準確的簡介。若無官網可略過。";
    overlay.classList.add("open");

    let dotTimer = null;

    const close = (website) => {
      clearInterval(dotTimer);
      overlay.classList.remove("open");
      skipBtn.onclick = null;
      confirmBtn.onclick = null;
      input.onkeydown = null;
      resolve(website);
    };

    skipBtn.onclick    = () => close(undefined);
    confirmBtn.onclick = () => close(input.value.trim());
    input.onkeydown    = e => { if (e.key === "Enter") close(input.value.trim()); };

    if (c?.website) {
      input.value = c.website;
      input.disabled = false;
      confirmBtn.disabled = false;
      input.placeholder = "https://example.com";
      setTimeout(() => input.focus(), 50);
    } else {
      // 搜尋期間：input 與確認按鈕均 disabled，動態省略號告知使用者等待中
      input.value = "";
      input.disabled = true;
      confirmBtn.disabled = true;
      if (hintEl) hintEl.textContent = "AI 正在自動搜尋官網，請耐心等候，搜尋完成前請勿按下按鈕…";

      const dotFrames = [".", "..", "..."];
      let dotIdx = 0;
      input.placeholder = "搜尋中.";
      dotTimer = setInterval(() => {
        dotIdx = (dotIdx + 1) % 3;
        input.placeholder = `搜尋中${dotFrames[dotIdx]}`;
      }, 500);

      const key = getAiKey();
      const findUrl = `/api/companies/${companyId}/find-website` +
        (key ? `?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}` : "");
      fetch(findUrl)
        .then(r => r.json())
        .then(data => {
          clearInterval(dotTimer);
          dotTimer = null;
          if (!overlay.classList.contains("open")) return;
          input.disabled = false;
          confirmBtn.disabled = false;
          if (data.website) {
            input.value = data.website;
            input.placeholder = "https://example.com";
            if (hintEl) hintEl.textContent = "提供官網可讓 AI 直接擷取業務資訊，生成更準確的簡介。若無官網可略過。";
          } else {
            input.placeholder = "https://example.com";
            if (hintEl) hintEl.textContent = "找不到官網，若您知道請手動填入（或留空略過）。";
          }
          input.focus();
        })
        .catch(() => {
          clearInterval(dotTimer);
          dotTimer = null;
          if (!overlay.classList.contains("open")) return;
          input.disabled = false;
          confirmBtn.disabled = false;
          input.placeholder = "https://example.com";
          if (hintEl) hintEl.textContent = "搜尋失敗，若您知道官網請手動填入（或留空略過）。";
          input.focus();
        });
    }
  });
}

async function regenSummary() {
  const id = _modalCompanyId;
  if (!id) return;

  const website = await _showWebsitePrompt(id);

  // undefined = 使用者按「略過」，不動 website 欄位
  // "" = 使用者清空網址
  // "https://..." = 使用者填入網址
  if (website !== undefined) {
    const c = state.companies.find(x => x.id === id);
    if (website !== (c?.website || "")) {
      try {
        const updated = await api("PUT", `/api/companies/${id}`, { website });
        const idx = state.companies.findIndex(x => x.id === id);
        if (idx !== -1) {
          state.companies[idx] = updated;
          document.getElementById("modal-info").innerHTML = _buildModalInfoHTML(updated);
        }
      } catch (_) {}
    }
  }

  const summaryEl = document.getElementById("modal-summary");
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">⏳ 重新生成中，請稍候（約 2–4 分鐘）…</p>";
  _expandSummarySection();
  _subscribeSummarize(id);
}

function deepEnrich() {
  const id = _modalCompanyId;
  if (!id) return;
  const summaryEl = document.getElementById("modal-summary");
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">🔍 深度搜尋媒體報導中，請稍候（約 3–5 分鐘）…</p>";
  _expandSummarySection();
  const btn = document.getElementById("modal-gen-btn");
  if (btn) btn.disabled = true;
  _subscribeDeepEnrich(id).finally(() => {
    if (btn) btn.disabled = false;
  });
}

function patentGen() {
  const id = _modalCompanyId;
  if (!id) return;
  const section = document.getElementById("modal-patents-section");
  const status  = document.getElementById("modal-patents-status");
  const table   = document.getElementById("modal-patents-table");
  if (section) section.style.display = "";
  if (table)   table.style.display = "none";
  if (status)  status.innerHTML = '<p class="summary-placeholder">📋 連接 TIPO 系統中，請稍候…</p>';
  _subscribePatent(id);
}

const _PATENT_FOLD = 3;

function toggleBrief(idx) {
  const pre  = document.getElementById(`brief-pre-${idx}`);
  const full = document.getElementById(`brief-full-${idx}`);
  const btn  = full && full.nextElementSibling;
  if (!full) return;
  const expanded = full.style.display !== "none";
  if (pre)  pre.style.display  = expanded ? "" : "none";
  full.style.display = expanded ? "none" : "";
  if (btn)  btn.textContent    = expanded ? "展開" : "收合";
}

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
  const es = new EventSource(`/api/companies/${companyId}/patents`);
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
    const briefHtml = brief
      ? `<span class="brief-preview" id="brief-pre-${idx}">${escHtml(briefPreview)}</span>`
        + (hasMore
          ? `<span class="brief-full" id="brief-full-${idx}" style="display:none">${escHtml(brief)}</span>`
            + `<button class="brief-toggle" onclick="toggleBrief(${idx})">展開</button>`
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
  if (autoShow) {
    if (table) table.style.display = "";
    const patH4 = document.querySelector(".patent-section-h4");
    if (patH4) patH4.classList.add("is-open");
  }
}

function _updateSummaryInModal(company) {
  if (_modalCompanyId !== company.id) return;
  if (!document.getElementById("modal-overlay").classList.contains("open")) return;
  const summaryEl = document.getElementById("modal-summary");
  if (!summaryEl) return;
  summaryEl.innerHTML = company.summary
    ? renderSummary(company.summary)
    : "<p class=\"summary-placeholder\">（公司簡介資料補充中，請稍後重整）</p>";
  _expandSummarySection();
}

function _subscribeSummarize(companyId) {
  if (isCloudDeploy() && !getAiKey()) {
    toast("請先在設定中輸入 API Key（建議 Gemini，免費）");
    openSettings();
    return Promise.resolve();
  }
  const key = getAiKey();
  const sseUrl = `/api/companies/${companyId}/summarize` +
    (key ? `?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}` : "");

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

function _subscribeDeepEnrich(companyId) {
  const key = getAiKey();
  const sseUrl = `/api/companies/${companyId}/deep-enrich` +
    (key ? `?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}` : "");

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
  document.getElementById("modal-overlay").classList.remove("open");
  document.body.classList.remove("detail-open");
  closeMemoPanel();
}
document.getElementById("modal-close").addEventListener("click", _closeDetailModal);
document.getElementById("modal-overlay").addEventListener("click", e => {
  // In side-by-side mode the overlay is pointer-events:none, so this only fires when alone
  if (e.target === document.getElementById("modal-overlay")) _closeDetailModal();
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

    alertDone("(!) 辨識完成 — 請確認", `✅ 找到 ${validCount} 間公司，請確認辨識結果`);
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

function _buildLabelOptions(suggestedLabel) {
  const sel = document.getElementById("manual-label-select");
  const custom = document.getElementById("manual-label-custom");
  const list = document.getElementById("manual-label-list");
  const labels = state.labels;
  const isKnown = suggestedLabel === "" || labels.includes(suggestedLabel);

  sel.innerHTML =
    `<option value="" disabled selected></option>` +
    labels.map(l => `<option value="${escHtml(l)}">${escHtml(l)}</option>`).join("") +
    `<option value="__new__"></option>`;

  list.innerHTML =
    `<li class="csel-header">（請選擇）</li>` +
    labels.map(l => `<li data-value="${escHtml(l)}">${escHtml(l)}</li>`).join("") +
    `<li data-value="__new__">＋ 輸入新標籤…</li>`;

  list.querySelectorAll("li[data-value]").forEach(li => {
    li.addEventListener("click", () => _selectCustomOption(li.dataset.value, li.textContent));
  });

  if (isKnown && suggestedLabel !== "") {
    sel.value = suggestedLabel;
    _setTriggerText(suggestedLabel, false);
    custom.style.display = "none";
    custom.value = "";
  } else if (!isKnown) {
    sel.value = "__new__";
    _setTriggerText("＋ 輸入新標籤…", false);
    custom.style.display = "";
    custom.value = suggestedLabel;
  } else {
    _setTriggerText("（請選擇）", true);
    custom.style.display = "none";
    custom.value = "";
  }
}

function _setTriggerText(text, isPlaceholder) {
  const trigger = document.getElementById("manual-label-trigger");
  trigger.textContent = text;
  trigger.classList.toggle("is-placeholder", isPlaceholder);
}

function _selectCustomOption(value, text) {
  const sel = document.getElementById("manual-label-select");
  const custom = document.getElementById("manual-label-custom");
  sel.value = value;
  _closeCustomSelect();
  if (value === "__new__") {
    _setTriggerText("＋ 輸入新標籤…", false);
    custom.style.display = "";
    setTimeout(() => custom.focus(), 50);
  } else {
    _setTriggerText(text, false);
    custom.style.display = "none";
    custom.value = "";
  }
}

function _openCustomSelect() {
  document.getElementById("manual-label-list").style.display = "";
}

function _closeCustomSelect() {
  const list = document.getElementById("manual-label-list");
  if (!list) return;
  list.style.display = "none";
}

function onManualLabelChange() {}

document.getElementById("manual-label-trigger").addEventListener("click", e => {
  e.stopPropagation();
  const list = document.getElementById("manual-label-list");
  list.style.display === "none" ? _openCustomSelect() : _closeCustomSelect();
});
document.getElementById("manual-label-list").addEventListener("click", e => e.stopPropagation());
document.addEventListener("click", _closeCustomSelect);

function _getManualLabel() {
  const sel = document.getElementById("manual-label-select");
  if (sel.value === "__new__") {
    return document.getElementById("manual-label-custom").value.trim();
  }
  return sel.value;
}

async function openManualDialog(suggestedLabel = "") {
  await loadLabels();
  document.getElementById("manual-names").value = "";
  _buildLabelOptions(suggestedLabel);
  document.getElementById("manual-overlay").classList.add("open");
  setTimeout(() => document.getElementById("manual-names").focus(), 50);
}

document.getElementById("manual-ok").addEventListener("click", async () => {
  const rawText = document.getElementById("manual-names").value;
  const label = _getManualLabel();

  const names = rawText.split("\n").map(n => n.trim()).filter(n => n.length > 0);
  if (names.length === 0) { toast("請輸入至少一個公司名稱", true); return; }

  document.getElementById("manual-overlay").classList.remove("open");

  // Lookup names via API to resolve official names and detect ambiguity
  let lookupResults = [];
  try {
    toast("正在查詢公司登記資料…");
    lookupResults = await api("POST", "/api/companies/name-lookup", { names });
  } catch (e) {
    lookupResults = names.map(n => ({ input: n, matches: [] }));
  }

  // All items with ≥1 match go to the disambiguation dialog so the user can verify
  const ambiguousItems = lookupResults.filter(item => item.matches.length >= 1);

  // Build candidates from disambiguation selections (no auto-resolve)
  const buildCandidates = (disambigSelections) => {
    const resolved = {};
    const skipped = new Set();
    for (const s of disambigSelections) {
      if (s.skipped) skipped.add(s.input);
      else resolved[s.input] = s.match;
    }

    const valid = [], uncertain = [];
    for (const name of names) {
      if (skipped.has(name)) continue;
      const match = resolved[name];
      const displayName = match ? match.full_name : name;
      const existing = state.companies.find(c => c.name === displayName || c.name === (match?.short_name ?? name));
      const candidate = {
        name: displayName,
        tax_id: match ? (match.tax_id || null) : null,
        suggested_label: label,
        suggested_industry: "",
        is_new: !existing,
        existing_id: existing ? existing.id : null,
        existing_labels: existing ? (existing.labels || []) : [],
      };
      if (match || displayName.endsWith("股份有限公司") || displayName.endsWith("有限公司")) {
        valid.push(candidate);
      } else {
        uncertain.push(candidate);
      }
    }
    openConfirmDialog(valid, uncertain, [], label);
  };

  if (ambiguousItems.length > 0) {
    openDisambigDialog(ambiguousItems, buildCandidates);
  } else {
    // No matches at all — go straight to confirm with uncertain items
    buildCandidates([]);
  }
});

/* ── Name Disambiguation Dialog ── */
let _disambigCallback = null;

function openDisambigDialog(items, onConfirm) {
  _disambigCallback = onConfirm;
  const body = document.getElementById("disambig-body");
  body.innerHTML = items.map((item, gi) => `
    <div class="disambig-group">
      <div class="disambig-input-label">「${escHtml(item.input)}」— 請選擇正確的公司（${item.matches.length} 筆）：</div>
      ${item.matches.map((m, mi) => {
        const statusDot = m.status === "核准設立"
          ? `<span class="disambig-status active" title="核准設立">●</span>`
          : `<span class="disambig-status unknown" title="${escHtml(m.status || '狀態不明')}">●</span>`;
        const corpBadge = m.is_corp
          ? `<span class="disambig-corp-badge">股份有限公司</span>`
          : `<span class="disambig-corp-badge limited">有限公司</span>`;
        return `
        <label class="disambig-option">
          <input type="radio" name="dg${gi}" value="${mi}" ${mi === 0 ? "checked" : ""} />
          ${statusDot}
          <span class="disambig-short">${escHtml(m.short_name)}</span>
          ${corpBadge}
          <span class="disambig-full">${escHtml(m.full_name)}</span>
        </label>`;
      }).join("")}
      <label class="disambig-option">
        <input type="radio" name="dg${gi}" value="skip" />
        <span class="disambig-skip">略過此公司</span>
      </label>
    </div>`).join("");
  document.getElementById("disambig-overlay").classList.add("open");
  _disambigItems = items;
}

let _disambigItems = [];

document.getElementById("disambig-cancel").addEventListener("click", () => {
  document.getElementById("disambig-overlay").classList.remove("open");
});

document.getElementById("disambig-ok").addEventListener("click", () => {
  const selections = _disambigItems.map((item, gi) => {
    const selected = document.querySelector(`input[name="dg${gi}"]:checked`);
    const val = selected ? selected.value : "0";
    if (val === "skip") return { input: item.input, skipped: true };
    return { input: item.input, skipped: false, match: item.matches[parseInt(val)] };
  });
  document.getElementById("disambig-overlay").classList.remove("open");
  if (_disambigCallback) _disambigCallback(selections);
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
      const hasData = !c.is_new && state.companies.find(x => x.id === c.existing_id)?.summary;
      const checked = hasData ? "" : "checked";
      const enrichHint = hasData ? `<span class="enrich-has-data" title="已有摘要，預設不重新生成">已生成</span>` : "";
      return `
        <div class="confirm-row">
          <div class="company-name-col">${escHtml(c.name)}${badge}${existingLabels}</div>
          <input type="text" id="label-v${i}" value="${escHtml(c.suggested_label)}" placeholder="標籤名稱" />
          <label class="enrich-check-label" title="是否生成 AI 摘要"><input type="checkbox" id="enrich-v${i}" ${checked} />生成${enrichHint}</label>
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
  // Collect valid candidates; track which ones user wants enriched
  const enrichFlags_v = [];
  const companies = state.pendingCandidates.map((c, i) => {
    enrichFlags_v.push(document.getElementById(`enrich-v${i}`)?.checked !== false);
    return {
      name: c.name,
      tax_id: c.tax_id ?? null,
      label: document.getElementById(`label-v${i}`)?.value.trim() ?? state.pendingLabel,
      is_new: c.is_new,
      existing_id: c.existing_id ?? null,
    };
  });

  // Collect accepted uncertain candidates (always enrich — they're always new)
  (state.pendingUncertain || []).forEach((c, i) => {
    const row = document.getElementById(`uncertain-row-${i}`);
    if (row?.dataset.accepted === "1") {
      companies.push({
        name: c.name,
        tax_id: c.tax_id ?? null,
        label: document.getElementById(`label-u${i}`)?.value.trim() ?? state.pendingLabel,
        is_new: true,
        existing_id: null,
      });
    }
  });

  // Collect rescued excluded candidates (always enrich — they're always new)
  (state.pendingExcluded || []).forEach((c, i) => {
    const row = document.getElementById(`excluded-row-${i}`);
    if (row?.dataset.accepted === "1") {
      companies.push({
        name: c.name,
        tax_id: c.tax_id ?? null,
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

  // Save first (no enrichment yet) so we control batching from the client side
  let saved_ids;
  try {
    const result = await api("POST", "/api/companies/confirm", { companies, enrich: false });
    saved_ids = result.saved_ids || [];
    toast(`已儲存 ${result.saved} 筆公司資料`);
    await loadCompanies();
    computeGroups();
    renderSidebar();
    renderGrid();
  } catch (err) {
    toast(`儲存失敗：${err.message}`, true);
    return;
  }

  if (saved_ids.length === 0) return;

  // Filter to only the IDs user checked for enrichment
  // saved_ids aligns with companies[] order (server preserves insertion order)
  const enrichSet = new Set(
    companies
      .map((_, idx) => {
        const wantEnrich = idx < enrichFlags_v.length
          ? enrichFlags_v[idx]
          : true; // uncertain/excluded rows always enrich
        return wantEnrich ? saved_ids[idx] : null;
      })
      .filter(Boolean)
  );
  const enrich_ids = saved_ids.filter(id => enrichSet.has(id));

  if (enrich_ids.length === 0) {
    toast(`已儲存，所有公司均已略過生成`);
    return;
  }

  // Ask for website URL before enrichment (improves AI summary quality)
  if (enrich_ids.length === 1) {
    const website = await _showWebsitePrompt(enrich_ids[0]);
    if (website !== undefined) {
      const c = state.companies.find(x => x.id === enrich_ids[0]);
      if (website !== (c?.website || "")) {
        try {
          const updated = await api("PUT", `/api/companies/${enrich_ids[0]}`, { website });
          const idx = state.companies.findIndex(x => x.id === enrich_ids[0]);
          if (idx !== -1) state.companies[idx] = updated;
        } catch (_) {}
      }
    }
  } else {
    const websiteMap = await _showBatchWebsitePrompt(enrich_ids);
    if (websiteMap) {
      for (const [eid, website] of Object.entries(websiteMap)) {
        const c = state.companies.find(x => x.id === eid);
        if (website !== (c?.website || "")) {
          try {
            const updated = await api("PUT", `/api/companies/${eid}`, { website });
            const idx = state.companies.findIndex(x => x.id === eid);
            if (idx !== -1) state.companies[idx] = updated;
          } catch (_) {}
        }
      }
    }
  }

  // Decide batching strategy
  let batchSize = enrich_ids.length;
  if (enrich_ids.length > 10) {
    const wantBatch = confirm(
      `本次共需生成 ${enrich_ids.length} 間公司資料。\n` +
      `數量較多，同時生成可能因 Claude rate limit 造成延遲或失敗。\n\n` +
      `是否分批生成？\n[確定] = 分批  [取消] = 一次全跑`
    );
    if (wantBatch) {
      const input = prompt(`每批要同時生成幾間？（建議 3–5）`, "5");
      const n = parseInt(input, 10);
      if (!input || isNaN(n) || n < 1) {
        toast("已取消分批生成", true);
        return;
      }
      batchSize = Math.max(1, n);
    }
  }

  await runEnrichmentInBatches(enrich_ids, batchSize);
});

async function runEnrichmentInBatches(ids, batchSize) {
  const total = ids.length;
  const chunks = [];
  for (let i = 0; i < ids.length; i += batchSize) chunks.push(ids.slice(i, i + batchSize));

  let done = 0;
  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i];
    console.log(`[batch] starting chunk ${i + 1}/${chunks.length}`, chunk);
    toast(`▶ 開始第 ${i + 1}/${chunks.length} 批（${chunk.length} 間）…`);
    try {
      await api("POST", "/api/companies/enrich-batch", { company_ids: chunk });
    } catch (e) {
      toast(`啟動第 ${i + 1} 批失敗：${e.message}`, true);
      return;
    }
    await Promise.allSettled(chunk.map(id => subscribeEnrichment(id)));
    done += chunk.length;
    console.log(`[batch] chunk ${i + 1}/${chunks.length} done, total ${done}/${total}`);

    if (i === chunks.length - 1) {
      toast(`✅ 全部 ${total} 間已完成生成`);
      break;
    }
    const remaining = total - done;
    const cont = await askBatchContinue({ batch: i + 1, totalBatches: chunks.length, done, total, remaining });
    if (!cont) {
      toast(`已中止，剩餘 ${remaining} 間未生成`, true);
      return;
    }
  }
}

/* ── Bulk Enrich Dialog ──
   兩種模式：
   (1) 補齊未完成 — 對因 session limit / 斷網 / 失敗 中斷的公司重跑
   (2) 全部重新生成 — 範圍內所有公司重跑（覆蓋寫入）
   範圍跟隨側邊欄目前篩選，與 renderGrid 同一份 filter 邏輯。 */

function isIncompleteCompany(c) {
  // 對齊 run_enrich.py 的判定條件
  if (!c) return false;
  if (!(c.representative || "").trim()) return true;
  const summary = (c.summary || "").trim();
  if (!summary) return true;
  if (summary.includes("尚待補充")) return true;
  return false;
}

function getScopedCompanies() {
  // 與 renderGrid 同步的篩選邏輯（但不含 search box 與排序）
  let companies = [...state.companies];
  let scopeLabel = "全部公司";

  if (state.activeTab === "watched") {
    companies = companies.filter(c => c.watched === true);
    scopeLabel = "⭐ 追蹤";
  } else if (state.activeLabel) {
    companies = companies.filter(c => (c.labels || []).includes(state.activeLabel));
    if (state.activeLabelIndustry === "__none__") {
      companies = companies.filter(c => !c.industry);
      scopeLabel = `標籤：${state.activeLabel} — 未分類`;
    } else if (state.activeLabelIndustry) {
      companies = companies.filter(c => c.industry === state.activeLabelIndustry);
      scopeLabel = `標籤：${state.activeLabel} — ${state.activeLabelIndustry}`;
    } else {
      scopeLabel = `標籤：${state.activeLabel}`;
    }
  } else if (state.activeIndustry) {
    companies = companies.filter(c => c.industry === state.activeIndustry);
    if (state.activeGroup === "__ungrouped__") {
      companies = companies.filter(c => !c.labels || c.labels.length === 0);
      scopeLabel = `產業：${state.activeIndustry} — 未分組`;
    } else if (state.activeGroup) {
      companies = companies.filter(c => (c.labels || []).includes(state.activeGroup));
      scopeLabel = `產業：${state.activeIndustry} — ${state.activeGroup}`;
    } else {
      scopeLabel = `產業：${state.activeIndustry}`;
    }
  }

  return { companies, scopeLabel };
}

function updateBulkEnrichButtonVisibility() {
  const btn = document.getElementById("bulk-enrich-btn");
  if (!btn) return;
  // 有任何公司就顯示；空清單下隱藏，避免 toolbar 視覺干擾
  btn.classList.toggle("visible", state.companies.length > 0);
}

function openBulkEnrichDialog() {
  const overlay = document.getElementById("bulk-enrich-overlay");
  const subtitle = document.getElementById("bulk-enrich-subtitle");
  const resumeCount = document.getElementById("bulk-resume-count");
  const reenrichCount = document.getElementById("bulk-reenrich-count");
  const resumeBtn = document.getElementById("bulk-resume-btn");
  const reenrichBtn = document.getElementById("bulk-reenrich-btn");
  const previewList = document.getElementById("bulk-preview-list");
  const previewDetails = document.getElementById("bulk-preview-details");

  const { companies, scopeLabel } = getScopedCompanies();
  const incomplete = companies.filter(isIncompleteCompany);

  subtitle.textContent = `目前範圍：${scopeLabel}（共 ${companies.length} 間）`;
  resumeCount.textContent = incomplete.length;
  reenrichCount.textContent = companies.length;

  resumeBtn.disabled = incomplete.length === 0;
  reenrichBtn.disabled = companies.length === 0;
  resumeBtn.textContent = incomplete.length === 0
    ? "範圍內已全數完成"
    : `開始補齊 ${incomplete.length} 間`;
  reenrichBtn.textContent = companies.length === 0
    ? "範圍為空"
    : `全部重跑 ${companies.length} 間`;

  // 預覽清單：未完成標紅，完成標綠
  if (companies.length > 0) {
    const incompleteIds = new Set(incomplete.map(c => c.id));
    previewList.innerHTML = companies.map(c => {
      const isInc = incompleteIds.has(c.id);
      const tag = isInc
        ? `<span class="bulk-preview-tag">未完成</span>`
        : `<span class="bulk-preview-tag ok">已完成</span>`;
      return `<div class="bulk-preview-item">
        <span class="bulk-preview-name">${escHtml(c.name)}</span>
        ${tag}
      </div>`;
    }).join("");
    previewDetails.style.display = "";
  } else {
    previewList.innerHTML = `<div class="bulk-preview-item"><span class="bulk-preview-name" style="color:var(--muted)">範圍內沒有公司</span></div>`;
    previewDetails.style.display = "none";
  }
  // 預設收合，使用者主動展開
  previewDetails.removeAttribute("open");

  resumeBtn.onclick = () => {
    overlay.classList.remove("open");
    const ids = incomplete.map(c => c.id);
    startBulkEnrich(ids, "resume", scopeLabel);
  };
  reenrichBtn.onclick = () => {
    overlay.classList.remove("open");
    if (companies.length > 5 && !confirm(
      `將對範圍內 ${companies.length} 間公司全部重新生成（覆蓋現有簡介與 GCIS 資料）。\n` +
      `範圍：${scopeLabel}\n\n` +
      `確定繼續？`
    )) return;
    const ids = companies.map(c => c.id);
    startBulkEnrich(ids, "reenrich", scopeLabel);
  };

  overlay.classList.add("open");
}

document.getElementById("bulk-enrich-cancel").addEventListener("click", () =>
  document.getElementById("bulk-enrich-overlay").classList.remove("open"));

async function startBulkEnrich(ids, mode, scopeLabel) {
  if (!ids || ids.length === 0) {
    toast("範圍內沒有需要生成的公司");
    return;
  }
  const modeLabel = mode === "resume" ? "補齊未完成" : "全部重跑";
  toast(`▶ ${modeLabel}：${scopeLabel}，共 ${ids.length} 間`);

  // 決定批次大小：>10 間時詢問是否分批，與既有 confirm 流程一致
  let batchSize = ids.length;
  if (ids.length > 10) {
    const wantBatch = confirm(
      `本次共需生成 ${ids.length} 間公司資料。\n` +
      `數量較多，同時生成可能因 Claude rate limit 造成延遲或失敗。\n\n` +
      `是否分批生成？\n[確定] = 分批  [取消] = 一次全跑`
    );
    if (wantBatch) {
      const input = prompt(`每批要同時生成幾間？（建議 3–5）`, "5");
      const n = parseInt(input, 10);
      if (!input || isNaN(n) || n < 1) {
        toast("已取消分批生成", true);
        return;
      }
      batchSize = Math.max(1, n);
    }
  }

  await runEnrichmentInBatches(ids, batchSize);
}

/* ── Batch continue dialog (DOM-based; survives Chrome's background-tab confirm() suppression) ── */
function askBatchContinue({ batch, totalBatches, done, total, remaining }) {
  return new Promise(resolve => {
    const overlay = document.getElementById("batch-overlay");
    const subtitle = document.getElementById("batch-subtitle");
    const okBtn = document.getElementById("batch-ok");
    const cancelBtn = document.getElementById("batch-cancel");

    subtitle.textContent =
      `第 ${batch}/${totalBatches} 批已完成（累計 ${done}/${total}）。\n` +
      `還剩 ${remaining} 間，是否繼續下一批？`;
    overlay.classList.add("open");

    // Notify + title flash unconditionally — user may have switched to another app entirely
    notifyUser("台灣產業商情平台", `✅ 第 ${batch}/${totalBatches} 批完成，還剩 ${remaining} 間，請確認是否繼續`);
    startTitleFlash("(!) 批次完成 — 等候確認");

    const cleanup = (answer) => {
      overlay.classList.remove("open");
      stopTitleFlash();
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      resolve(answer);
    };
    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
  });
}

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
    if (_modalCompanyId && state.enrichingIds.has(_modalCompanyId) && document.getElementById("modal-overlay").classList.contains("open")) openModal(_modalCompanyId);
  }, 30000);
}

function subscribeEnrichment(companyId) {
  // On cloud deploy, AI features require a key. Prompt the user before firing
  // the SSE call so they don't see "簡介生成失敗" with a cryptic message.
  if (isCloudDeploy() && !getAiKey()) {
    toast("請先在設定中輸入 API Key（建議 Gemini，免費）");
    openSettings();
    state.enrichingIds.delete(companyId);
    renderGrid();
    return Promise.resolve();
  }

  state.enrichingIds.add(companyId);
  _startEnrichPoll();
  renderGrid();

  const key = getAiKey();
  const sseUrl = key
    ? `/api/companies/enrich/${companyId}?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}`
    : `/api/companies/enrich/${companyId}`;

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
          if (_modalCompanyId === companyId && document.getElementById("modal-overlay").classList.contains("open")) {
            openModal(companyId);
            _expandSummarySection();
          }
        }

      } else if (event.type === "progress") {
        toast(event.message);

      } else if (event.type === "done") {
        es.close();
        settle();
        state.enrichingIds.delete(companyId);
        state.doneIds.add(companyId);
        // Notify if user isn't looking at the page
        alertDone("(!) 摘要生成完成", `✅ ${state.companies.find(c => c.id === companyId)?.name ?? "公司"} 摘要已生成完成`);
        try {
          await loadCompanies();
          computeGroups();
          renderSidebar();
          renderGrid();
          if (_modalCompanyId === companyId) openModal(companyId);
        } catch (err) {
          console.error("post-enrichment refresh failed:", err);
        }
        setTimeout(() => {
          state.doneIds.delete(companyId);
          stopTitleFlash();
          renderGrid();
        }, 3000);
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

/* ── Relationship Graph (independent modal) ── */
let _cy = null;                  // active Cytoscape instance
let _cyResizeObserver = null;    // observes the canvas to keep the graph fitted
let _relBuildingId = null;       // company id whose relationship is being built
let _relGraphCompanyId = null;   // company currently shown in the relationship modal

function _disposeCy() {
  if (_cyResizeObserver) { _cyResizeObserver.disconnect(); _cyResizeObserver = null; }
  if (_cy) { try { _cy.destroy(); } catch (_) {} _cy = null; }
}

function openRelationshipGraph(companyId) {
  const id = companyId || _modalCompanyId;
  if (!id) return;
  _relGraphCompanyId = id;
  const c = state.companies.find(x => x.id === id);
  document.getElementById("rel-graph-title").textContent =
    `${c ? shortName(c.name) : "公司"} — 母子公司關係圖`;
  const sub = document.getElementById("rel-graph-subtitle");
  if (sub) {
    const parent = c?.relationship_graph?.parent;
    if (parent) {
      const kindLbl = parent.kind === "person" ? "自然人" : "法人";
      sub.textContent = `當前錨點：${parent.name}（${kindLbl}）`;
    } else {
      sub.textContent = "尚未分析。點右上「🔗 重新分析」開始，或在公司詳情中於董監事表格點 ⊕ 指定錨點。";
    }
  }
  const btn = document.getElementById("rel-graph-rebuild-btn");
  if (btn) {
    btn.disabled = false;
    btn.textContent = c?.relationship_graph ? "🔗 重新分析" : "🔗 開始分析";
  }
  document.getElementById("rel-graph-overlay").classList.add("open");
  document.body.classList.add("rel-open");
  _expandParentRows();
  // Cytoscape needs a layout pass after the container resizes (side-by-side mode shrinks it)
  setTimeout(() => renderOwnershipGraph(id), 50);
}

function closeRelationshipGraph() {
  document.getElementById("rel-graph-overlay").classList.remove("open");
  document.body.classList.remove("rel-open");
  _disposeCy();
  _relGraphCompanyId = null;
  _collapseParentRows();
}

document.getElementById("rel-graph-overlay").addEventListener("click", e => {
  // In side-by-side mode the overlay is pointer-events:none, so this only fires when alone
  if (e.target === document.getElementById("rel-graph-overlay")) closeRelationshipGraph();
});

async function renderOwnershipGraph(companyId) {
  const wrap = document.getElementById("rel-graph-canvas");
  const noteEl = document.getElementById("rel-graph-note");
  const statusEl = document.getElementById("rel-graph-status");
  if (!wrap) return;

  _disposeCy();
  wrap.innerHTML = "";
  noteEl.textContent = "";

  let graph;
  try {
    graph = await api("GET", `/api/companies/${companyId}/ownership-graph`);
  } catch (err) {
    statusEl.textContent = `載入關係圖失敗：${err.message}`;
    return;
  }

  if (!graph.nodes || graph.nodes.length <= 1) {
    const c = state.companies.find(x => x.id === companyId);
    const hasParent = !!c?.relationship_graph?.parent;
    if (hasParent) {
      statusEl.innerHTML = `<div class="rel-empty">分析結果無關聯公司可顯示。可點上方「🔗 重新分析」重試，或在公司詳情中改選其他董事為錨點。</div>`;
    } else {
      statusEl.innerHTML = `<div class="rel-empty">尚未分析母子公司關係。點擊上方「🔗 開始分析」按鈕。</div>`;
    }
    return;
  }

  statusEl.textContent = graph.last_updated
    ? `關係資料更新時間：${new Date(graph.last_updated).toLocaleString("zh-TW")}`
    : "";
  noteEl.textContent = graph.note || "";

  if (typeof cytoscape === "undefined") {
    statusEl.textContent = "圖表元件尚未載入完成，請稍候再試";
    return;
  }

  _cy = cytoscape({
    container: wrap,
    elements: { nodes: graph.nodes, edges: graph.edges },
    layout: {
      name: "breadthfirst",
      roots: graph.nodes.filter(n => n.data.role === "parent").map(n => n.data.id),
      directed: true,
      spacingFactor: 1.4,
      padding: 24,
    },
    style: [
      {
        selector: "node",
        style: {
          "label": "data(label)",
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "wrap",
          "text-max-width": 110,
          "font-size": 12,
          "font-family": "-apple-system, 'Microsoft JhengHei', sans-serif",
          "color": "#fff",
          "background-color": "#94a3b8",
          "border-width": 2,
          "border-color": "#fff",
          "width": 100,
          "height": 50,
          "shape": "round-rectangle",
        },
      },
      { selector: 'node[role = "self"]',
        style: { "background-color": "#1d4ed8", "border-color": "#fbbf24", "border-width": 4, "width": 120, "height": 56, "font-size": 13 }
      },
      { selector: 'node[role = "parent"][kind = "legal_entity"]',
        style: { "background-color": "#7c3aed", "width": 130, "height": 56, "font-size": 13 }
      },
      { selector: 'node[role = "parent"][kind = "person"]',
        style: { "background-color": "#ea580c", "shape": "ellipse", "width": 110, "height": 60, "font-size": 13 }
      },
      { selector: 'node[role = "sibling"][in_db = true]',
        style: { "background-color": "#059669" }
      },
      { selector: 'node[role = "sibling"][in_db = false]',
        style: { "background-color": "#fff", "color": "#475569", "border-color": "#94a3b8", "border-style": "dashed", "border-width": 2 }
      },
      {
        selector: "edge",
        style: {
          "curve-style": "bezier",
          "target-arrow-shape": "triangle",
          "line-color": "#cbd5e1",
          "target-arrow-color": "#cbd5e1",
          "width": 2,
          "label": "data(ratio)",
          "font-size": 10,
          "color": "#64748b",
          "text-background-color": "#fff",
          "text-background-opacity": 1,
          "text-background-padding": 2,
        },
      },
    ],
  });

  _cy.edges().forEach(e => {
    const r = e.data("ratio");
    if (r) e.data("ratio", `${(r * 100).toFixed(2)}%`);
    else   e.data("ratio", "");
  });

  // Auto-fit graph when canvas resizes (entering/leaving side-by-side mode)
  _cyResizeObserver = new ResizeObserver(() => {
    if (!_cy) return;
    _cy.resize();
    _cy.fit(undefined, 30);
  });
  _cyResizeObserver.observe(wrap);

  _cy.on("tap", "node", evt => {
    const d = evt.target.data();
    if (d.role === "self") return;
    // Person anchor cannot be added as a company
    if (d.role === "parent" && d.kind === "person") {
      toast(`「${d.label}」是自然人，無法加入公司列表`);
      return;
    }
    if (d.in_db && d.company_id) {
      // Switch the relationship-graph modal to that company; also sync detail modal if open
      _modalCompanyId = d.company_id;
      if (document.getElementById("modal-overlay").classList.contains("open")) {
        openModal(d.company_id);
      }
      openRelationshipGraph(d.company_id);
      return;
    }
    // Not in DB → confirm dialog
    _openFromGraphDialog({
      name: d.label,
      tax_id: d.tax_id || "",
      role: d.role,
    });
  });
}

async function setAnchorDirector(idx) {
  // Open relationship modal for the current detail-modal company, then build with that anchor
  const id = _modalCompanyId;
  if (!id) return;
  openRelationshipGraph(id);
  await buildRelationship(idx);
}

async function buildRelationship(directorIndex) {
  // Prefer the relationship-graph modal's company; fall back to detail modal's
  const id = _relGraphCompanyId || _modalCompanyId;
  if (!id) return;
  if (_relBuildingId === id) return;  // already running
  _relBuildingId = id;

  // 「重新分析」: if no explicit choice, reuse the previously stored anchor
  // so a person-anchor doesn't silently fall back to auto legal-entity pick.
  if (directorIndex == null) {
    const c = state.companies.find(x => x.id === id);
    const stored = c?.relationship_graph?.director_index;
    if (Number.isInteger(stored)) directorIndex = stored;
  }

  const btn = document.getElementById("rel-graph-rebuild-btn");
  const statusEl = document.getElementById("rel-graph-status");
  if (btn) { btn.disabled = true; btn.textContent = "🔍 分析中…"; }
  if (statusEl) statusEl.innerHTML = `<div class="rel-progress">正在分析中…</div>`;

  const url = directorIndex != null && directorIndex >= 0
    ? `/api/companies/${id}/build-relationship?director_index=${directorIndex}`
    : `/api/companies/${id}/build-relationship`;

  await new Promise(resolve => {
    const es = new EventSource(url);
    es.onmessage = async e => {
      const event = JSON.parse(e.data);
      if (event.type === "progress" && statusEl) {
        statusEl.innerHTML = `<div class="rel-progress">${escHtml(event.message)}</div>`;
      } else if (event.type === "done") {
        es.close();
        try { await loadCompanies(); } catch (_) {}
        if (_relGraphCompanyId === id) {
          // Refresh subtitle with new anchor info
          const c = state.companies.find(x => x.id === id);
          const sub = document.getElementById("rel-graph-subtitle");
          const parent = c?.relationship_graph?.parent;
          if (sub && parent) {
            const kindLbl = parent.kind === "person" ? "自然人" : "法人";
            sub.textContent = `當前錨點：${parent.name}（${kindLbl}）`;
          }
          await renderOwnershipGraph(id);
        }
        // Also refresh the detail modal so the director ⊕/✓ marker updates
        if (_modalCompanyId === id && document.getElementById("modal-overlay").classList.contains("open")) {
          openModal(id);
          // If relationship graph is still open, restore expanded state that openModal reset
          if (document.getElementById("rel-graph-overlay").classList.contains("open")) {
            _expandParentRows();
          }
        }
        if (btn) { btn.disabled = false; btn.textContent = "🔗 重新分析"; }
        _relBuildingId = null;
        resolve();
      }
    };
    es.onerror = () => {
      es.close();
      if (statusEl) statusEl.innerHTML = `<div class="rel-error">分析中斷，請重試</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "🔗 重新分析"; }
      _relBuildingId = null;
      resolve();
    };
  });
}

/* ── Add From Graph Dialog ── */
let _fromGraphPayload = null;

async function _openFromGraphDialog(node) {
  await loadLabels();
  _fromGraphPayload = node;
  document.getElementById("from-graph-name").textContent = node.name;
  document.getElementById("from-graph-tax-id").textContent = node.tax_id || "（未知）";

  const labelSel = document.getElementById("from-graph-label");
  labelSel.innerHTML =
    `<option value="">（無）</option>` +
    state.labels.map(l => `<option value="${escAttr(l)}">${escHtml(l)}</option>`).join("");
  // Default to first existing label (often the originating company's primary label)
  const sourceCompany = state.companies.find(c => c.id === _modalCompanyId);
  const defaultLabel = sourceCompany?.labels?.[0] || "";
  if (defaultLabel) labelSel.value = defaultLabel;

  const indSel = document.getElementById("from-graph-industry");
  indSel.innerHTML =
    `<option value="">— 未指定 —</option>` +
    state.industries.map(i => `<option value="${escAttr(i)}">${escHtml(i)}</option>`).join("");
  if (sourceCompany?.industry) indSel.value = sourceCompany.industry;

  document.getElementById("from-graph-overlay").classList.add("open");
}

function _closeFromGraphDialog() {
  document.getElementById("from-graph-overlay").classList.remove("open");
  _fromGraphPayload = null;
}

document.getElementById("from-graph-cancel").addEventListener("click", _closeFromGraphDialog);
document.getElementById("from-graph-overlay").addEventListener("click", e => {
  if (e.target === document.getElementById("from-graph-overlay")) _closeFromGraphDialog();
});

document.getElementById("from-graph-ok").addEventListener("click", async () => {
  if (!_fromGraphPayload) return;
  const sourceId = _relGraphCompanyId || _modalCompanyId;
  const payload = {
    name: _fromGraphPayload.name,
    tax_id: _fromGraphPayload.tax_id || null,
    label: document.getElementById("from-graph-label").value || "",
    industry: document.getElementById("from-graph-industry").value || "",
    source_company_id: sourceId || null,
  };
  _closeFromGraphDialog();

  try {
    const res = await api("POST", "/api/companies/from-graph", payload);
    if (res.existed) {
      toast(`公司已存在：${res.name}`);
      await loadCompanies();
      _modalCompanyId = res.company_id;
      openModal(res.company_id);
      return;
    }
    toast(`已加入「${res.name}」，正在自動生成內容…`);
    await loadCompanies();
    computeGroups();
    renderSidebar();
    renderGrid();
    // Subscribe to enrichment progress; when done, refresh the source graph so the new node turns green
    subscribeEnrichment(res.company_id).then(async () => {
      if (sourceId && _relGraphCompanyId === sourceId) {
        await renderOwnershipGraph(sourceId);
      }
    });
  } catch (err) {
    toast(`加入失敗：${err.message}`, true);
  }
});

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

// Middle-truncate labels longer than 4 chars: keep first 2 + "…" + last 2.
// Always call escHtml on the result; pass full label as title for tooltip.
function truncLabel(label) {
  if (!label || label.length <= 4) return label;
  return label.slice(0, 2) + "…" + label.slice(-2);
}

/* ── Init ── */
boot();
