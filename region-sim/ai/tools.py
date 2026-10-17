"""AI 秘书的三个工具 —— 试算 / 校验 / 起草（**全部只读**）。

依据《规则基线》§11.4（权限阶梯）：

    AI 可解释 → 可试算 → 可起草 → **玩家确认** → 提交 → 结算时统一执行。
    **AI 无 commit。**

═══════════════════════════════════════════════════════════════════
★ import 白名单自检 —— 把「AI 无 commit」从注释变成代码
═══════════════════════════════════════════════════════════════════

任务书原话："要把这条约束变成代码，不是注释"。

这一节的敌人是**未来某次重构**：某天有人给 ai/ 加功能，顺手
`from engine.pipeline import advance`（或者摸了 `server.session`、
`storage` 的写入口），AI 就从"只能试算"滑向"能真结算"。
等测试失败才发现就晚了 —— 那时坏代码可能已经进了主干。

所以本模块在**调用时自检**（`assert_import_boundary()`）：

    · 不看源码、不 lint —— 直接查 `sys.modules` 里**实际加载了**什么；
    · 源码可以撒谎（import 起别名、`importlib`、exec），
      `sys.modules` 里的键是 Python 解释器记下的**既成事实**，撒不了谎；
    · 命中黑名单 → 抛 `AisImportBoundaryError`，**直接报错**（任务书原话）。

黑白名单都是**显式常量**，写在下面，改名单要过审查。
"""

from __future__ import annotations

import sys
from typing import Optional

from engine import pipeline, validate
from engine.loader import Config, load_config
from engine.models import (
    BuildOrder,
    Plan,
    PlayerState,
    PlotPlan,
    PlotType,
)

from ai.rules_digest import digest

# ══════════════════════════════════════════════════════════════════
# ★ import 白名单 / 黑名单（显式常量 —— 为什么是这两个集合，见注释）
# ══════════════════════════════════════════════════════════════════

#: 允许 ai/ 触达的 engine 子模块（**白名单**）。
#:
#: 为什么恰好是这几个：
#:   · `pipeline`    —— 只用其中的 `preview`（深拷贝后跑，不改状态）；
#:                      ⚠️ `advance` 在**同一个模块**里，所以白名单靠
#:                      `_FORBIDDEN_ATTRS` 在**属性层**再挡一刀。
#:   · `validate`    —— `validate_plan` 本身只收 Plan/Player/Config，
#:                      签名上就杜绝了改状态。
#:   · `loader`/`models` —— 纯数据，没有写入口。
#:   · `construction` —— 工程表/路线/开局减免的**只读查表**（2026-10-18
#:                      起草编译器要用；它没有 commit 入口）。
ALLOWED_ENGINE_MODULES: frozenset[str] = frozenset({
    "engine.pipeline",
    "engine.validate",
    "engine.loader",
    "engine.models",
    "engine.construction",
})

#: 黑名单：`advance` = 真结算（§11.4「AI 永远调不到 advance」）。
#:
#: 为什么 **storage / server.session 整体**进黑名单：
#:   · `GameSession` / `Storage` 的写路径（`save` / `submit` / `advance`）
#:     都是 commit 动作；AI 连"读到它们"都不该发生 ——
#:     产生了依赖，迟早有人调用。
#:   · 黑名单按**模块整名**匹配（含子模块），不数里面哪些是"写入口"：
#:     免得下次谁新增一个写函数、名单忘了加。
_FORBIDDEN_MODULE_PREFIXES: frozenset[str] = frozenset({
    "server.session",
    "server.registry",
    "storage",
})

#: 黑名单（属性层）：这些**名字**不许出现在 ai/ 能摸到的对象上。
#: `pipeline` 是白名单模块，但 `pipeline.advance` 必须被单独点名 ——
#: advance 与 preview **住在同一个模块**，模块级白名单挡不住它。
_FORBIDDEN_ATTRS: frozenset[str] = frozenset({"advance"})


class AisImportBoundaryError(RuntimeError):
    """ai/ 层摸到了 commit 入口（命中黑名单）。

    ⚠️ 刻意继承 `RuntimeError` 而不是 `ImportError`：这不是"装不上包"，
       是**跑起来之后**发现依赖被污染了 —— 让 CI 的异常分类一页就能看穿。
    """


# ── 记录基线：ai/ 这一层**开始时**进程里已经有哪些模块 ──────────────
# ⚠️ 为什么必须有这个基线（2026-10-17 凌晨修的）：
#   本函数最初的实现是「扫全进程 `sys.modules`，命中黑名单就报」，
#   但注释声称的是「只查 ai/ 自己直接加载的」——**实现与注释不符**。
#   后果是真会误伤：正常跑着的服务器进程里，`server.app` 已经 import 了
#   `storage` 与 `server.session`，于是**任何一次自检都报"ai/ 越界"**，
#   而 ai/ 其实一个 commit 入口都没碰过。安全机制动不动误报，
#   下一步就是有人把整个自检删掉 —— 那才是最坏的结局。
#
#   所以改成：**只算 ai/ 层的账** —— 记下 import 本模块那一刻的既成事实，
#   之后**新出现**的黑名单模块才算 ai/ 带进来的。
_LOADED_AT_AI_IMPORT: frozenset[str] = frozenset(sys.modules)


# ══════════════════════════════════════════════════════════════════
# ★ 自检本体（调用时跑，不是 import 时跑 —— import 时会误伤正常链路）
# ══════════════════════════════════════════════════════════════════

def assert_import_boundary() -> None:
    """检查 ai/ 这一层**自己**有没有把 commit 入口带进来。

    命中黑名单就直接抛 `AisImportBoundaryError`。

    两道检查：

      ① **模块级**：只数「ai/ 加载**之后新出现**的黑名单模块」。
         进程启动时就已存在的（例如服务器进程里的 `storage`）不算 ai/ 的账 ——
         那不是 ai/ 拉进来的，拿它定罪就是误报（见 `_LOADED_AT_AI_IMPORT`）。
      ② **属性级**：`advance` 这个**函数对象**不许被任何一个 `ai.*` 模块持有。
         用 `is` 比对象身份，不做名字匹配 —— 叫 advance 的局部变量不算违规。

    ⚠️ 为什么用 `sys.modules` + 对象身份，而不扫源码：
       import 可以起别名（`from engine.pipeline import advance as _x`）、
       可以 `importlib.import_module`、可以 `exec` —— 源码扫描全部漏。
       `sys.modules` 与函数对象是解释器记下的**实际事实**，绕不过去。
    """
    loaded = set(sys.modules)

    # ── ① 模块级黑名单：只算"ai/ 带进来的" ──────────────────
    prefixes = tuple(_FORBIDDEN_MODULE_PREFIXES)
    hits_mod = sorted(
        m for m in loaded
        if m not in _LOADED_AT_AI_IMPORT
        and any(m == p or m.startswith(p + ".") for p in prefixes)
    )

    # ── ② 属性级黑名单：`advance` 不许被 ai/ 的任何模块持有 ──
    # ⚠️ `advance` 和 `preview` 住在同一个模块（pipeline 同时是白名单），
    #    模块级名单挡不住属性级别的抓取 —— 所以这里单独查。
    real_advance = getattr(pipeline, "advance", None)
    hits_attr: list[str] = []
    for name in sorted(loaded):
        if not (name == "ai" or name.startswith("ai.")):
            continue
        mod = sys.modules[name]
        if mod is None:
            continue
        for attr in sorted(_FORBIDDEN_ATTRS):
            obj = getattr(mod, attr, None)
            if obj is not None and real_advance is not None and obj is real_advance:
                hits_attr.append(f"{name}.{attr}")

    if hits_mod or hits_attr:
        parts: list[str] = []
        if hits_mod:
            parts.append(
                "黑名单模块已被加载：" + ", ".join(hits_mod)
                + "（AI 无 commit：storage / server.* 的写入口不该出现在 ai/ 的依赖里）"
            )
        if hits_attr:
            parts.append(
                "黑名单符号已被 ai/ 持有：" + ", ".join(hits_attr)
                + "（advance 是真结算，§11.4 明写「AI 永远调不到 advance」）"
            )
        raise AisImportBoundaryError(
            "ai/ 层 import 边界被突破：\n  " + "\n  ".join(parts)
        )


# ══════════════════════════════════════════════════════════════════
# 工具 1：试算（只读）
# ══════════════════════════════════════════════════════════════════

def tool_preview(state, plans: dict[str, Plan], cfg: Config) -> dict:
    """调 `pipeline.preview` 返回**试算报告**（只读，状态一个字不动）。

    对应 §11.4 的 `simulate_plan`。数字**全部来自引擎**（不许编）：
    报告里没有的字段，AI 就说"拿不到"，不许猜。

    返回 `QuarterReport.to_dict()` —— dict 形状，**前端只显示、零计算**。
    """
    assert_import_boundary()
    report = pipeline.preview(state, plans, cfg)
    return report.to_dict()


# ══════════════════════════════════════════════════════════════════
# 工具 2：校验（只读）
# ══════════════════════════════════════════════════════════════════

def tool_validate(
    plan: Plan,
    player: PlayerState,
    cfg: Config,
    player_id: Optional[str] = None,
) -> list[dict]:
    """调 `engine.validate.validate_plan` 返回 issues（**不改** plan / player）。

    对应 §11.4 的 `validate_plan`。返回 `[PlanIssue.to_dict(), ...]`，
    空列表 = 通过。
    """
    assert_import_boundary()
    issues = validate.validate_plan(plan, player, cfg, player_id)
    return [i.to_dict() for i in issues]


# ══════════════════════════════════════════════════════════════════
# 工具 3：起草（只产出**计划数据**，不是提交、不是执行）
# ══════════════════════════════════════════════════════════════════

def tool_draft_plan(
    player_id: str,
    player: Optional[PlayerState] = None,
    cfg: Optional[Config] = None,
    *,
    run_all: bool = True,
    u: float = 1.0,
    purchases: Optional[dict[str, float]] = None,
    food: Optional[dict[str, float]] = None,
    builds: Optional[list[BuildOrder]] = None,
    with_rule_digest: bool = False,
) -> dict:
    """产出一份**合法的 models.Plan 形状**，交给人确认。

    ⚠️ **不是提交、不是执行**：返回值里没有"已提交"这回事，
       提交永远走 `/api/submit`（§11.4 权限阶梯）。

    形状：`{"ok": bool, "plan": dict|None, "issues": [...], ...}`。

      · 自检能过 `validate_plan` → `ok=True`，`plan` 是 `Plan.to_dict()`；
      · 过不了 → `ok=False`，`plan=None`，返回**问题清单**
        （任务书："而不是产出一个坏计划"）。

    `run_all=True` 时会把这个玩家**全部 RUNNING 地块**按开工比例 `u`
    排进计划（数字来自 `player.plots` 与 `cfg`，不是编的）。
    `with_rule_digest=True` 时附一段基线摘要（`rules_digest.digest`），
    供提示词引用 —— 解释权归 AI，**判决权永远在 validate**。
    """
    assert_import_boundary()
    if cfg is None:
        cfg = load_config()

    plot_plans: dict[str, PlotPlan] = {}
    if run_all and player is not None:
        for pid in sorted(player.plots):
            plot = player.plots[pid]
            if not plot.is_running:
                continue
            plot_plans[pid] = PlotPlan(plot_id=pid, active=u > 0, u=u)

    plan = Plan(
        player_id=player_id,
        plot_plans=plot_plans,
        purchases=dict(purchases or {}),
        food=dict(food or {}),
        builds=list(builds or []),
    )

    out: dict = {
        "ok": False,
        "plan": None,
        "issues": [],
        # 这五个字是权限阶梯的落点：AI 只起草，**提交走 /api/submit**。
        "next_step": "玩家确认后由前端走 /api/submit 提交（AI 无 commit）",
    }
    if with_rule_digest:
        out["rule_digest"] = digest("11.4")

    if player is None:
        # 拿不到玩家状态 → 无法自检，宁可明说拿不到（不编数字）
        out["issues"] = [{
            "code": "no_player_state",
            "target": player_id,
            "message": "拿不到玩家状态，无法自检；起草被放弃。",
            "requested": None, "available": None, "shortfall": None,
        }]
        return out

    issues = validate.validate_plan(plan, player, cfg, player_id)
    if issues:
        out["issues"] = [i.to_dict() for i in issues]
        return out

    out["ok"] = True
    out["plan"] = plan.to_dict()
    return out
