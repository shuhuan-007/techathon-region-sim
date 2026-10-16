"""ai/ 层 —— **AI 秘书的地盘**。

═══════════════════════════════════════════════════════════════════
本层边界（一句话）
═══════════════════════════════════════════════════════════════════

**AI 可解释 → 可试算 → 可起草 → 玩家确认 → 提交。**
**AI 无 commit。**

（《规则基线》§11.4。本条不是注释，由 `ai/tools.py` 的
`assert_import_boundary()` 在**调用时**用代码守着。）

本层导出的三个工具（名字照 §11.4 的既有说法）：

    tool_preview      —— 试算（`pipeline.preview`，只读）
    tool_validate     —— 校验（`engine.validate.validate_plan`，只读）
    tool_draft_plan   —— 起草一份 Plan（**不是提交、不是执行**）

以及规则检索（`ai/rules_digest.py`，直读基线文件、不走 HTTP）：

    rule_sections / rule_section / digest
"""

from ai.tools import (
    AisImportBoundaryError,
    assert_import_boundary,
    tool_draft_plan,
    tool_preview,
    tool_validate,
)

__all__ = [
    "AisImportBoundaryError",
    "assert_import_boundary",
    "tool_preview",
    "tool_validate",
    "tool_draft_plan",
]
