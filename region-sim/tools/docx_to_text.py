import sys, os
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.oxml.ns import qn

def iter_block_items(parent):
    body = parent.element.body
    for child in body.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, parent)
        elif child.tag == qn('w:tbl'):
            yield Table(child, parent)

def dump(path):
    doc = Document(path)
    out = []
    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            t = block.text.strip()
            if t:
                out.append(t)
        else:
            out.append("=== TABLE ===")
            for row in block.rows:
                cells = [c.text.strip().replace("\n", " / ") for c in row.cells]
                out.append(" | ".join(cells))
            out.append("=== /TABLE ===")
    return "\n".join(out)

src = sys.argv[1]
dst = sys.argv[2]
text = dump(src)
with open(dst, "w", encoding="utf-8") as f:
    f.write(text)
print("wrote", dst, len(text))
