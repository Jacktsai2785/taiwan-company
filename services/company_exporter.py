"""
Generate DOCX / PDF snapshots that mirror the company-modal visual style.

Color palette (matches style.css):
  dark-bg      #1e2d4d   header background
  accent       #2e6da8   section titles, table headers
  accent-light #edf4fb   table header fill, section bottom border
  border       #d1dae6   table cell borders
  text         #1a2537   body text
  muted        #7990a8   labels, secondary text
  alt-row      #f7fafd   alternating table row
"""
import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


# ── Colour tokens ─────────────────────────────────────────────────────────────

class _R:   # python-docx RGBColor constants
    ACCENT       = RGBColor(0x2E, 0x6D, 0xA8)
    ACCENT_LIGHT = RGBColor(0xED, 0xF4, 0xFB)
    TEXT         = RGBColor(0x1A, 0x25, 0x37)
    MUTED        = RGBColor(0x79, 0x90, 0xA8)
    DARK_BG      = RGBColor(0x1E, 0x2D, 0x4D)
    WHITE        = RGBColor(0xFF, 0xFF, 0xFF)
    ALT_ROW      = RGBColor(0xF7, 0xFA, 0xFD)

class _F:   # PyMuPDF float tuples (0–1)
    ACCENT       = (0.180, 0.428, 0.659)
    ACCENT_LIGHT = (0.929, 0.957, 0.984)
    BORDER       = (0.820, 0.855, 0.902)
    TEXT         = (0.102, 0.145, 0.216)
    MUTED        = (0.475, 0.565, 0.659)
    DARK_BG      = (0.118, 0.176, 0.302)
    ALT_ROW      = (0.969, 0.980, 0.992)
    WHITE        = (1.0,   1.0,   1.0  )


# ── Shared markdown helpers ───────────────────────────────────────────────────

def _md_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into [(heading, body)] pairs. Strips preamble before first ##."""
    if not text:
        return []
    text = re.sub(r"^##\s+.+公司簡介[^\n]*\n+", "", text)
    # Mirror JS renderSummary: drop anything before the first ## heading
    if not text.lstrip().startswith("#"):
        m = re.search(r"^##", text, re.MULTILINE)
        if m:
            text = text[m.start():]
    parts = re.split(r"^(#{1,3}\s+.+)$", text, flags=re.MULTILINE)
    sections: list[tuple[str, str]] = []
    if parts[0].strip():
        sections.append(("", parts[0].strip()))
    i = 1
    while i < len(parts) - 1:
        sections.append((parts[i].lstrip("#").strip(), parts[i + 1].strip()))
        i += 2
    return sections


def _strip_inline_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\*(.+?)\*", r"\1", text)


def _wrap_smart(text: str, size: float, max_w: float) -> list[str]:
    """Width-aware line wrap: CJK chars ≈ 1.0× size, ASCII ≈ 0.55× size."""
    if not text:
        return [""]
    lines, cur, cur_w = [], "", 0.0
    for ch in text:
        ch_w = size * (1.0 if ord(ch) > 0x3000 else 0.55)
        if cur_w + ch_w > max_w and cur:
            lines.append(cur)
            cur, cur_w = "", 0.0
        cur += ch
        cur_w += ch_w
    return (lines + [cur]) if cur else (lines or [""])


def _fmt_capital(n) -> str:
    return f"NT$ {int(n):,} 元" if n else "—"


# ── DOCX width helpers ────────────────────────────────────────────────────────

def _docx_est_pt(text: str, size_pt: float = 9) -> float:
    """Estimate text width in points (CJK ≈ 1.0×, ASCII ≈ 0.55×)."""
    return sum(size_pt * (1.0 if ord(ch) > 0x00FF else 0.55)
               for ch in _strip_inline_md(text))


def _docx_auto_cols(rows: list[list[str]], size_pt: float,
                    total_pt: float, pad_pt: float = 18) -> list:
    """Return Pt() column widths fitted to content, summing to total_pt."""
    if not rows:
        return []
    n = max(len(r) for r in rows)
    ideal = []
    for ci in range(n):
        mx = max((_docx_est_pt(str(r[ci]), size_pt) if ci < len(r) else 0)
                 for r in rows) + pad_pt
        ideal.append(mx)
    total = sum(ideal)
    if total <= total_pt:
        extra = total_pt - total
        if n > 2:
            mid = max(range(1, n - 1), key=lambda i: ideal[i])
            ideal[mid] += extra
    else:
        scale = total_pt / total
        ideal = [w * scale for w in ideal]
    return [Pt(w) for w in ideal]


# ══════════════════════════════════════════════════════════════════════════════
#  DOCX
# ══════════════════════════════════════════════════════════════════════════════

# ── DOCX XML helpers ──────────────────────────────────────────────────────────

def _shading(cell, hex_fill: str, hex_color: str = "auto"):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"),   "clear")
    shd.set(qn("w:color"), hex_color)
    shd.set(qn("w:fill"),  hex_fill)
    tcPr.append(shd)


def _cell_margins(cell, top=60, bottom=60, left=100, right=100):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcMar = OxmlElement("w:tcMar")
    for side, val in [("top", top), ("bottom", bottom),
                      ("left", left), ("right", right)]:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"),    str(val))
        el.set(qn("w:type"), "dxa")
        tcMar.append(el)
    tcPr.append(tcMar)


def _no_table_borders(table):
    tbl   = table._tbl
    tblPr = tbl.tblPr if tbl.tblPr is not None else OxmlElement("w:tblPr")
    tblBdr = OxmlElement("w:tblBorders")
    for side in ["top", "left", "bottom", "right", "insideH", "insideV"]:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"),   "none")
        el.set(qn("w:sz"),    "0")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "auto")
        tblBdr.append(el)
    tblPr.append(tblBdr)


def _set_cell_borders(cell, hex_color: str = "d1dae6", sides=("bottom",)):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcBdr = OxmlElement("w:tcBorders")
    for side in sides:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"),   "single")
        el.set(qn("w:sz"),    "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), hex_color)
        tcBdr.append(el)
    tcPr.append(tcBdr)


def _para_bottom_border(para, hex_color: str = "c7d9ee", sz: int = 4):
    pPr   = para._p.get_or_add_pPr()
    pBdr  = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"),   "single")
    bottom.set(qn("w:sz"),    str(sz))
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), hex_color)
    pBdr.append(bottom)
    pPr.append(pBdr)


def _para_left_border(para, hex_color: str = "2e6da8", sz: int = 18):
    pPr  = para._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"),   "single")
    left.set(qn("w:sz"),    str(sz))
    left.set(qn("w:space"), "9")
    left.set(qn("w:color"), hex_color)
    pBdr.append(left)
    pPr.append(pBdr)
    ind = OxmlElement("w:ind")
    ind.set(qn("w:left"), "180")
    pPr.append(ind)


def _para_spacing(para, before: int = 0, after: int = 0, line: int | None = None):
    pPr = para._p.get_or_add_pPr()
    sp  = OxmlElement("w:spacing")
    sp.set(qn("w:before"), str(before))
    sp.set(qn("w:after"),  str(after))
    if line:
        sp.set(qn("w:line"),     str(line))
        sp.set(qn("w:lineRule"), "auto")
    pPr.append(sp)


def _letter_spacing(run, val: int = 40):
    rPr = run._r.get_or_add_rPr()
    sp  = OxmlElement("w:spacing")
    sp.set(qn("w:val"), str(val))
    rPr.append(sp)


def _add_md_runs(para, text: str, size_pt: float, color: RGBColor):
    """Parse **bold** inline markdown and add styled runs."""
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    for part in parts:
        bold = part.startswith("**") and part.endswith("**")
        r = para.add_run(part[2:-2] if bold else part)
        r.bold = bold
        r.font.size  = Pt(size_pt)
        r.font.color.rgb = color


# ── DOCX builder helpers ──────────────────────────────────────────────────────

def _docx_section_heading(doc, text: str):
    """Section title: uppercase, accent blue, bottom border — mirrors .modal-section h4"""
    p = doc.add_paragraph()
    _para_spacing(p, before=200, after=80)
    _para_bottom_border(p, "c7d9ee", sz=4)
    r = p.add_run(text.upper())
    r.bold = True
    r.font.size = Pt(9)
    r.font.color.rgb = _R.ACCENT
    _letter_spacing(r, 40)
    return p


def _docx_summary_h3(doc, text: str):
    """Summary ## heading: left accent border, mirrors #modal-summary h3"""
    p = doc.add_paragraph()
    _para_spacing(p, before=180, after=60)
    _para_left_border(p, "2e6da8", sz=18)
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(10)
    r.font.color.rgb = _R.TEXT
    return p


def _docx_summary_h4(doc, text: str):
    p = doc.add_paragraph()
    _para_spacing(p, before=80, after=20)
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(9)
    r.font.color.rgb = _R.MUTED
    return p


def _docx_summary_body(doc, lines: list[str],
                       content_pt: float = 468):
    """Render body lines: lists, tables, paragraphs."""
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip blank lines and horizontal rules
        if not stripped or re.match(r"^-{3,}$", stripped):
            i += 1
            continue

        # ### sub-heading
        if stripped.startswith("###"):
            _docx_summary_h4(doc, stripped.lstrip("#").strip())
            i += 1
            continue

        # Markdown table block
        if stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[-| ]+\|\s*$", lines[i + 1]):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            data_rows = [r for r in table_lines
                         if not re.match(r"^\s*\|[-| ]+\|\s*$", r.strip())]
            if data_rows:
                grid = [[c.strip() for c in r.strip().strip("|").split("|")]
                        for r in data_rows]
                cols  = max(len(r) for r in grid)
                cws   = _docx_auto_cols(grid, 9, content_pt)
                t     = doc.add_table(rows=len(grid), cols=cols)
                t.style = "Table Grid"
                for ci, cw in enumerate(cws):
                    for cell in (t.columns[ci].cells if ci < cols else []):
                        cell.width = cw
                for ri, row_cells in enumerate(grid):
                    for ci, val in enumerate(row_cells[:cols]):
                        cell = t.cell(ri, ci)
                        cell.width = cws[ci] if ci < len(cws) else Pt(content_pt / cols)
                        cell.paragraphs[0].clear()
                        _cell_margins(cell, top=40, bottom=40, left=80, right=80)
                        r = cell.paragraphs[0].add_run(_strip_inline_md(val))
                        r.font.size = Pt(9)
                        if ri == 0:
                            _shading(cell, "edf4fb")
                            r.bold = True
                            r.font.color.rgb = _R.ACCENT
                        else:
                            r.font.color.rgb = _R.TEXT
                            if ri % 2 == 0:
                                _shading(cell, "f7fafd")
            continue

        # List item — hanging indent matching PDF (bullet at 10pt, text at 22pt)
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            content = re.sub(r"^[-*]\s+", "", re.sub(r"^\d+\.\s+", "", stripped))
            p   = doc.add_paragraph()
            _para_spacing(p, before=0, after=40, line=276)
            pPr = p._p.get_or_add_pPr()
            ind = OxmlElement("w:ind")
            ind.set(qn("w:left"),    str(22 * 20))   # 22pt in twips
            ind.set(qn("w:hanging"), str(12 * 20))   # 12pt hanging → bullet at 10pt
            pPr.append(ind)
            br = p.add_run("• ")
            br.font.size      = Pt(8)
            br.font.color.rgb = _R.TEXT
            _add_md_runs(p, content, 9.5, _R.TEXT)
            i += 1
            continue

        # Regular paragraph
        p = doc.add_paragraph()
        _para_spacing(p, before=20, after=60, line=276)
        _add_md_runs(p, stripped, 9.5, _R.TEXT)
        i += 1


# ── Main DOCX builder ─────────────────────────────────────────────────────────

def build_docx(company: dict) -> bytes:
    doc = Document()

    for sec in doc.sections:
        sec.top_margin    = Inches(0.8)
        sec.bottom_margin = Inches(0.8)
        sec.left_margin   = Inches(1.0)
        sec.right_margin  = Inches(1.0)

    # ── Dark header block (mirrors #modal-header background: var(--sb-bg)) ──
    header_tbl = doc.add_table(rows=1, cols=1)
    _no_table_borders(header_tbl)
    hcell = header_tbl.cell(0, 0)
    _shading(hcell, "1e2d4d")
    _cell_margins(hcell, top=220, bottom=180, left=300, right=300)

    name_para = hcell.paragraphs[0]
    _para_spacing(name_para, before=0, after=80)
    name_run = name_para.add_run(company.get("name", "—"))
    name_run.bold = True
    name_run.font.size = Pt(18)
    name_run.font.color.rgb = _R.WHITE

    listing = company.get("listing_status", "")
    if listing:
        badge_para = hcell.add_paragraph()
        _para_spacing(badge_para, before=0, after=0)
        br = badge_para.add_run(f"[{listing}]")
        br.font.size = Pt(9)
        br.font.color.rgb = _R.MUTED

    labels = company.get("labels") or []
    if labels:
        lp = hcell.add_paragraph()
        _para_spacing(lp, before=60, after=0)
        lr = lp.add_run("  ".join(labels[:8]))
        lr.font.size = Pt(8.5)
        lr.font.color.rgb = RGBColor(0xBB, 0xCC, 0xDD)

    doc.add_paragraph()  # spacer

    # ── Basic info ──
    _docx_section_heading(doc, "基本資料")

    info_tbl = doc.add_table(rows=0, cols=2)
    _no_table_borders(info_tbl)
    info_tbl.columns[0].width = Inches(1.4)
    info_tbl.columns[1].width = Inches(4.2)

    for label, value in [
        ("統一編號",   company.get("tax_id") or "—"),
        ("公司代表人", company.get("representative") or "—"),
        ("資本總額",   _fmt_capital(company.get("authorized_capital"))),
        ("實收資本額", _fmt_capital(company.get("capital"))),
        ("公司所在地", company.get("address") or "—"),
        ("設立日期",   company.get("setup_date") or "—"),
        ("產業別",     company.get("industry") or "—"),
    ]:
        row = info_tbl.add_row()
        lc, vc = row.cells
        _cell_margins(lc, top=30, bottom=30, left=0, right=60)
        _cell_margins(vc, top=30, bottom=30, left=0, right=0)
        _set_cell_borders(lc, "d1dae6", ("bottom",))
        _set_cell_borders(vc, "d1dae6", ("bottom",))
        _para_spacing(lc.paragraphs[0], before=0, after=0)
        lr = lc.paragraphs[0].add_run(label)
        lr.font.size = Pt(9)
        lr.font.color.rgb = _R.MUTED
        _para_spacing(vc.paragraphs[0], before=0, after=0)
        vr = vc.paragraphs[0].add_run(value)
        vr.font.size = Pt(9)
        vr.bold = True
        vr.font.color.rgb = _R.TEXT

    doc.add_paragraph()

    # ── Directors ──
    directors = company.get("directors") or []
    if directors:
        _docx_section_heading(doc, "董監事名單")

        CONTENT_PT = 468  # 6.5 in × 72 pt
        dir_rows = [["職稱", "姓名", "所代表法人", "持股比例"]]
        for d in directors:
            ratio = f"{d['ratio']*100:.2f}%" if d.get("ratio") is not None else "—"
            dir_rows.append([
                d.get("title") or "—",
                d.get("name") or "—",
                d.get("representative_of") or "—",
                ratio,
            ])
        cws = _docx_auto_cols(dir_rows, 9, CONTENT_PT)

        dt = doc.add_table(rows=1, cols=4)
        dt.style = "Table Grid"
        hdr_row = dt.rows[0]
        for cell, txt, cw in zip(hdr_row.cells, dir_rows[0], cws):
            cell.width = cw
            _shading(cell, "edf4fb")
            _cell_margins(cell, top=50, bottom=50, left=80, right=80)
            cell.paragraphs[0].clear()
            r = cell.paragraphs[0].add_run(txt)
            r.bold = True
            r.font.size = Pt(9)
            r.font.color.rgb = _R.ACCENT

        for idx, vals in enumerate(dir_rows[1:]):
            row = dt.add_row()
            if idx % 2 == 1:
                for cell in row.cells:
                    _shading(cell, "f7fafd")
            for cell, val, cw in zip(row.cells, vals, cws):
                cell.width = cw
                _cell_margins(cell, top=40, bottom=40, left=80, right=80)
                cell.paragraphs[0].clear()
                r = cell.paragraphs[0].add_run(val)
                r.font.size = Pt(9)
                r.font.color.rgb = _R.TEXT

        doc.add_paragraph()

    # ── Summary ──
    summary_raw = company.get("summary", "")
    if summary_raw:
        _docx_section_heading(doc, "公司簡介")

        for heading, body in _md_sections(summary_raw):
            if heading:
                _docx_summary_h3(doc, heading)
            _docx_summary_body(doc, body.split("\n"))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  PDF
# ══════════════════════════════════════════════════════════════════════════════

def build_pdf(company: dict) -> bytes:
    import fitz

    PAGE_W, PAGE_H = 595, 842
    ML, MR = 40, 40
    MB = 65
    CW = PAGE_W - ML - MR    # 475 pt

    doc  = fitz.open()
    page: fitz.Page = None    # type: ignore[assignment]
    y    = 0.0

    # Pre-create font objects for width calculation
    _fhelv = fitz.Font("helv")
    _fcjk  = fitz.Font("china-t")

    # ── Page management ───────────────────────────────────────────────────────

    def _new_page() -> fitz.Page:
        nonlocal page, y
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y    = 60.0
        return page

    def _need_space(h: float):
        if y + h > PAGE_H - MB:
            _new_page()

    # ── Drawing primitives ────────────────────────────────────────────────────

    def filled_rect(x0, y0, x1, y1, fill):
        page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None, fill=fill, width=0)

    def hline(y_pos: float, x0=ML, x1=None, color=_F.BORDER, w=0.5):
        page.draw_line(fitz.Point(x0, y_pos),
                       fitz.Point(x1 or PAGE_W - MR, y_pos),
                       color=color, width=w)

    def vline(x_pos: float, y0: float, y1: float, color=_F.ACCENT, w=3.0):
        page.draw_line(fitz.Point(x_pos, y0),
                       fitz.Point(x_pos, y1),
                       color=color, width=w)

    # ── Text helpers ──────────────────────────────────────────────────────────

    def _seg_width(s: str, size: float, is_cjk: bool) -> float:
        """Estimate segment width using font metrics."""
        if is_cjk:
            return _fcjk.text_length(s, fontsize=size)
        return _fhelv.text_length(s, fontsize=size)

    def _text_segments(s: str) -> list[tuple[str, bool]]:
        """Split into [(text, is_cjk)] alternating runs.
        Threshold >0x00FF: numbers/basic-latin → helv; everything else → china-t.
        This ensures em-dash (U+2014), Chinese, etc. all use the CJK font."""
        segs, cur, cur_cjk = [], "", None
        for ch in s:
            is_cjk = ord(ch) > 0x00FF
            if cur_cjk is not None and is_cjk != cur_cjk:
                segs.append((cur, cur_cjk)); cur = ""
            cur_cjk = is_cjk; cur += ch
        if cur: segs.append((cur, cur_cjk if cur_cjk is not None else False))
        return segs

    def txt(s: str, x: float, y_baseline: float, size: float = 10, color=_F.TEXT):
        """Render text with helv for ASCII/numbers, china-t for CJK."""
        for seg, is_cjk in _text_segments(s):
            fn = "china-t" if is_cjk else "helv"
            page.insert_text(fitz.Point(x, y_baseline), seg,
                             fontname=fn, fontsize=size, color=color)
            x += _seg_width(seg, size, is_cjk)

    def _line_width(s: str, size: float) -> float:
        return sum(_seg_width(seg, size, is_cjk) for seg, is_cjk in _text_segments(s))

    def _wrap_mixed(s: str, size: float, max_w: float) -> list[str]:
        """Width-accurate wrap using font metrics."""
        if not s:
            return [""]
        lines, cur, cur_w = [], "", 0.0
        for ch in s:
            is_cjk = ord(ch) > 0x00FF
            ch_w   = _fcjk.text_length(ch, fontsize=size) if is_cjk \
                     else _fhelv.text_length(ch, fontsize=size)
            if cur_w + ch_w > max_w and cur:
                lines.append(cur); cur, cur_w = "", 0.0
            cur += ch; cur_w += ch_w
        return (lines + [cur]) if cur else (lines or [""])

    def _auto_col_widths(rows: list[list[str]], size: float,
                         total_w: float, pad: float = 10) -> list[float]:
        """Compute column widths so every cell fits in one line, scaled to total_w."""
        if not rows:
            return []
        n = max(len(r) for r in rows)
        ideal = []
        for ci in range(n):
            max_cell = 0.0
            for row in rows:
                if ci < len(row):
                    w = _line_width(_strip_inline_md(str(row[ci])), size)
                    max_cell = max(max_cell, w)
            ideal.append(max_cell + pad)
        total_ideal = sum(ideal)
        if total_ideal <= total_w:
            # Give leftover to the widest middle column
            extra = total_w - total_ideal
            if n > 2:
                mid = max(range(1, n - 1), key=lambda i: ideal[i])
                ideal[mid] += extra
        else:
            scale = total_w / total_ideal
            ideal = [w * scale for w in ideal]
        return ideal

    def put(s: str, size: float = 10, x: float = ML,
            color=_F.TEXT, gap_after: float = 3, max_w: float | None = None) -> None:
        """Wrap and place text, advance y, handle page breaks."""
        nonlocal y
        eff_w = (max_w if max_w is not None else CW) - (x - ML)
        for ln in _wrap_mixed(_strip_inline_md(s), size, eff_w):
            _need_space(size + 4)
            txt(ln, x, y + size, size, color)
            y += size + 3
        y += gap_after

    # ── Section heading ───────────────────────────────────────────────────────

    def section_heading(title: str, gap_before: float = 18):
        nonlocal y
        _need_space(gap_before + 14)
        y += gap_before
        txt(title.upper(), ML, y + 9, size=9, color=_F.ACCENT)
        y += 9 + 4
        hline(y, color=_F.ACCENT_LIGHT, w=2)
        y += 8

    # ── Summary heading helpers ───────────────────────────────────────────────

    def summary_h3(title: str):
        nonlocal y
        _need_space(26)
        y += 12
        vline(ML, y, y + 13, color=_F.ACCENT, w=3)
        # h3 might be long — wrap it within content width minus indent
        for i, ln in enumerate(_wrap_mixed(title, 10, CW - 9)):
            txt(ln, ML + 9, y + 10 + i * 13, size=10, color=_F.TEXT)
        y += 13 + 5

    def summary_h4(title: str):
        nonlocal y
        y += 6
        put(title, size=9, color=_F.MUTED, gap_after=3)

    # ── Table renderer ────────────────────────────────────────────────────────

    def pdf_table(rows: list[list[str]], col_widths: list[float],
                  x0: float = ML, header: bool = True):
        nonlocal y
        n_cols    = len(col_widths)
        total_w   = sum(col_widths)
        CELL_PAD  = 5
        MIN_ROW_H = 18.0

        for ri, row in enumerate(rows):
            is_header = header and ri == 0
            is_alt    = (not is_header) and ri % 2 == 0
            cell_size = 8.5

            # Pre-wrap each cell to know row height
            wrapped_cells = []
            for ci, val in enumerate(row[:n_cols]):
                max_cw = col_widths[ci] - CELL_PAD * 2
                wrapped_cells.append(_wrap_mixed(_strip_inline_md(str(val)), cell_size, max_cw))
            row_lines = max(len(wc) for wc in wrapped_cells)
            row_h     = max(MIN_ROW_H, row_lines * (cell_size + 2) + CELL_PAD * 2)

            _need_space(row_h + 2)

            if is_header:
                filled_rect(x0, y, x0 + total_w, y + row_h, _F.ACCENT_LIGHT)
            elif is_alt:
                filled_rect(x0, y, x0 + total_w, y + row_h, _F.ALT_ROW)

            cx = x0
            for ci, wlines in enumerate(wrapped_cells):
                cell_color = _F.ACCENT if is_header else _F.TEXT
                for li, ln in enumerate(wlines):
                    txt(ln, cx + CELL_PAD, y + CELL_PAD + cell_size + li * (cell_size + 2),
                        size=cell_size, color=cell_color)
                cx += col_widths[ci]

            hline(y + row_h, x0=x0, x1=x0 + total_w, color=_F.BORDER, w=0.4)
            y += row_h
        y += 6

    # ── PDF summary body ──────────────────────────────────────────────────────

    def pdf_summary_body(lines: list[str]):
        nonlocal y
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or re.match(r"^-{3,}$", line):   # skip blank / hr
                y += 4; i += 1; continue

            # Markdown table
            if line.startswith("|") and i + 1 < len(lines) and \
               re.match(r"^\s*\|[-| ]+\|\s*$", lines[i + 1].strip()):
                block = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    block.append(lines[i]); i += 1
                data = [r for r in block if not re.match(r"^\s*\|[-| ]+\|\s*$", r.strip())]
                rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in data]
                if rows:
                    n  = max(len(r) for r in rows)
                    pdf_table(rows, _auto_col_widths(rows, 8.5, CW))
                continue

            # List item — hanging indent: bullet at BX, text+wrap at TX
            if re.match(r"^[-*]\s+", line) or re.match(r"^\d+\.\s+", line):
                content = re.sub(r"^[-*]\s+", "", re.sub(r"^\d+\.\s+", "", line))
                BX, TX = ML + 10, ML + 22          # bullet pos, text pos
                TW     = CW - (TX - ML)             # wrap width from TX to right margin
                lns    = _wrap_mixed(_strip_inline_md(content), 9.5, TW)
                for li, ln in enumerate(lns):
                    _need_space(9.5 + 3)
                    if li == 0:
                        txt("•", BX, y + 9, size=7, color=_F.TEXT)
                    txt(ln, TX, y + 9.5, size=9.5, color=_F.TEXT)
                    y += 9.5 + 3
                y += 3
                i += 1; continue

            # ### sub-heading
            if line.startswith("###"):
                summary_h4(line.lstrip("#").strip())
                i += 1; continue

            # Regular paragraph
            put(line, size=9.5, gap_after=5)
            i += 1

    # ═══════════════ Build document ═══════════════

    _new_page()

    # ── Header block (dark navy) ──────────────────────────────────────────────
    HDR_H = 88
    filled_rect(0, 0, PAGE_W, HDR_H, _F.DARK_BG)

    name       = company.get("name", "—")
    name_lines = _wrap_mixed(name, 18, CW)[:2]
    txt(name_lines[0], ML, 32, size=18, color=_F.WHITE)
    if len(name_lines) > 1:
        txt(name_lines[1], ML, 52, size=18, color=_F.WHITE)

    tags = []
    if company.get("listing_status"):
        tags.append(f"[{company['listing_status']}]")
    tags += (company.get("labels") or [])[:5]
    if tags:
        txt("  ".join(tags), ML, 76, size=8.5, color=_F.MUTED)

    y = HDR_H + 16

    # ── Basic info ────────────────────────────────────────────────────────────
    section_heading("基本資料", gap_before=0)

    COL1     = 88
    VAL_MAXW = CW - COL1

    for label, value in [
        ("統一編號",   company.get("tax_id") or "—"),
        ("公司代表人", company.get("representative") or "—"),
        ("資本總額",   _fmt_capital(company.get("authorized_capital"))),
        ("實收資本額", _fmt_capital(company.get("capital"))),
        ("公司所在地", company.get("address") or "—"),
        ("設立日期",   company.get("setup_date") or "—"),
        ("產業別",     company.get("industry") or "—"),
    ]:
        val_lines = _wrap_mixed(value, 9, VAL_MAXW)
        row_h     = max(16, len(val_lines) * 12 + 4)
        _need_space(row_h)
        txt(label, ML, y + 10, size=9, color=_F.MUTED)
        for li, vl in enumerate(val_lines):
            txt(vl, ML + COL1, y + 10 + li * 12, size=9, color=_F.TEXT)
        hline(y + row_h - 2, color=_F.BORDER, w=0.3)
        y += row_h

    # ── Directors ─────────────────────────────────────────────────────────────
    directors = company.get("directors") or []
    if directors:
        section_heading("董監事名單")
        rows = [["職稱", "姓名", "所代表法人", "持股比例"]]
        for d in directors:
            ratio = f"{d['ratio']*100:.2f}%" if d.get("ratio") is not None else "—"
            rows.append([
                d.get("title") or "—",
                d.get("name") or "—",
                d.get("representative_of") or "—",
                ratio,
            ])
        col_widths = _auto_col_widths(rows, 8.5, CW)
        pdf_table(rows, col_widths)

    # ── Summary ───────────────────────────────────────────────────────────────
    summary_raw = company.get("summary", "")
    if summary_raw:
        section_heading("公司簡介")
        for heading, body in _md_sections(summary_raw):
            if heading:
                summary_h3(heading)
            pdf_summary_body(body.split("\n"))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
