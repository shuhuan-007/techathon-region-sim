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

from engine.loader import Config, ConfigError
from engine.models import (
    BuildOrder,
    Plan,
    PlayerState,
    PlotPlan,
    PlotStatus,
    PlotType,
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
    """建设 / 升级指令的形状校验。

    ⚠️ **这里只查"这条指令本身合不合法"，不查"能不能负担"** ——
       `construction.works`（工程表）**目前是空的**（M4 才录，见 §5.2），
       所以任何建设指令都会在结算时被 `pipeline._construction_materials()`
       明确报错。本函数的作用是**让错误更早、更具体**，而不是替代那一步。
    """
    issues: list[PlanIssue] = []

    for idx, order in enumerate(plan.builds):
        bid = order.plot_id
        plot = player.plots.get(bid)

        # ── ⓪ 空地新建：**只能从 1 级起**（工程表只有 0→1 / 1→2 / 2→3）──
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

        # ── ① 空地新建：plot_id 可以不在 plots 里（还没建）──
        # 但一旦存在，就必须满足状态约束。
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

        # ── ② 目标等级必须在 1–3 ────────────────────────────
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

        # ── ③ ★ 产出/路线：建造时选定，建后不可更换（§5.1）──
        issues += _check_build_product(order, idx)

    return issues


def _check_build_product(order: BuildOrder, idx: int) -> list[PlanIssue]:
    """农业 / 工业**建造时必须选定产物或路线**（✅ 2026-10-16 用户拍板）。

    > 「建造动作里有"选产出/路线"这一步 → 农业/工业建造时必须选定产物/路线，
    >   建后不可更换」

    这是**刻意的强制**：不选就报错，而不是替玩家挑一个默认值 ——
    选错了要拆掉重建，代价太大，不能让程序替他猜。
    """
    ptype = order.plot_type
    if ptype not in (PlotType.AGRICULTURE, PlotType.INDUSTRY):
        # 资源 / 服务 / 研究没有多路线，不需要选
        return []

    if order.product:
        return []

    where = "农业" if ptype is PlotType.AGRICULTURE else "工业"
    if ptype is PlotType.AGRICULTURE:
        options = ["A", "B", "C", "D"]
    else:
        options = (
            ["G"] if order.target_level == 1
            else ["H-A", "H-F", "H-G", "H-J"] if order.target_level == 2
            else ["H-A+", "H-F+", "H-G+", "H-J+", "I"]
        )

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


def _check_cash(
    plan: Plan, player: PlayerState, cfg: Config
) -> list[PlanIssue]:
    """资金**粗校验** —— 提前告诉玩家钱可能不够。

    ⚠️ 这是**粗**校验，故意的。它用**参考价 `P0`** 估算，因为：

      · 买入按**上季结算价**记账，而本模块拿不到"上季实际结算价"
        （那是 `GameState.world.price_history`，本模块只收 `PlayerState`）；
      · 真正的账单要到第 4.5 步算完 `B_t` 才知道。

    所以这里的估算**可能偏低或偏高**。它的价值只有一个：**早说**。
    **第 5 步的资金检查才是最终判据**（§11.3 不允许部分执行）。

    ⚠️ **不要**为了"消除重复"而删掉第 5 步的检查。
    """
    # 无采购 → 工资/经营费要等第 3、4 步算完才知道，这里估不了，放行。
    if not plan.purchases:
        return []

    estimate = 0.0
    for goods, qty in plan.purchases.items():
        if goods in cfg.market and qty > 0:
            estimate += cfg.market[goods].P0 * qty

    if estimate <= player.cash + 1e-9:
        return []

    return [
        PlanIssue(
            code="insufficient_cash_estimate",
            target=player.player_id,
            message=(
                f"现金可能不够：仅采购一项按参考价估算就要 {estimate:.2f}，"
                f"手上只有 {player.cash:.2f}。\n"
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
