/* ── Manual Input Dialog ── */
document.getElementById("manual-input-btn").addEventListener("click", () => {
  _addSubmenu.style.display = "none";
  _addArrow.style.transform = "";
  openManualDialog();
});
document.getElementById("manual-cancel").addEventListener("click", () =>
  closeOverlay("manual-overlay"));
document.getElementById("manual-overlay").addEventListener("click", e => {
  if (e.target === document.getElementById("manual-overlay"))
    closeOverlay("manual-overlay");
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
  openOverlay("manual-overlay");
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
  openOverlay("manual-overlay");
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
// 只在「括號外」切，避免切到括號內的分隔符（如「Guardsquare NV（DexGuard / iXGuard）」
// 的 / 是產品名分隔，不是兩家公司）。
function _splitCompCell(content) {
  const parts = [];
  let buf = "", depth = 0;
  for (const ch of (content || "")) {
    if (ch === "（" || ch === "(") depth++;
    else if (ch === "）" || ch === ")") depth = Math.max(0, depth - 1);
    if (depth === 0 && (ch === "／" || ch === "/" || ch === "、" || ch === "與")) {
      if (buf.trim()) parts.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts.map(s => s.replace(/^(上游|下游|中游)/, "").trim()).filter(Boolean);
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

  closeOverlay("manual-overlay");

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
        else if (m.is_unverified) statusBadge = `<span class="disambig-status unverified" title="登記名單顯示核准，但政府資料庫查無此公司，請謹慎確認">待確認</span>`;
        else if (m.is_api_error)  statusBadge = `<span class="disambig-status api-error"  title="政府登記資料查詢逾時，登記名單顯示核准，建議稍後重新查詢">驗證逾時</span>`;
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
  openOverlay("disambig-overlay");
  _disambigItems = items;
}

let _disambigItems = [];

document.getElementById("disambig-cancel").addEventListener("click", () => {
  closeOverlay("disambig-overlay");
});

document.getElementById("disambig-ok").addEventListener("click", () => {
  const selections = _disambigItems.map((item, gi) => {
    const selected = document.querySelector(`input[name="dg${gi}"]:checked`);
    const val = selected ? selected.value : "0";
    if (val === "skip") return { input: item.input, skipped: true };
    return { input: item.input, skipped: false, match: item.matches[parseInt(val)] };
  });
  closeOverlay("disambig-overlay");
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

  openOverlay("name-review-overlay");
  // Focus first input
  setTimeout(() => document.querySelector(".name-review-input")?.focus(), 50);
}

document.getElementById("name-review-cancel").addEventListener("click", () =>
  closeOverlay("name-review-overlay"));

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

  closeOverlay("name-review-overlay");

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
        suggested_industry: existing ? ((existing.industries || [])[0] || "") : (state.industries[0] || ""),
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
    if (valid.some(c => c.is_api_error && c.tax_id)) {
      setTimeout(() => _reverifyTimeouts(1), 2500);
    }
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

/* ── 背景自動重查「驗證逾時」項目 ── */
// is_api_error 的公司是 GCIS 驗證逾時（多半被 rate limit 擋），不是真有問題。
// 辨識結果開啟後，背景靜默對這些統編重打 reverify-status；後端有退避 + 12h 快取，
// 通常一兩輪就轉成正常狀態，使用者不必手動重查。最多 3 輪、間隔遞增、dialog 關了就停。
async function _reverifyTimeouts(attempt = 1) {
  const MAX_ATTEMPTS = 3;
  const overlay = document.getElementById("confirm-overlay");
  if (!overlay || !overlay.classList.contains("open")) return;   // dialog 已關，停手
  const cands = state.pendingCandidates || [];
  const pending = cands.filter(c => c.is_api_error && c.tax_id);
  if (!pending.length) return;

  const taxIds = [...new Set(pending.map(c => c.tax_id))];
  let res;
  try {
    res = await api("POST", "/api/companies/reverify-status", { tax_ids: taxIds });
  } catch (_) {
    if (attempt < MAX_ATTEMPTS) setTimeout(() => _reverifyTimeouts(attempt + 1), 4000 * attempt);
    return;
  }

  let changed = false, stillPending = false;
  for (const c of cands) {
    if (!c.is_api_error || !c.tax_id) continue;
    const r = res[c.tax_id];
    if (!r || r.is_api_error) { stillPending = true; continue; }   // 還是逾時 → 下一輪再試
    c.is_api_error = false;
    if (r.is_dissolved) c.rejected = true;            // 已廢止 → 不予儲存
    else c.is_unverified = !!r.is_unverified;         // 查無 → 待確認；否則正常
    changed = true;
  }

  if (changed && overlay.classList.contains("open")) {
    const labelVals = {};   // 重渲染前保存使用者已輸入的標籤
    cands.forEach((_, idx) => {
      const el = document.getElementById(`label-v${idx}`);
      if (el) labelVals[idx] = el.value;
    });
    openConfirmDialog(cands, state.pendingUncertain, state.pendingExcluded, state.pendingLabel);
    setTimeout(() => {
      Object.entries(labelVals).forEach(([idx, val]) => {
        const el = document.getElementById(`label-v${idx}`);
        if (el) el.value = val;
      });
    }, 0);
  }

  if (stillPending && attempt < MAX_ATTEMPTS) {
    setTimeout(() => _reverifyTimeouts(attempt + 1), 4000 * attempt);
  }
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
        ? `<span class="unverified-badge" title="登記名單顯示核准，但政府資料庫查無此公司，請確認是否仍為現役">⚠ 待確認</span>`
        : c.is_api_error
          ? `<span class="unverified-badge api-error" title="政府登記資料查詢逾時（網路不穩），登記名單顯示核准，建議稍後重新查詢">⏱ 驗證逾時</span>`
          : "";
      const existingLabels = c.existing_labels?.length
        ? `<div class="existing-labels">現有標籤：${c.existing_labels.join("、")}</div>`
        : "";
      // list 投影不含 summary 本文——沿用 isIncompleteCompany 的雙態判定：
      // 有完整資料看 summary、投影資料看後端算好的 summary_incomplete。
      // （若直接 ?.summary，投影後永遠 falsy → 既有公司全被預設勾「生成」→ 覆蓋重燒）
      const _ex = !c.is_new ? state.companies.find(x => x.id === c.existing_id) : null;
      const hasData = !!_ex && ("summary" in _ex ? !!_ex.summary : _ex.summary_incomplete === false);
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
  openOverlay("confirm-overlay");
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
  closeOverlay("confirm-overlay"));

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

  closeOverlay("confirm-overlay");

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

  // Decide batching strategy（改用自繪對話框，不再一連串原生 confirm/prompt）
  let batchSize = enrich_ids.length;
  let autoRunAll = false;
  if (enrich_ids.length > 10) {
    const s = await askBatchSettings(enrich_ids.length);
    if (!s) { toast("已取消生成"); return; }
    batchSize = s.batchSize;
    autoRunAll = s.autoRunAll;
  }

  await runEnrichmentInBatches(enrich_ids, batchSize, autoRunAll);
});

let _batchStopRequested = false;

function _showBatchStopButton(getRemaining) {
  let btn = document.getElementById("batch-stop-fab");
  if (!btn) {
    btn = document.createElement("button");
    btn.id = "batch-stop-fab";
    document.body.appendChild(btn);
  }
  btn.textContent = `■ 停止批次（剩 ${getRemaining()} 間）`;
  btn.onclick = () => {
    _batchStopRequested = true;
    btn.textContent = "停止中…跑完本批後結束";
    btn.disabled = true;
  };
  btn.disabled = false;
  btn.style.display = "block";
  return btn;
}

function _hideBatchStopButton() {
  const btn = document.getElementById("batch-stop-fab");
  if (btn) btn.style.display = "none";
}

// 統一的批次設定對話框（取代原生 confirm/prompt）。回傳 {batchSize, autoRunAll} 或 null（取消）。
function askBatchSettings(count) {
  return new Promise(resolve => {
    const overlay = document.getElementById("batch-settings-overlay");
    document.getElementById("batch-settings-desc").textContent =
      `本次共需生成 ${count} 間公司資料。數量較多，可選擇一次全跑或分批。`;
    const opts = document.getElementById("bs-batch-opts");
    const radios = overlay.querySelectorAll('input[name="bs-mode"]');
    radios.forEach(r => { r.checked = (r.value === "all"); });
    opts.classList.remove("open");
    document.getElementById("bs-batch-size").value = "5";
    document.getElementById("bs-auto").checked = false;
    radios.forEach(r => r.onchange = () =>
      opts.classList.toggle("open", document.querySelector('input[name="bs-mode"]:checked')?.value === "batch"));
    overlay.classList.add("open");
    const ok = document.getElementById("bs-ok");
    const cancel = document.getElementById("bs-cancel");
    const cleanup = (val) => { overlay.classList.remove("open"); ok.onclick = null; cancel.onclick = null; resolve(val); };
    ok.onclick = () => {
      const mode = document.querySelector('input[name="bs-mode"]:checked')?.value || "all";
      if (mode === "all") return cleanup({ batchSize: count, autoRunAll: false });
      const n = parseInt(document.getElementById("bs-batch-size").value, 10);
      if (isNaN(n) || n < 1) { toast("每批數量需為正整數", true); return; }
      cleanup({ batchSize: Math.max(1, n), autoRunAll: document.getElementById("bs-auto").checked });
    };
    cancel.onclick = () => cleanup(null);
  });
}

async function runEnrichmentInBatches(ids, batchSize, autoRunAll = false) {
  const total = ids.length;
  const chunks = [];
  for (let i = 0; i < ids.length; i += batchSize) chunks.push(ids.slice(i, i + batchSize));

  let done = 0;
  _batchStopRequested = false;
  const fab = autoRunAll ? _showBatchStopButton(() => total - done) : null;
  try {
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
        // 依 enrich_status 給真實成敗摘要，而非一律報「全部完成」（失敗的卡片上可點「重試」）
        try { await loadCompanies(); } catch (_) {}
        const failed = ids.filter(id => state.companies.find(c => c.id === id)?.enrich_status === "failed");
        if (failed.length) {
          toast(`批次完成：${total - failed.length} 間成功、${failed.length} 間失敗（失敗卡片可點「重試」）`, true);
        } else {
          toast(`✅ 全部 ${total} 間已完成生成`);
        }
        break;
      }
      const remaining = total - done;
      if (_batchStopRequested) {
        toast(`已停止，剩餘 ${remaining} 間未生成`, true);
        return;
      }
      if (autoRunAll) {
        // 全程自動：不再逐批問，更新停止鈕的剩餘數後直接續跑
        if (fab) fab.textContent = `■ 停止批次（剩 ${remaining} 間）`;
        continue;
      }
      const cont = await askBatchContinue({ batch: i + 1, totalBatches: chunks.length, done, total, remaining });
      if (!cont) {
        toast(`已中止，剩餘 ${remaining} 間未生成`, true);
        return;
      }
    }
  } finally {
    _hideBatchStopButton();
  }
}

