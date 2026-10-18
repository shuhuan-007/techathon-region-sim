# -*- coding: utf-8 -*-
"""Render a DOCX to PNG pages via the bundled LibreOffice kit."""
import json
import subprocess
import sys
from pathlib import Path

NODE = r"D:\dsh\resources\runtime\primary-runtime\dependencies\node\bin\node.exe"
CLI = (r"D:\dsh\resources\app.asar.unpacked\dsh\node_modules"
       r"\@deepseek-ai\libreoffice-kit\lib\cli.js")

docx = Path(sys.argv[1])
outdir = Path(sys.argv[2])
outdir.mkdir(parents=True, exist_ok=True)
pdf = outdir / "doc.pdf"

r = subprocess.run([NODE, CLI, "convert", "--input", str(docx), "--output", str(pdf)],
                   capture_output=True, text=True, encoding="utf-8")
print("convert rc:", r.returncode)
r = subprocess.run([NODE, CLI, "render", "--input", str(pdf), "--output-dir",
                    str(outdir / "pages"), "--dpi", "80"],
                   capture_output=True, text=True, encoding="utf-8")
print("render rc:", r.returncode, r.stderr.strip()[:200])
data = json.loads(r.stdout)
print("pages:", data["pageCount"], "missingFonts:", data.get("missingFonts"))
