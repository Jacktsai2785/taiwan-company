/* ── State ── */
const state = {
  companies: [],
  industries: [],
  labels: [],
  groups: {},                    // {industry: [group, ...]}
  industryTree: {},              // {parentName: [childName, ...]} from config
  expandedTreeNodes: new Set(), // parents expanded in side panel
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

/* ── AI engine selection (localStorage) ── */
const AI_ENGINE_LABELS = {
  claude: "本機 Claude",
  codex:  "GPT (Codex)",
  gemini: "Gemini",
  ollama: "開源模型 (Ollama)",
};

function getAiEngine() {
  return localStorage.getItem("ai_engine") || "claude";
}

function _updateAiModeLabel() {
  const el = document.getElementById("ai-mode-label");
  if (!el) return;
  const eng = getAiEngine();
  el.textContent = AI_ENGINE_LABELS[eng] || eng;
}

function openSettings() {
  const eng = getAiEngine();
  const radio = document.querySelector(`input[name="ai-engine"][value="${eng}"]`);
  if (radio) radio.checked = true;
  openOverlay("settings-overlay");
}

function saveSettings() {
  const eng = document.querySelector('input[name="ai-engine"]:checked')?.value || "claude";
  localStorage.setItem("ai_engine", eng);
  closeOverlay("settings-overlay");
  _updateAiModeLabel();
  toast(`已切換為「${AI_ENGINE_LABELS[eng] || eng}」引擎`);
}

function closeSettings() {
  closeOverlay("settings-overlay");
}

/* ── API helpers ── */
async function api(method, path, body) {
  const opts = { method, headers: {} };
  opts.headers["X-AI-Engine"] = getAiEngine();
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

/* ── Overlay open/close ──
   Every modal/overlay in this app toggles visibility via a shared "open" CSS
   class. These two helpers replace the same two-line
   document.getElementById(id).classList.add/remove("open") pattern that was
   copy-pasted at ~35 call sites. Accepts either an id string or an already-
   fetched element (several callers keep a local `overlay` const). */
function openOverlay(idOrEl) {
  const el = typeof idOrEl === "string" ? document.getElementById(idOrEl) : idOrEl;
  el?.classList.add("open");
}
function closeOverlay(idOrEl) {
  const el = typeof idOrEl === "string" ? document.getElementById(idOrEl) : idOrEl;
  el?.classList.remove("open");
  // modal 關閉時把窗內建立的 SSE（專利、findbiz 串流）一併關掉，
  // 否則串流途中關窗會留下殭屍長連線（後端掛著直到任務結束）
  if (el && el.id === "modal-overlay") _closeModalStreams();
}

/* modal 範圍的 EventSource 註冊表——只收「關窗即無意義」的串流（專利、findbiz）；
   summarize/deep-enrich 刻意不收：那是背景生成，關窗後要繼續跑並靠 SSE 清 enrichingIds */
const _modalStreams = [];
function trackModalES(es) { _modalStreams.push(es); return es; }
function _closeModalStreams() {
  while (_modalStreams.length) {
    const es = _modalStreams.pop();
    try { es.close(); } catch (_) {}
  }
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

async function dismissArticle(url, title, source, sourceUrl, btn) {
  _dismissedUrls.add(url);
  const item = btn.closest(".daily-news-item");
  if (item) { item.style.opacity = "0"; item.style.height = "0"; item.style.overflow = "hidden"; item.style.transition = "opacity .2s, height .3s"; }
  try {
    const data = await api("POST", "/api/news/dismiss", { url, title, source, source_url: sourceUrl || "" });
    if (data.dismissed_count % 5 === 0 && data.rules?.ai_summary) {
      _showAiHint("AI 已更新過濾規則：" + data.rules.ai_summary);
    }
  } catch { /* article already hidden */ }
}

/* ── Boot ── */
async function boot() {
  await Promise.all([loadIndustries(), loadIndustryTree(), loadCompanies(), loadLabels(), loadLabelGroups()]);
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
  // Show engine picker only on very first visit (localStorage never set)
  if (localStorage.getItem("ai_engine") === null) openSettings();
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

async function loadIndustryTree() {
  state.industryTree = await api("GET", "/api/config/industry-tree");
}

function _industryFilterSet(name) {
  const children = (state.industryTree[name] || []).filter(c => state.industries.includes(c));
  return new Set([name, ...children]);
}

async function loadLabels() {
  state.labels = await api("GET", "/api/config/labels");
}

async function loadLabelGroups() {
  state.labelGroups = await api("GET", "/api/config/label-groups");
}

async function loadCompanies() {
  // view=list：輕量投影（無 summary/directors/patents 等重欄位），payload 從 ~5MB
  // 降到 ~0.3MB。完整資料由 openModal 開窗時單筆抓取。
  const fresh = await api("GET", "/api/companies?view=list");
  // modal 開著時，把該公司已抓到的重欄位搬到新物件上，
  // 避免背景 refetch 後視窗內的互動（關係圖、專利表）突然缺資料
  try {
    if (_modalCompanyId) {
      const old = (state.companies || []).find(c => c.id === _modalCompanyId);
      const neu = fresh.find(c => c.id === _modalCompanyId);
      if (old && neu) {
        for (const k of ["summary", "competitors", "directors", "patents",
                         "investee_candidates", "materials_summary"]) {
          if (!(k in neu) && k in old) neu[k] = old[k];
        }
      }
    }
  } catch (_) {}
  state.companies = fresh;
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
      pool = pool.filter(c => !(c.industries || []).length);
    } else if (state.activeLabelIndustry) {
      pool = pool.filter(c => (c.industries || []).includes(state.activeLabelIndustry));
    }
  } else if (state.activeIndustry) {
    const _fset = _industryFilterSet(state.activeIndustry);
    pool = pool.filter(c => (c.industries || []).some(i => _fset.has(i)));
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
    for (const ind of (c.industries && c.industries.length ? c.industries : [""])) {
      if (!g[ind]) g[ind] = [];
      for (const label of (c.labels || [])) {
        if (!g[ind].includes(label)) g[ind].push(label);
      }
    }
  }
  state.groups = g;
}

