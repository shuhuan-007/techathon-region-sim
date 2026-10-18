# -*- coding: utf-8 -*-
"""把 01-说明文档.md 转成排版规整的 DOCX。

用法:
    python md2docx.py <input.md> <output.docx> [--title "文档标题"]
"""
import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

LATIN_FONT = "Times New Roman"
CJK_FONT = "微软雅黑"
CODE_FONT = "Consolas"
ACCENT = RGBColor(0x1F, 0x3B, 0x73)
HEADER_FILL = "1F3B73"
ZEBRA_FILL = "F2F5FA"
CODE_SHADE = "F4F4F4"
QUOTE_BAR = "8AA4C8"


# ---------------------------------------------------------------- 底层工具
def set_run_font(run, name, size=None, bold=None, color=None, italic=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(attr), name)
    rfonts.set(qn("w:eastAsia"), CJK_FONT if name != CODE_FONT else name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def shade(element_pr, fill):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    element_pr.append(shd)


def cell_shade(cell, fill):
    shade(cell._tc.get_or_add_tcPr(), fill)


def para_shade(paragraph, fill):
    shade(paragraph._p.get_or_add_pPr(), fill)


def para_border(paragraph, color=QUOTE_BAR, size=18, side="left"):
    ppr = paragraph._p.get_or_add_pPr()
    bdr = ppr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        ppr.append(bdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), "6")
    el.set(qn("w:color"), color)
    bdr.append(el)


def set_table_borders(table, color="B9C4D8", size=4):
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tbl_pr.append(borders)


def set_cell_margins(table, top=60, bottom=60, left=90, right=90):
    tbl_pr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for edge, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tbl_pr.append(mar)


def repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def prevent_row_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:cantSplit")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


# ---------------------------------------------------------------- 行内解析
INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def add_inline(paragraph, text, size=10.5, base_bold=False, color=None):
    """把 **加粗**、`代码`、[文字](链接) 解析成 run。"""
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            set_run_font(run, LATIN_FONT, size, bold=True, color=color)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            set_run_font(run, CODE_FONT, size - 0.5, bold=base_bold,
                         color=RGBColor(0xB0, 0x30, 0x5E))
        else:
            m = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", piece)
            if m:
                label, url = m.group(1), m.group(2)
                if url.startswith("http"):
                    run = paragraph.add_run(label)
                    set_run_font(run, LATIN_FONT, size, bold=base_bold,
                                 color=RGBColor(0x0B, 0x53, 0x94))
                    run.font.underline = True
                    # 追加不可见链接文本，便于 PDF 保留地址
                    tail = paragraph.add_run(f"（{url}）")
                    set_run_font(tail, LATIN_FONT, size - 1, bold=False,
                                 color=RGBColor(0x60, 0x60, 0x60))
                else:
                    run = paragraph.add_run(label)
                    set_run_font(run, LATIN_FONT, size, bold=base_bold, color=color)
            else:
                run = paragraph.add_run(piece)
                set_run_font(run, LATIN_FONT, size, bold=base_bold, color=color)


# ---------------------------------------------------------------- 文档装配
def setup_styles(doc):
    normal = doc.styles["Normal"]
    normal.font.name = LATIN_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = 1.45
    pf.space_before = Pt(0)
    pf.space_after = Pt(5)

    specs = {
        "Heading 1": (18, True, ACCENT, 18, 10),
        "Heading 2": (13.5, True, RGBColor(0x1F, 0x3B, 0x73), 14, 7),
        "Heading 3": (11.5, True, RGBColor(0x33, 0x33, 0x33), 10, 5),
    }
    for name, (size, bold, color, before, after) in specs.items():
        st = doc.styles[name]
        st.font.name = LATIN_FONT
        st.font.size = Pt(size)
        st.font.bold = bold
        st.font.color.rgb = color
        rpr = st.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = OxmlElement("w:rFonts")
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), CJK_FONT)
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.keep_with_next = True
        st.paragraph_format.line_spacing = 1.3


def add_page_number_footer(section):
    footer = section.footer
    para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run("— ")
    set_run_font(run, LATIN_FONT, 9, color=RGBColor(0x66, 0x66, 0x66))
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run2 = para.add_run()
    set_run_font(run2, LATIN_FONT, 9, color=RGBColor(0x66, 0x66, 0x66))
    run2._r.append(fld_begin)
    run2._r.append(instr)
    run2._r.append(fld_end)
    run3 = para.add_run(" —")
    set_run_font(run3, LATIN_FONT, 9, color=RGBColor(0x66, 0x66, 0x66))


def add_title_block(doc, title, subtitle=None):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(4)
    run = p.add_run(title)
    set_run_font(run, LATIN_FONT, 26, bold=True, color=ACCENT)

    if subtitle:
        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.paragraph_format.space_after = Pt(2)
        r2 = p2.add_run(subtitle)
        set_run_font(r2, LATIN_FONT, 13, bold=False, color=RGBColor(0x44, 0x44, 0x44))

    # 底部细线
    line = doc.add_paragraph()
    line.paragraph_format.space_after = Pt(12)
    para_border(line, color=HEADER_FILL, size=12, side="bottom")


def split_table_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator(line):
    s = line.strip()
    if not s.startswith("|"):
        return False
    cells = split_table_row(s)
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells)


def align_from_sep(sep_cell, body_cell):
    sep_cell = sep_cell.strip()
    if sep_cell.startswith(":") and sep_cell.endswith(":"):
        return WD_ALIGN_PARAGRAPH.CENTER
    if sep_cell.endswith(":"):
        return WD_ALIGN_PARAGRAPH.RIGHT
    # 纯数字列右对齐更美观
    if re.fullmatch(r"[-\d.,%？]+", body_cell or "") and re.search(r"\d", body_cell or ""):
        return WD_ALIGN_PARAGRAPH.RIGHT
    return WD_ALIGN_PARAGRAPH.LEFT


def _text_width_units(text, size_pt):
    """粗略估算文本单行占用的可用宽度（cm）。中文按 1em，ASCII 按 0.55em。"""
    em_cm = size_pt / 72 * 2.54
    width = 0.0
    for ch in text:
        if ord(ch) > 0x2E80:
            width += em_cm
        elif ch == " ":
            width += em_cm * 0.28
        else:
            width += em_cm * 0.55
    return width


def _longest_token(text):
    toks = re.split(r"[\s，。、；：（）()「」【】]+", text)
    return max((len(t) for t in toks), default=0)


def plan_column_widths(header, rows, usable_cm, size_pt):
    """按内容分配列宽：代码/路径列给足宽度，短列（序号/等级）给下限。返回 (宽列表, 字号)。"""
    ncols = len(header)
    pad = 0.34  # 左右单元格边距合计
    details = []
    for ci in range(ncols):
        cells = [header[ci]] + [(r[ci] if ci < len(r) else "") for r in rows]
        longest_line = max((_text_width_units(c.split("\n")[0], size_pt) for c in cells),
                           default=0.0)
        mean_line = sum(_text_width_units(c, size_pt) for c in cells) / max(len(cells), 1)
        longest_tok = max([_text_width_units(t, size_pt)
                           for c in cells
                           for t in re.split(r"[\s，。；：]+", c)] or [0.0])
        details.append({"full": longest_line + pad,
                        "mean": mean_line + pad,
                        "tok": longest_tok + pad,
                        "hdr": _text_width_units(header[ci], size_pt) + pad})

    def floor_w(n):
        # 下限：表头不可断 + 最短可读宽度
        return min(n["full"], max(n["hdr"], n["tok"], 1.15))

    def widths_for(f):
        out = []
        for n in details:
            need = max(n["mean"], n["tok"])
            out.append(max(floor_w(n), min(n["full"], need * f) + pad))
        return out

    f = 1.0
    while f > 0.55:
        w = widths_for(f)
        if sum(w) <= usable_cm:
            break
        f -= 0.03
    w = widths_for(f)
    total = sum(w)
    floors = [floor_w(n) for n in details]
    if total > usable_cm:
        if sum(floors) >= usable_cm:
            # 下限之和已超宽：只能等比压缩（保持各列比例，绝不出现负宽）
            w = [x * usable_cm / total for x in w]
        else:
            slack = usable_cm - sum(floors)
            over = [max(0.0, x - fl) for x, fl in zip(w, floors)]
            so = sum(over)
            if so <= 0:
                w = [fl * usable_cm / sum(floors) for fl in floors]
            else:
                w = [fl + s * slack / so for fl, s in zip(floors, over)]
    else:
        extra = usable_cm - total
        tot = sum(w)
        w = [x + extra * (x / tot) for x in w]
    w = [max(0.4, x) for x in w]
    scale = usable_cm / sum(w)
    if abs(scale - 1.0) > 1e-6:
        w = [x * scale for x in w]
    if f <= 0.7:
        size = 8.5
    elif f <= 0.9:
        size = 9.0
    else:
        size = size_pt
    return w, size


def add_table(doc, header, aligns, rows, usable_cm=16.8):
    ncols = len(header)
    size_pt = 9.5
    widths_cm, size_pt = plan_column_widths(header, rows, usable_cm, size_pt)

    table = doc.add_table(rows=1, cols=ncols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(table)
    set_cell_margins(table)

    # 关闭自动调整，写入固定列宽
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    tbl_pr_tblw = OxmlElement("w:tblW")
    tbl_pr_tblw.set(qn("w:w"), str(int(sum(widths_cm) * 567)))
    tbl_pr_tblw.set(qn("w:type"), "dxa")
    tbl_pr.append(tbl_pr_tblw)
    for gc, wcm in zip(table._tbl.tblGrid.findall(qn("w:gridCol")), widths_cm):
        gc.set(qn("w:w"), str(int(wcm * 567)))

    hdr = table.rows[0]
    repeat_header(hdr)
    prevent_row_split(hdr)
    for i, text in enumerate(header):
        cell = hdr.cells[i]
        cell.width = Cm(widths_cm[i])
        cell_shade(cell, HEADER_FILL)
        para = cell.paragraphs[0]
        para.alignment = aligns[i] if i < len(aligns) else WD_ALIGN_PARAGRAPH.LEFT
        para.paragraph_format.space_before = Pt(2)
        para.paragraph_format.space_after = Pt(2)
        para.paragraph_format.line_spacing = 1.15
        cleaned = text.replace("**", "")
        run = para.add_run(cleaned)
        set_run_font(run, LATIN_FONT, size_pt, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))

    for ridx, row in enumerate(rows):
        cells = table.add_row()
        prevent_row_split(cells)
        for i in range(ncols):
            text = row[i] if i < len(row) else ""
            cell = cells.cells[i]
            cell.width = Cm(widths_cm[i])
            if ridx % 2 == 1:
                cell_shade(cell, ZEBRA_FILL)
            para = cell.paragraphs[0]
            para.alignment = aligns[i] if i < len(aligns) else WD_ALIGN_PARAGRAPH.LEFT
            para.paragraph_format.space_before = Pt(2)
            para.paragraph_format.space_after = Pt(2)
            para.paragraph_format.line_spacing = 1.15
            add_inline(para, text, size=size_pt)

    # 只让表头行与其后一行保持同页（防止表头孤立在页底）；
    # 不整表锁死，否则长表会被整块推到下一页、留下大片空白。
    for cell in table.rows[0].cells:
        for para in cell.paragraphs:
            para.paragraph_format.keep_with_next = True
    return table


def add_quote(doc, lines):
    for line in lines:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.5)
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.35
        para_border(p, color=QUOTE_BAR, size=18, side="left")
        add_inline(p, line, size=10, color=RGBColor(0x44, 0x44, 0x44))
        for run in p.runs:
            run.font.italic = False


def add_bullet(doc, text, ordered=False, marker="•"):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.95)
    p.paragraph_format.first_line_indent = Cm(-0.5)
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.line_spacing = 1.4
    run = p.add_run(marker + " ")
    set_run_font(run, LATIN_FONT, 10.5, bold=ordered, color=ACCENT)
    add_inline(p, text)


def add_code_block(doc, lines):
    for line in lines:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.6)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.2
        para_shade(p, CODE_SHADE)
        run = p.add_run(line if line else " ")
        set_run_font(run, CODE_FONT, 9.5)


def convert(md_path: Path, docx_path: Path, title: str, subtitle: str = None):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    doc = Document()
    setup_styles(doc)

    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.left_margin = Cm(1.8)
    sec.right_margin = Cm(1.8)
    sec.top_margin = Cm(2.2)
    sec.bottom_margin = Cm(2.0)
    add_page_number_footer(sec)
    usable_cm = (sec.page_width - sec.left_margin - sec.right_margin) / 360000

    add_title_block(doc, title, subtitle)

    i = 0
    n = len(lines)
    first_h1 = True
    stats = {"h1": 0, "h2": 0, "h3": 0, "tables": 0, "table_rows": 0, "bullets": 0, "quotes": 0}
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(8)
            para_border(p, color="D0D7E4", size=6, side="bottom")
            i += 1
            continue

        # 代码块
        if stripped.startswith("```"):
            i += 1
            block = []
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            add_code_block(doc, block)
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and is_separator(lines[i + 1]):
            header = split_table_row(stripped)
            sep = split_table_row(lines[i + 1].strip())
            rows = []
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(split_table_row(lines[j].strip()))
                j += 1
            aligns = []
            for k, cell in enumerate(header):
                body = ""
                for r in rows:
                    if k < len(r) and r[k]:
                        body = r[k]
                        break
                aligns.append(align_from_sep(sep[k] if k < len(sep) else "", body))
            add_table(doc, header, aligns, rows, usable_cm=usable_cm)
            stats["tables"] += 1
            stats["table_rows"] += len(rows)
            # 表后留白
            spacer = doc.add_paragraph()
            spacer.paragraph_format.space_after = Pt(0)
            spacer.paragraph_format.line_spacing = 0.6
            i = j
            continue

        # 引用块
        if stripped.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            add_quote(doc, quote)
            stats["quotes"] += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1:
                if not first_h1:
                    doc.add_page_break()
                first_h1 = False
                h = doc.add_heading(level=1)
                add_inline(h, text, size=18, base_bold=True, color=ACCENT)
                h.paragraph_format.space_before = Pt(18)
                para_border(h, color=HEADER_FILL, size=10, side="bottom")
                stats["h1"] += 1
            elif level == 2:
                h = doc.add_heading(level=2)
                add_inline(h, text, size=13.5, base_bold=True,
                           color=RGBColor(0x1F, 0x3B, 0x73))
                stats["h2"] += 1
            else:
                h = doc.add_heading(level=3)
                add_inline(h, text, size=11.5, base_bold=True,
                           color=RGBColor(0x33, 0x33, 0x33))
                stats["h3"] += 1
            i += 1
            continue

        # 有序列表（1. 2. 等）/ 无序列表
        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            add_bullet(doc, m.group(2), ordered=True, marker=m.group(1) + ".")
            stats["bullets"] += 1
            i += 1
            continue
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            add_bullet(doc, m.group(1), ordered=False, marker="•")
            stats["bullets"] += 1
            i += 1
            continue

        # 普通段落（合并同一段的续行）
        para_lines = [stripped]
        j = i + 1
        while j < n:
            nxt = lines[j].strip()
            if (not nxt or nxt.startswith(("#", "|", ">", "-", "*", "`"))
                    or re.fullmatch(r"-{3,}", nxt)):
                break
            para_lines.append(nxt)
            j += 1
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        add_inline(p, " ".join(para_lines))
        i = j

    doc.save(str(docx_path))
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--title", default=None)
    ap.add_argument("--subtitle", default=None)
    a = ap.parse_args()
    src = Path(a.input)
    dst = Path(a.output)
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = convert(src, dst, a.title or src.stem, a.subtitle)
    print("saved:", dst)
    print("stats:", s)
