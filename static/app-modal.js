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
       <span class="info-value website-value"><a href="${escAttr(c.website)}" target="_blank" rel="noopener noreferrer">${escHtml(c.website.replace(/\/$/, ""))}</a><button class="edit-website-btn" onclick="editWebsite()" title="編輯官方網站">✏️ 編輯</button></span>`
    : `<span class="info-label">官方網站</span>
       <span class="info-value website-value"><span class="muted">—</span><button class="edit-website-btn" onclick="editWebsite()" title="新增官方網站">＋ 新增</button></span>`;
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
    <span class="info-value modal-industry-wrap" id="modal-industry-wrap">
      ${(c.industries || []).map(ind => `<span class="modal-ind-chip">${escHtml(ind)}<button class="modal-ind-remove" onclick="removeModalIndustry('${escAttr(ind)}')" title="移除">×</button></span>`).join("")}
      <select id="modal-industry-select" onchange="addModalIndustry()">
        <option value="">＋ 新增產業別</option>
        ${state.industries.filter(ind => !(c.industries || []).includes(ind)).map(ind => `<option value="${escHtml(ind)}">${escHtml(ind)}</option>`).join("")}
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
    // 後端查找完成一定會寫入 patents 陣列（即使查無資料也是空陣列），
    // 故以「是否為陣列」區分「已查找過但無結果」與「從未查找」。
    const scanned = Array.isArray(c.patents);
    if (patentStatus) patentStatus.innerHTML = scanned
      ? '<p class="summary-placeholder">已查詢專利資料庫，查無此公司的專利資料。如需重新查詢，點右上「📋 生成專利」。</p>'
      : '<p class="summary-placeholder">尚未生成專利資料，點右上「📋 生成專利」開始（約 15–45 秒）。</p>';
  }

  _refreshModalBookmarks();
  showModalSection("info");   // default page

  openOverlay("modal-overlay");
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
const _COMP_TYPE_DEFS = {
  "正面競業": "同產品／服務，直接搶相同客戶或標案",
  "替代路徑": "客戶解決相同問題的不同技術或商業模式",
  "側翼潛入": "現在不在此市場，但有能力、有誘因跨入的鄰近業者（如大型集團、跨國廠商）",
  "垂直整合": "重要客戶或上游供應商有可能自行發展相同能力者",
};

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
    add.textContent = "⚙ 編輯競業";
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
    // exclude comp-del-col (delete button appended during render) so the last data cell is 競業類型
    const cells = [...tr.querySelectorAll("td")].filter(td => !td.classList.contains("comp-del-col"));
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
    if (_COMP_TYPE_DEFS[t]) tab.dataset.tip = _COMP_TYPE_DEFS[t];   // hover 顯示定義
    tab.innerHTML = `${t}<span class="comp-type-ct">${n}</span>`;
    tab.onclick = () => _showCompetitorType(page, t);
    bar.appendChild(tab);
  });
  const add = document.createElement("button");
  add.className = "add-comp-btn comp-bar-add";
  add.textContent = "⚙ 編輯競業";
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
  btn.textContent = open ? "✕ 取消" : "⚙ 編輯競業";
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

  btn.disabled = true;
  status.innerHTML = `🔍 AI 分析中（約 30–60 秒）<span class="dots-anim"><span>.</span><span>.</span><span>.</span></span>`;
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

async function removeCompetitorRow(btn) {
  const id = _modalCompanyId;
  if (!id) return;
  const name = btn.dataset.cname || "";
  const disp = _displayCompName(name) || name;
  if (!confirm(`確定要刪除競業「${disp}」嗎？\n此操作會從競業分析表格移除這一列。`)) return;

  // 刪除後要停留在原本檢視的競業類型頁籤
  const page = btn.closest(".summary-tab-page");
  const prevType = page ? page.dataset.activeCtype : null;

  btn.disabled = true;
  try {
    const res = await api("POST", `/api/companies/${id}/competitors/remove`, { name });
    const c = state.companies.find(x => x.id === id);
    if (c) { c.summary = res.summary; c.competitors = res.competitors; }
    _updateSummaryInModal(c);   // rebuild summary tabs without the removed row
    const ct = [...document.querySelectorAll("#summary-tabbar .summary-tab")].find(t => t.textContent.includes("競業"));
    if (ct) ct.click();         // stay on 競業分析 tab
    if (prevType) {
      const compPage = [...document.querySelectorAll("#modal-summary .summary-tab-page")]
        .find(p => p.querySelector(".comp-type-bar"));
      if (compPage) _showCompetitorType(compPage, prevType);
    }
    toast(`已刪除競業：${disp}`);
  } catch (err) {
    btn.disabled = false;
    toast(`刪除失敗：${err.message}`);
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



function _modalIndustries() {
  const c = state.companies.find(x => x.id === _modalCompanyId);
  return c ? [...(c.industries || [])] : [];
}

async function _saveModalIndustries(newInds) {
  const id = _modalCompanyId;
  if (!id) return;
  try {
    const updated = await api("PUT", `/api/companies/${id}`, { industries: newInds });
    const idx = state.companies.findIndex(c => c.id === id);
    if (idx !== -1) state.companies[idx] = updated;
    computeGroups();
    renderSidebar();
    renderGrid();
    // re-render just the industry section
    const wrap = document.getElementById("modal-industry-wrap");
    if (wrap) {
      const c = state.companies[state.companies.findIndex(x => x.id === id)];
      wrap.innerHTML =
        (c.industries || []).map(ind => `<span class="modal-ind-chip">${escHtml(ind)}<button class="modal-ind-remove" onclick="removeModalIndustry('${escAttr(ind)}')" title="移除">×</button></span>`).join("") +
        `<select id="modal-industry-select" onchange="addModalIndustry()"><option value="">＋ 新增產業別</option>${state.industries.filter(i => !(c.industries || []).includes(i)).map(i => `<option value="${escHtml(i)}">${escHtml(i)}</option>`).join("")}</select>`;
    }
  } catch (err) {
    toast(`更新失敗：${err.message}`, true);
  }
}

async function addModalIndustry() {
  const sel = document.getElementById("modal-industry-select");
  const ind = sel ? sel.value : "";
  if (!ind) return;
  const inds = _modalIndustries();
  if (!inds.includes(ind)) await _saveModalIndustries([...inds, ind]);
}

async function removeModalIndustry(ind) {
  const inds = _modalIndustries().filter(i => i !== ind);
  await _saveModalIndustries(inds);
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
  openOverlay("findbiz-overlay");
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
  closeOverlay("findbiz-overlay");
  _findBizSessionId = null;
}

