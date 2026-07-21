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
    let ths = headerCells.map(c => `<th>${inlineMarkdown(c)}</th>`).join("");
    const trs = body.map(row => {
      const cells = row.split("|").filter((_,i,a) => i>0 && i<a.length-1);
      const firstContent = (cells[0] || "").trim();
      const isCaseRow = firstContent.includes("（本案）");
      const lastIdx = cells.length - 1;
      const tds = cells.map((c, ci) => {
        const content = c.trim();
        if (isCompetitorTable && ci === 0) {
          // 公司名稱也收成 3 行、超長名稱（如英文全名／品牌別名）尾端加展開鈕，
          // 跟核心業務／主要差異化特點一致，靠共用的 .comp-clamp（見 _setupCompClampButtons）。
          // 本案列：不可點，去尾綴顯示
          if (content.includes("（本案）")) {
            return `<td><div class="comp-clamp">${inlineMarkdown(_displayCompName(content))}</div></td>`;
          }
          // 一格可能塞多家（如「雙鴻（3324）／奇鋐（3017）」）→ 拆成各自獨立的 chip，
          // 每家自己一個＋、自己可點，新增流程就只會加被點的那一家；每個 chip 各自收 3 行。
          const chips = _splitCompCell(content).map(tok => {
            const disp = _displayCompName(tok);
            const rawName = tok.replace(/（[^）]*）/g, "").trim();
            const alreadyAdded = state.companies.some(co => _coreName(co.name) === _coreName(rawName));
            const cls   = (alreadyAdded ? "competitor-chip competitor-chip--added" : "competitor-chip") + " comp-clamp";
            const title = alreadyAdded ? "已在清單中，點擊開啟" : "點擊新增此公司";
            return `<span class="${cls}" data-cname="${escHtml(rawName)}" data-added="${alreadyAdded}" onclick="handleCompetitorChip(this)" title="${title}">${inlineMarkdown(disp)}</span>`;
          }).join("");
          return `<td><div class="comp-name-cell">${chips}</div></td>`;
        }
        // 核心業務／主要差異化特點兩欄收成 3 行，尾端加「展開」鈕看全文（跟專利頁一致），
        // 避免長文字撐版。是否真的需要展開鈕由 _setupCompClampButtons() 量測後決定。
        // line-clamp 需要的 display:-webkit-box 只能套在 td 內的 div 上——直接套在 <td>
        // 會讓它失去 table-cell 顯示型態，被瀏覽器包一層匿名欄位，整張表格因此欄位跑位。
        if (isCompetitorTable && (ci === 1 || ci === 2)) {
          return `<td><div class="comp-clamp">${inlineMarkdown(content)}</div></td>`;
        }
        // 刪除鈕疊在最後一欄右上角（hover 才浮現），不再獨立佔一整欄——獨立欄位
        // 平時是空的，跟旁邊短內容的欄位之間又沒有格線，看起來會像是「這欄怎麼
        // 明明有空間卻還是跳行」的錯覺（其實那塊空白屬於隔壁的刪除鈕欄）。
        // data-ctype 存原始競業類型文字，供 app-modal.js 的頁籤篩選直接讀取——
        // 不能再靠 textContent 解析，按鈕的「✕」會混進去。
        if (isCompetitorTable && ci === lastIdx) {
          const delBtn = isCaseRow ? "" :
            `<button class="comp-del-btn" data-cname="${escHtml(firstContent)}" onclick="removeCompetitorRow(this)" title="刪除此競業">✕</button>`;
          return `<td class="comp-last-col" data-ctype="${escAttr(content)}">${inlineMarkdown(content)}${delBtn}</td>`;
        }
        return `<td>${inlineMarkdown(content)}</td>`;
      }).join("");
      return `<tr>${tds}</tr>`;
    }).join("");
    const tcls = "summary-table" + (isCompetitorTable ? " competitor-table" : "");
    // table-layout:fixed 只吃 <colgroup>／第一列的欄寬設定才準；純靠 nth-child CSS
    // 在多列情境下會跑掉（曾出現某欄擠成逐字直排、某欄整欄空白的變形）。
    // 欄數要跟實際 <th> 數一致（刪除鈕已併入最後一欄，不再是獨立欄位）；
    // 百分比務必精準加總 = 100%，否則瀏覽器會把差額塞到某一欄（通常是最後一欄）。
    const colgroup = isCompetitorTable
      ? (headerCells.length === 5
          ? `<colgroup><col style="width:14%"><col style="width:28%"><col style="width:28%">` +
            `<col style="width:15%"><col style="width:15%"></colgroup>`
          : `<colgroup><col style="width:18%"><col style="width:30%"><col style="width:30%">` +
            `<col style="width:22%"></colgroup>`)
      : "";
    out.push(`<table class="${tcls}">${colgroup}<thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`);
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

    // 「競業類型定義：…」改用頁籤 tooltip 呈現，這裡直接濾掉避免重複佔版面
    if (/^競業類型定義[：:]/.test(line)) continue;

    // 洩漏的競業表「填寫規則」prompt echo（見 services/report_generator.py），
    // 模型有時會把教它怎麼填表的指示連同表格一起輸出，落地存進 summary。
    // 比照競業類型定義一併濾掉，避免在表格上方顯示一大段指示文字。
    if (/^\**「公司名稱」\s*欄一律填/.test(line)) continue;
    if (/^\**一列只填一家公司/.test(line)) continue;

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
  "簡報": { cls: "deck",  icon: _CLIP_SVG, label: "補充資料" },
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

// A list item is a supplement note if it contains a「（XX補充…）」marker ANYWHERE
// — start (risks: "（訪談補充）標題：…"), mid ("三層收入結構（簡報補充）：…") or
// end ("…可擴展性（簡報補充）。"). The whole bullet is treated as one source's note
// and rendered as a green callout block (matching 主要風險 / 業務概況), with every
// marker token stripped: inline「（XX補充：payload）」keeps its payload, standalone
// /trailing「（XX補充）」is removed. Returns {inner, src} or null.
function _bulletSupInner(raw) {
  const first = _findSup(raw, 0);
  if (!first) return null;
  let inner = "", i = 0;
  for (;;) {
    const f = _findSup(raw, i);
    if (!f) { inner += raw.slice(i); break; }
    inner += raw.slice(i, f.idx);
    if (raw[f.idx + _SUP_MARKLEN] === "）") {
      i = f.idx + _SUP_MARKLEN + 1;            // drop standalone「（XX補充）」
    } else {
      const { inner: payload, end } = _supSpan(raw, f.idx);
      inner += payload;                        // keep「（XX補充：payload）」content
      i = end;
    }
  }
  return { inner: inner.trim(), src: first.src };
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

// A non-bullet paragraph line that contains ANY「（XX補充…）」marker is treated as
// fully supplement-sourced — same rule as bullets (_bulletSupInner) — and rendered
// as ONE callout. We accumulate the whole line, dropping every standalone「（XX補充）」
// tag (wherever it sits — start, middle or trailing) and keeping inline
// 「（XX補充：payload）」/「（XX補充；payload）」content. Lines with no marker stay as
// public text. This kills the old failure mode where a trailing「…內容（簡報補充）。」
// left the real content in the public paragraph and rendered an empty「。」callout.
function _splitSupplements(line) {
  const first = _findSup(line, 0);
  if (!first) return line.length ? [{ type: "text", text: line }] : [];
  let inner = "", i = 0;
  for (;;) {
    const f = _findSup(line, i);
    if (!f) { inner += line.slice(i); break; }
    inner += line.slice(i, f.idx);
    if (line[f.idx + _SUP_MARKLEN] === "）") {
      i = f.idx + _SUP_MARKLEN + 1;            // drop standalone「（XX補充）」tag
    } else {
      const { inner: payload, end } = _supSpan(line, f.idx);
      inner += payload;                        // keep「（XX補充：payload）」content
      i = end;
    }
  }
  return [{ type: "sup", text: inner.trim(), src: first.src }];
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

// Format an ISO timestamp as a zh-TW local datetime (24h). Empty string for falsy input.
function formatTimestamp(iso) {
  return iso ? new Date(iso).toLocaleString("zh-TW", { hour12: false }) : "";
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
