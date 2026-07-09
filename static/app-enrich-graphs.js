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
      companies = companies.filter(c => !(c.industries || []).length);
      scopeLabel = `標籤：${state.activeLabel} — 未分類`;
    } else if (state.activeLabelIndustry) {
      companies = companies.filter(c => (c.industries || []).includes(state.activeLabelIndustry));
      scopeLabel = `標籤：${state.activeLabel} — ${state.activeLabelIndustry}`;
    } else {
      scopeLabel = `標籤：${state.activeLabel}`;
    }
  } else if (state.activeIndustry) {
    companies = companies.filter(c => (c.industries || []).includes(state.activeIndustry));
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
  closeOverlay("bulk-enrich-overlay"));

async function startBulkEnrich(ids, mode, scopeLabel) {
  if (!ids || ids.length === 0) {
    toast("範圍內沒有需要生成的公司");
    return;
  }
  const modeLabel = mode === "resume" ? "補齊未完成" : "全部重跑";
  toast(`▶ ${modeLabel}：${scopeLabel}，共 ${ids.length} 間`);

  // 決定批次大小：>10 間時詢問是否分批，與既有 confirm 流程一致
  let batchSize = ids.length;
  let autoRunAll = false;
  if (ids.length > 10) {
    const wantBatch = confirm(
      `本次共需生成 ${ids.length} 間公司資料。\n` +
      `數量較多，建議分批，以免 AI 額度暫時用滿造成延遲（用滿時會自動等待續跑）。\n\n` +
      `是否分批生成？\n[確定] = 分批　[取消] = 一次全跑`
    );
    if (wantBatch) {
      const input = prompt(`每批要同時生成幾間？（建議 3–5）`, "5");
      const n = parseInt(input, 10);
      if (!input || isNaN(n) || n < 1) {
        toast("已取消分批生成", true);
        return;
      }
      batchSize = Math.max(1, n);
      autoRunAll = confirm(
        `要全部自動跑完嗎？\n\n` +
        `[確定] = 全部自動跑完（中途可按畫面右下「■ 停止批次」喊停）\n` +
        `[取消] = 每批跑完都問我一次再續`
      );
    }
  }

  await runEnrichmentInBatches(ids, batchSize, autoRunAll);
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
  state.enrichingIds.add(companyId);
  _startEnrichPoll();
  renderGrid();

  const sseUrl = `/api/companies/enrich/${companyId}?engine=${encodeURIComponent(getAiEngine())}`;

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
  openOverlay("rel-graph-overlay");
  document.body.classList.add("rel-open");
  _expandParentRows();
  // Cytoscape needs a layout pass after the container resizes (side-by-side mode shrinks it)
  setTimeout(() => switchRelTab("ownership"), 50);
}

function closeRelationshipGraph() {
  closeOverlay("rel-graph-overlay");
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

  openOverlay("from-graph-overlay");
}

function _closeFromGraphDialog() {
  closeOverlay("from-graph-overlay");
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
// 縮放模型：地圖跟著「產業樹的節點」走，沒有窄/中/廣。有子產業 → 父模式（子產業當可
// 展開的磚 + 未分類區），無子產業 → 葉模式（完整公司歸位）。stack 記錄 drill 路徑，
// 讓子產業能「← 返回」父產業。
const _imState = {
  industry: null,
  data: null,
  generating: false,
  evtSrc: null,
  stack: [],   // drill 路徑上層的產業名（不含目前這層）
};

// 葉模式「產業鏈」呈現用的狀態與常數
const _IM_MAX = 8;                 // 精簡模式每張卡片最多顯示幾家公司，超過用「⋯ 還有 N 家」
let _imChainSections = [];         // 目前葉圖排序後的 sections（供聚焦/hover 取用）
let _imFocus = null;               // 目前聚焦的分區 index（null = 全部顯示）

// 分區配色（沉穩多色，供左軸圓點／面板標題／左緣識別；公司卡片本身維持中性）
const _IM_PAL = ["#3b6ea3","#2f857a","#4f8a52","#b07f3e","#6a5f9e","#a85a76","#2f7f97","#8a7a3e","#4a6fa0","#9c5a6a"];

// 依分類名稱關鍵字對應圖示；對不到用通用符號
const _IM_ICONS = [
  [/發電|電廠|能源開發|renewable|power\b/i, "⚡"],
  [/太陽|光電|solar/i, "☀️"], [/風力|風電|離岸|wind/i, "🌬️"],
  [/電池|儲能|battery|storage/i, "🔋"],
  [/半導|晶圓|晶片|\bIC\b|chip|wafer|semi/i, "🔲"],
  [/材料|化學|material|chemical/i, "🧪"],
  [/循環|回收|廢棄|再生|recycl|circular/i, "♻️"],
  [/碳|ESG|永續|淨零|sustain|carbon/i, "🌱"],
  [/水處理|廢水|環境工程|water|environ/i, "💧"],
  [/醫療|生技|診斷|健康|醫材|medical|bio|health/i, "🧬"],
  [/農業|食品|畜|agri|food/i, "🌾"],
  [/軟體|雲端|SaaS|software|cloud/i, "☁️"],
  [/\bAI\b|人工智慧|智慧|智能|影像辨識/i, "🤖"],
  [/資安|資訊安全|security|cyber/i, "🛡️"],
  [/金融|支付|fintech|pay|銀行/i, "💳"],
  [/通訊|射頻|網通|5G|telecom|\brf\b/i, "📡"],
  [/製造|工業|自動化|工業物聯|manufactur|industr/i, "🏭"],
  [/電動|車|運具|載具|mobility|vehicle|\bev\b/i, "🚗"],
  [/消費|品牌|零售|電商|retail|consumer|brand/i, "🛍️"],
  [/顧問|服務|平台|整合|consult|platform|service/i, "🧩"],
  [/法律|legal|保險|insur/i, "⚖️"],
];
function _imSectionIcon(title) {
  const t = title || "";
  for (const [re, ic] of _IM_ICONS) if (re.test(t)) return ic;
  return "▪";
}

async function openIndustryMap(industry, opts = {}) {
  if (!industry) return;
  // drill 進子產業時把目前這層推進 stack；從工具列重新打開則清空
  if (opts.drill && _imState.industry) {
    _imState.stack.push(_imState.industry);
  } else if (!opts.drill) {
    _imState.stack = [];
  }
  _imState.industry = industry;
  _imState.data = null;
  document.getElementById("industry-map-title").textContent = `${industry} — 產業地圖`;
  _imRenderBreadcrumb();
  document.getElementById("industry-map-status").innerHTML = "";
  document.getElementById("industry-map-canvas").innerHTML = "";
  document.getElementById("industry-map-meta").innerHTML = "";
  openOverlay("industry-map-overlay");

  // Try cached first
  try {
    const cached = await api("GET", `/api/industry-map/${encodeURIComponent(industry)}`);
    _imState.data = cached;
    _renderIndustryMap(cached);
  } catch (err) {
    // 404 → 顯示空狀態，提示按生成
    document.getElementById("industry-map-canvas").innerHTML = `
      <div class="im-empty">
        <div class="im-empty-icon">🗺️</div>
        <div class="im-empty-title">尚未生成「${escHtml(industry)}」的產業地圖</div>
        <div class="im-empty-hint">按右上方「生成地圖」</div>
      </div>`;
  }
}

function _imRenderBreadcrumb() {
  const el = document.getElementById("industry-map-subtitle");
  if (!el) return;
  if (!_imState.stack.length) { el.innerHTML = ""; return; }
  const parent = _imState.stack[_imState.stack.length - 1];
  el.innerHTML = `<button class="im-back-btn" onclick="_imDrillUp()">← 返回 ${escHtml(parent)}</button>`
    + `<span class="im-crumb">${_imState.stack.map(escHtml).join(" › ")} › <b>${escHtml(_imState.industry)}</b></span>`;
}

function _imDrillUp() {
  const parent = _imState.stack.pop();
  if (parent) openIndustryMap(parent);  // 不帶 drill：不再往 stack 推，直接回到父層
}

function closeIndustryMap() {
  if (_imState.evtSrc) {
    try { _imState.evtSrc.close(); } catch (_) {}
    _imState.evtSrc = null;
  }
  _imState.generating = false;
  _imState.stack = [];
  closeOverlay("industry-map-overlay");
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

  const params = new URLSearchParams({ engine: getAiEngine() });
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
  const isParent = data.mode === "parent";
  const sections = [...data.sections].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  const stats = data.stats || {};
  const ts = formatTimestamp(data.generated_at);
  const statPill = isParent
    ? `<span class="im-meta-pill">${stats.child_count ?? 0} 個子產業${stats.unclassified_count ? ` · 未分類 ${stats.unclassified_count} 家` : ""}</span>`
    : `<span class="im-meta-pill">共 ${sections.length} 個主分類 / ${stats.rendered_nodes ?? 0} 家公司</span>`
      + `<span class="im-meta-pill">已收錄 ${stats.in_db_count ?? "?"} 家 · 擴充候選 ${stats.expansion_pool_count ?? "?"} 家</span>`;
  // 葉圖：若這些分類含已收錄公司，提供「收成子產業」；父圖：提供「取消細分」還原成單張完整地圖
  let actionBanner = "";
  if (isParent) {
    const children = sections.filter(s => s.drill_to).map(s => s.drill_to);
    if (children.length) {
      actionBanner = `<div class="im-subdivide-banner im-merge-banner">
         <span>此產業已細分為 ${children.length} 個子產業（總覽模式）。想回到<b>單張完整地圖</b>（含 AI 挖出的競業候選）？取消細分即可還原。</span>
         <button class="im-merge-btn" onclick="_imMergeSubdivision()">↩ 取消細分</button>
       </div>`;
    }
  } else {
    const leafGroups = _imGroupsFromSections(sections);
    if (leafGroups.length >= 2) {
      actionBanner = `<div class="im-subdivide-banner">
         <span>公司很多、想分層瀏覽時，可把這 ${leafGroups.length} 個分類收成子產業（改為總覽＋各子產業獨立地圖，<b>隨時可取消細分還原</b>）。小產業建議保留這張含競業候選的完整地圖。</span>
         <button class="im-subdivide-btn" onclick="_imPromoteLeaf()">✨ 收成子產業</button>
       </div>`;
    }
  }
  meta.innerHTML = `
    <div class="im-meta">
      <span class="im-meta-pill">${layout === "layered" ? "🪜 上下分層" : "🔲 矩陣並列"}</span>
      ${statPill}
      ${ts ? `<span class="im-meta-time">生成於 ${escHtml(ts)}</span>` : ""}
      ${data.rationale ? `<div class="im-rationale">${escHtml(data.rationale)}</div>` : ""}
    </div>${actionBanner}`;

  if (isParent) {
    canvas.className = `im-canvas im-${layout}`;
    canvas.innerHTML = sections.map(s => _imSectionHtml(s, layout)).join("");
    canvas.onmouseover = null;
    canvas.onmouseout = null;
  } else {
    // 葉模式：C 版「產業鏈上下游」——左時間軸 + 右面板牆 + 聚焦 + 漸進揭露
    canvas.className = "im-canvas im-leaf";
    _imChainSections = sections;
    _imFocus = null;
    canvas.innerHTML = _imLeafChainHtml(sections);
    _imWireChainHover(canvas);
  }

  // Wire clicks (delegate on canvas)
  canvas.onclick = ev => {
    // 父模式 drill：點子產業磚 → 進入該子產業的地圖
    const drill = ev.target.closest("[data-drill-to]");
    if (drill) {
      openIndustryMap(drill.dataset.drillTo, { drill: true });
      return;
    }
    // 葉模式 C：聚焦控制（點左軸環節／面板標題／「還有 N 家」都聚焦；重置鈕還原）
    if (!isParent) {
      if (ev.target.closest(".im-chain-reset")) { _imChainSetFocus(null); return; }
      const more = ev.target.closest(".im-more-chip");
      if (more) { _imChainSetFocus(+more.dataset.idx); return; }
      const stop = ev.target.closest(".im-stop");
      if (stop) { _imChainToggle(+stop.dataset.idx); return; }
      const ph = ev.target.closest(".im-panel-h");
      if (ph) { _imChainToggle(+ph.dataset.idx); return; }
    }
    // 公司卡：已收錄 → 開詳情；未收錄 → 加入
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

/* ── 葉模式：產業鏈上下游（左時間軸 + 面板牆 + 聚焦 + 漸進揭露）── */

function _imLeafChainHtml(sections) {
  const rail = `<div class="im-rail">
    <button class="im-chain-reset" type="button">✕ 顯示全部</button>
    <div class="im-rail-cap">↓ 上游 → 下游</div>
    ${sections.map((s, i) => {
      const z = _IM_PAL[i % _IM_PAL.length];
      const nco = (s.subgroups || []).reduce((n, g) => n + (g.companies || []).length, 0);
      return `<div class="im-stop" data-idx="${i}" style="--z:${z}" title="只看「${escAttr(s.title || "")}」">
        <span class="im-stop-dot"></span>
        <div class="im-stop-t">${_imSectionIcon(s.title)} ${escHtml(s.title || "")}</div>
        <div class="im-stop-c">${nco} 家 · ${(s.subgroups || []).length} 子分類</div>
      </div>`;
    }).join("")}
  </div>`;

  const wall = `<div class="im-wall">${sections.map((s, i) => {
    const z = _IM_PAL[i % _IM_PAL.length];
    const all = (s.subgroups || []).flatMap(g => g.companies || []);
    const shown = all.slice(0, _IM_MAX);
    const rest = all.length - shown.length;
    const compact = shown.map(_imCardHtml).join("")
      + (rest > 0 ? `<span class="im-more-chip" data-idx="${i}" title="點看完整名單">⋯ 還有 ${rest} 家</span>` : "");
    const full = (s.subgroups || []).map(_imSubgroupHtml).join("");
    return `<div class="im-panel" data-idx="${i}" style="--z:${z}">
      <div class="im-panel-h" data-idx="${i}" title="只看「${escAttr(s.title || "")}」的完整名單">
        <span class="im-panel-i">${_imSectionIcon(s.title)}</span>
        <span class="im-panel-t">${escHtml(s.title || "")}</span>
        <span class="im-panel-c">${all.length}</span>
      </div>
      <div class="im-panel-b">
        <div class="im-pb-compact"><div class="im-cards">${compact}</div></div>
        <div class="im-pb-full">${full}</div>
      </div>
    </div>`;
  }).join("")}</div>`;

  return `<div class="im-chain">${rail}${wall}</div>`;
}

function _imChainToggle(i) { _imChainSetFocus(_imFocus === i ? null : i); }
function _imChainSetFocus(i) { _imFocus = i; _imChainApplyFocus(); }
function _imChainApplyFocus() {
  const chain = document.querySelector(".im-chain");
  if (!chain) return;
  const on = _imFocus !== null;
  chain.classList.toggle("focusing", on);
  chain.querySelectorAll(".im-stop").forEach(el => {
    const i = +el.dataset.idx;
    el.classList.toggle("active", on && i === _imFocus);
    el.classList.toggle("dim", on && i !== _imFocus);
  });
  chain.querySelectorAll(".im-panel").forEach(el =>
    el.classList.toggle("hi", on && +el.dataset.idx === _imFocus));
}

// 滑鼠移上「⋯ 還有 N 家」→ 浮出被截斷公司的預覽
function _imWireChainHover(canvas) {
  let pop = document.getElementById("im-hover-pop");
  if (!pop) { pop = document.createElement("div"); pop.id = "im-hover-pop"; document.body.appendChild(pop); }
  const hide = () => { pop.style.display = "none"; };
  canvas.onmouseover = e => {
    const chip = e.target.closest(".im-more-chip");
    if (!chip) return;
    const s = _imChainSections[+chip.dataset.idx];
    if (!s) return;
    const extra = (s.subgroups || []).flatMap(g => g.companies || []).slice(_IM_MAX);
    if (!extra.length) return;
    pop.innerHTML = `<div class="hp-t">還有 ${extra.length} 家（點展開完整名單）</div>`
      + `<div class="hp-list">${extra.map(c =>
          `<span class="hp-item ${c.in_db ? "in" : "out"}">${escHtml(c.name || "")}</span>`).join("")}</div>`;
    pop.style.display = "block";
    const r = chip.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - pw - 10);
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = r.top - ph - 6;
    pop.style.left = Math.max(8, left) + "px";
    pop.style.top = Math.max(8, top) + "px";
  };
  canvas.onmouseout = e => { if (e.target.closest(".im-more-chip")) hide(); };
}

function _imSectionHtml(section, layout) {
  // 父模式：可展開的子產業磚（整塊可點 → drill-in）
  if (section.drill_to) {
    const reps = (section.subgroups?.[0]?.companies || []).map(_imCardHtml).join("");
    return `<div class="im-section im-section-child">
      <button class="im-child-head" data-drill-to="${escAttr(section.drill_to)}"
        title="展開「${escAttr(section.drill_to)}」的產業地圖">
        <span class="im-section-title">${escHtml(section.title || "")}</span>
        <span class="im-child-count">共 ${section.total_count ?? 0} 家 · 展開 →</span>
      </button>
      <div class="im-subgroups"><div class="im-subgroup"><div class="im-cards">${reps || `<div class="im-empty-sub">—</div>`}</div></div></div>
    </div>`;
  }
  // 未分類區塊：本產業底下尚未細分為子產業的公司
  if (section.unclassified) {
    const subs = (section.subgroups || []).map(_imSubgroupHtml).join("");
    return `<div class="im-section im-section-unclassified">
      <div class="im-section-head">
        <span class="im-section-title">${escHtml(section.title || "")}</span>
        <button class="im-subdivide-btn" onclick="_imSubdivide()" title="用 AI 把這些公司細分成子產業（Phase 2）">✨ 細分成子產業</button>
      </div>
      <div class="im-subgroups">${subs}</div>
    </div>`;
  }
  // 葉模式：一般主分類
  const subs = (section.subgroups || []).map(_imSubgroupHtml).join("");
  return `<div class="im-section">
    <div class="im-section-head"><span class="im-section-title">${escHtml(section.title || "")}</span></div>
    <div class="im-subgroups">${subs}</div>
  </div>`;
}

/* ── Phase 2：把產業細分成子產業（sections → 子產業 + retag） ── */

// 從地圖 sections 攤出「子產業分組」：section.title 當子產業名，其中已收錄公司當成員。
// 與後端 industry_map.groups_from_sections 同規則。
function _imGroupsFromSections(sections) {
  const groups = [];
  for (const s of sections || []) {
    const ids = [], names = [];
    for (const sub of (s.subgroups || [])) {
      for (const co of (sub.companies || [])) {
        if (co.in_db && co.company_id) { ids.push(co.company_id); names.push(co.name || ""); }
      }
    }
    const title = (s.title || "").trim();
    if (ids.length && title) {
      groups.push({ name: title, company_ids: ids, sample_names: names.slice(0, 6), count: ids.length });
    }
  }
  return groups;
}

// 葉圖橫幅：直接用現成 sections 收成子產業（不必再呼叫 AI）
function _imPromoteLeaf() {
  const groups = _imGroupsFromSections(_imState.data?.sections || []);
  if (groups.length < 2) { toast("目前分類不足以收成子產業", true); return; }
  _imShowSubdividePreview(_imState.industry, groups);
}

// 父圖橫幅：取消細分 → 子產業合併回本產業，回到單張完整地圖（含競業候選）
async function _imMergeSubdivision() {
  const industry = _imState.industry;
  const sections = _imState.data?.sections || [];
  const children = sections.filter(s => s.drill_to).map(s => s.drill_to);
  if (!industry || !children.length) return;
  // 估合併後的公司總數：各子產業家數 + 未分類
  const total = sections.reduce((n, s) => n + (s.total_count || 0), 0);
  const LEAF_MAX = 70;
  let msg =
    `確定取消「${industry}」的細分？\n\n` +
    `這會把 ${children.length} 個子產業（${children.join("、")}）合併回「${industry}」，` +
    `公司重新掛回「${industry}」，並移除這些子產業。\n\n` +
    `之後「${industry}」會變回單張完整地圖，重新生成即可看到 AI 挖出的競業候選。會先自動備份。`;
  if (total > LEAF_MAX) {
    msg =
      `⚠ 注意：「${industry}」合併後會有約 ${total} 家公司。\n\n` +
      `這麼多公司「無法」生成單張地圖（會逾時）——大產業本來就該分層。\n` +
      `你可能只是想還原某個「小的子產業」？那請先 drill 進那個子產業再取消細分，不要在這一層。\n\n` +
      `仍要把 ${children.length} 個子產業全部壓平成單一「${industry}」嗎？`;
  }
  const ok = confirm(msg);
  if (!ok) return;
  try {
    const res = await api("POST", `/api/industry-map/${encodeURIComponent(industry)}/merge`, {});
    if (res.industries) state.industries = res.industries;
    if (res.tree) state.industryTree = res.tree;
    await loadCompanies();
    renderSidebar();
    toast(`已把 ${res.merged?.length ?? 0} 個子產業合併回「${industry}」，${res.retagged ?? 0} 家公司歸位。請按「生成地圖」重建完整地圖`, false);
    openIndustryMap(industry);  // 已回葉模式、快取已刪 → 空狀態，按生成即得含競業的完整地圖
  } catch (err) {
    toast(`取消細分失敗：${err.message}`, true);
  }
}

// 未分類區塊：AI 分組要跑數分鐘 → 丟到背景執行，右下角浮動通知，完成可點檢視。
// 使用者可關掉地圖視窗、去看別的東西，工作在後端持續進行（SSE 只是接進度）。
let _imBgJob = null;  // { industry, es, status: "running"|"done"|"error", groups? }

function _imSubdivide() {
  const industry = _imState.industry;
  if (!industry) return;
  if (_imBgJob && _imBgJob.status === "running") {
    toast(`「${_imBgJob.industry}」正在背景細分中，完成會通知你`, false);
    return;
  }
  _imStartSubdivideJob(industry);
  toast(`已在背景細分「${industry}」，可關閉此視窗繼續操作，完成會通知你`, false);
}

function _imStartSubdivideJob(industry) {
  const url = `/api/industry-map/${encodeURIComponent(industry)}/subdivide/propose?engine=${encodeURIComponent(getAiEngine())}`;
  const es = new EventSource(url);
  _imBgJob = { industry, es, status: "running" };
  _imRenderBgJob({ industry, state: "running", message: "啟動中…" });

  es.onmessage = e => {
    let ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (ev.type === "progress") {
      if (_imBgJob && _imBgJob.status === "running")
        _imRenderBgJob({ industry, state: "running", message: ev.message });
    } else if (ev.type === "done") {
      es.close();
      const groups = ev.data?.groups || [];
      if (groups.length) {
        _imBgJob = { industry, es: null, status: "done", groups };
        _imRenderBgJob({ industry, state: "done", groups });
      } else {
        _imBgJob = { industry, es: null, status: "error" };
        _imRenderBgJob({ industry, state: "error", message: "AI 未能產生有效分組，請重試" });
      }
    } else if (ev.type === "error") {
      es.close();
      _imBgJob = { industry, es: null, status: "error" };
      _imRenderBgJob({ industry, state: "error", message: ev.message });
    }
  };
  es.onerror = () => {
    es.close();
    if (_imBgJob && _imBgJob.status === "running") {
      _imBgJob = { industry, es: null, status: "error" };
      _imRenderBgJob({ industry, state: "error", message: "連線中斷，請重試" });
    }
  };
}

// 右下角浮動通知（跨視窗、開關地圖都在）
function _imRenderBgJob({ industry, state, message, groups }) {
  let el = document.getElementById("im-bg-job");
  if (!el) {
    el = document.createElement("div");
    el.id = "im-bg-job";
    document.body.appendChild(el);
  }
  el.className = `im-bg-job im-bg-${state}`;
  if (state === "running") {
    el.innerHTML = `
      <span class="im-bg-spin"></span>
      <div class="im-bg-text">
        <b>細分「${escHtml(industry)}」中…</b>
        <small>${escHtml(message || "")}　可繼續操作，完成會通知你</small>
      </div>`;
  } else if (state === "done") {
    const n = (groups || []).length;
    el.innerHTML = `
      <span class="im-bg-icon">✅</span>
      <div class="im-bg-text"><b>「${escHtml(industry)}」細分完成</b><small>AI 提議 ${n} 個子產業</small></div>
      <button class="im-bg-action" onclick="_imBgReview()">檢視分組</button>
      <button class="im-bg-dismiss" onclick="_imDismissBgJob()" aria-label="關閉">✕</button>`;
  } else {
    el.innerHTML = `
      <span class="im-bg-icon">⚠️</span>
      <div class="im-bg-text"><b>「${escHtml(industry)}」細分失敗</b><small>${escHtml(message || "")}</small></div>
      <button class="im-bg-action" onclick="_imBgRetry()">重試</button>
      <button class="im-bg-dismiss" onclick="_imDismissBgJob()" aria-label="關閉">✕</button>`;
  }
}

function _imBgReview() {
  if (!_imBgJob || _imBgJob.status !== "done") return;
  const { industry, groups } = _imBgJob;
  _imDismissBgJob();
  _imShowSubdividePreview(industry, groups);
}

function _imBgRetry() {
  const industry = _imBgJob?.industry;
  _imDismissBgJob();
  if (industry) { _imStartSubdivideJob(industry); toast(`重新在背景細分「${industry}」…`, false); }
}

function _imDismissBgJob() {
  const el = document.getElementById("im-bg-job");
  if (el) el.remove();
  if (_imBgJob?.es) { try { _imBgJob.es.close(); } catch (_) {} }
  _imBgJob = null;
}

// 共用預覽 modal：勾選要收成的子產業，確認後 apply
function _imShowSubdividePreview(industry, groups) {
  let overlay = document.getElementById("im-subdivide-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "im-subdivide-overlay";
    document.body.appendChild(overlay);
  }
  const total = groups.reduce((n, g) => n + g.company_ids.length, 0);
  const rows = groups.map((g, i) => `
    <label class="im-sd-row">
      <input type="checkbox" class="im-sd-check" data-i="${i}" checked />
      <span class="im-sd-name">${escHtml(g.name)}</span>
      <span class="im-sd-count">${g.company_ids.length} 家</span>
      <span class="im-sd-sample">${escHtml((g.sample_names || []).join("、"))}</span>
    </label>`).join("");
  overlay.innerHTML = `
    <div class="im-sd-dialog" role="dialog" aria-modal="true">
      <div class="im-sd-header">
        <div class="im-sd-title">把「${escHtml(industry)}」細分成子產業</div>
        <button class="im-sd-close" onclick="_imCloseSubdivide()" aria-label="關閉">✕</button>
      </div>
      <div class="im-sd-body">
        <p class="im-sd-hint">確認後：勾選的子產業會加入產業樹掛在「${escHtml(industry)}」底下，
          對應的 <b>${total}</b> 家公司會從「${escHtml(industry)}」<b>移到</b>各自子產業；
          「${escHtml(industry)}」之後會變成可展開的父產業。寫入前會自動備份一次。未收錄的候選不受影響。</p>
        <div class="im-sd-list">${rows}</div>
      </div>
      <div class="im-sd-footer">
        <button class="btn btn-ghost" onclick="_imCloseSubdivide()">取消</button>
        <button class="btn btn-primary" id="im-sd-apply">✓ 收成子產業</button>
      </div>
    </div>`;
  overlay.classList.add("open");
  overlay.onclick = e => { if (e.target === overlay) _imCloseSubdivide(); };
  document.getElementById("im-sd-apply").onclick = () => {
    const picked = [];
    overlay.querySelectorAll(".im-sd-check").forEach(cb => {
      if (cb.checked) picked.push(groups[+cb.dataset.i]);
    });
    if (!picked.length) { toast("請至少勾選一個子產業", true); return; }
    _imApplySubdivide(industry, picked);
  };
}

function _imCloseSubdivide() {
  const overlay = document.getElementById("im-subdivide-overlay");
  if (overlay) overlay.classList.remove("open");
}

async function _imApplySubdivide(industry, groups) {
  const applyBtn = document.getElementById("im-sd-apply");
  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = "處理中…"; }
  try {
    const res = await api("POST", `/api/industry-map/${encodeURIComponent(industry)}/subdivide`, { groups });
    // 同步前端狀態（後端已回傳最新 tree / industries）
    if (res.industries) state.industries = res.industries;
    if (res.tree) state.industryTree = res.tree;
    await loadCompanies();
    renderSidebar();
    _imCloseSubdivide();
    toast(`已新增 ${res.added_children?.length ?? 0} 個子產業、重新歸類 ${res.retagged ?? 0} 家公司`, false);
    // 舊快取已被後端刪除 → 重新打開會以父模式重生成總覽
    openIndustryMap(industry);
  } catch (err) {
    if (applyBtn) { applyBtn.disabled = false; applyBtn.textContent = "✓ 收成子產業"; }
    toast(`收成失敗：${err.message}`, true);
  }
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

