# -*- coding: utf-8 -*-
"""Merge markdown docs with continuous chapter renumbering.

Rule 1 (structure): level-2 chapters are renumbered continuously 一,二,三...
Rule 2 (sub-numbers): "N.M" in a heading is rewritten only when N equals the old
        level-2 index of that heading's own chapter (02's "7.8" in 七 -> "15.8").
        Title prefixes of a DIFFERENT original (e.g. "02-走查/") are left alone.
Rule 3 (cross-refs): internal refs "§X" / "见 X.Y" are rewritten only when they
        clearly point inside the merged doc. Refs into OTHER files (e.g.
        "《规则基线》§6", "icons/README.md §二", "server/ai_host.py L40 ...§11.4")
        are left untouched. Cross-file refs are listed in the report so a human
        can decide.
"""
import re
import sys

CJK = "一二三四五六七八九十"
# Lines that quote another file's own numbering - never rewrite these.
EXTERNAL_MARKERS = ("规则基线", "README.md", "icons/README.md", "ai_host.py",
                    "prototype", "前文", "上一份")


def cjk_numeral(n):
    if n <= 10:
        return CJK[n - 1]
    if n < 20:
        return "十" + CJK[n - 11]
    tens, ones = divmod(n, 10)
    return CJK[tens - 1] + "十" + (CJK[ones - 1] if ones else "")


def cjk_value(s):
    """Parse 一..二十 etc."""
    s = s.strip()
    if "十" not in s:
        return CJK.index(s) + 1
    left, _, right = s.partition("十")
    tens = CJK.index(left) + 1 if left else 1
    ones = CJK.index(right) + 1 if right else 0
    return tens * 10 + ones


def convert_files(paths, body_refs=None):
    """body_refs: list of (old_text, new_text) applied to body lines."""
    body_refs = body_refs or []
    out, report = [], {"h1": 0, "h2": 0, "h3": 0, "h3_renumbered": 0,
                       "body_refs_applied": 0, "external_refs_kept": []}
    counter = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        old_chapter = 0
        in_fence = False
        for line in lines:
            s = line.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue
            m = None if in_fence else re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                level, text = len(m.group(1)), m.group(2).strip()
                if level == 1:
                    report["h1"] += 1
                    out.append(line)
                    continue
                if level == 2:
                    mm = re.match(r"^([一二三四五六七八九十]+)、(.*)$", text)
                    old_chapter = cjk_value(mm.group(1)) if mm else old_chapter + 1
                    counter += 1
                    text = cjk_numeral(counter) + "、" + (mm.group(2) if mm else text)
                    report["h2"] += 1
                    out.append("#" * level + " " + text)
                    continue
                mm = re.match(r"^(\d+)\.(\d+)\s+(.*)$", text)
                if mm and int(mm.group(1)) == old_chapter:
                    text = f"{counter}.{mm.group(2)} {mm.group(3)}"
                    report["h3_renumbered"] += 1
                report["h3"] += 1
                out.append("#" * level + " " + text)
                continue

            # body line
            new = line
            for old, rep in body_refs:
                if old in new:
                    # skip if the line points into another file's numbering
                    if any(mk in new for mk in EXTERNAL_MARKERS) and "README.md" not in old:
                        continue
                    new = new.replace(old, rep)
                    report["body_refs_applied"] += 1
            if new != line:
                report.setdefault("body_ref_changes", []).append((line.strip()[:70], new.strip()[:70]))
            for mk in ("规则基线", "README.md", "icons/README.md", "ai_host.py"):
                if mk in new and "§" in new:
                    report["external_refs_kept"].append(mk)
                    break
            out.append(new)
        out.append("")
    report["external_refs_kept"] = sorted(set(report["external_refs_kept"]))
    return out, report


if __name__ == "__main__":
    out_path = sys.argv[1]
    merged, rep = convert_files(sys.argv[2:])
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(merged))
    print("report:", rep)
