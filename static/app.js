/* ── State ── */
const state = {
  companies: [],
  industries: [],
  labels: [],
  groups: {},                    // {industry: [group, ...]}
  expandedIndustries: new Set(),
  activeIndustry: null,          // null = all
  activeGroup: null,
  activeLabel: null,
  activeLabelIndustry: null,
  expandedLabels: new Set(),
  activeTab: "all",              // "all" | "watched"
  sortBy: "capital",
  sortDir: "desc",
  searchQuery: "",
  pendingCandidates: [],
  pendingUncertain: [],
  pendingLabel: "",
  enrichingIds: new Set(),
  doneIds: new Set(),
  pinnedItems: new Set(JSON.parse(localStorage.getItem("pinnedItems") || "[]")),
  labelGroups: {},               // {AAMA: ["AAMA-1", ...]} persisted in config
  expandedLabelGroups: new Set(),
  activeLabelGroup: null,        // parent group filter (union of all child labels)
  dismissedGroupSuggestions: new Set(), // session-only
  sidePanelTab: "industry",
  sidePanelSearch: "",
  sidePanelSort: "alpha",
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

/* ── News blacklist / dismiss ── */
const _dismissedUrls = new Set();

function _showAiHint(msg) {
  let el = document.getElementById("ai-hint-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "ai-hint-toast";
    el.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:9999;background:#1d4ed8;color:#fff;padding:10px 16px;border-radius:8px;font-size:12px;display:flex;align-items:center;gap:8px;box-shadow:0 4px 16px rgba(0,0,0,.25);transition:opacity .3s;max-width:360px;";
    document.body.appendChild(el);
  }
  el.innerHTML = `<span>🤖</span><span>${msg}</span>`;
  el.style.opacity = "1";
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.style.opacity = "0"; }, 6000);
}

async function dismissArticle(url, title, source, btn) {
  _dismissedUrls.add(url);
  const item = btn.closest(".daily-news-item");
  if (item) { item.style.opacity = "0"; item.style.height = "0"; item.style.overflow = "hidden"; item.style.transition = "opacity .2s, height .3s"; }
  try {
    const data = await api("POST", "/api/news/dismiss", { url, title, source });
    if (data.dismissed_count % 5 === 0 && data.rules?.ai_summary) {
      _showAiHint("AI 已更新過濾規則：" + data.rules.ai_summary);
    }
  } catch { /* article already hidden */ }
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

  await Promise.all([loadIndustries(), loadCompanies(), loadLabels(), loadLabelGroups()]);
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

async function loadLabelGroups() {
  state.labelGroups = await api("GET", "/api/config/label-groups");
}

async function loadCompanies() {
  state.companies = await api("GET", "/api/companies");
  updateWatchCount();
}

function updateWatchCount() {
  // 算「當前 sidebar scope 內」的追蹤數，讓 tab 上的 (N) 等於點下去看到的數量
  let pool = state.companies;
  if (state.activeLabelGroup) {
    const gLabels = (state.labelGroups[state.activeLabelGroup] || []).filter(l => state.pinnedItems.has(l));
    pool = pool.filter(c => (c.labels || []).some(l => gLabels.includes(l)));
  } else if (state.activeLabel) {
    pool = pool.filter(c => (c.labels || []).includes(state.activeLabel));
    if (state.activeLabelIndustry === "__none__") {
      pool = pool.filter(c => !c.industry);
    } else if (state.activeLabelIndustry) {
      pool = pool.filter(c => c.industry === state.activeLabelIndustry);
    }
  } else if (state.activeIndustry) {
    pool = pool.filter(c => c.industry === state.activeIndustry);
    if (state.activeGroup === "__ungrouped__") {
      pool = pool.filter(c => !c.labels || c.labels.length === 0);
    } else if (state.activeGroup) {
      pool = pool.filter(c => (c.labels || []).includes(state.activeGroup));
    }
  }
  const n = pool.filter(c => c.watched).length;
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
  // 全部公司 / 面板入口
  const mainBtn = document.getElementById("sb-main-btn");
  const isAll = state.activeIndustry === null && state.activeLabel === null && state.activeLabelGroup === null;
  mainBtn.className = "sb-row" + (isAll ? " active" : "");
  document.getElementById("sb-all-count").textContent = state.companies.length;

  // 未分類警示
  const unclassifiedCount = state.companies.filter(c => !c.industry).length;
  const uncWrap = document.getElementById("sb-unclassified-wrap");
  if (unclassifiedCount > 0) {
    uncWrap.innerHTML = `
      <div class="sb-unclassified">
        <span class="sb-unc-dot"></span>
        <span class="sb-unc-label">${unclassifiedCount} 間未分類</span>
        <button class="sb-unc-btn">✨ 自動分類</button>
      </div>`;
    uncWrap.querySelector(".sb-unc-btn").addEventListener("click", e => {
      e.stopPropagation();
      runClassify();
    });
  } else {
    uncWrap.innerHTML = "";
  }

  const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))];

  // 釘選區
  const pinnedEl = document.getElementById("pinned-sidebar");
  const nat = (a, b) => a.localeCompare(b, "zh-TW", { numeric: true });
  const pinnedIndustries = state.industries.filter(i => state.pinnedItems.has(i)).sort(nat);
  const pinnedLabels = allLabels.filter(l => state.pinnedItems.has(l)).sort(nat);

  if (pinnedIndustries.length === 0 && pinnedLabels.length === 0) {
    pinnedEl.innerHTML = `<div style="padding:6px 14px;font-size:12px;color:var(--sb-muted);font-style:italic">點面板中的 ☆ 釘選常用項目</div>`;
  } else {
    // Build label → group reverse map
    const labelToGroup = {};
    for (const [gName, gLabels] of Object.entries(state.labelGroups)) {
      for (const l of gLabels) labelToGroup[l] = gName;
    }

    // Partition pinned labels: grouped vs standalone
    // A group only shows its parent row when ≥2 of its members are currently pinned;
    // a lone pinned member falls back to the standalone list.
    const groupedPinned = {};  // {groupName: [child labels that are pinned]}
    const standalonePinned = [];
    for (const label of pinnedLabels) {
      const g = labelToGroup[label];
      if (g) {
        if (!groupedPinned[g]) groupedPinned[g] = [];
        groupedPinned[g].push(label);
      } else {
        standalonePinned.push(label);
      }
    }
    // Demote single-member groups to standalone
    for (const [gName, gLabels] of Object.entries(groupedPinned)) {
      if (gLabels.length < 2) {
        standalonePinned.push(...gLabels);
        delete groupedPinned[gName];
      }
    }

    // Detect group suggestions from all pinned labels
    const suggestions = _detectLabelGroupSuggestions(pinnedLabels);

    let html = "";

    // Suggestion banners (non-blocking, inline)
    for (const { prefix, labels, mode } of suggestions) {
      const sorted = [...labels].sort(nat);
      const dismissKey = mode === "extend" ? prefix + ":" + sorted.join(",") : prefix;
      let msg;
      if (mode === "extend") {
        const tails = sorted.map(l => l.replace(/^.+?[-_]/, "")).join("、");
        msg = `💡 <b>${escHtml(prefix)}-${escHtml(tails)}</b> 可加入既有 <b>${escHtml(prefix)}</b> 群組`;
      } else {
        const first = sorted[0].replace(/^.+?[-_]/, "");
        const last  = sorted[sorted.length - 1].replace(/^.+?[-_]/, "");
        msg = `💡 <b>${escHtml(prefix)}-${escHtml(first)}~${escHtml(last)}</b> 可歸攏`;
      }
      const acceptLabel = mode === "extend" ? "加入" : "歸攏";
      html += `<div class="lgs-banner" data-prefix="${escHtml(prefix)}" data-mode="${mode}">
        ${msg}
        <button class="lgs-accept" data-prefix="${escHtml(prefix)}" data-mode="${mode}" data-dismiss-key="${escHtml(dismissKey)}">${acceptLabel}</button>
        <button class="lgs-dismiss" data-prefix="${escHtml(prefix)}" data-dismiss-key="${escHtml(dismissKey)}">略過</button>
      </div>`;
    }

    // Industry pinned rows
    if (pinnedIndustries.length > 0) {
      html += `<div class="sb-section-label">產業別</div>`;
      for (const name of pinnedIndustries) {
        const count = state.companies.filter(c => c.industry === name).length;
        const isActive = state.activeIndustry === name && state.activeGroup === null;
        html += `<div class="sb-row ${isActive ? "active" : ""}" data-pinned="${escHtml(name)}" data-is-label="false">
          <span class="sb-label">${escHtml(name)}</span>
          <span class="sb-count">${count}</span>
        </div>`;
      }
    }

    // Label pinned rows (grouped + standalone)
    if (pinnedLabels.length > 0) {
      html += `<div class="sb-section-label">標籤</div>`;

      // Grouped parent rows (sorted naturally by group name)
      for (const gName of Object.keys(groupedPinned).sort(nat)) {
        const gChildLabels = groupedPinned[gName].sort(nat);
        const expanded = state.expandedLabelGroups.has(gName);
        const totalCount = state.companies.filter(c =>
          (c.labels || []).some(l => gChildLabels.includes(l))
        ).length;
        const isGroupActive = state.activeLabelGroup === gName;
        html += `<div class="sb-row sb-lg-parent ${isGroupActive ? "active-label" : ""}" data-label-group="${escHtml(gName)}">
          <span class="sb-label">${escHtml(gName)}<span class="sb-lg-arrow">${expanded ? "▴" : "▾"}</span></span>
          <span class="sb-count">${totalCount}</span>
        </div>`;
        if (expanded) {
          for (const child of gChildLabels) {
            const childCount = state.companies.filter(c => (c.labels || []).includes(child)).length;
            const isChildActive = state.activeLabel === child;
            html += `<div class="sb-row sb-lg-child ${isChildActive ? "active-label" : ""}" data-pinned="${escHtml(child)}" data-is-label="true">
              <span class="sb-label">${escHtml(child)}</span>
              <span class="sb-count">${childCount}</span>
            </div>`;
          }
        }
      }

      // Standalone pinned labels
      for (const name of standalonePinned) {
        const count = state.companies.filter(c => (c.labels || []).includes(name)).length;
        const isActive = state.activeLabel === name;
        html += `<div class="sb-row ${isActive ? "active-label" : ""}" data-pinned="${escHtml(name)}" data-is-label="true">
          <span class="sb-label">${escHtml(name)}</span>
          <span class="sb-count">${count}</span>
        </div>`;
      }
    }

    pinnedEl.innerHTML = html;

    // Suggestion banner buttons
    pinnedEl.querySelectorAll(".lgs-accept").forEach(btn => {
      btn.addEventListener("click", async () => {
        const prefix = btn.dataset.prefix;
        const mode = btn.dataset.mode;
        const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))];
        let members;
        if (mode === "extend") {
          const existing = state.labelGroups[prefix] || [];
          const pinnedMatching = allLabels.filter(l =>
            state.pinnedItems.has(l) && /^(.+?)[-_]\d+$/.exec(l)?.[1] === prefix
          );
          members = [...new Set([...existing, ...pinnedMatching])].sort(nat);
        } else {
          members = allLabels.filter(l =>
            state.pinnedItems.has(l) && /^(.+?)[-_]\d+$/.exec(l)?.[1] === prefix
          ).sort(nat);
        }
        await api("POST", "/api/config/label-groups", { name: prefix, labels: members });
        state.labelGroups[prefix] = members;
        renderSidebar();
        renderGrid();
      });
    });
    pinnedEl.querySelectorAll(".lgs-dismiss").forEach(btn => {
      btn.addEventListener("click", () => {
        state.dismissedGroupSuggestions.add(btn.dataset.dismissKey || btn.dataset.prefix);
        renderSidebar();
      });
    });

    // Parent group row click: toggle expand + set activeLabelGroup
    pinnedEl.querySelectorAll(".sb-lg-parent").forEach(row => {
      row.addEventListener("click", () => {
        const gName = row.dataset.labelGroup;
        if (state.expandedLabelGroups.has(gName)) {
          state.expandedLabelGroups.delete(gName);
        } else {
          state.expandedLabelGroups.add(gName);
        }
        state.activeLabelGroup = state.activeLabelGroup === gName ? null : gName;
        state.activeLabel = null;
        state.activeLabelIndustry = null;
        state.activeIndustry = null;
        state.activeGroup = null;
        state.activeTab = "all";
        document.querySelectorAll(".tab-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.tab === "all"));
        renderSidebar();
        renderGrid();
      });
    });

    // Individual label rows (standalone + children)
    pinnedEl.querySelectorAll(".sb-row[data-pinned]").forEach(row => {
      row.addEventListener("click", () => {
        const name = row.dataset.pinned;
        const isLabel = row.dataset.isLabel === "true";
        if (isLabel) {
          state.activeLabel = state.activeLabel === name ? null : name;
          state.activeLabelGroup = null;
          state.activeLabelIndustry = null;
          state.activeIndustry = null;
          state.activeGroup = null;
          state.activeTab = "all";
          document.querySelectorAll(".tab-btn").forEach(b =>
            b.classList.toggle("active", b.dataset.tab === "all"));
        } else {
          state.activeIndustry = state.activeIndustry === name ? null : name;
          state.activeGroup = null;
          state.activeLabel = null;
          state.activeLabelGroup = null;
          state.activeLabelIndustry = null;
        }
        renderSidebar();
        renderGrid();
      });
    });
  }
}

function _detectLabelGroupSuggestions(pinnedLabels) {
  const re = /^(.+?)[-_](\d+)$/;
  const prefixMap = {};
  for (const label of pinnedLabels) {
    const m = label.match(re);
    if (!m) continue;
    const prefix = m[1];
    if (!prefixMap[prefix]) prefixMap[prefix] = [];
    prefixMap[prefix].push(label);
  }
  const out = [];
  for (const [prefix, labels] of Object.entries(prefixMap)) {
    const existing = state.labelGroups[prefix];
    if (existing) {
      // 既有 group：找出 pinned 中還沒納入的成員
      const missing = labels.filter(l => !existing.includes(l));
      if (missing.length === 0) continue;
      const sortedMissing = missing.slice().sort((a, b) => a.localeCompare(b, "zh-TW", { numeric: true }));
      if (state.dismissedGroupSuggestions.has(prefix + ":" + sortedMissing.join(","))) continue;
      out.push({ prefix, labels: sortedMissing, mode: "extend", existing });
    } else {
      // 新 group：維持既有規則（≥2 個成員）
      if (labels.length < 2) continue;
      if (state.dismissedGroupSuggestions.has(prefix)) continue;
      out.push({ prefix, labels, mode: "create" });
    }
  }
  return out;
}

/* ── Side Panel ── */
function _clearFilter() {
  state.activeIndustry = null;
  state.activeGroup = null;
  state.activeLabel = null;
  state.activeLabelIndustry = null;
  state.activeLabelGroup = null;
  renderSidebar();
  renderSidePanel();
  renderGrid();
}

function openSidePanel() {
  document.getElementById("side-panel").classList.add("open");
  document.getElementById("side-panel-backdrop").classList.add("open");
  document.getElementById("main").classList.add("side-panel-open");
  _renderSidePanelToolbar();
  renderSidePanel();
}
function closeSidePanel() {
  document.getElementById("side-panel").classList.remove("open");
  document.getElementById("side-panel-backdrop").classList.remove("open");
  document.getElementById("main").classList.remove("side-panel-open");
}

function _renderSidePanelToolbar() {
  const isPinned = state.sidePanelTab === "pinned";
  document.getElementById("sp-search").style.display = isPinned ? "none" : "";
  document.getElementById("sp-sort").style.display = isPinned ? "none" : "";
  const addBtn = document.getElementById("sp-add-btn");
  addBtn.classList.toggle("visible", state.sidePanelTab === "industry");
}

function renderSidePanel() {
  const list = document.getElementById("sp-list");
  const q = state.sidePanelSearch.toLowerCase();

  if (state.sidePanelTab === "pinned") {
    const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))];
    const _nat = (a, b) => a.localeCompare(b, "zh-TW", { numeric: true });
    const pinnedInds = state.industries.filter(i => state.pinnedItems.has(i)).sort(_nat);
    const pinnedLbls = allLabels.filter(l => state.pinnedItems.has(l)).sort(_nat);
    if (pinnedInds.length === 0 && pinnedLbls.length === 0) {
      list.innerHTML = `<div class="sp-empty">尚未釘選任何項目。<br>切到「產業別」或「標籤」分頁，點 ☆ 即可釘選。</div>`;
      return;
    }
    const renderGroup = (items, isLabel) => items.map(name => {
      const count = isLabel
        ? state.companies.filter(c => (c.labels || []).includes(name)).length
        : state.companies.filter(c => c.industry === name).length;
      return `<div class="sp-item">
        <span class="sp-name">${escHtml(name)}</span>
        <span class="sp-count">${count}</span>
        <button class="sp-pin-btn pinned" data-name="${escHtml(name)}" title="取消釘選">★</button>
      </div>`;
    }).join("");
    list.innerHTML = `
      ${pinnedInds.length > 0 ? `<div class="sp-section-label">產業別</div>${renderGroup(pinnedInds, false)}` : ""}
      ${pinnedLbls.length > 0 ? `<div class="sp-section-label">標籤</div>${renderGroup(pinnedLbls, true)}` : ""}
    `;
    list.querySelectorAll(".sp-pin-btn").forEach(btn => {
      btn.addEventListener("click", e => { e.stopPropagation(); toggleSidePin(btn.dataset.name); });
    });
    return;
  }

  const isIndustryTab = state.sidePanelTab === "industry";
  let items;
  if (isIndustryTab) {
    items = state.industries.map(name => ({
      name,
      count: state.companies.filter(c => c.industry === name).length,
    }));
  } else {
    const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))];
    items = allLabels.map(name => ({
      name,
      count: state.companies.filter(c => (c.labels || []).includes(name)).length,
    }));
  }

  if (q) items = items.filter(x => x.name.toLowerCase().includes(q));
  if (state.sidePanelSort === "count") items.sort((a, b) => b.count - a.count);
  else items.sort((a, b) => a.name.localeCompare(b.name, "zh-TW", { numeric: true }));

  // 全部公司頂列
  const isAllActive = state.activeIndustry === null && state.activeLabel === null;
  const allRow = `<div class="sp-item ${isAllActive ? "active-filter" : ""}" id="sp-all-row">
    <span class="sp-name" style="font-weight:600">全部公司</span>
    <span class="sp-count">${state.companies.length}</span>
  </div>`;

  if (items.length === 0) {
    list.innerHTML = allRow + `<div class="sp-empty">${q ? "無符合的項目" : "尚無資料"}</div>`;
    list.querySelector("#sp-all-row").addEventListener("click", _clearFilter);
    return;
  }

  list.innerHTML = allRow + items.map(x => {
    const isActive = isIndustryTab
      ? state.activeIndustry === x.name
      : state.activeLabel === x.name;
    const pinned = state.pinnedItems.has(x.name);
    const actions = isIndustryTab
      ? `<span class="sp-actions">
           <button class="sp-rename-btn" data-name="${escHtml(x.name)}" title="重新命名">✏️</button>
           <button class="sp-delete-btn" data-name="${escHtml(x.name)}" title="刪除">🗑</button>
         </span>`
      : "";
    return `<div class="sp-item ${isActive ? (isIndustryTab ? "active-filter" : "active-filter-label") : ""}"
               data-name="${escHtml(x.name)}" data-is-label="${!isIndustryTab}">
      <span class="sp-name">${escHtml(x.name)}</span>
      <span class="sp-count">${x.count}</span>
      ${actions}
      <button class="sp-pin-btn ${pinned ? "pinned" : ""}" data-name="${escHtml(x.name)}" title="${pinned ? "取消釘選" : "釘選到側欄"}">${pinned ? "★" : "☆"}</button>
    </div>`;
  }).join("");

  list.querySelector("#sp-all-row")?.addEventListener("click", _clearFilter);

  list.querySelectorAll(".sp-item:not(#sp-all-row)").forEach(item => {
    item.addEventListener("click", e => {
      if (e.target.closest(".sp-pin-btn") || e.target.closest(".sp-actions")) return;
      const name = item.dataset.name;
      const isLabel = item.dataset.isLabel === "true";
      if (isLabel) {
        state.activeLabel = state.activeLabel === name ? null : name;
        state.activeLabelIndustry = null;
        state.activeIndustry = null;
        state.activeGroup = null;
        state.activeTab = "all";
        document.querySelectorAll(".tab-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.tab === "all"));
      } else {
        state.activeIndustry = state.activeIndustry === name ? null : name;
        state.activeGroup = null;
        state.activeLabel = null;
        state.activeLabelIndustry = null;
      }
      renderSidebar();
      renderSidePanel();
      renderGrid();
    });
  });

  list.querySelectorAll(".sp-pin-btn").forEach(btn => {
    btn.addEventListener("click", e => { e.stopPropagation(); toggleSidePin(btn.dataset.name); });
  });

  if (isIndustryTab) {
    list.querySelectorAll(".sp-rename-btn").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        const oldName = btn.dataset.name;
        const nameEl = btn.closest(".sp-item").querySelector(".sp-name");
        startRenameIndustryInPanel(nameEl, oldName);
      });
    });
    list.querySelectorAll(".sp-delete-btn").forEach(btn => {
      btn.addEventListener("click", async e => {
        e.stopPropagation();
        const name = btn.dataset.name;
        if (!confirm(`確定要刪除產業別「${name}」嗎？`)) return;
        await api("DELETE", `/api/config/industries/${encodeURIComponent(name)}`);
        if (state.activeIndustry === name) { state.activeIndustry = null; state.activeGroup = null; }
        state.expandedIndustries.delete(name);
        state.pinnedItems.delete(name);
        _savePinnedItems();
        await loadIndustries();
        renderSidebar();
        renderSidePanel();
        renderGrid();
      });
    });
  }
}

function toggleSidePin(name) {
  if (state.pinnedItems.has(name)) state.pinnedItems.delete(name);
  else state.pinnedItems.add(name);
  _savePinnedItems();
  renderSidebar();
  renderSidePanel();
}

function _savePinnedItems() {
  localStorage.setItem("pinnedItems", JSON.stringify([...state.pinnedItems]));
}

function startRenameIndustryInPanel(nameEl, oldName) {
  const input = document.createElement("input");
  input.className = "industry-edit-input";
  input.value = oldName;
  input.style.cssText = "font-size:13px;padding:2px 6px;border:1px solid #3b82f6;border-radius:4px;width:100%;outline:none;";
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      await api("PUT", "/api/config/industries", { old_name: oldName, new_name: newName });
      if (state.activeIndustry === oldName) state.activeIndustry = newName;
      if (state.pinnedItems.has(oldName)) {
        state.pinnedItems.delete(oldName);
        state.pinnedItems.add(newName);
        _savePinnedItems();
      }
      await Promise.all([loadIndustries(), loadCompanies()]);
      computeGroups();
    }
    renderSidebar();
    renderSidePanel();
    renderGrid();
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { input.value = oldName; input.blur(); }
  });
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

document.getElementById("sp-add-btn").addEventListener("click", async () => {
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
      const unchecked = rows.querySelectorAll("input[type=checkbox]").length;
      if (unchecked > 0)
        toast(`Claude 未能為 ${unchecked} 間公司找到適合的產業別，請在對話框中手動選擇後再確認`, true);
      else
        toast("未套用任何分類");
      return;
    }

    try {
      await api("PUT", "/api/companies/batch-industry", { updates });
      await loadCompanies();
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
        <button class="daily-news-dismiss" title="不想看這篇" onclick="dismissArticle(${JSON.stringify(a.url)},${JSON.stringify(a.title)},${JSON.stringify(a.source)},this)">×</button>
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
      companies = companies.filter(c => !c.industry);
      scopeTitle = `${state.activeLabel} — 未分類`;
    } else if (state.activeLabelIndustry) {
      companies = companies.filter(c => c.industry === state.activeLabelIndustry);
      scopeTitle = `${state.activeLabel} — ${state.activeLabelIndustry}`;
    } else {
      scopeTitle = `標籤：${state.activeLabel}`;
    }
  } else if (state.activeIndustry) {
    companies = companies.filter(c => c.industry === state.activeIndustry);
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
    ? "✦ 用 Opus 4.7 重新生成（納入新資料）"
    : "✦ 用 Opus 4.7 更新公司簡介";
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
  const when = generatedAt ? new Date(generatedAt).toLocaleString("zh-TW", { hour12: false }) : "";
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
      us.textContent = "✅ 已移除檔案。內容已變動，建議按「✦ 用 Opus 4.7 重新生成」更新簡介。";
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
  if (isCloudDeploy() && !getAiKey()) {
    toast("請先在設定中輸入 API Key（建議 Gemini，免費）");
    openSettings();
    return;
  }
  // Persist the 訪談備忘錄 fields first so the backend reads the latest content.
  await saveMemo(true);

  const status = document.getElementById("materials-gen-status");
  const btn = document.getElementById("materials-gen-btn");

  // Animated, reassuring "still working" indicator with an elapsed-time counter
  let elapsed = 0;
  const renderProgress = () => {
    status.innerHTML =
      `<span class="mat-spinner"></span>` +
      `<span>Opus 4.7 正在閱讀所有補充資料並更新簡介` +
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
    const fields = await api("POST", `/api/companies/${id}/materials/generate`);
    const c = state.companies.find(x => x.id === id);
    if (c) Object.assign(c, fields);
    _renderMaterialsResult(fields.materials_summary || "", fields.materials_generated_at || "");
    status.textContent = "✅ 已生成，請於審核框選取要套用的段落";
    status.className = "memo-status-ok";
    openMatReview(fields.materials_summary || "");
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
  document.getElementById("mat-review-overlay").classList.add("open");
}

function closeMatReview() {
  document.getElementById("mat-review-overlay").classList.remove("open");
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
      status.textContent = `✅ 已新增 ${data.saved.length} 個檔案。內容已變動，請按下方「✦ 用 Opus 4.7 重新生成」以納入新檔案（只按「重新審核」不會讀到新檔）。`;
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

/* ── Tabs ── */
document.getElementById("tab-group").addEventListener("click", e => {
  const btn = e.target.closest(".tab-btn");
  if (!btn) return;
  // 切 tab 只改 activeTab，保留 sidebar 的 scope（產業 / 標籤 / 群組）
  // 「追蹤」是當前 scope 下的子集過濾，不是跨產業視圖
  state.activeTab = btn.dataset.tab;
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

  const supBtnHtml = `<button id="materials-open-btn" onclick="openMaterialsPanel()">➕ 補充資料</button>`;
  document.getElementById("modal-name").innerHTML =
    escHtml(shortName(c.name)) + listingBadge(c.listing_status) + supBtnHtml;

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
      const repBadge = d.representative_of
        ? (d.representative_of_listing
            ? listingBadge(d.representative_of_listing)
            : _dirRepListingBadge(d.representative_of, d.representative_of_tax_id))
        : "";
      const nameBadge = (!d.representative_of && d.name_listing) ? listingBadge(d.name_listing) : "";
      return `
      <tr${isActive ? ' class="director-row-active"' : ""} data-dir-idx="${i}">
        <td>${escHtml(d.title || "—")}</td>
        <td>${escHtml(d.name || "—")}${badge}${nameBadge}</td>
        <td>${escHtml(d.representative_of || "—")}${repBadge}</td>
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
    c.summary ? renderSummary(c.summary, c.materials_applied_headings) : "<p class=\"summary-placeholder\">（公司簡介資料補充中，請稍後重整）</p>";
  summaryEl.style.display = "";   // visible within its page; the bookmark controls page visibility
  applySummaryTabs(summaryEl);    // 公司簡介內部橫向分頁

  // Patents: render rows if data exists (section visibility handled by bookmarks)
  const patentStatus = document.getElementById("modal-patents-status");
  const patTable = document.getElementById("modal-patents-table");
  if (c.patents && c.patents.length) {
    if (patentStatus) patentStatus.innerHTML = "";
    if (patTable) patTable.style.display = "";
    _renderPatents(id);
  } else {
    if (patTable) patTable.style.display = "none";
    // 清掉前一間殘留的列與「共 N 筆」，否則 badge 與抬頭會沿用上一間的數字
    const patBody = document.getElementById("modal-patents-body");
    if (patBody) patBody.innerHTML = "";
    const patHint = document.getElementById("modal-patents-hint");
    if (patHint) patHint.textContent = "";
    if (patentStatus) patentStatus.innerHTML = '<p class="summary-placeholder">尚未生成專利資料，點右上「📋 生成專利」開始（約 15–45 秒）。</p>';
  }

  _refreshModalBookmarks();
  showModalSection("info");   // default page

  document.getElementById("modal-overlay").classList.add("open");
  document.body.classList.add("detail-open");
}

/* ── Modal 左側 3 頁籤導覽 ── */
// A section is "empty" (hide even within its page) when it has no rendered data.
function _modalSectionEmpty(sec) {
  if (sec.id === "modal-directors-section")
    return (document.getElementById("modal-directors")?.childElementCount || 0) === 0;
  if (sec.id === "modal-shareholders-section")
    return !(document.getElementById("modal-shareholder-content")?.innerHTML.trim());
  // 專利頁永遠顯示（空的時候顯示「生成專利」按鈕 + 提示），故不視為空
  return false;  // 基本資料 / 公司簡介 / 專利 are never page-empty
}

function showModalSection(page) {
  const bm = document.querySelector(`#modal-nav .modal-bm[data-page="${page}"]`);
  if (bm && bm.classList.contains("disabled")) return;   // 灰掉的頁籤不動作
  document.querySelectorAll("#modal-content .modal-section").forEach(s => {
    s.style.display = (s.dataset.page === page && !_modalSectionEmpty(s)) ? "" : "none";
  });
  document.querySelectorAll("#modal-nav .modal-bm").forEach(b => {
    b.classList.toggle("on", b.dataset.page === page);
  });
  const content = document.getElementById("modal-content");
  if (content) content.scrollTop = 0;
}

// Update count badges. All three pages are always clickable (專利 page lets you
// generate when empty), so no bookmark is greyed out.
function _refreshModalBookmarks() {
  // 排除「展開全部」那一列 fold row，只算真正的專利列
  const patCount = document.getElementById("modal-patents-body")
    ?.querySelectorAll("tr:not(#patent-fold-row)").length || 0;
  document.querySelectorAll("#modal-nav .modal-bm").forEach(b => b.classList.remove("disabled"));
  const patBadge = document.getElementById("bm-ct-patents");
  if (patBadge) { patBadge.textContent = patCount || ""; patBadge.style.display = patCount ? "" : "none"; }
}

// Split the rendered 公司簡介 into horizontal sub-tabs (one section shown at a time).
function applySummaryTabs(container) {
  const tabbar = document.getElementById("summary-tabbar");
  if (!tabbar) return;
  tabbar.innerHTML = "";
  // Each top-level section = a bare <h3>(+siblings) OR a .summary-mat-section wrapper.
  const sections = [];   // { title, isMat, nodes: [...] }
  let cur = null;
  for (const node of [...container.children]) {
    if (node.classList && node.classList.contains("summary-mat-section")) {
      const h3 = node.querySelector("h3");
      sections.push({ title: _cleanHeading(h3), isMat: true, nodes: [node] });
      cur = null;
    } else if (node.tagName === "H3") {
      cur = { title: _cleanHeading(node), isMat: false, nodes: [node] };
      sections.push(cur);
    } else if (cur) {
      cur.nodes.push(node);
    }
  }
  if (sections.length < 1) { tabbar.style.display = "none"; return; }
  tabbar.style.display = "";

  // wrap each section's nodes in a page div, hidden by default
  sections.forEach((sec, i) => {
    const page = document.createElement("div");
    page.className = "summary-tab-page";
    page.dataset.idx = i;
    sec.nodes[0].before(page);
    sec.nodes.forEach(n => page.appendChild(n));
    if (sec.title === "競業分析") _setupCompetitorTabs(page);
    page.style.display = i === 0 ? "" : "none";
    const tab = document.createElement("div");
    tab.className = "summary-tab" + (i === 0 ? " on" : "");
    // tab IS the section heading (the in-page h3 is hidden); 簡報段落帶 📎
    tab.innerHTML = `<span>${escHtml(sec.title || ("第 " + (i + 1) + " 段"))}</span>` +
      (sec.isMat ? `<span class="summary-tab-clip" title="含補充資訊">${_CLIP_SVG}</span>` : "");
    tab.onclick = () => _showSummaryTab(container, i);
    tabbar.appendChild(tab);
  });
}

function _cleanHeading(h3) {
  if (!h3) return "";
  // text without the「簡報」chip / 📎 icon
  return (h3.textContent || "").replace(/\s+/g, " ").trim();
}

function _showSummaryTab(container, idx) {
  container.querySelectorAll(".summary-tab-page").forEach(p => {
    p.style.display = (p.dataset.idx == idx) ? "" : "none";
  });
  document.querySelectorAll("#summary-tabbar .summary-tab").forEach((t, i) => {
    t.classList.toggle("on", i == idx);
  });
}

/* ── 競業分析：四種類型 sub-tab + 手動新增 ── */
const _COMP_TYPES = ["正面競業", "替代路徑", "側翼潛入", "垂直整合"];

function _setupCompetitorTabs(page) {
  const table = page.querySelector(".competitor-table");
  if (!table) { page.appendChild(_addCompetitorForm()); return; }

  // Legacy 4-column tables have no 競業類型 → no type sub-tabs, just the add button.
  const headers = [...table.querySelectorAll("thead th")].map(h => h.textContent.trim());
  if (!headers.some(h => h.includes("競業類型"))) {
    const w = document.createElement("div");
    w.className = "comp-add-only";
    const add = document.createElement("button");
    add.className = "add-comp-btn";
    add.textContent = "＋ 新增競業";
    add.onclick = function () { toggleAddCompetitor(this); };
    w.appendChild(add);
    table.before(w);
    w.after(_addCompetitorForm());
    return;
  }

  // tag each body row with its 競業類型 (last cell); 本案 row always shown.
  // 本案列只認第一欄（公司名稱）的「（本案）」標記——不能用整列含「本案」判斷，
  // 否則差異化特點寫到「本案客戶」之類的競業列會被誤判成本案列。
  const rows = [...table.querySelectorAll("tbody tr")];
  rows.forEach(tr => {
    const cells = tr.querySelectorAll("td");
    const firstCell = cells.length ? cells[0].textContent : "";
    if (firstCell.includes("（本案）")) { tr.dataset.ctype = "__case__"; return; }
    const last = cells.length ? cells[cells.length - 1].textContent.trim() : "";
    tr.dataset.ctype = _COMP_TYPES.includes(last) ? last : "其他";
  });

  // type bar (left) + 新增競業 button (right), placed just above the table
  const bar = document.createElement("div");
  bar.className = "comp-type-bar";
  _COMP_TYPES.forEach((t, i) => {
    const n = rows.filter(r => r.dataset.ctype === t).length;
    const tab = document.createElement("div");
    tab.className = "comp-type-tab" + (i === 0 ? " on" : "");
    tab.dataset.type = t;
    tab.innerHTML = `${t}<span class="comp-type-ct">${n}</span>`;
    tab.onclick = () => _showCompetitorType(page, t);
    bar.appendChild(tab);
  });
  const add = document.createElement("button");
  add.className = "add-comp-btn comp-bar-add";
  add.textContent = "＋ 新增競業";
  add.onclick = function () { toggleAddCompetitor(this); };
  bar.appendChild(add);
  table.before(bar);
  bar.after(_addCompetitorForm());   // hidden form right below the bar

  _showCompetitorType(page, _COMP_TYPES[0]);   // default 正面競業
}

function _showCompetitorType(page, type) {
  page.querySelectorAll(".competitor-table tbody tr").forEach(tr => {
    const ct = tr.dataset.ctype;
    tr.style.display = (ct === "__case__" || ct === type || !_COMP_TYPES.includes(ct)) ? "" : "none";
  });
  page.querySelectorAll(".comp-type-bar .comp-type-tab").forEach(t => {
    t.classList.toggle("on", t.dataset.type === type);
  });
  page.dataset.activeCtype = type;
}

function _addCompetitorForm() {
  const wrap = document.createElement("div");
  wrap.className = "add-comp-wrap";
  wrap.innerHTML =
    `<div class="add-comp-form" style="display:none">` +
      `<input class="add-comp-name" placeholder="競業公司名稱" />` +
      `<select class="add-comp-type">` +
        _COMP_TYPES.map(t => `<option value="${t}">${t}</option>`).join("") +
      `</select>` +
      `<button class="add-comp-submit" onclick="submitAddCompetitor(this)">分析並加入</button>` +
      `<span class="add-comp-status"></span>` +
    `</div>`;
  return wrap;
}

function toggleAddCompetitor(btn) {
  const page = btn.closest(".summary-tab-page");
  const form = page && page.querySelector(".add-comp-form");
  if (!form) return;
  const open = form.style.display === "none";
  form.style.display = open ? "" : "none";
  btn.textContent = open ? "✕ 取消" : "＋ 新增競業";
  if (open) {
    // pre-select the currently-viewed type
    const t = page.dataset.activeCtype;
    if (t) form.querySelector(".add-comp-type").value = t;
    form.querySelector(".add-comp-name").focus();
  }
}

async function submitAddCompetitor(btn) {
  const id = _modalCompanyId;
  if (!id) return;
  const form = btn.closest(".add-comp-form");
  const name = form.querySelector(".add-comp-name").value.trim();
  const type = form.querySelector(".add-comp-type").value;
  const status = form.querySelector(".add-comp-status");
  if (!name) { status.textContent = "請輸入公司名稱"; status.className = "add-comp-status err"; return; }
  if (isCloudDeploy() && !getAiKey()) { toast("請先在設定中輸入 API Key"); openSettings(); return; }

  btn.disabled = true;
  status.textContent = "🔍 AI 分析中（約 30–60 秒）…";
  status.className = "add-comp-status info";
  try {
    const res = await api("POST", `/api/companies/${id}/competitors/add`, { name, competition_type: type });
    const c = state.companies.find(x => x.id === id);
    if (c) { c.summary = res.summary; c.competitors = res.competitors; }
    _updateSummaryInModal(c);   // rebuild summary tabs with the new row
    const ct = [...document.querySelectorAll("#summary-tabbar .summary-tab")].find(t => t.textContent.includes("競業"));
    if (ct) ct.click();         // stay on 競業分析 tab
    const compPage = [...document.querySelectorAll("#modal-summary .summary-tab-page")]
      .find(p => p.querySelector(".comp-type-bar"));
    if (compPage) _showCompetitorType(compPage, type);   // jump to the added type
    toast(`已新增競業：${name}`);
  } catch (err) {
    status.textContent = `❌ ${err.message}`;
    status.className = "add-comp-status err";
    btn.disabled = false;
  }
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
  if (!hasRatio) { content.innerHTML = ""; section.style.display = "none"; return; }
  section.style.display = "";
  content.style.display = "";   // visible within section (bookmark controls section)
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

function _showWebsitePrompt(companyId) {
  return new Promise(resolve => {
    const c = state.companies.find(x => x.id === companyId);
    const overlay    = document.getElementById("website-prompt-overlay");
    const nameEl     = document.getElementById("website-prompt-company-name");
    const input      = document.getElementById("website-prompt-input");
    const skipBtn    = document.getElementById("website-prompt-skip");
    const confirmBtn = document.getElementById("website-prompt-confirm");
    const hintEl     = document.getElementById("website-prompt-hint");
    const progressEl   = document.getElementById("website-prompt-progress");
    const progressFill = document.getElementById("website-prompt-progress-fill");
    const progressPctEl= document.getElementById("website-prompt-progress-pct");

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

    if (c?.website) {
      input.value = c.website;
      input.disabled = false;
      confirmBtn.disabled = false;
      input.placeholder = "https://example.com";
      setTimeout(() => input.focus(), 50);
    } else {
      // 搜尋期間：input 與確認按鈕均 disabled，shimmer 動畫 + 動態省略號告知使用者等待中
      input.value = "";
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

      const key = getAiKey();
      const findUrl = `/api/companies/${companyId}/find-website` +
        (key ? `?api_key=${encodeURIComponent(key)}&provider=${encodeURIComponent(getAiProvider())}` : "");
      fetch(findUrl)
        .then(r => r.json())
        .then(data => {
          if (!overlay.classList.contains("open")) { _endSearchingUi(); return; }
          _endSearchingUi();
          if (data.website) {
            input.value = data.website;
            if (hintEl) hintEl.textContent = "提供官網可讓 AI 直接擷取業務資訊，生成更準確的簡介。若無官網可略過。";
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
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">⏳ 重新生成中，請稍候（約 3–7 分鐘）…</p>";
  _expandSummarySection();
  _subscribeSummarize(id);
}

function deepEnrich() {
  const id = _modalCompanyId;
  if (!id) return;
  const summaryEl = document.getElementById("modal-summary");
  if (summaryEl) summaryEl.innerHTML = "<p class=\"summary-placeholder\">🔍 深度搜尋媒體報導中，請稍候（約 4–8 分鐘）…</p>";
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
  document.getElementById("patent-brief-overlay").classList.add("open");
}

function closeBriefModal(e) {
  if (e && e.target !== document.getElementById("patent-brief-overlay")) return;
  document.getElementById("patent-brief-overlay").classList.remove("open");
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") document.getElementById("patent-brief-overlay")?.classList.remove("open");
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
document.getElementById("manual-input-btn").addEventListener("click", () => {
  _addSubmenu.style.display = "none";
  _addArrow.style.transform = "";
  openManualDialog();
});
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
  try { await loadLabels(); } catch (_) { /* 標籤 API 失敗時用快取的 state.labels */ }
  document.getElementById("manual-names").value = "";
  document.getElementById("manual-hint").style.display = "none";
  _buildLabelOptions(suggestedLabel);
  document.getElementById("manual-overlay").classList.add("open");
  setTimeout(() => document.getElementById("manual-names").focus(), 50);
}

// 正式登記名稱通常含「公司」等法人實體標記；品牌／商標（如「超木 GREENuWood」）多半沒有
function _looksLikeCompanyName(name) {
  return /(公司|銀行|商行|商號|企業社|合作社|事務所|工作室|股份)/.test(name || "");
}

async function openManualDialogWithName(name, warn = false) {
  try { await loadLabels(); } catch (_) {}
  document.getElementById("manual-names").value = name;
  _buildLabelOptions("");
  const hint = document.getElementById("manual-hint");
  if (warn) {
    hint.innerHTML = `「${escHtml(name)}」看起來是<b>品牌或商標</b>，不是正式公司登記名稱。<br>請改填正式登記名稱（如「○○股份有限公司」）再按「下一步」，否則查不到公司登記資料、無法生成簡介。`;
    hint.style.display = "";
  } else {
    hint.style.display = "none";
  }
  document.getElementById("manual-overlay").classList.add("open");
  const ta = document.getElementById("manual-names");
  setTimeout(() => { ta.focus(); if (warn) ta.select(); }, 50);
}

// Core name without the legal suffix, so 短名/全名 compare equal
// (e.g.「廣太綠能」≡「廣太綠能股份有限公司」).
function _coreName(s) {
  return (s || "").replace(/(股份有限公司|有限公司)$/, "").trim();
}

// 顯示用：去掉法定尾綴但保留後面的括號註記（（本案）/（2308）…）。
// 尾綴可能在字串結尾，或緊接在括號前。
function _displayCompName(s) {
  return (s || "").replace(/(股份有限公司|有限公司)(?=（|$)/, "").trim();
}

// 一格塞多家公司時拆成單家陣列。分隔符涵蓋「／」「/」「、」「與」（如
// 「雙鴻（3324）／奇鋐（3017）」「臻鼎、欣興、健鼎與上游南亞、長春」），並去掉
// 「上游/下游/中游」這類方向描述詞，讓每家是乾淨的公司名（名稱解析交給 name-lookup）。
function _splitCompCell(content) {
  return (content || "")
    .split(/[／/、]|與/)
    .map(s => s.replace(/^(上游|下游|中游)/, "").trim())
    .filter(Boolean);
}

function openCompanyByName(name) {
  const co = state.companies.find(c => _coreName(c.name) === _coreName(name));
  if (co) openModal(co.id);
}

// Re-evaluate competitor chips' 「已加入」state in-place (no full re-render), e.g.
// after a competitor was just added to the company list from its chip.
function _refreshCompetitorChips() {
  document.querySelectorAll("#modal-summary .competitor-chip").forEach(chip => {
    const added = state.companies.some(co => _coreName(co.name) === _coreName(chip.dataset.cname));
    chip.dataset.added = added;
    chip.classList.toggle("competitor-chip--added", added);
    chip.title = added ? "已在清單中，點擊開啟" : "點擊新增此公司";
  });
}

function handleCompetitorChip(el) {
  const name  = el.dataset.cname;
  const added = el.dataset.added === "true";
  if (added) { openCompanyByName(name); return; }
  // 保險：不像正式公司名（多半是品牌／商標）時，提示使用者改填登記名稱，避免建出查無登記的假公司
  openManualDialogWithName(name, !_looksLikeCompanyName(name));
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
      const existing = _findExistingCompany(displayName) || _findExistingCompany(match?.short_name ?? name);
      const candidate = {
        name: displayName,
        tax_id: match ? (match.tax_id || null) : null,
        suggested_label: label,
        suggested_industry: "",
        is_new: !existing,
        existing_id: existing ? existing.id : null,
        existing_labels: existing ? (existing.labels || []) : [],
        is_unverified: match?.is_unverified || false,
        is_api_error: match?.is_api_error || false,
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
      <div class="disambig-input-label">${item._label || `「${escHtml(item.input)}」— 請選擇正確的公司（${item.matches.length} 筆）：`}</div>
      ${item.matches.map((m, mi) => {
        const _ACTIVE = new Set(["核准設立","登記","認許"]);
        const _DISSOLVED = new Set(["解散","廢止","撤銷","命令解散","廢止認許","撤回認許"]);
        const st = m.status || "";
        const isMDissolved = m.is_dissolved || _DISSOLVED.has(st) || ["解散","撤銷","廢止","命令解散"].some(k => st.includes(k));
        let statusBadge;
        if (isMDissolved)        statusBadge = `<span class="disambig-status dissolved" title="${escHtml(st || '已解散')}">解散</span>`;
        else if (m.is_unverified) statusBadge = `<span class="disambig-status unverified" title="Ronny 顯示核准，但政府資料庫查無此公司，請謹慎確認">待確認</span>`;
        else if (m.is_api_error)  statusBadge = `<span class="disambig-status api-error"  title="GCIS API 驗證逾時，Ronny 顯示核准，建議稍後重新查詢">驗證逾時</span>`;
        else if (_ACTIVE.has(st)) statusBadge = `<span class="disambig-status active"     title="${escHtml(st)}">核准</span>`;
        else                      statusBadge = `<span class="disambig-status unknown"    title="${escHtml(st || '狀態不明')}">?</span>`;
        const corpBadge = m.is_corp
          ? `<span class="disambig-corp-badge">股份有限公司</span>`
          : `<span class="disambig-corp-badge limited">有限公司</span>`;
        const isFirstActive = mi === item.matches.findIndex(x => !x.is_dissolved);
        if (isMDissolved) {
          return `
          <div class="disambig-option dissolved-ref" title="已解散，僅供參考">
            <span class="disambig-radio-spacer"></span>
            ${statusBadge}
            <span class="disambig-short">${escHtml(m.short_name)}</span>
            ${corpBadge}
            <span class="disambig-full">${escHtml(m.full_name)}</span>
          </div>`;
        }
        return `
        <label class="disambig-option">
          <input type="radio" name="dg${gi}" value="${mi}" ${isFirstActive ? "checked" : ""} />
          ${statusBadge}
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

function _normCompanyName(name) {
  return (name || "").replace(/股份有限公司$|有限公司$/, "").trim();
}

function _findExistingCompany(name) {
  const norm = _normCompanyName(name);
  return state.companies.find(c => _normCompanyName(c.name) === norm) || null;
}

function openNameReviewDialog(valid, uncertain, excluded, suggestedLabel) {
  if (valid.length === 0 && uncertain.length === 0 && excluded.length === 0) {
    openConfirmDialog([], [], [], suggestedLabel);
    return;
  }

  _nameReviewMeta = { suggestedLabel };

  const rows = [
    ...valid.map(c => ({ name: c.name, kind: "valid" })),
    ...excluded.map(c => ({ name: c.name, kind: "excluded" })),
    ...uncertain.map(c => ({ name: c.name, kind: "uncertain" })),
  ];

  const kindMeta = {
    valid:    { cls: "nr-valid",    icon: "✔", title: "含股份有限公司" },
    excluded: { cls: "nr-excluded", icon: "!",  title: "含有限公司（下一步可確認是否升格）" },
    uncertain:{ cls: "nr-uncertain",icon: "?",  title: "名稱待確認" },
  };

  document.getElementById("name-review-rows").innerHTML = rows.map((c, i) => {
    const m = kindMeta[c.kind];
    return `
    <div class="name-review-row" id="nr-row-${i}">
      <span class="nr-kind ${m.cls}" title="${m.title}">${m.icon}</span>
      <input class="name-review-input" id="nr-input-${i}" value="${escHtml(c.name)}" placeholder="公司名稱" />
      <button class="nr-delete" onclick="document.getElementById('nr-row-${i}').remove()">✕</button>
    </div>`;
  }).join("");

  document.getElementById("name-review-overlay").classList.add("open");
  // Focus first input
  setTimeout(() => document.querySelector(".name-review-input")?.focus(), 50);
}

document.getElementById("name-review-cancel").addEventListener("click", () =>
  document.getElementById("name-review-overlay").classList.remove("open"));

document.getElementById("name-review-ok").addEventListener("click", async () => {
  const inputs = document.querySelectorAll(".name-review-input");
  if (inputs.length === 0) {
    toast("未保留任何公司名稱", true);
    return;
  }

  const validNames = [];
  const newExcluded = [];
  const uncertainCandidates = [];
  const { suggestedLabel } = _nameReviewMeta;

  inputs.forEach(input => {
    const name = input.value.trim();
    if (!name) return;
    if (name.includes("股份有限公司")) {
      validNames.push(name);
    } else if (name.includes("有限公司")) {
      newExcluded.push({ name });
    } else {
      uncertainCandidates.push({ name, suggested_label: suggestedLabel, suggested_industry: state.industries[0] || "" });
    }
  });

  document.getElementById("name-review-overlay").classList.remove("open");

  if (validNames.length === 0) {
    openConfirmDialog([], uncertainCandidates, newExcluded, suggestedLabel);
    return;
  }

  // Same lookup + disambig flow as manual input
  let lookupResults = [];
  try {
    toast("正在驗證公司登記狀態…");
    lookupResults = await api("POST", "/api/companies/name-lookup", { names: validNames });
  } catch (e) {
    lookupResults = validNames.map(n => ({ input: n, matches: [] }));
  }

  // Auto-resolve single matches; only show disambig for truly ambiguous (>1 match)
  const autoResolved = {};
  const ambiguousItems = [];
  const rejectedNames = new Set();
  const notFoundNames = new Set();
  const notFoundSuggestions = {};   // name → suggestions array from backend
  for (const item of lookupResults) {
    if (item.rejected) {
      rejectedNames.add(item.input);
    } else if (item.not_found) {
      notFoundNames.add(item.input);
      if (item.suggestions?.length) notFoundSuggestions[item.input] = item.suggestions;
    } else if (item.matches.length === 1) {
      autoResolved[item.input] = item.matches[0];
    } else if (item.matches.length > 1) {
      ambiguousItems.push(item);
    }
  }

  const buildCandidates = (disambigSelections) => {
    const resolved = { ...autoResolved };
    const skipped = new Set();
    for (const s of disambigSelections) {
      if (s.skipped) skipped.add(s.input);
      else resolved[s.input] = s.match;
    }

    const valid = [];
    for (const name of validNames) {
      if (skipped.has(name)) continue;
      const match = resolved[name];
      const displayName = match ? match.full_name : name;
      const existing = _findExistingCompany(displayName) || _findExistingCompany(match?.short_name ?? name);
      valid.push({
        name: displayName,
        tax_id: match ? (match.tax_id || null) : null,
        suggested_label: suggestedLabel,
        suggested_industry: existing ? existing.industry : (state.industries[0] || ""),
        is_new: !existing,
        existing_id: existing ? existing.id : null,
        existing_labels: existing ? (existing.labels || []) : [],
        rejected: rejectedNames.has(name),
        not_found: notFoundNames.has(name),
        suggestions: notFoundSuggestions[name] || [],
        is_unverified: match?.is_unverified || false,
        is_api_error: match?.is_api_error || false,
      });
    }
    openConfirmDialog(valid, uncertainCandidates, newExcluded, suggestedLabel);
  };

  if (ambiguousItems.length > 0) {
    openDisambigDialog(ambiguousItems, buildCandidates);
  } else {
    buildCandidates([]);
  }
});

/* ── Not-found suggestion picker ── */
function selectNotFoundSuggestion(i, suggestion) {
  const c = state.pendingCandidates[i];
  if (!c) return;

  // Capture current label values before re-render
  const labelVals = {};
  state.pendingCandidates.forEach((_, idx) => {
    const el = document.getElementById(`label-v${idx}`);
    if (el) labelVals[idx] = el.value;
  });

  const existing = _findExistingCompany(suggestion.full_name);
  Object.assign(c, {
    name: suggestion.full_name,
    tax_id: suggestion.tax_id || null,
    not_found: false,
    is_new: !existing,
    existing_id: existing?.id ?? null,
    existing_labels: existing?.labels ?? [],
    suggestions: [],
  });

  openConfirmDialog(state.pendingCandidates, state.pendingUncertain, state.pendingExcluded, state.pendingLabel);

  // Restore labels for rows that had user input
  setTimeout(() => {
    Object.entries(labelVals).forEach(([idx, val]) => {
      const el = document.getElementById(`label-v${idx}`);
      if (el) el.value = val;
    });
  }, 0);
}

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
      if (c.rejected) {
        return `
          <div class="confirm-row dissolved-row">
            <div class="company-name-col">${escHtml(c.name)}<span class="dissolved-badge">廢止</span></div>
            <div style="font-size:11px;color:#991b1b;grid-column:2/-1;">已於主管機關登記廢止，不予儲存</div>
          </div>`;
      }
      if (c.not_found) {
        const suggestHtml = (c.suggestions || []).length
          ? `<div class="nf-suggest-wrap">
               <span class="nf-suggest-label">可能已更名：</span>
               ${c.suggestions.map(s => `<button class="nf-suggest-btn" onclick="selectNotFoundSuggestion(${i}, ${escAttr(JSON.stringify(s))})">${escHtml(s.full_name)}</button>`).join("")}
             </div>`
          : "";
        return `
          <div class="confirm-row dissolved-row" id="nf-row-${i}">
            <div class="company-name-col" style="grid-column:1/-1">${escHtml(c.name)}<span class="not-found-badge">查無登記</span>
              <span style="font-size:11px;color:#92400e;margin-left:8px;">登記資料查無此公司，不予儲存</span>
              ${suggestHtml}
            </div>
          </div>`;
      }
      const badge = c.is_new
        ? `<span class="new-badge">新增</span>`
        : `<span class="update-badge">既有</span>`;
      const unverifiedBadge = c.is_unverified
        ? `<span class="unverified-badge" title="Ronny 顯示核准，但政府資料庫查無此公司，請確認是否仍為現役">⚠ 待確認</span>`
        : c.is_api_error
          ? `<span class="unverified-badge api-error" title="GCIS API 驗證逾時（網路不穩），Ronny 顯示核准，建議稍後重新查詢">⏱ 驗證逾時</span>`
          : "";
      const existingLabels = c.existing_labels?.length
        ? `<div class="existing-labels">現有標籤：${c.existing_labels.join("、")}</div>`
        : "";
      const hasData = !c.is_new && state.companies.find(x => x.id === c.existing_id)?.summary;
      const checked = hasData ? "" : "checked";
      const enrichHint = hasData ? `<span class="enrich-has-data" title="已有摘要，預設不重新生成">已生成</span>` : "";
      return `
        <div class="confirm-row">
          <div class="company-name-col">${escHtml(c.name)}${badge}${unverifiedBadge}${existingLabels}</div>
          <input type="text" id="label-v${i}" value="${escHtml(c.suggested_label)}" placeholder="標籤名稱" />
          <label class="enrich-check-label" title="是否生成 AI 摘要"><input type="checkbox" id="enrich-v${i}" ${checked} />生成${enrichHint}</label>
        </div>`;
    }).join("")}` : "";

  // ── Section 2: uncertain (neither suffix) ──
  const uncertainHtml = uncertain.length ? `
    <div class="confirm-section-title uncertain-title">❓ 不含標準公司結尾，搜尋登記資料後決定是否納入</div>
    ${uncertain.map((c, i) => `
      <div class="confirm-row uncertain-row" id="uncertain-row-${i}">
        <div class="company-name-col uncertain-name">${escHtml(c.name)}</div>
        <div class="uncertain-actions">
          <button class="unc-btn unc-yes" onclick="toggleUncertain(${i}, true)">✔ 搜尋並納入</button>
          <button class="unc-btn unc-no active" onclick="toggleUncertain(${i}, false)">✘ 否，略過</button>
        </div>
        <div class="uncertain-fields" id="uncertain-fields-${i}" style="display:none; grid-column:1/-1;">
          <div class="confirm-row" style="border:none;padding:4px 0;">
            <div></div>
            <input type="text" id="label-u${i}" value="${escHtml(c.suggested_label || suggestedLabel)}" placeholder="標籤名稱" />
          </div>
        </div>
      </div>`).join("")}` : "";

  // ── Section 3: excluded (有限公司 only) — three options ──
  const excludedHtml = excluded.length ? `
    <div class="confirm-section-title excluded-title">⚠️ 僅含「有限公司」，請選擇處理方式</div>
    ${excluded.map((c, i) => `
      <div class="confirm-row excluded-row" id="excluded-row-${i}">
        <div class="company-name-col excluded-name">${escHtml(c.name)}</div>
        <div class="uncertain-actions">
          <button class="unc-btn unc-upgrade" onclick="toggleExcluded(${i}, true)">↑ 升格搜尋</button>
          <button class="unc-btn unc-direct" onclick="acceptExcludedDirect(${i})">✔ 直接納入</button>
          <button class="unc-btn unc-no active" onclick="toggleExcluded(${i}, false)">✘ 排除</button>
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

async function toggleUncertain(i, accept) {
  const row = document.getElementById(`uncertain-row-${i}`);
  const fields = document.getElementById(`uncertain-fields-${i}`);
  row.querySelectorAll(".unc-btn").forEach(b => b.classList.remove("active"));

  if (!accept) {
    row.querySelector(".unc-no").classList.add("active");
    fields.style.display = "none";
    row.dataset.accepted = "0";
    const cu = (state.pendingUncertain || [])[i];
    if (cu?._origName) {
      cu.name = cu._origName;
      const nameEl = row.querySelector(".uncertain-name");
      if (nameEl) nameEl.textContent = cu._origName;
    }
    return;
  }

  const c = (state.pendingUncertain || [])[i];
  if (c) {
    const coreSearch = c.name.replace(/股份有限公司$/, "").replace(/有限公司$/, "").trim() || c.name;
    row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = true; });
    let lr = null;
    try {
      const res = await api("POST", "/api/companies/name-lookup", { names: [coreSearch] });
      lr = res?.[0] ?? null;
    } catch (_) { /* network error → allow through */ }
    row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = false; });

    if (lr?.rejected) {
      row.querySelector(".unc-no").classList.add("active");
      row.dataset.accepted = "0";
      toast(`「${c.name}」在主管機關登記已廢止，無法納入`, true);
      return;
    }
    if (lr?.not_found) {
      toast(`「${c.name}」查無相似公司名稱，依您判斷納入`, true);
      // Allow through — uncertain companies may use non-standard names (e.g. associations)
    }
    if (lr?.matches?.length === 1) {
      _applyUncertainMatch(i, lr.matches[0]);   // single match → auto-fill name
    } else if (lr?.matches?.length > 1) {
      // Multiple matches → let user disambiguate, then mark accepted in callback
      openDisambigDialog([{ input: c.name, matches: lr.matches }], (selections) => {
        const sel = selections[0];
        if (sel.skipped) {
          row.querySelector(".unc-no").classList.add("active");
          fields.style.display = "none";
          row.dataset.accepted = "0";
          return;
        }
        _applyUncertainMatch(i, sel.match);
        row.querySelector(".unc-yes").classList.add("active");
        fields.style.display = "";
        row.dataset.accepted = "1";
      });
      return;   // wait for disambig callback before marking accepted
    }
  }

  row.querySelector(".unc-yes").classList.add("active");
  fields.style.display = "";
  row.dataset.accepted = "1";
}

function _applyUncertainMatch(i, match) {
  const c = (state.pendingUncertain || [])[i];
  if (!c) return;
  c._origName = c._origName ?? c.name;
  c.name   = match.full_name;
  c.tax_id = match.tax_id || null;
  const nameEl = document.getElementById(`uncertain-row-${i}`)?.querySelector(".uncertain-name");
  if (nameEl) nameEl.textContent = match.full_name;
}


async function toggleExcluded(i, accept) {
  const row = document.getElementById(`excluded-row-${i}`);
  const fields = document.getElementById(`excluded-fields-${i}`);
  row.querySelectorAll(".unc-btn").forEach(b => b.classList.remove("active"));

  if (!accept) {
    row.querySelector(".unc-no").classList.add("active");
    fields.style.display = "none";
    row.dataset.accepted = "0";
    const ce = (state.pendingExcluded || [])[i];
    if (ce?._origName) {
      ce.name = ce._origName;
      const nameEl = row.querySelector(".excluded-name");
      if (nameEl) nameEl.textContent = ce._origName;
    }
    return;
  }

  const c = (state.pendingExcluded || [])[i];
  if (c) {
    // 用核心名稱（去掉有限公司後綴）搜尋，Ronny 模糊比對效果更好
    const coreSearch = c.name.replace(/股份有限公司$/, "").replace(/有限公司$/, "").trim() || c.name;
    const displayName = c.name.endsWith("股份有限公司") ? c.name : c.name.replace(/有限公司$/, "股份有限公司");
    row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = true; });
    let lr = null;
    try {
      const res = await api("POST", "/api/companies/name-lookup", { names: [coreSearch] });
      lr = res?.[0] ?? null;
    } catch (_) { /* network error → allow through */ }
    row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = false; });

    if (lr?.rejected) {
      row.querySelector(".unc-no").classList.add("active");
      row.dataset.accepted = "0";
      toast(`「${displayName}」在主管機關登記已廢止，無法納入`, true);
      return;
    }

    // Only accept 股份有限公司 matches; filter out plain 有限公司 results
    let corpMatches = (lr?.matches || []).filter(m => m.is_corp);

    // If primary search found nothing useful, retry with 3-char prefix
    // (handles renamed companies: 林三益筆墨→林三益股份有限公司)
    if (corpMatches.length === 0 && coreSearch.length > 3) {
      const shortKey = coreSearch.slice(0, 3);
      try {
        row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = true; });
        const kr = await api("POST", "/api/companies/name-lookup", { names: [shortKey] });
        row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = false; });
        const km = (kr?.[0]?.matches || []).filter(m => m.is_corp && m.full_name !== displayName);
        if (km.length > 0) corpMatches = km;
      } catch (_) {
        row.querySelectorAll(".unc-btn").forEach(b => { b.disabled = false; });
      }
    }

    if (corpMatches.length === 0) {
      // No 股份有限公司 version found — warn but allow force-upgrade
      const reason = lr?.not_found
        ? "查無此公司"
        : "查無對應的股份有限公司版本（僅找到有限公司）";
      toast(`⚠️ ${reason}，若確認升格將以「${displayName}」儲存（名稱未經驗證）`, true);
    } else if (corpMatches.length === 1) {
      _applyExcludedMatch(i, corpMatches[0]);
    } else {
      openDisambigDialog([{ input: coreSearch, matches: corpMatches }], (selections) => {
        const sel = selections[0];
        if (sel.skipped) {
          row.querySelector(".unc-no").classList.add("active");
          fields.style.display = "none";
          row.dataset.accepted = "0";
          return;
        }
        _applyExcludedMatch(i, sel.match);
        row.querySelector(".unc-upgrade").classList.add("active");
        fields.style.display = "";
        row.dataset.accepted = "1";
        delete row.dataset.direct;
      });
      return;
    }
  }

  row.querySelector(".unc-upgrade").classList.add("active");
  fields.style.display = "";
  row.dataset.accepted = "1";
  delete row.dataset.direct;
}

function acceptExcludedDirect(i) {
  const row = document.getElementById(`excluded-row-${i}`);
  const fields = document.getElementById(`excluded-fields-${i}`);
  if (!row) return;
  row.querySelectorAll(".unc-btn").forEach(b => b.classList.remove("active"));
  row.querySelector(".unc-direct").classList.add("active");
  fields.style.display = "";
  row.dataset.accepted = "1";
  row.dataset.direct = "1";
}

function _applyExcludedMatch(i, match) {
  const c = (state.pendingExcluded || [])[i];
  if (!c) return;
  c._origName = c._origName ?? c.name;
  c.name   = match.full_name;
  c.tax_id = match.tax_id || null;
  const nameEl = document.getElementById(`excluded-row-${i}`)?.querySelector(".excluded-name");
  if (nameEl) nameEl.textContent = match.full_name;
}

document.getElementById("confirm-cancel").addEventListener("click", () =>
  document.getElementById("confirm-overlay").classList.remove("open"));

document.getElementById("confirm-ok").addEventListener("click", async () => {
  // toSave: companies to persist; enrichFlags: aligned bool array (same indices)
  const toSave = [];
  const enrichFlags = [];

  // Valid (股份有限公司) candidates:
  //   - is_new + unchecked → skip entirely (user doesn't want to add)
  //   - is_new + checked   → save + enrich
  //   - existing + unchecked → save (update label) but no re-enrich
  //   - existing + checked   → save + enrich
  state.pendingCandidates.forEach((c, i) => {
    if (c.rejected || c.not_found) return;         // 廢止 or 查無登記 → never save
    const wantEnrich = document.getElementById(`enrich-v${i}`)?.checked !== false;
    if (c.is_new && !wantEnrich) return;
    toSave.push({
      name: c.name,
      tax_id: c.tax_id ?? null,
      label: document.getElementById(`label-v${i}`)?.value.trim() ?? state.pendingLabel,
      is_new: c.is_new,
      existing_id: c.existing_id ?? null,
    });
    enrichFlags.push(wantEnrich);
  });

  // Accepted uncertain candidates (always new → always enrich)
  (state.pendingUncertain || []).forEach((c, i) => {
    const row = document.getElementById(`uncertain-row-${i}`);
    if (row?.dataset.accepted === "1") {
      toSave.push({
        name: c.name,
        tax_id: c.tax_id ?? null,
        label: document.getElementById(`label-u${i}`)?.value.trim() ?? state.pendingLabel,
        is_new: true,
        existing_id: null,
      });
      enrichFlags.push(true);
    }
  });

  // Rescued excluded candidates (always new → always enrich)
  // dataset.direct === "1": user chose "直接納入" — keep 有限公司 name as-is
  // dataset.direct unset: user chose "升格搜尋" — _applyExcludedMatch may have updated name;
  //   if not matched, fall back to suffix conversion (有限公司 → 股份有限公司)
  (state.pendingExcluded || []).forEach((c, i) => {
    const row = document.getElementById(`excluded-row-${i}`);
    if (row?.dataset.accepted === "1") {
      const isDirect = row.dataset.direct === "1";
      const finalName = isDirect
        ? c.name
        : c.name.endsWith("股份有限公司")
          ? c.name
          : c.name.replace(/有限公司$/, "股份有限公司");
      toSave.push({
        name: finalName,
        tax_id: c.tax_id ?? null,
        label: document.getElementById(`label-e${i}`)?.value.trim() ?? state.pendingLabel,
        is_new: true,
        existing_id: null,
      });
      enrichFlags.push(true);
    }
  });

  document.getElementById("confirm-overlay").classList.remove("open");

  if (toSave.length === 0) {
    toast("未選擇任何公司，已取消");
    return;
  }

  // Save first (no enrichment yet) so we control batching from the client side
  let saved_ids;
  try {
    const result = await api("POST", "/api/companies/confirm", { companies: toSave, enrich: false });
    saved_ids = result.saved_ids || [];
    toast(`已儲存 ${result.saved} 筆公司資料`);
    await loadCompanies();
    computeGroups();
    renderSidebar();
    renderGrid();
    _refreshCompetitorChips();   // 若 modal 開著，更新競業表格「已加入」綠勾
  } catch (err) {
    toast(`儲存失敗：${err.message}`, true);
    return;
  }

  if (saved_ids.length === 0) return;

  // enrichFlags is aligned with toSave[] (and thus saved_ids[])
  const enrich_ids = saved_ids.filter((_, idx) => enrichFlags[idx]);

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
  // 1. 先套 sidebar scope；2. 再套 watched tab
  let companies = [...state.companies];
  let scopeLabel = "全部公司";

  if (state.activeLabelGroup) {
    const gLabels = (state.labelGroups[state.activeLabelGroup] || []).filter(l => state.pinnedItems.has(l));
    companies = companies.filter(c => (c.labels || []).some(l => gLabels.includes(l)));
    scopeLabel = `標籤群組：${state.activeLabelGroup}`;
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

  if (state.activeTab === "watched") {
    companies = companies.filter(c => c.watched === true);
    scopeLabel = scopeLabel === "全部公司" ? "⭐ 追蹤" : `${scopeLabel} — ⭐ 追蹤`;
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
      `將對範圍內 ${companies.length} 間公司全部重新生成（覆蓋現有簡介與登記資料）。\n` +
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
          // 生成完成後自動補抓每股金額（有 persistent cookie 時完全無需人工）
          const fresh = state.companies.find(x => x.id === companyId);
          if (fresh && _needsAutoFetch(fresh)) _enqueueAutoFetch(companyId);
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
let _relActiveTab = "ownership"; // currently active tab

function _disposeCy() {
  if (_cyResizeObserver) { _cyResizeObserver.disconnect(); _cyResizeObserver = null; }
  if (_cy) { try { _cy.destroy(); } catch (_) {} _cy = null; }
}

function switchRelTab(tab) {
  _relActiveTab = tab;
  document.querySelectorAll(".rel-tab").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.tab === tab));

  const ownerLegend = document.getElementById("rel-graph-legend");
  const compLegend  = document.getElementById("rel-graph-legend-competitor");
  if (ownerLegend) ownerLegend.style.display = tab === "ownership" ? "" : "none";
  if (compLegend)  compLegend.style.display  = tab === "competitor" ? "" : "none";

  const c = state.companies.find(x => x.id === _relGraphCompanyId);
  const titleLabel = tab === "ownership" ? "股權關係" : "競業版圖";
  document.getElementById("rel-graph-title").textContent =
    `${c ? shortName(c.name) : "公司"} — ${titleLabel}`;

  const sub = document.getElementById("rel-graph-subtitle");
  if (sub) {
    if (tab === "ownership") {
      const parent = c?.relationship_graph?.parent;
      sub.textContent = parent
        ? `當前錨點：${parent.name}（${parent.kind === "person" ? "自然人" : "法人"}）`
        : "尚未分析。點右上「🔗 重新分析」開始，或在公司詳情中於董監事表格點 ⊕ 指定錨點。";
    } else {
      const cnt = (c?.competitors || []).length;
      sub.textContent = cnt ? `共 ${cnt} 家已記錄競業（含雙向關聯）` : "尚無競業資料，請先生成公司簡介";
    }
  }

  const rebuildBtn = document.getElementById("rel-graph-rebuild-btn");
  if (rebuildBtn) rebuildBtn.style.display = tab === "ownership" ? "" : "none";

  if (!_relGraphCompanyId) return;
  if (tab === "ownership") renderOwnershipGraph(_relGraphCompanyId);
  else                     renderCompetitorGraph(_relGraphCompanyId);
}

function openRelationshipGraph(companyId) {
  const id = companyId || _modalCompanyId;
  if (!id) return;
  _relGraphCompanyId = id;
  _relActiveTab = "ownership";
  document.getElementById("rel-graph-overlay").classList.add("open");
  document.body.classList.add("rel-open");
  _expandParentRows();
  // Cytoscape needs a layout pass after the container resizes (side-by-side mode shrinks it)
  setTimeout(() => switchRelTab("ownership"), 50);
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
      { selector: 'node[role = "sibling"][?in_db]',
        style: { "background-color": "#059669" }
      },
      { selector: 'node[role = "sibling"][!in_db]',
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
    if (d.role === "self") {
      if (d.company_id) openModal(d.company_id);   // 點本案節點＝開回自己的 modal
      return;
    }
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

  // 滑過節點時游標變小手，提示可點擊
  _cy.on("mouseover", "node", () => { wrap.style.cursor = "pointer"; });
  _cy.on("mouseout",  "node", () => { wrap.style.cursor = ""; });
}

async function renderCompetitorGraph(companyId) {
  const wrap     = document.getElementById("rel-graph-canvas");
  const statusEl = document.getElementById("rel-graph-status");
  if (!wrap) return;

  _disposeCy();
  wrap.innerHTML = "";
  if (statusEl) statusEl.textContent = "";

  let graph;
  try {
    graph = await api("GET", `/api/companies/${companyId}/competitor-graph`);
  } catch (err) {
    if (statusEl) statusEl.textContent = `載入競業圖失敗：${err.message}`;
    return;
  }

  if (!graph.nodes || graph.nodes.length <= 1) {
    if (statusEl) statusEl.innerHTML =
      `<div class="rel-empty">尚無競業資料。請先生成公司簡介，系統會自動解析競業分析表格。</div>`;
    return;
  }

  if (typeof cytoscape === "undefined") {
    if (statusEl) statusEl.textContent = "圖表元件尚未載入，請稍候再試";
    return;
  }

  _cy = cytoscape({
    container: wrap,
    elements: { nodes: graph.nodes, edges: graph.edges },
    layout: {
      name: "concentric",
      concentric: node => node.data("role") === "self" ? 10 : 1,
      levelWidth: () => 1,
      spacingFactor: 1.6,
      padding: 36,
    },
    style: [
      {
        selector: "node",
        style: {
          "label": "data(label)",
          "text-valign": "center", "text-halign": "center",
          "text-wrap": "wrap", "text-max-width": 100,
          "font-size": 12,
          "font-family": "-apple-system, 'Microsoft JhengHei', sans-serif",
          "color": "#fff",
          "background-color": "#94a3b8",
          "border-width": 2, "border-color": "#fff",
          "width": 96, "height": 48,
          "shape": "round-rectangle",
        },
      },
      {
        selector: 'node[role = "self"]',
        style: {
          "background-color": "#1d4ed8",
          "border-color": "#fbbf24", "border-width": 4,
          "width": 120, "height": 56, "font-size": 13,
        },
      },
      {
        selector: 'node[role = "competitor"][?in_db]',
        style: {
          "background-color": "#d97706",
          "shape": "hexagon",
          "width": 104, "height": 58,
        },
      },
      {
        selector: 'node[role = "competitor"][!in_db]',
        style: {
          "background-color": "#fffbeb",
          "color": "#92400e",
          "border-color": "#d97706", "border-style": "dashed", "border-width": 2,
          "shape": "hexagon",
          "width": 104, "height": 58,
        },
      },
      {
        selector: "edge",
        style: {
          "curve-style": "bezier",
          "line-color": "#d97706",
          "line-style": "dashed",
          "line-dash-pattern": [6, 3],
          "width": 2,
          "target-arrow-shape": "none",
        },
      },
    ],
  });

  _cyResizeObserver = new ResizeObserver(() => {
    if (!_cy) return;
    _cy.resize();
    _cy.fit(undefined, 36);
  });
  _cyResizeObserver.observe(wrap);

  _cy.on("tap", "node", evt => {
    const d = evt.target.data();
    // 已收錄者（含中心「本案」節點）都開對應 modal；點中心節點即可返回本案，
    // 點已收錄競業則切到該公司——modal 可在 graph 不變的情況下來回切換。
    if (d.in_db && d.company_id) {
      openModal(d.company_id);
      return;
    }
    if (d.role === "self") return;
    _openFromGraphDialog({ name: d.name, tax_id: "", role: "competitor" });
  });

  // 滑過節點時游標變小手，提示可點擊
  _cy.on("mouseover", "node", () => { wrap.style.cursor = "pointer"; });
  _cy.on("mouseout",  "node", () => { wrap.style.cursor = ""; });
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
  // 預設順序：node.defaultIndustry（產業地圖傳入）→ 來源公司的產業
  if (node.defaultIndustry) indSel.value = node.defaultIndustry;
  else if (sourceCompany?.industry) indSel.value = sourceCompany.industry;

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
      // 若產業地圖開著，且加入的公司屬於該產業，把節點狀態翻成已收錄
      if (_imState.industry && document.getElementById("industry-map-overlay").classList.contains("open")) {
        _imMarkNodeInDb(_fromGraphPayload?.name || payload.name, res.company_id);
      }
    });
  } catch (err) {
    toast(`加入失敗：${err.message}`, true);
  }
});

/* ── Industry Map ── */
const _imState = {
  industry: null,
  breadth: "medium",
  data: null,
  generating: false,
  evtSrc: null,
};

async function openIndustryMap(industry) {
  if (!industry) return;
  _imState.industry = industry;
  _imState.data = null;
  document.getElementById("industry-map-title").textContent = `${industry} — 產業地圖`;
  document.getElementById("industry-map-subtitle").textContent = "";
  document.getElementById("industry-map-status").innerHTML = "";
  document.getElementById("industry-map-canvas").innerHTML = "";
  document.getElementById("industry-map-meta").innerHTML = "";
  _imSetBreadthActive(_imState.breadth);
  document.getElementById("industry-map-overlay").classList.add("open");

  // Try cached first
  try {
    const cached = await api("GET", `/api/industry-map/${encodeURIComponent(industry)}`);
    _imState.data = cached;
    if (cached.breadth) {
      _imState.breadth = cached.breadth;
      _imSetBreadthActive(cached.breadth);
    }
    _renderIndustryMap(cached);
  } catch (err) {
    // 404 → 顯示空狀態，提示按生成
    document.getElementById("industry-map-canvas").innerHTML = `
      <div class="im-empty">
        <div class="im-empty-icon">🗺️</div>
        <div class="im-empty-title">尚未生成「${escHtml(industry)}」的產業地圖</div>
        <div class="im-empty-hint">選擇廣度後按右上方「生成地圖」</div>
      </div>`;
  }
}

function closeIndustryMap() {
  if (_imState.evtSrc) {
    try { _imState.evtSrc.close(); } catch (_) {}
    _imState.evtSrc = null;
  }
  _imState.generating = false;
  document.getElementById("industry-map-overlay").classList.remove("open");
}

function setIndustryMapBreadth(b) {
  if (!["narrow", "medium", "broad"].includes(b)) return;
  _imState.breadth = b;
  _imSetBreadthActive(b);
}

function _imSetBreadthActive(b) {
  document.querySelectorAll("#industry-map-controls .im-breadth-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.breadth === b);
  });
}

async function generateIndustryMap() {
  if (_imState.generating) return;
  const industry = _imState.industry;
  if (!industry) return;

  _imState.generating = true;
  const btn = document.getElementById("industry-map-generate-btn");
  btn.disabled = true;
  btn.textContent = "⏳ 生成中…";
  const statusEl = document.getElementById("industry-map-status");
  statusEl.innerHTML = `<div class="im-progress">準備中…</div>`;
  document.getElementById("industry-map-canvas").innerHTML = "";

  const params = new URLSearchParams({ breadth: _imState.breadth });
  if (typeof getAiKey === "function") {
    const k = getAiKey();
    if (k) params.set("api_key", k);
  }
  if (typeof getAiProvider === "function") {
    const p = getAiProvider();
    if (p) params.set("provider", p);
  }
  const url = `/api/industry-map/${encodeURIComponent(industry)}/generate?${params.toString()}`;

  await new Promise(resolve => {
    const es = new EventSource(url);
    _imState.evtSrc = es;
    es.onmessage = async e => {
      let event;
      try { event = JSON.parse(e.data); } catch (_) { return; }
      if (event.type === "progress") {
        statusEl.innerHTML = `<div class="im-progress">${escHtml(event.message)}</div>`;
      } else if (event.type === "done") {
        es.close();
        _imState.evtSrc = null;
        _imState.data = event.data;
        if (event.data?.breadth) {
          _imState.breadth = event.data.breadth;
          _imSetBreadthActive(event.data.breadth);
        }
        statusEl.innerHTML = "";
        _renderIndustryMap(event.data);
        resolve();
      } else if (event.type === "error") {
        es.close();
        _imState.evtSrc = null;
        statusEl.innerHTML = `<div class="im-error">生成失敗：${escHtml(event.message)}</div>`;
        resolve();
      }
    };
    es.onerror = () => {
      es.close();
      _imState.evtSrc = null;
      statusEl.innerHTML = `<div class="im-error">連線中斷，請重試</div>`;
      resolve();
    };
  });

  _imState.generating = false;
  btn.disabled = false;
  btn.textContent = "🗺️ 重新生成";
}

function _renderIndustryMap(data) {
  const canvas = document.getElementById("industry-map-canvas");
  const meta = document.getElementById("industry-map-meta");
  if (!data || !data.sections || data.sections.length === 0) {
    canvas.innerHTML = `<div class="im-empty"><div class="im-empty-title">空白地圖</div></div>`;
    return;
  }

  const layout = data.layout_type === "layered" ? "layered" : "matrix";
  const sections = [...data.sections].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  const stats = data.stats || {};
  const ts = data.generated_at ? new Date(data.generated_at).toLocaleString("zh-TW", { hour12: false }) : "";
  meta.innerHTML = `
    <div class="im-meta">
      <span class="im-meta-pill">${layout === "layered" ? "🪜 上下分層" : "🔲 矩陣並列"}</span>
      <span class="im-meta-pill">共 ${sections.length} 個主分類 / ${stats.rendered_nodes ?? 0} 家公司</span>
      <span class="im-meta-pill">已收錄 ${stats.in_db_count ?? "?"} 家 · 擴充候選 ${stats.expansion_pool_count ?? "?"} 家</span>
      ${ts ? `<span class="im-meta-time">生成於 ${escHtml(ts)}</span>` : ""}
      ${data.rationale ? `<div class="im-rationale">${escHtml(data.rationale)}</div>` : ""}
    </div>`;

  canvas.className = `im-canvas im-${layout}`;
  canvas.innerHTML = sections.map(s => _imSectionHtml(s, layout)).join("");

  // Wire clicks (delegate on canvas)
  canvas.onclick = ev => {
    const card = ev.target.closest(".im-card");
    if (!card) return;
    const inDb = card.dataset.inDb === "true";
    const cid = card.dataset.companyId || "";
    const name = card.dataset.name || "";
    const taxId = card.dataset.taxId || "";
    if (inDb && cid) {
      openModal(cid);
    } else {
      _openFromGraphDialog({
        name,
        tax_id: taxId,
        role: "competitor",
        defaultIndustry: _imState.industry,
      });
    }
  };
}

function _imSectionHtml(section, layout) {
  const subs = (section.subgroups || []).map(_imSubgroupHtml).join("");
  return `<div class="im-section">
    <div class="im-section-head"><span class="im-section-title">${escHtml(section.title || "")}</span></div>
    <div class="im-subgroups">${subs}</div>
  </div>`;
}

function _imSubgroupHtml(sub) {
  const cos = (sub.companies || []).map(_imCardHtml).join("");
  return `<div class="im-subgroup">
    <div class="im-subgroup-title">${escHtml(sub.title || "")}</div>
    <div class="im-cards">${cos || `<div class="im-empty-sub">—</div>`}</div>
  </div>`;
}

function _imCardHtml(co) {
  const inDb = !!co.in_db;
  const cls = `im-card${inDb ? " im-card-in" : " im-card-out"}`;
  const note = co.note || co.core_biz || "";
  const noteHtml = note ? `<div class="im-card-note">${escHtml(note)}</div>` : "";
  const badge = inDb
    ? `<span class="im-badge im-badge-in">✓</span>`
    : `<span class="im-badge im-badge-out">+</span>`;
  return `<button class="${cls}"
    data-in-db="${inDb}"
    data-company-id="${escAttr(co.company_id || "")}"
    data-name="${escAttr(co.name || "")}"
    data-tax-id="${escAttr(co.tax_id || "")}"
    title="${inDb ? "點擊開啟詳情" : "點擊加入公司列表"}">
    ${badge}<span class="im-card-name">${escHtml(co.name || "")}</span>
    ${noteHtml}
  </button>`;
}

function _imMarkNodeInDb(name, companyId) {
  // 把畫面上同名節點翻面（不重新生成地圖）
  if (!name || !companyId) return;
  const canvas = document.getElementById("industry-map-canvas");
  if (!canvas) return;
  canvas.querySelectorAll(`.im-card[data-name="${escAttr(name)}"]`).forEach(el => {
    el.dataset.inDb = "true";
    el.dataset.companyId = companyId;
    el.classList.remove("im-card-out");
    el.classList.add("im-card-in");
    const badge = el.querySelector(".im-badge");
    if (badge) {
      badge.className = "im-badge im-badge-in";
      badge.textContent = "✓";
    }
    el.title = "點擊開啟詳情";
  });
  // 同步更新 _imState.data
  if (_imState.data?.sections) {
    for (const s of _imState.data.sections) {
      for (const sub of (s.subgroups || [])) {
        for (const co of (sub.companies || [])) {
          if (co.name === name) {
            co.in_db = true;
            co.company_id = companyId;
          }
        }
      }
    }
  }
}

// Click backdrop / Esc to close
document.getElementById("industry-map-overlay").addEventListener("click", e => {
  if (e.target.id === "industry-map-overlay") closeIndustryMap();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("industry-map-overlay").classList.contains("open")) {
    closeIndustryMap();
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
function applyCollapsible(container) {
  // Every top-level section (## → h3) is collapsible — no hard-coded whitelist,
  // so it doesn't matter what headings a particular deck produces.
  for (const h3 of [...container.querySelectorAll("h3")]) {
    const body = document.createElement("div");
    body.className = "collapsible-body";
    let next = h3.nextElementSibling;
    // Stop at the next section boundary: another H3, or a「簡報」section wrapper
    // (.summary-mat-section) — otherwise a wrapped section like 營運綜覽 would be
    // swallowed into the previous section's collapsed body and vanish.
    while (next && next.tagName !== "H3" && !(next.classList && next.classList.contains("summary-mat-section"))) {
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
// 長段正文依句號（。）拆成數個 <p>，讓不同主題各自成段、不再擠成一坨。
// 短段（<100 字）或單句不拆，避免太碎。
function _proseParagraphs(text) {
  const plain = text.replace(/<[^>]+>/g, "");
  if (plain.length < 100) return `<p>${text}</p>`;
  const parts = text.split(/(?<=。)/)
    .map(s => s.trim()).filter(Boolean);
  if (parts.length < 2) return `<p>${text}</p>`;
  return parts.map(s => `<p>${s}</p>`).join("");
}

function renderSummary(raw, matHeadings) {
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
    const headerCells = (header || "").split("|").filter((_,i,a) => i>0 && i<a.length-1).map(c => c.trim());
    const isCompetitorTable = headerCells[0] === "公司名稱";
    const ths = headerCells.map(c => `<th>${inlineMarkdown(c)}</th>`).join("");
    const trs = body.map(row => {
      const cells = row.split("|").filter((_,i,a) => i>0 && i<a.length-1);
      const tds = cells.map((c, ci) => {
        const content = c.trim();
        if (isCompetitorTable && ci === 0) {
          // 本案列：不可點，去尾綴顯示
          if (content.includes("（本案）")) {
            return `<td>${inlineMarkdown(_displayCompName(content))}</td>`;
          }
          // 一格可能塞多家（如「雙鴻（3324）／奇鋐（3017）」）→ 拆成各自獨立的 chip，
          // 每家自己一個＋、自己可點，新增流程就只會加被點的那一家。
          const chips = _splitCompCell(content).map(tok => {
            const disp = _displayCompName(tok);
            const rawName = tok.replace(/（[^）]*）/g, "").trim();
            const alreadyAdded = state.companies.some(co => _coreName(co.name) === _coreName(rawName));
            const cls   = alreadyAdded ? "competitor-chip competitor-chip--added" : "competitor-chip";
            const title = alreadyAdded ? "已在清單中，點擊開啟" : "點擊新增此公司";
            return `<span class="${cls}" data-cname="${escHtml(rawName)}" data-added="${alreadyAdded}" onclick="handleCompetitorChip(this)" title="${title}">${inlineMarkdown(disp)}</span>`;
          }).join("");
          return `<td><div class="comp-name-cell">${chips}</div></td>`;
        }
        return `<td>${inlineMarkdown(content)}</td>`;
      }).join("");
      return `<tr>${tds}</tr>`;
    }).join("");
    const tcls = "summary-table" + (isCompetitorTable ? " competitor-table" : "");
    out.push(`<table class="${tcls}"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`);
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
      const usup = _bulletSupInner(ul[1]);
      if (usup) { flushLists(); out.push(_supCallout(usup.inner, usup.src)); continue; }
      if (inOList) { out.push("</ol>"); inOList = false; }
      if (!inList)  { out.push("<ul>");  inList  = true;  }
      out.push(`<li>${_wrapSupplements(inlineMarkdown(ul[1]))}</li>`);
      continue;
    }

    // Ordered list item (1. 2. 3.) — render as bullet for visual consistency
    const ol = line.match(/^\d+[.)]\s+(.+)/);
    if (ol) {
      const osup = _bulletSupInner(ol[1]);
      if (osup) { flushLists(); out.push(_supCallout(osup.inner, osup.src)); continue; }
      if (inOList) { out.push("</ol>"); inOList = false; }
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${_wrapSupplements(inlineMarkdown(ol[1]))}</li>`);
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

    // Close lists before normal content. Split out 簡報補充 notes into their own
    // collapsible callout blocks; the surrounding public text stays as paragraphs.
    flushLists();
    for (const piece of _splitSupplements(line)) {
      if (piece.type === "sup") out.push(_supCallout(piece.text, piece.src));
      else if (piece.text.trim()) out.push(inlineMarkdown(piece.text));
    }
  }

  flushLists();
  if (inTable) flushTable();

  // Group consecutive non-empty lines into <p> blocks
  const html = [];
  let para = [];
  const BLOCK = s => s.startsWith("<h") || s.startsWith("<ul") || s.startsWith("</ul")
    || s.startsWith("<ol") || s.startsWith("</ol")
    || s.startsWith("<li")
    || s.startsWith("<div")
    || s.startsWith("<table") || s === "<hr>";
  for (const l of out) {
    if (l === "") {
      if (para.length) { html.push(_proseParagraphs(para.join(" "))); para = []; }
    } else if (BLOCK(l)) {
      if (para.length) { html.push(_proseParagraphs(para.join(" "))); para = []; }
      html.push(l);
    } else {
      para.push(l);
    }
  }
  if (para.length) html.push(_proseParagraphs(para.join(" ")));

  // Mark sections that were applied from uploaded materials (簡報) with a
  // distinct wrapper + chip so the user can see what came from the deck.
  const matSet = new Set(Array.isArray(matHeadings) ? matHeadings : []);
  if (matSet.size) {
    const result = [];
    let matOpen = false;
    const closeMat = () => { if (matOpen) { result.push("</div>"); matOpen = false; } };
    for (const item of html) {
      const hm = item.match(/^<h3>([\s\S]*)<\/h3>$/);
      if (hm) {
        closeMat();
        const text = hm[1].replace(/<[^>]+>/g, "").trim();
        if (matSet.has(text)) {
          result.push('<div class="summary-mat-section">');
          matOpen = true;
          result.push(`<h3><span class="mat-h3-label">${hm[1]} <span class="summary-mat-icon" title="此段含補充資訊">${_CLIP_SVG}</span></span></h3>`);
          continue;
        }
      }
      result.push(item);
    }
    closeMat();
    return result.join("\n");
  }

  return html.join("\n");
}

function inlineMarkdown(str) {
  return escHtml(str)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

// Inline SVG paperclip (consistent across OSes, unlike the 📎 emoji which renders
// thin/ugly on some systems). Tilted clip, inherits colour via currentColor.
const _CLIP_SVG = '<svg class="ico-clip" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';

// Supplement markers by source. All markers are「（XX補充」(opening paren + 4 hanzi).
const _SUP_RE = /（(簡報|訪談|介紹|筆記)補充/;
const _SUP_META = {
  "簡報": { cls: "deck",  icon: _CLIP_SVG, label: "簡報補充" },
  "訪談": { cls: "talk",  icon: "🎙", label: "訪談補充" },
  "介紹": { cls: "intro", icon: "📄", label: "介紹補充" },
  "筆記": { cls: "note",  icon: "✏", label: "筆記補充" },
};
const _SUP_MARKLEN = 5;  // （ + 簡報/訪談/… (2) + 補充 (2)

// Find the earliest supplement marker at/after `from`. Returns {idx, src, meta} or null.
function _findSup(str, from) {
  const m = _SUP_RE.exec(str.slice(from));
  if (!m) return null;
  return { idx: from + m.index, src: m[1], meta: _SUP_META[m[1]] };
}

// Span of one「（XX補充…）」note starting at idx. Returns {inner, end}: `inner` is
// the note text (marker + outer parens stripped), `end` is just past the note.
// Handles inline「（XX補充：…）」(balanced full-width parens, nested-safe) and the
// prefix「（XX補充）整段…」form (whole remainder of the line is the note).
function _supSpan(str, idx) {
  const sep = str[idx + _SUP_MARKLEN];
  if (sep === "）") {
    return { inner: str.slice(idx + _SUP_MARKLEN + 1), end: str.length };
  }
  let depth = 0, j = idx;
  for (; j < str.length; j++) {
    if (str[j] === "（") depth++;
    else if (str[j] === "）") { depth--; if (depth === 0) { j++; break; } }
  }
  const innerStart = idx + _SUP_MARKLEN + (sep === "：" || sep === ":" ? 1 : 0);
  return { inner: str.slice(innerStart, j - 1), end: j };
}

// If a list item is itself a supplement note — marker at the start, possibly
// inside a leading **bold** (risks: "**（訪談補充）標題**：…") — return the bullet
// text with just the marker token removed + its source. Otherwise null.
function _bulletSupInner(raw) {
  const m = /^(\*\*)?（(簡報|訪談|介紹|筆記)補充[）：]/.exec(raw);
  if (!m) return null;
  const src = m[2];
  const idx = raw.indexOf("（" + src + "補充");
  if (raw[idx + _SUP_MARKLEN] === "）") {
    return { inner: raw.slice(0, idx) + raw.slice(idx + _SUP_MARKLEN + 1), src };
  }
  const { inner, end } = _supSpan(raw, idx);
  return { inner: raw.slice(0, idx) + inner + raw.slice(end), src };
}

// Inline highlight for supplements inside list items (kept inline so the bullet
// structure isn't broken). Wraps the note in a source-coloured span.
function _wrapSupplements(html) {
  let out = "", i = 0;
  for (;;) {
    const f = _findSup(html, i);
    if (!f) { out += html.slice(i); break; }
    out += html.slice(i, f.idx);
    const { end } = _supSpan(html, f.idx);
    out += `<span class="mat-supplement mat-sup-${f.meta.cls}">${html.slice(f.idx, end)}</span>`;
    i = end;
  }
  return out;
}

// Split a paragraph line into public-text pieces and supplement notes (which
// become collapsible, source-coloured callout blocks).
function _splitSupplements(line) {
  const pieces = [];
  let i = 0;
  for (;;) {
    const f = _findSup(line, i);
    if (!f) { if (i < line.length) pieces.push({ type: "text", text: line.slice(i) }); break; }
    if (f.idx > i) pieces.push({ type: "text", text: line.slice(i, f.idx) });
    const { inner, end } = _supSpan(line, f.idx);
    pieces.push({ type: "sup", text: inner, src: f.src });
    i = end;
  }
  return pieces;
}

// A callout body already carries a source label, so any further「（XX補充…）」
// markers inside it are redundant double-tagging. They also mark the natural
// topic boundaries, so split the body INTO paragraphs at those markers (instead
// of flattening to one wall of text): head text + each note's content = one 段.
function _splitCalloutBody(inner) {
  const parts = [];
  let i = 0;
  for (;;) {
    const f = _findSup(inner, i);
    if (!f) { const t = inner.slice(i).trim(); if (t) parts.push(t); break; }
    const t = inner.slice(i, f.idx).trim(); if (t) parts.push(t);
    const { inner: sub, end } = _supSpan(inner, f.idx);
    const s = sub.trim(); if (s) parts.push(s);
    i = end;
  }
  return parts.length ? parts : [inner.trim()];
}

function _supCallout(inner, src) {
  const meta = _SUP_META[src] || _SUP_META["簡報"];
  const body = _splitCalloutBody(inner).map(p => `<p>${inlineMarkdown(p)}</p>`).join("");
  return `<div class="sup-callout sup-${meta.cls} open">` +
    '<div class="sup-callout-head" onclick="this.parentElement.classList.toggle(&quot;open&quot;)">' +
    `<span class="sup-callout-label">${meta.icon} ${meta.label}</span>` +
    '<span class="sup-callout-caret">▸</span></div>' +
    `<div class="sup-callout-body">${body}</div></div>`;
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
