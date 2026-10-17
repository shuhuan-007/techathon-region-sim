"""计划校验 —— **在结算之前**把无效的 Plan 挡下来。

依据《规则基线》§11.3（无效 Plan 处理，✅ 采纳为工程建议）：

    · 采购超限、材料不足或资金不足时**返回明确错误**；
    · **不自动减少**采购或开工量；**不产生部分状态变更**；
    · AI 秘书提出修改方案后，**由玩家重新确认**；
    · 错误信息至少含：**错误类型 / 涉及商品或地块 / 申请值 / 可用值 / 差额**。

═══════════════════════════════════════════════════════════════════
为什么需要这个文件（它是第 1 优先级）
═══════════════════════════════════════════════════════════════════

在它出现之前，`server/session.py` 的 `submit()` **只检查"是不是重复提交"**。
也就是说：一个地块 ID 不存在、或者买了不存在商品的计划，**能一路通过提交**，
直到 `pipeline.advance()` 第 5 步扣款时才炸。

两个后果：

  ① **报错点离犯错点太远**。玩家点"提交"时什么都没发生，
     点"结束季度"才看到一个关于扣款的报错 —— 他不知道是哪一步错了。
  ② **AI 试算与真结算不一致**。AI 的 Plan 与玩家的 Plan 走**同一条** validate
     （见 `models.py` 顶部），所以这里也是 AI 的护栏。

═══════════════════════════════════════════════════════════════════
三条设计口径
═══════════════════════════════════════════════════════════════════

**一、只读，绝不改状态。**
   本模块不接收 `GameState`（只接收 `PlayerState` 与 `Config`），
   从签名上就杜绝了"顺手改一下"的可能。

**二、一次报全，不是报第一个就返回。**
   玩家改一次计划不容易，一次把 5 个问题都列出来比"修一个报一个"好。
   所以 `validate_plan()` 返回的是**问题列表**，不是抛异常。

**三、校验 ≠ 结算。**
   资金校验在这里是**粗校验**（见 `_check_cash`），精确的扣款仍在第 5 步。
   两处都查不是重复 —— 这里是为了"早点告诉玩家"，那里是**唯一权威**。

    ⚠️ 因此**不要**为了消除"重复"而删掉第 5 步的资金检查。
       本模块可能因为拿不到本季真实价而低估或高估，**第 5 步才是最终判据**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine import construction
from engine.loader import Config, ConfigError
from engine.models import (
    BuildOrder,
    Plan,
    PlayerState,
    PlotPlan,
    PlotStatus,
    PlotType,
    parse_plot_id,
)


# ══════════════════════════════════════════════════════════════════
# 错误对象
# ══════════════════════════════════════════════════════════════════


@dataclass
class PlanIssue:
    """一条计划问题。

    字段命名直接对齐 §11.3 要求的五要素
    「**错误类型 / 涉及商品或地块 / 申请值 / 可用值 / 差额**」，
    这样服务层与前端**不需要再解析字符串**就能把数字填进提示框。
    """

    code: str                    # 错误类型（机器可读，如 "unknown_plot"）
    target: str                  # 涉及的商品或地块
    message: str                 # 给人看的一句话
    requested: Optional[float] = None   # 申请值
    available: Optional[float] = None   # 可用值

    @property
    def shortfall(self) -> Optional[float]:
        """差额 = 申请值 − 可用值。

        两者缺一就返回 `None`（**不编造 0** —— 那会让人以为"差额为零"）。
        """
        if self.requested is None or self.available is None:
            return None
        return self.requested - self.available

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "target": self.target,
            "message": self.message,
            "requested": self.requested,
            "available": self.available,
            "shortfall": self.shortfall,
        }

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        if self.shortfall is None:
            return base
        return (
            f"{base}\n"
            f"    申请 {self.requested:g} / 可用 {self.available:g} / "
            f"差额 {self.shortfall:g}"
        )


class PlanRejected(Exception):
    """计划被拒绝（`require_valid` 用）。

    ⚠️ **不是** `SettlementError`：这个错发生在**提交时**，不是结算时。
       两者混用会让服务层分不清"该让玩家改计划"还是"该回滚结算"。
    """

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        joined = "\n  ".join(str(i) for i in issues)
        super().__init__(f"计划有 {len(issues)} 处问题：\n  {joined}")


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════


def validate_plan(
    plan: Plan,
    player: PlayerState,
    cfg: Config,
    player_id: Optional[str] = None,
) -> list[PlanIssue]:
    """校验一份计划。**返回问题列表，空列表 = 通过。**

    参数
    ----
    plan      : 待校验的计划（**只读，不会被改**）
    player    : 提交者的私有状态（只看 `plots` / `cash`）
    cfg       : 静态配置
    player_id : 期望的提交者。**给了就查一致性**，不给就用 `plan.player_id`。

    ⚠️ 本函数**一次报全**（见模块头"三条设计口径"其二）。
    """
    issues: list[PlanIssue] = []

    # ── 0. 提交者一致性（红线 1：玩家身份永远用 player_id 索引）──
    if player_id is not None and plan.player_id != player_id:
        issues.append(
            PlanIssue(
                code="player_mismatch",
                target=plan.player_id,
                message=(
                    f"计划里的 player_id ({plan.player_id!r}) "
                    f"与提交者 ({player_id!r}) 不一致"
                ),
            )
        )

    # ⚠️ 计划里的 player_id 与实际玩家不一致时，后面所有校验都不可信
    #    （可能整份计划都是别人的地块）。**立刻返回**，避免刷出一堆
    #    "地块不存在"的噪声把真正的问题埋掉。
    if issues:
        return issues

    issues += _check_plot_plans(plan, player, cfg)
    issues += _check_purchases(plan, cfg)
    issues += _check_food(plan, cfg)
    issues += _check_builds(plan, player, cfg)
    issues += _check_contracts(plan, cfg)
    issues += _check_cash(plan, player, cfg)

    return issues


def require_valid(
    plan: Plan,
    player: PlayerState,
    cfg: Config,
    player_id: Optional[str] = None,
) -> None:
    """校验并在有问题时抛 `PlanRejected`（给服务层用）。"""
    issues = validate_plan(plan, player, cfg, player_id)
    if issues:
        raise PlanRejected(issues)


def is_valid(
    plan: Plan,
    player: PlayerState,
    cfg: Config,
    player_id: Optional[str] = None,
) -> bool:
    """只问"能不能过"（给 `can_settle` 之类的 UI 判断用）。"""
    return not validate_plan(plan, player, cfg, player_id)


# ══════════════════════════════════════════════════════════════════
# 分项校验
# ══════════════════════════════════════════════════════════════════


def _check_plot_plans(
    plan: Plan, player: PlayerState, cfg: Config
) -> list[PlanIssue]:
    """地块安排：ID 存在、状态可开工、农业/工业**必须能查出配方**。

    ⚠️ **"能不能查出配方"是本项目最容易漏的一条校验。**
       农业有 4 种产出（A/B/C/D），`Config.recipe()` 在 `product=None` 时
       **会报错**（见 `loader.recipe()`：农业产出不能猜默认值）。
       如果这里不查，玩家漏填 `product` 的计划会一路走到第 4 步生产时才炸。
    """
    issues: list[PlanIssue] = []

    for pid in sorted(plan.plot_plans):
        pp: PlotPlan = plan.plot_plans[pid]

        # ── ① 地块存在（且在**自己**名下 —— plots 本来就是私有的）──
        plot = player.plots.get(pid)
        if plot is None:
            issues.append(
                PlanIssue(
                    code="unknown_plot",
                    target=pid,
                    message=(
                        f"地块 {pid!r} 不存在（不属于 {player.player_id}）。\n"
                        f"    可用地块：{sorted(player.plots) if player.plots else '（一块都没有）'}"
                    ),
                )
            )
            continue   # 后面的检查全依赖 plot，跳过

        # ── ② 状态必须 RUNNING ─────────────────────────────
        # 在建 / 升级中的地块**不能开工**（§6 第 1 步：到期才切 RUNNING）
        if plot.status is not PlotStatus.RUNNING:
            issues.append(
                PlanIssue(
                    code="plot_not_running",
                    target=pid,
                    message=(
                        f"地块 {pid!r} 当前是 {plot.status.value}，本季不能开工。\n"
                        f"    ⚠️ 在建/升级中的地块要等工期归零"
                        f"（还剩 {plot.construction_left} 季）。"
                    ),
                    available=float(plot.construction_left),
                )
            )
            continue

        # ── ③ u 的范围 ─────────────────────────────────────
        # ⚠️ `PlotPlan.__post_init__` 已经挡了构造期的非法 u，
        #    但 `from_dict()` 走的是同一个构造器，所以这里查的是
        #    **反序列化/手工拼 dict 绕过构造器**的情况，不是重复检查。
        if not 0.0 <= pp.u <= 1.0:
            issues.append(
                PlanIssue(
                    code="u_out_of_range",
                    target=pid,
                    message=f"地块 {pid!r} 的开工比例 u 必须在 0–1 之间",
                    requested=pp.u,
                    available=1.0,
                )
            )
            continue

        # ── ④ 不开工就不必查配方（u=0 或 active=False 都不消耗任何东西）──
        if pp.effective_u <= 0.0:
            continue

        # ── ⑤ 必须能查出配方 ───────────────────────────────
        # 农业缺 product、工业路线非法，都在这里被挡住。
        try:
            cfg.recipe(plot.plot_type, plot.level, plot.product)
        except ConfigError as e:
            issues.append(
                PlanIssue(
                    code="recipe_unavailable",
                    target=pid,
                    message=(
                        f"地块 {pid!r}（{plot.plot_type.value} {plot.level} 级、"
                        f"产出 {plot.product!r}）查不到配方：{e}"
                    ),
                )
            )

    return issues


def _check_purchases(plan: Plan, cfg: Config) -> list[PlanIssue]:
    """采购：商品必须在市场里、数量必须为正。

    ══════════════════════════════════════════════════════════════
    ⚠️ 这里**故意不检查**"口粮混进了 purchases"
    ══════════════════════════════════════════════════════════════

    曾经有过一条 `food_in_purchases` 检查（"A/B/C/D 是口粮，应填在
    `food` 里"）。**它是错的，已删除**，原因：

        **C 和 D 是双用途商品** —— 既是优秀档口粮，**又是工业 G 的生产原料**
        （配方 `G_L1: input {C:2, D:1, F:3}`，见《规则基线》§2.3）。

    所以"这个商品是不是口粮"**无法由商品编码判定** ——
    同一次采购里，C 既可能是拿去喂居民的、也可能是拿去投 G 厂的。

    `purchases` 与 `food` 本来就是**两条独立的通道**：

        `purchases`  → 进 `B_t` 的 `other_purchases` 段，第 5 步扣款
        `food`       → 走"自产先顶 + 缺口买入"，且**能被居民吃掉**

    两者在流水线里**各算各的**（`pipeline` 第 4.5 步把两者分别汇总），
    混填**不会**让"自产顶上"算不出来。原来那条检查是基于一个错误的前提，
    会**误拦合法计划**（例如"买 C×2 投 G 厂"）。

    → 结论：**不做这条检查。** 若将来确实要区分"买来干嘛"，
      正确的做法是让玩家在**申报时标明用途**，而不是由程序猜。
    """
    issues: list[PlanIssue] = []
    known = set(cfg.market)

    for goods in sorted(plan.purchases):
        qty = plan.purchases[goods]

        if goods not in known:
            issues.append(
                PlanIssue(
                    code="unknown_goods",
                    target=goods,
                    message=(
                        f"商品 {goods!r} 不存在，不能采购。\n"
                        f"    市场里的商品：{sorted(known)}"
                    ),
                )
            )
            continue

        if qty < 0:
            issues.append(
                PlanIssue(
                    code="negative_purchase",
                    target=goods,
                    message=(
                        f"商品 {goods!r} 的采购量为负（{qty:g}）。\n"
                        f"    ⚠️ 想减少采购就把这一项去掉或写 0，"
                        f"没有『负数买入』这回事。"
                    ),
                    requested=qty,
                    available=0.0,
                )
            )
            continue

    return issues


def _check_food(plan: Plan, cfg: Config) -> list[PlanIssue]:
    """口粮申报：**只能是 A/B/C/D**，数量不能为负。

    可吃的商品集合 = 两档配比里出现过的商品并集
    （基准档 A/B，优秀档 A/B/C/D → 并集就是 A/B/C/D）。
    """
    issues: list[PlanIssue] = []
    edible = set(cfg.food.baseline_quota) | set(cfg.food.good_quota)

    for goods in sorted(plan.food):
        qty = plan.food[goods]

        if goods not in edible:
            issues.append(
                PlanIssue(
                    code="inedible_food",
                    target=goods,
                    message=(
                        f"商品 {goods!r} 不能作为口粮。\n"
                        f"    可作口粮的：{sorted(edible)}\n"
                        f"    ⚠️ 两档配比见《规则基线》§3.3.3"
                        f"（基准档 A/B，优秀档 A/B/C/D）。"
                    ),
                )
            )
            continue

        if qty < 0:
            issues.append(
                PlanIssue(
                    code="negative_food",
                    target=goods,
                    message=f"口粮 {goods!r} 的申报量为负（{qty:g}）",
                    requested=qty,
                    available=0.0,
                )
            )

    return issues


def _check_builds(
    plan: Plan, player: PlayerState, cfg: Config
) -> list[PlanIssue]:
    """建设 / 升级指令的**可执行性**校验（形状 + 坐标 + 重复 + 工程表）。

    ★ 2026-10-18 硬化（AI 秘书起草方案的漏洞修复，见
      `docs/03-架构/AI秘书-有效起草方案.md` §二 / §三.1）：

        ① `plot_id` 必须是合法的 `"r,c"`，且落在
           `0 ≤ r < cfg.rows`、`0 ≤ c < cfg.cols` 之内（越界 = 地图上没这块地）；
        ② 同一份 `plan.builds` 里**同一块地只能下一条**建设指令
           （第一条保留，后面每一条都报 `duplicate_build_plot`）；
        ③ 农业 / 工业的 `product` 必须是该类型、该等级**真实存在**的
           产出 / 路线（真值表见 `_BUILD_PRODUCTS`）；
        ④ 用 `construction.find_work_for_build()` 做一次 **dry-run** ——
           结算第 6 步真去找的就是这条工程，查不到就现在报
           `build_work_unavailable`。

    ⚠️ 为什么必须有 ④：`construction.works` 已经按基线 §5.2 录满 14 行，
       "查不到工程"不再是"表还没填"，而是**这条指令根本执行不了**。
       在此之前校验只查 `product` 非空，于是"在空地上建研究地块"这类计划
       能过校验、提交成功，直到结算才抛 `ConfigError` —— 那一季直接作废。
       这是 AI 起草计划时最容易踩的一类坑（模型不知道工程表长什么样）。

    ⚠️ 仍然**不查"能不能负担"**（现金在 `_check_cash` 里按 P0 粗估）。
       本函数只回答"这条指令本身能不能执行"。
    """
    issues: list[PlanIssue] = []

    #: 已经下过指令的地块：`(row, col)` → 第一次出现的下标（0 基）。
    #   ⚠️ 用**解析后的坐标**而不是原始字符串做键：'3,4' 与 '03,4' 是同一块地，
    #      用字符串会让"同一块地下两条指令"漏过去（重复建造 = 重复扣钱/重复改状态）。
    seen_plots: dict[tuple[int, int], int] = {}

    for idx, order in enumerate(plan.builds):
        bid = order.plot_id
        plot = player.plots.get(bid)

        # ── ① 坐标格式：必须是 "r,c" ────────────────────────
        coords = _parse_build_coords(bid)
        if coords is None:
            issues.append(
                PlanIssue(
                    code="bad_build_plot_id",
                    target=bid,
                    message=(
                        f"第 {idx + 1} 条建设指令的地块 ID {bid!r} 格式非法。\n"
                        f"    地块 ID 一律是 'r,c'（行列从 0 起，如 '0,0' / '3,4'）。"
                    ),
                )
            )
            continue

        r, c = coords
        if not (0 <= r < cfg.rows and 0 <= c < cfg.cols):
            axis, value, limit = (
                ("行 r", r, cfg.rows) if not 0 <= r < cfg.rows
                else ("列 c", c, cfg.cols)
            )
            issues.append(
                PlanIssue(
                    code="build_plot_out_of_bounds",
                    target=bid,
                    message=(
                        f"第 {idx + 1} 条建设指令的地块 {bid!r} 超出了地图范围："
                        f"{axis} = {value}，而地图是 "
                        f"{cfg.rows} 行 × {cfg.cols} 列"
                        f"（合法下标 0–{cfg.rows - 1} / 0–{cfg.cols - 1}）。\n"
                        f"    ⚠️ 地块 ID 用 'r,c'（0 基）：'0,0' 是左上角、"
                        f"'{cfg.rows - 1},{cfg.cols - 1}' 是右下角。"
                    ),
                    requested=float(value),
                    available=float(limit - 1),
                )
            )
            continue

        # ── ② 同一份计划里重复下指令 ────────────────────────
        first_idx = seen_plots.get(coords)
        if first_idx is not None:
            issues.append(
                PlanIssue(
                    code="duplicate_build_plot",
                    target=bid,
                    message=(
                        f"第 {idx + 1} 条建设指令与第 {first_idx + 1} 条**重复**："
                        f"同一份计划里，地块 {bid!r} 只能下**一条**建设指令。\n"
                        f"    ⚠️ 这里只认第 {first_idx + 1} 条，后面的不生效；"
                        f"想「先新建再升级」请分两季（工程表只有 0→1 / 1→2 / 2→3，"
                        f"一季只能推进一步）。"
                    ),
                    requested=float(idx + 1),
                    available=float(first_idx + 1),
                )
            )
            continue
        seen_plots[coords] = idx

        # ── ③ 空地新建：**只能从 1 级起**（工程表只有 0→1 / 1→2 / 2→3）──
        # ⚠️ 2026-10-17 修：原来没有这条。玩家（或 AI 起草的计划）在空地上直接写
        #    target_level=2 时，校验**放行**，到**结算**才抛 ConfigError
        #    「没有这条工程：agriculture 0→2」—— 那一季直接结算失败。
        #    校验阶段就该拦住，并告诉玩家正确做法。
        if plot is None and 1 < order.target_level <= 3:
            issues.append(
                PlanIssue(
                    code="cannot_build_level_directly",
                    target=bid,
                    message=(
                        f"地块 {bid!r} 目前是空地，**新建只能建 1 级**"
                        f"（工程表只有 0→1、1→2、2→3），"
                        f"但这条指令写的是 {order.target_level} 级。\n"
                        f"    ⚠️ 想建 2/3 级：先建 1 级，建成后再下升级工程。"
                    ),
                    requested=float(order.target_level),
                    available=1.0,
                )
            )
            continue

        # ── ④ 已有地块：状态 / 等级 / 类型约束 ──────────────
        # 空地（plot is None）跳过这一段：`plot_id` 可以不在 plots 里（还没建）。
        if plot is not None:
            if plot.status is PlotStatus.CONSTRUCTING:
                issues.append(
                    PlanIssue(
                        code="plot_already_constructing",
                        target=bid,
                        message=(
                            f"地块 {bid!r} 已经在建/升级中（还剩 "
                            f"{plot.construction_left} 季），不能重复下达建设指令。"
                        ),
                        available=float(plot.construction_left),
                    )
                )
                continue

            if order.target_level <= plot.level:
                issues.append(
                    PlanIssue(
                        code="build_not_an_upgrade",
                        target=bid,
                        message=(
                            f"地块 {bid!r} 已经是 {plot.level} 级，"
                            f"而建设指令的目标等级是 {order.target_level} —— "
                            f"目标等级必须高于当前等级。\n"
                            f"    ⚠️ 想换产出/路线只能**拆掉重建**（建后不可更换，§5.1）。"
                        ),
                        requested=float(order.target_level),
                        available=float(plot.level),
                    )
                )
                continue

            # 已有地块的**类型**不能改（拆掉重建 = 先删再建，是两步）
            if order.plot_type is not plot.plot_type:
                issues.append(
                    PlanIssue(
                        code="build_type_mismatch",
                        target=bid,
                        message=(
                            f"地块 {bid!r} 是 {plot.plot_type.value}，"
                            f"但建设指令写的是 {order.plot_type.value}。\n"
                            f"    ⚠️ 升级不能改变地块类型；换类型＝拆掉重建。"
                        ),
                    )
                )
                continue

        # ── ⑤ 目标等级必须在 1–3 ────────────────────────────
        if not 1 <= order.target_level <= 3:
            issues.append(
                PlanIssue(
                    code="bad_target_level",
                    target=bid,
                    message=(
                        f"地块 {bid!r} 的目标等级 {order.target_level} 非法（只能是 1/2/3）"
                    ),
                    requested=float(order.target_level),
                    available=3.0,
                )
            )
            continue

        # ── ⑥ ★ 产出/路线：建造时选定，建后不可更换（§5.1）──
        # ⚠️ 有产品问题时**直接 continue**：`find_work_for_build()` 是用 product
        #    反推工业路线的（研究线 vs 机械线），product 本身不合法时再去 dry-run，
        #    只会按一条错误的路线上报个"工程查到了"，把真正的问题埋掉。
        product_issues = _check_build_product(order, idx)
        if product_issues:
            issues += product_issues
            continue

        # ── ⑦ ★ 工程表 dry-run：结算第 6 步真去找的就是这条工程 ──
        # 查不到 = 这条指令根本执行不了（例如"空地上新建研究地块"——工程表
        # 14 行里没有 research 这一类）。校验阶段就挡住，别拖到结算把整季废掉。
        try:
            construction.find_work_for_build(cfg, plot, order)
        except ConfigError as e:
            issues.append(
                PlanIssue(
                    code="build_work_unavailable",
                    target=bid,
                    message=(
                        f"第 {idx + 1} 条建设指令在工程表里查不到对应的工程：{e}\n"
                        f"    ⚠️ 结算时会在这里失败，整份账单不成立"
                        f"（§11.3 不允许部分执行），所以提交前就挡住。"
                    ),
                )
            )
            continue

    return issues


def _parse_build_coords(plot_id: object) -> Optional[tuple[int, int]]:
    """把 `plot_id` 解析成 `(row, col)`；格式不对返回 `None`。

    ⚠️ **不抛异常**：`PlotIssue` 才是本模块的出口，
       抛 `ValueError` 会把一次"计划有错"变成服务层的 500。
    """
    try:
        return parse_plot_id(plot_id)   # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


# ══════════════════════════════════════════════════════════════════
# ★ 建造时可选产出 / 路线的**真值表**（校验用）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 为什么另起一张表，而不是复用 `buildable_products()`：
#    后者是**给前端下拉框**用的，工业二级只列 H-A/H-F/H-G/H-J；
#    但"研究线"（`product="研究"`）是**规则上合法**的二级路线
#    （`construction.find_work_for_build` 认它 → `ind_1_2_research`，6 季），
#    只是前端暂时没把它放进下拉框。校验若照抄下拉框，就会把这条合法路线判死。
#    → 校验真值以本表为准；UI 选项等前端契约定了再单独改 `buildable_products()`。
#
# 资源 / 服务 / 研究三类没有多路线（不在这张表里 → 不校验 product）。
_BUILD_PRODUCTS: dict[tuple[PlotType, int], tuple[str, ...]] = {
    (PlotType.AGRICULTURE, 1): ("A", "B", "C", "D"),
    (PlotType.AGRICULTURE, 2): ("A", "B", "C", "D"),
    (PlotType.AGRICULTURE, 3): ("A", "B", "C", "D"),
    (PlotType.INDUSTRY, 1): ("G",),
    # ★ 工业二级多一条「研究」（研究线：6 季，之后才能升三级 I）
    (PlotType.INDUSTRY, 2): ("H-A", "H-F", "H-G", "H-J", "研究"),
    (PlotType.INDUSTRY, 3): ("H-A+", "H-F+", "H-G+", "H-J+", "I"),
}


def _allowed_build_products(
    plot_type: PlotType, target_level: int
) -> Optional[tuple[str, ...]]:
    """某类型 / 某等级**建造时**允许的产出或路线；`None` = 该类型没有多路线。"""
    return _BUILD_PRODUCTS.get((plot_type, target_level))


def _check_build_product(order: BuildOrder, idx: int) -> list[PlanIssue]:
    """农业 / 工业**建造时必须选定产物或路线**，且必须是真实存在的那几条。

    > 「建造动作里有"选产出/路线"这一步 → 农业/工业建造时必须选定产物/路线，
    >   建后不可更换」（✅ 2026-10-16 用户拍板）

    两条检查（**不选** / **选错** 分开报，玩家要修的地方不一样）：

        · 留空 → `build_product_required`（程序不替玩家挑默认值：选错要拆掉重建）
        · 不在真值表里 → `bad_build_product`（如农业写 "Z"、工业二级写 "G"）
    """
    ptype = order.plot_type
    allowed = _allowed_build_products(ptype, order.target_level)
    if not allowed:
        # 资源 / 服务 / 研究没有多路线，不需要选
        return []

    where = "农业" if ptype is PlotType.AGRICULTURE else "工业"
    options = list(allowed)

    if not order.product:
        return [
            PlanIssue(
                code="build_product_required",
                target=order.plot_id,
                message=(
                    f"第 {idx + 1} 条建设指令：{where} {order.target_level} 级"
                    f"**必须选定产出/路线**，不能留空。\n"
                    f"    可选：{options}\n"
                    f"    ⚠️ 产出/路线**建造时选定、建后不可更换**（§5.1，"
                    f"2026-10-16 拍板）—— 选错要拆掉重建，所以程序不替你猜默认值。"
                ),
            )
        ]

    if order.product not in allowed:
        return [
            PlanIssue(
                code="bad_build_product",
                target=order.plot_id,
                message=(
                    f"第 {idx + 1} 条建设指令：{where} {order.target_level} 级"
                    f"没有 {order.product!r} 这条产出/路线。\n"
                    f"    可选：{options}\n"
                    f"    ⚠️ 产出/路线**建造时选定、建后不可更换**（§5.1）；"
                    f"写错的产物结算时不会被纠正，只会在建完之后一直产错东西。"
                ),
            )
        ]

    return []


def _check_contracts(plan: Plan, cfg: Config) -> list[PlanIssue]:
    """合同通道（§7.2，玩家对玩家 A 方案）：**形状校验**。

    2026-10-16 用户裁定（A 方案）：**双方各自提交、互相确认才生效**。
    本函数只做**单份计划自己**能判的事（§11.3 五要素）：

      ① 数量 ≤ 0      → `contract_qty_not_positive`
      ② 单价 < 0      → `contract_price_negative`
      ③ 商品不在市场表 → `unknown_goods`（与采购共用同一码）
      ④ 对手方为空    → `contract_no_counterparty`
      ⑤ 对手方是自己  → `contract_self_deal`（自合同不构成交易，§7.2）

    ⚠️ **"单方提交（对手方没跟上）"在这里查不到** —— validate 只收**一份**计划，
       看不到全局对账。这条校验在 `pipeline._settle_contracts` 的失败路径里做：
       **待裁定**（2026-10-16）：**多人局**单方提交**按整季报错**（§11.3「不产生
       部分状态变更」），不是挂起——容忍挂起等于让脏声明随存档滚进下一季继续扰
       动对账；正确修法是**让对家在他自己的 Plan 里补一条镜像**。**单人局**例外
       放行（不落账、不报错）：A 方案下合同**机制上不可达**（永远没有第二个玩家），
       不是"计划有问题"。以后要改（比如引入"挂起等待一整个季度"），**改 pipeline
       不动 validate**。

    ⚠️ **本函数只读 `plan`**、不改也不访问 `GameState`（与其他 `_check_*` 一致）。
    """
    if not plan.contracts:
        return []
    issues: list[PlanIssue] = []
    known = set(cfg.market)

    for idx, act in enumerate(plan.contracts):
        target = act.goods or f"合同 #{idx + 1}"
        if act.goods not in known:
            issues.append(PlanIssue(
                code="unknown_goods",
                target=act.goods,
                message=(
                    f"合同 #{idx + 1} 的商品 {act.goods!r} 不在市场表，不能作为合同标的。\n"
                    f"    市场里的商品：{sorted(known)}"
                ),
            ))
        if act.qty <= 0:
            issues.append(PlanIssue(
                code="contract_qty_not_positive",
                target=target,
                message=(
                    f"合同 #{idx + 1} 的数量必须为正（当前 {act.qty:g}）。\n"
                    f"    ⚠️ 想取消合同：双方协商解除（§7.2），不是报 0。"
                ),
                requested=act.qty,
                available=0.0,
            ))
        if act.price < 0:
            issues.append(PlanIssue(
                code="contract_price_negative",
                target=target,
                message=(
                    f"合同 #{idx + 1} 的单价不能为负（当前 {act.price:g}）。\n"
                    f"    ⚠️ 合同价由双方协商；想表达「对方倒贴」请另立补偿条款，"
                    f"不是负价。"
                ),
                requested=act.price,
                available=0.0,
            ))
        if not act.counterparty:
            issues.append(PlanIssue(
                code="contract_no_counterparty",
                target=f"合同 #{idx + 1}",
                message=(
                    f"合同 #{idx + 1} 缺对手方（counterparty 为空）。\n"
                    f"    ⚠️ A 方案的合同是**玩家对玩家**，没有「外部对手方」"
                    f"这个选项——counterparty 必须填另一个玩家 ID。"
                ),
            ))
        elif act.counterparty == plan.player_id:
            issues.append(PlanIssue(
                code="contract_self_deal",
                target=plan.player_id,
                message=(
                    f"合同 #{idx + 1} 的对手方是自己（{plan.player_id!r}）。\n"
                    f"    ⚠️ 自己跟自己成交不构成合同（§7.2 玩家对玩家）。"
                ),
            ))

    return issues


def _p0(cfg: Config, goods: str) -> Optional[float]:
    """取参考价；商品不在市场表里返回 `None`（**不编造价格**）。"""
    curve = cfg.market.get(goods)
    return None if curve is None else curve.P0


def _p0_value(cfg: Config, goods: str, qty: float) -> Optional[float]:
    """按参考价折一份货值；查不到参考价返回 `None`。"""
    p0 = _p0(cfg, goods)
    return None if p0 is None else p0 * qty


def _estimate_build_cash(plan: Plan, player: PlayerState, cfg: Config) -> float:
    """本季**建造项**的粗估 = 折后现金费 + 工程物资按 P0 折算。

    ★ 2026-10-18 新增。它直接复用 `construction.build_cash_plan()`
      （与结算第 5 步**同一个函数**），所以开局减免名额、折后价、
      名额按 `plan.builds` 顺序消耗这些细节**不会在两处算出两个数**。

    ⚠️ 物资按 **P0** 估（结算时按上季价买，本模块拿不到历史价）。

    ⚠️ 查不到工程（`ConfigError` / 商品缺价 `KeyError`）时**整段跳过**，
       绝不从这里抛 —— 那是 `_check_builds`（`build_work_unavailable`）
       该报的问题。这里再抛一次只会把"计划有问题"变成 500。
       代价是：坏掉的那条会让建造项估成 0（少报），但那份计划本来就会被拒。
    """
    if not plan.builds:
        return 0.0

    try:
        rows = construction.build_cash_plan(plan, player, cfg)
    except (ConfigError, KeyError):
        # 有一条查不到工程 → 整份建造报价不可信 → 跳过建造项（见 docstring）
        return 0.0

    total = 0.0
    for row in rows:
        total += float(row["cash"])                    # 折后实付现金费
        for goods, qty in (row["work"].materials or {}).items():
            value = _p0_value(cfg, goods, float(qty))
            if value is not None:
                total += value
    return total


def _estimate_food_cash(plan: Plan, cfg: Config) -> float:
    """本季**口粮**的粗估：`plan.food` 按 P0 全额折价。

    ⚠️ 只估**申报量的全额**，不扣"自产顶上"那部分（本模块算不出产量：
       `E` 要看邻接/幸福/磨合）。所以这一项**偏保守**（可能高估），
       宁可多提醒一次"可能不够"，也不要漏掉一笔自动买入。
    ⚠️ 口粮缺口是**自动买**的（用户原话："会在购入环节购入"），
       所以它必须算进现金压力里，否则又是一个"结算才爆"的口子。
    """
    total = 0.0
    for goods, qty in plan.food.items():
        if qty <= 0:
            continue                                   # 负数由 _check_food 报
        value = _p0_value(cfg, goods, float(qty))
        if value is not None:
            total += value
    return total


def _check_cash(
    plan: Plan, player: PlayerState, cfg: Config
) -> list[PlanIssue]:
    """资金**粗校验** —— 提前告诉玩家钱可能不够。

    ⚠️ 这是**粗**校验，故意的。它用**参考价 `P0`** 估算，因为：

      · 买入按**上季结算价**记账，而本模块拿不到"上季实际结算价"
        （那是 `GameState.world.price_history`，本模块只收 `PlayerState`）；
      · 真正的账单要到第 4.5 步算完 `B_t` 才知道。

    估算口径（★ 2026-10-18 起不再只算采购）：

        采购  = Σ `plan.purchases` × P0
        建造  = Σ `plan.builds` 的**折后现金费** + 工程物资 × P0
                （见 `_estimate_build_cash`，与第 5 步同一个 `build_cash_plan`）
        口粮  = Σ `plan.food` × P0（口粮缺口是自动买的，见 `_estimate_food_cash`）

    ⚠️ 这三项之外**仍然没算**工资与经营费（要等第 3、4 步），
       所以真实账单只会更贵。这里的价值只有一个：**早说**。
       **第 5 步的资金检查才是最终判据**（§11.3 不允许部分执行）。

    ⚠️ **不要**为了"消除重复"而删掉第 5 步的检查。
       本模块拿不到本季真实价，只能估；第 5 步才是权威。
    """
    estimate_purchases = 0.0
    for goods, qty in plan.purchases.items():
        if qty <= 0:
            continue                                   # 负数由 _check_purchases 报
        value = _p0_value(cfg, goods, float(qty))
        if value is not None:
            estimate_purchases += value

    estimate_builds = _estimate_build_cash(plan, player, cfg)
    estimate_food = _estimate_food_cash(plan, cfg)

    estimate = estimate_purchases + estimate_builds + estimate_food

    # 没有任何可估的支出 → 放行（工资/经营费要等第 3、4 步才知道，估不了）。
    if estimate <= 0.0 or estimate <= player.cash + 1e-9:
        return []

    return [
        PlanIssue(
            code="insufficient_cash_estimate",
            target=player.player_id,
            message=(
                f"现金可能不够：按参考价 P0 粗估，本季至少要约 {estimate:.2f}"
                f"（采购 {estimate_purchases:.2f} + 建造 {estimate_builds:.2f} + "
                f"口粮 {estimate_food:.2f}），手上只有 {player.cash:.2f}。\n"
                f"    ⚠️ 建造 = 工程现金费（已含开局减免）+ 工程物资按 P0 折算；"
                f"口粮按申报量全额估（自产可能顶掉一部分，这一项偏保守）。\n"
                f"    ⚠️ 这还**没算**工资与经营费，实际只会更贵。\n"
                f"    ⚠️ 这是**按参考价 P0 的粗估**（买入实际按上季价记账），"
                f"最终以结算第 5 步为准。"
            ),
            requested=estimate,
            available=player.cash,
        )
    ]


# ══════════════════════════════════════════════════════════════════
# 辅助：给 UI 用的"这块地本季能干什么"
# ══════════════════════════════════════════════════════════════════


def buildable_products(plot_type: PlotType, target_level: int) -> list[str]:
    """某类型 / 某等级在建造时**可选**的产出或路线。

    给前端建造面板填下拉框用（**前端零计算**，所以选项在引擎给）。
    没有多路线的类型返回空列表。

    ⚠️ 本函数是**前端下拉框契约**，不等于校验真值：
       工业二级的「研究」线（`product="研究"` → `ind_1_2_research`，6 季）
       规则上合法、`_check_build_product` 也放行它，但**这里不列**——
       前端下拉框要不要放它，得先问 UI（`/api/build_options` 直接吃这个列表）。
       校验以 `_BUILD_PRODUCTS` 为准，见那里的注释。
    """
    if plot_type is PlotType.AGRICULTURE:
        return ["A", "B", "C", "D"]
    if plot_type is PlotType.INDUSTRY:
        if target_level == 1:
            return ["G"]
        if target_level == 2:
            return ["H-A", "H-F", "H-G", "H-J"]
        return ["H-A+", "H-F+", "H-G+", "H-J+", "I"]
    return []
