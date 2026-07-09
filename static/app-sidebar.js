/* ── Sidebar ── */
function renderSidebar() {
  // 全部公司 / 面板入口
  const mainBtn = document.getElementById("sb-main-btn");
  const isAll = state.activeIndustry === null && state.activeLabel === null && state.activeLabelGroup === null;
  mainBtn.className = "sb-row" + (isAll ? " active" : "");
  document.getElementById("sb-all-count").textContent = state.companies.length;

  // 未分類警示
  const unclassifiedCount = state.companies.filter(c => !(c.industries || []).length).length;
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

    // Industry pinned rows — tree-aware
    if (pinnedIndustries.length > 0) {
      html += `<div class="sb-section-label">產業別</div>`;

      const _childSet = new Set(Object.values(state.industryTree).flat());
      const _pinnedSet = new Set(pinnedIndustries);

      // Auto-expand parent when a child is the active filter
      if (state.activeIndustry && _childSet.has(state.activeIndustry)) {
        for (const [_p, _kids] of Object.entries(state.industryTree)) {
          if ((_kids || []).includes(state.activeIndustry)) state.expandedTreeNodes.add(_p);
        }
      }

      // Top-level: not a child, OR is a child whose parent is not pinned (show standalone)
      const topLevelPinned = pinnedIndustries.filter(i => {
        if (!_childSet.has(i)) return true;
        for (const [_p, _kids] of Object.entries(state.industryTree)) {
          if ((_kids || []).includes(i) && _pinnedSet.has(_p)) return false;
        }
        return true;
      });

      for (const name of topLevelPinned) {
        const pinnedKids = (state.industryTree[name] || []).filter(c => state.industries.includes(c) && _pinnedSet.has(c));
        const hasKids = pinnedKids.length > 0;
        const fset = _industryFilterSet(name);
        const count = hasKids
          ? state.companies.filter(c => (c.industries || []).some(i => fset.has(i))).length
          : state.companies.filter(c => (c.industries || []).includes(name)).length;
        const isActive = state.activeIndustry === name && state.activeGroup === null;
        const isExpanded = state.expandedTreeNodes.has(name);

        const toggleBtn = hasKids
          ? `<button class="sb-tree-toggle" data-toggle="${escHtml(name)}">${isExpanded ? "▼" : "▶"}</button>`
          : `<span class="sb-tree-spacer"></span>`;

        html += `<div class="sb-row ${isActive ? "active" : ""}" data-pinned="${escHtml(name)}" data-is-label="false">
          ${toggleBtn}<span class="sb-label">${escHtml(name)}</span>
          <span class="sb-count">${count}</span>
        </div>`;

        if (hasKids && isExpanded) {
          for (const child of pinnedKids) {
            const childCount = state.companies.filter(c => (c.industries || []).includes(child)).length;
            const isChildActive = state.activeIndustry === child && state.activeGroup === null;
            html += `<div class="sb-row sb-ind-child ${isChildActive ? "active" : ""}" data-pinned="${escHtml(child)}" data-is-label="false">
              <span class="sb-label">${escHtml(child)}</span>
              <span class="sb-count">${childCount}</span>
            </div>`;
          }
        }
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

    // Industry tree toggle in sidebar
    pinnedEl.querySelectorAll(".sb-tree-toggle").forEach(btn => {
      btn.addEventListener("click", e => {
        e.stopPropagation();
        const name = btn.dataset.toggle;
        if (state.expandedTreeNodes.has(name)) state.expandedTreeNodes.delete(name);
        else state.expandedTreeNodes.add(name);
        renderSidebar();
        renderSidePanel();
      });
    });

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
  openOverlay("side-panel");
  openOverlay("side-panel-backdrop");
  document.getElementById("main").classList.add("side-panel-open");
  _renderSidePanelToolbar();
  renderSidePanel();
}
function closeSidePanel() {
  closeOverlay("side-panel");
  closeOverlay("side-panel-backdrop");
  document.getElementById("main").classList.remove("side-panel-open");
}

function _renderSidePanelToolbar() {
  const addBtn = document.getElementById("sp-add-btn");
  addBtn.classList.toggle("visible", state.sidePanelTab === "industry");
}

function renderSidePanel() {
  const list = document.getElementById("sp-list");
  const q = state.sidePanelSearch.toLowerCase();



  const isIndustryTab = state.sidePanelTab === "industry";
  let items;

  if (isIndustryTab) {
    const childSet = new Set(Object.values(state.industryTree).flat());

    // Auto-expand parent when a child is the active filter
    if (state.activeIndustry && childSet.has(state.activeIndustry)) {
      for (const [parent, children] of Object.entries(state.industryTree)) {
        if ((children || []).includes(state.activeIndustry)) state.expandedTreeNodes.add(parent);
      }
    }

    if (!q) {
      const topLevel = state.industries.filter(i => !childSet.has(i));
      if (state.sidePanelSort === "count") {
        topLevel.sort((a, b) => {
          const fa = _industryFilterSet(a), fb = _industryFilterSet(b);
          return state.companies.filter(c => (c.industries || []).some(i => fb.has(i))).length
               - state.companies.filter(c => (c.industries || []).some(i => fa.has(i))).length;
        });
      } else {
        topLevel.sort((a, b) => a.localeCompare(b, "zh-TW", { numeric: true }));
      }
      items = [];
      for (const name of topLevel) {
        const fset = _industryFilterSet(name);
        const count = state.companies.filter(c => (c.industries || []).some(i => fset.has(i))).length;
        const children = (state.industryTree[name] || []).filter(c => state.industries.includes(c));
        const isExpanded = state.expandedTreeNodes.has(name);
        items.push({ name, count, isChild: false, hasChildren: children.length > 0, isExpanded });
        if (isExpanded) {
          for (const child of children) {
            const childCount = state.companies.filter(c => (c.industries || []).includes(child)).length;
            items.push({ name: child, count: childCount, isChild: true, hasChildren: false, isExpanded: false });
          }
        }
      }
    } else {
      // Search mode: flat display of all matching industries
      items = state.industries
        .filter(name => name.toLowerCase().includes(q))
        .map(name => {
          const isChild = childSet.has(name);
          const count = isChild
            ? state.companies.filter(c => (c.industries || []).includes(name)).length
            : state.companies.filter(c => { const f = _industryFilterSet(name); return (c.industries || []).some(i => f.has(i)); }).length;
          return { name, count, isChild: false, hasChildren: false, isExpanded: false };
        });
      if (state.sidePanelSort === "count") items.sort((a, b) => b.count - a.count);
      else items.sort((a, b) => a.name.localeCompare(b.name, "zh-TW", { numeric: true }));
    }
  } else {
    const allLabels = [...new Set(state.companies.flatMap(c => c.labels || []))];
    items = allLabels.map(name => ({
      name,
      count: state.companies.filter(c => (c.labels || []).includes(name)).length,
      isChild: false, hasChildren: false, isExpanded: false,
    }));
    if (q) items = items.filter(x => x.name.toLowerCase().includes(q));
    if (state.sidePanelSort === "count") items.sort((a, b) => b.count - a.count);
    else items.sort((a, b) => a.name.localeCompare(b.name, "zh-TW", { numeric: true }));
  }

  if (items.length === 0) {
    list.innerHTML = `<div class="sp-empty">${q ? "無符合的項目" : "尚無資料"}</div>`;
    return;
  }

  // 右側面板為純管理介面，不顯示 active-filter 狀態，不觸發篩選
  const pinnedInSidebar = new Set(
    state.industries.filter(i => state.pinnedItems.has(i))
      .concat([...state.companies.flatMap(c => c.labels || [])].filter(l => state.pinnedItems.has(l)))
  );

  list.innerHTML = items.map(x => {
    const pinned = state.pinnedItems.has(x.name);
    const actions = isIndustryTab
      ? `<span class="sp-actions">
           <button class="sp-scan-btn" data-name="${escHtml(x.name)}" title="掃描遺珠">🔍</button>
           <button class="sp-rename-btn" data-name="${escHtml(x.name)}" title="重新命名">✏️</button>
           <button class="sp-delete-btn" data-name="${escHtml(x.name)}" title="刪除">🗑</button>
         </span>`
      : "";
    let treeCtrl = "";
    if (isIndustryTab) {
      if (x.hasChildren)
        treeCtrl = `<button class="sp-tree-toggle" data-toggle="${escHtml(x.name)}" title="${x.isExpanded ? "收合" : "展開子產業"}">${x.isExpanded ? "▼" : "▶"}</button>`;
      else if (x.isChild)
        treeCtrl = `<span class="sp-tree-indent"></span>`;
      else
        treeCtrl = `<span class="sp-tree-spacer"></span>`;
    }
    const childClass = x.isChild ? " sp-child-item" : "";
    return `<div class="sp-item${childClass}" data-name="${escHtml(x.name)}" data-is-label="${!isIndustryTab}">
      ${treeCtrl}<span class="sp-name">${escHtml(x.name)}</span>
      <span class="sp-count">${x.count}</span>
      ${actions}
      <button class="sp-pin-btn ${pinned ? "pinned" : ""}" data-name="${escHtml(x.name)}" title="${pinned ? "從側欄移除" : "釘選到側欄"}">${pinned ? "★" : "☆"}</button>
    </div>`;
  }).join("");

  // Tree expand/collapse — 純展開，不篩選
  list.querySelectorAll(".sp-tree-toggle").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const name = btn.dataset.toggle;
      if (state.expandedTreeNodes.has(name)) state.expandedTreeNodes.delete(name);
      else state.expandedTreeNodes.add(name);
      renderSidePanel();
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

    list.querySelectorAll(".sp-scan-btn").forEach(btn => {
      btn.addEventListener("click", async e => {
        e.stopPropagation();
        await openIndustryScanDialog(btn.dataset.name);
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

function _showAddIndustryForm() {
  return new Promise((resolve, reject) => {
    const childSet = new Set(Object.values(state.industryTree).flat());
    const parents = state.industries.filter(i => !childSet.has(i));
    const overlay = document.createElement("div");
    overlay.className = "ind-add-overlay";
    overlay.innerHTML = `
      <div class="ind-add-box">
        <div class="ind-add-title">新增產業別</div>
        <label class="ind-add-label">名稱
          <input type="text" id="ind-add-name-inp" placeholder="例：AgriTech" autocomplete="off">
        </label>
        <label class="ind-add-label">歸屬
          <select id="ind-add-parent-sel">
            <option value="">頂層產業（與前瞻科技同層）</option>
            ${parents.map(p => `<option value="${escHtml(p)}">${escHtml(p)} 的子產業</option>`).join("")}
          </select>
        </label>
        <div class="ind-add-footer">
          <button class="ind-add-cancel-btn">取消</button>
          <button class="ind-add-ok-btn">確定</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const inp = overlay.querySelector("#ind-add-name-inp");
    inp.focus();
    const close = result => { overlay.remove(); result ? resolve(result) : reject(null); };
    overlay.querySelector(".ind-add-cancel-btn").onclick = () => close(null);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(null); });
    overlay.querySelector(".ind-add-ok-btn").onclick = () => {
      const n = inp.value.trim(); if (!n) { inp.focus(); return; }
      close({ name: n, parent: overlay.querySelector("#ind-add-parent-sel").value });
    };
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") overlay.querySelector(".ind-add-ok-btn").click();
      if (e.key === "Escape") close(null);
    });
  });
}

document.getElementById("sp-add-btn").addEventListener("click", async () => {
  let addChoice;
  try { addChoice = await _showAddIndustryForm(); } catch (_) { return; }
  const { name: indName, parent: parentName } = addChoice;

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
        ${(c.industries || []).length ? `<span class="suggest-ind-badge">${escHtml((c.industries || []).join(", "))}</span>` : ""}
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

      if (parentName) {
        const tree = { ...state.industryTree };
        if (!tree[parentName]) tree[parentName] = [];
        if (!tree[parentName].includes(indName)) tree[parentName].push(indName);
        await api("PUT", "/api/config/industry-tree", tree);
        state.industryTree = tree;
      }

      if (checked.length > 0) {
        await api("PUT", "/api/companies/batch-industry", {
          updates: checked.map(id => ({ id, industry: indName })),
        });
        checked.forEach(id => {
          const idx = state.companies.findIndex(c => c.id === id);
          if (idx !== -1) { const c = state.companies[idx]; if (!(c.industries || []).includes(indName)) { c.industries = [...(c.industries || []), indName]; } }
        });
      }
      const parentNote = parentName ? `（${parentName} 的子產業）` : "";
      toast(`產業別「${indName}」${parentNote}已新增${checked.length > 0 ? `，${checked.length} 間公司已歸入` : ""}`);
    } catch (e) {
      toast(`新增失敗：${e.message}`, true);
    } finally {
      await Promise.all([loadIndustries(), loadIndustryTree()]);
      computeGroups();
      renderSidebar();
      renderSidePanel();
      renderGrid();
    }
  };
});

/* ── 掃描產業遺珠（對已存在的產業重新比對未歸入的公司）── */
async function openIndustryScanDialog(indName) {
  const overlay = document.getElementById("ind-suggest-overlay");
  document.getElementById("ind-suggest-title").textContent = `掃描「${indName}」遺珠`;
  document.getElementById("ind-suggest-subtitle").textContent = "Claude 正在比對尚未歸入的公司…";
  document.getElementById("ind-suggest-loading").style.display = "flex";
  document.getElementById("ind-suggest-rows").innerHTML = "";
  document.getElementById("ind-suggest-ok").disabled = true;
  overlay.classList.add("open");

  // 只列尚未歸入此產業的公司
  const candidates = state.companies.filter(c => !(c.industries || []).includes(indName));

  let matchedIds = [];
  try {
    const res = await api("POST", "/api/config/industries/suggest", { name: indName });
    // suggest 已排除 already_tagged，回傳的都是新比對到的
    matchedIds = res.matched_ids || [];
  } catch (e) {
    toast(`比對失敗：${e.message}，可手動勾選`, true);
  }

  document.getElementById("ind-suggest-loading").style.display = "none";
  document.getElementById("ind-suggest-title").textContent = `掃描「${indName}」遺珠`;
  const matchSet = new Set(matchedIds);
  const rows = document.getElementById("ind-suggest-rows");

  if (candidates.length === 0) {
    rows.innerHTML = `<p class="suggest-empty">所有公司都已歸入此產業別</p>`;
  } else {
    document.getElementById("ind-suggest-subtitle").textContent =
      matchedIds.length > 0
        ? `Claude 建議以下 ${matchedIds.length} 間公司可能漏歸（可調整勾選）`
        : "Claude 未發現遺珠，可手動勾選補充";
    rows.innerHTML = candidates.map(c => `
      <label class="suggest-row${matchSet.has(c.id) ? " suggested" : ""}">
        <input type="checkbox" value="${c.id}" ${matchSet.has(c.id) ? "checked" : ""} />
        <span class="suggest-name">${escHtml(c.name.replace(/股份有限公司$/, ""))}</span>
        <span class="suggest-blurb">${escHtml(c.blurb || "—")}</span>
        ${(c.industries || []).length ? `<span class="suggest-ind-badge">${escHtml((c.industries || []).join(", "))}</span>` : ""}
      </label>`).join("");
  }
  document.getElementById("ind-suggest-ok").disabled = false;

  document.getElementById("ind-suggest-cancel").onclick = () => overlay.classList.remove("open");
  document.getElementById("ind-suggest-ok").onclick = async () => {
    const checked = [...rows.querySelectorAll("input[type=checkbox]:checked")].map(el => el.value);
    overlay.classList.remove("open");
    if (checked.length === 0) { toast("未勾選任何公司，無異動"); return; }
    try {
      await api("PUT", "/api/companies/batch-industry", {
        updates: checked.map(id => ({ id, industry: indName })),
      });
      checked.forEach(id => {
        const idx = state.companies.findIndex(c => c.id === id);
        if (idx !== -1) state.companies[idx].industry = indName;
      });
      toast(`已將 ${checked.length} 間公司歸入「${indName}」`);
    } catch (e) {
      toast(`更新失敗：${e.message}`, true);
    } finally {
      await loadIndustries();
      computeGroups();
      renderSidebar();
      renderGrid();
    }
  };
}

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

