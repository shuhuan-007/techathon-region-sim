"""起草编译器 —— 把模型回的 Plan 形状 JSON **规整成一份真能跑的计划**。

依据《规则基线》§11.4：AI 可解释 / 可试算 / 可起草 → 玩家确认 → 提交；
**AI 无 commit**。本模块只产出 `Plan` 数据，不提交、不落盘、不改状态。

═══════════════════════════════════════════════════════════════════
为什么需要这一层（2026-10-18）
═══════════════════════════════════════════════════════════════════

真跑复现（save 1.json，qwen-flash，draft=True）：

    · 模型把玩家**已有的** `4,5` / `5,4` / `6,3` 写成 `builds`（新建一级）；
    · 模型**完全没写** `plot_plans`（按引擎语义 = 5 块地全部不开工）；
    · 引擎校验回 3 条 `build_not_an_upgrade`，`suggested_plan = null`。

另测「10 条工业新建」：`validate` / `submit` 都放行，点结算才报「资金不足」。

这两类都不是「模型不会写 JSON」，而是**模型输出没有被规整**：
已有地块当建造、漏写开工、超预算、产物写错、坐标越界 —— 全是可修的。

本模块的口径：**模型给的是“意图”，引擎给的是“事实”**。
编译器拿事实去修意图，修完必须同时过：

    ① `engine.validate.validate_plan`（合法性）
    ② `engine.pipeline.preview`（真跑一遍，资金/结算不炸）

两条都过才返回 `plan`；否则返回 `plan=None` + issues，绝不把坏计划交给前端。

═══════════════════════════════════════════════════════════════════
修复顺序（确定性，不猜）
═══════════════════════════════════════════════════════════════════

    1. 规整字段（类型/产物/坐标/数字）
    2. 语义归位：已有地块的 builds → 本季开工；在建 → 丢弃；
       空地 → 保留，并把**零邻接加成**的新建搬到推荐邻格（`auto_place`）
    3. 补默认：所有 RUNNING 地块默认 u=1；没给口粮 → 按基准档补
       （★ 2026-10-18 R2：口粮的两档需求一律按**完工后人口**算，见
        `population_after_completion()` —— 结算第 1 步先完工、第 4 步才算人口）
    4. validate；不过 → 丢掉「能定位的坏项」再验（最多 3 轮）
    5. preview；资金不足 → 先降 u（100→75→50→25→0），
       再逐条移除建造，再降口粮，最后移除采购
    6. 全过 → 返回 plan + notes（逐条说明引擎改了什么）

⚠️ **只读**：本模块不修改 `state` / `player`，也不 import 任何 commit 入口。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ai.plan_context import (
    adjacency_bonus_for_new_plot,
    format_neighbors,
    recommend_build_cells,
)
from engine import construction as C
from engine import pipeline, validate
from engine.loader import Config
from engine.models import (
    BuildOrder,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    parse_plot_id,
)
from engine.population import PopulationResult, compute_population

# ══════════════════════════════════════════════════════════════════
# 常量：可选路线 / 默认值 / 修复阶梯
# ══════════════════════════════════════════════════════════════════

#: 农业可选产出
AGRI_PRODUCTS: tuple[str, ...] = ("A", "B", "C", "D")

#: 工业各等级可选路线。
#: ⚠️ 二级「研究」是引擎里的特殊哨兵（`construction.find_work_for_build`
#:    用 `product not in MACHINE_GOODS` 判研究线），不是商品代码。
INDUSTRY_PRODUCTS: dict[int, tuple[str, ...]] = {
    1: ("G",),
    2: ("H-A", "H-F", "H-G", "H-J", "研究"),
    3: ("H-A+", "H-F+", "H-G+", "H-J+", "I"),
}

#: 类型中英文归一
_TYPE_ALIASES: dict[str, PlotType] = {
    "agriculture": PlotType.AGRICULTURE,
    "agri": PlotType.AGRICULTURE,
    "farm": PlotType.AGRICULTURE,
    "农业": PlotType.AGRICULTURE,
    "农": PlotType.AGRICULTURE,
    "resource": PlotType.RESOURCE,
    "res": PlotType.RESOURCE,
    "资源": PlotType.RESOURCE,
    "industry": PlotType.INDUSTRY,
    "ind": PlotType.INDUSTRY,
    "工业": PlotType.INDUSTRY,
    "service": PlotType.SERVICE,
    "srv": PlotType.SERVICE,
    "服务": PlotType.SERVICE,
    "research": PlotType.RESEARCH,
    "研究": PlotType.RESEARCH,
}

#: 开工率阶梯（从高到低；0 = 全停）
_U_LADDER: tuple[float, ...] = (1.0, 0.75, 0.5, 0.25, 0.0)

#: 口粮缩减阶梯
#  2026-10-18 AI-food-default-excellent: `_shrink_until_ok` 现金不足时，
#  沿本阶梯把 food 从（默认的）优秀档往下乘系数，直到试算跑得通——
#  1.0 = 全额（默认优秀档全额）、0.75/0.5/0.25 依次缩量、0.0 = 不买口粮。
_FOOD_LADDER: tuple[float, ...] = (1.0, 0.75, 0.5, 0.25, 0.0)

#: 口粮「算不算差一截」的浮点容差（2026-10-18 第九轮）。
#:
#: 第八轮实测的 `plan_notes` 噪声：
#:
#:   · `口粮 B=35.77 超过优秀档需求 35.77` —— 相等（差 0.00）却报「超过」；
#:   · `按优秀档补口粮：A×0.00、C×0.00` —— 补了 0 也发一条。
#:
#: 这两条都不影响合法性，但玩家读起来像报错。口径：**差值 < 0.005 当作相等**
#: （口粮单位是「份」，0.005 份没有任何实际意义），既不改 Plan 的数量，
#: 也不发 note。
FOOD_FLOAT_TOL: float = 0.005


class DraftCompileError(ValueError):
    """编译器自身无法继续（输入形状完全不对）。"""


# ══════════════════════════════════════════════════════════════════
# 小工具：归一化
# ══════════════════════════════════════════════════════════════════


def _num(v: Any) -> Optional[float]:
    """把模型给的“数字”转成 float；转不了返回 None（不猜）。

    支持：int/float/bool、字符串数字、百分数（"50%" → 0.5）。
    """
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("％", "%").replace("，", "").replace(",", "")
        if not s:
            return None
        if s.endswith("%"):
            try:
                return float(s[:-1]) / 100.0
            except ValueError:
                return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _norm_type(v: Any) -> Optional[PlotType]:
    """中英文类型名 → `PlotType`；认不出返回 None。"""
    if isinstance(v, PlotType):
        return v
    if not isinstance(v, str):
        return None
    key = v.strip().lower().replace(" ", "").replace("_", "")
    # 先试原样（中文/英文别名表）
    for alias, ptype in _TYPE_ALIASES.items():
        if key == alias.lower().replace(" ", "").replace("_", ""):
            return ptype
    return None


def _norm_product(v: Any, ptype: PlotType, level: int) -> Optional[str]:
    """产物/路线大写化 + 全角连字符归一；认不出返回 None。"""
    if v is None:
        return None
    if not isinstance(v, str):
        return None
    s = (
        str(v).strip().upper()
        .replace("－", "-")
        .replace("—", "-")
        .replace("–", "-")
        .replace(" ", "")
    )
    if not s:
        return None
    # 研究线的中文哨兵
    if s in ("研究", "研究线", "RESEARCH", "R&D", "RD"):
        return "研究" if ptype is PlotType.INDUSTRY and level == 2 else None
    return s


def _allowed_products(ptype: PlotType, level: int) -> tuple[str, ...]:
    """某类型/等级在引擎里**真实可选**的产物/路线。"""
    if ptype is PlotType.AGRICULTURE:
        return AGRI_PRODUCTS
    if ptype is PlotType.INDUSTRY:
        return INDUSTRY_PRODUCTS.get(int(level), ())
    return ()


def _default_product(
    ptype: PlotType,
    level: int,
    *,
    existing: Optional[Any] = None,
) -> Optional[str]:
    """模型没给/给错时的兜底产物。

    - 农业 → A
    - 工业 1 级 → G
    - 工业 2 级 → H-A（机械线；研究线必须模型显式写「研究」）
    - 工业 3 级 → 继承当前路线：机械 H-*+，研究 I
    """
    if ptype is PlotType.AGRICULTURE:
        return "A"
    if ptype is not PlotType.INDUSTRY:
        return None
    if level == 1:
        return "G"
    if level == 2:
        return "H-A"
    if level == 3:
        cur = getattr(existing, "product", None) if existing is not None else None
        if cur in C.MACHINE_GOODS:
            return _MACHINE_TO_AUTOMATED.get(cur, "H-A+")
        return "I"
    return None


#: 工业机械线 2 级 → 3 级的唯一产物映射（建后不可更换路线）
_MACHINE_TO_AUTOMATED: dict[str, str] = {
    "H-A": "H-A+",
    "H-F": "H-F+",
    "H-G": "H-G+",
    "H-J": "H-J+",
}


def _expected_upgrade_product(
    existing: Any,
    ptype: PlotType,
    target_level: int,
) -> Optional[str]:
    """已有地块升级时，**规则上唯一允许**的产物；`None` = 模型可自由选。

    “产出/路线：建造时选定，建后不可更换”（《规则基线》§5.1）：

      · 农业升级 → 必须继续种原来的作物（A 不能升一级就变 B）；
      · 工业 1→2 → 这是**第一次选路线**，模型可以选机械 H-* 或研究线；
      · 工业 2→3 → 必须沿当前路线：机械 H-A → H-A+，研究线 → I。
    """
    if ptype is PlotType.AGRICULTURE:
        return getattr(existing, "product", None)

    if ptype is PlotType.INDUSTRY and target_level == 3:
        cur = getattr(existing, "product", None)
        if cur in C.MACHINE_GOODS:
            return _MACHINE_TO_AUTOMATED.get(cur)
        # 研究线（`product=None` / 「研究」）→ 三级只能产 I
        return "I"

    return None



def _norm_goods(v: Any, cfg: Config) -> Optional[str]:
    """商品代码归一（大小写/空格）；不在市场表返回 None。"""
    if not isinstance(v, str):
        return None
    s = v.strip().upper().replace(" ", "").replace("－", "-")
    if s in cfg.market:
        return s
    # 少数模型会写中文名（如“主粮”）；按 goods_name 反向找
    for code, name in cfg.goods_name.items():
        if isinstance(name, str) and s == name.strip().upper():
            return code
    return None


def _is_running(plot) -> bool:
    return getattr(plot, "status", None) is PlotStatus.RUNNING


def _other_player_owns(state: GameState, player_id: str, plot_id: str) -> bool:
    """这块地是不是**别的玩家**的（多人局防抢地）。"""
    for pid, other in state.players.items():
        if pid == player_id:
            continue
        if plot_id in other.plots:
            return True
    return False


def _append_note(notes: list[str], text: str) -> None:
    """去重追加（同一类校正只留一条）。"""
    if text and text not in notes:
        notes.append(text)


def _join_ids(ids: Iterable[str], limit: int = 6) -> str:
    ids = list(ids)
    if not ids:
        return "（无）"
    shown = "、".join(ids[:limit])
    if len(ids) > limit:
        shown += f" 等 {len(ids)} 个"
    return shown


# ══════════════════════════════════════════════════════════════════
# 结果形状
# ══════════════════════════════════════════════════════════════════


@dataclass
class DraftCompileResult:
    """编译结果。

    · `ok=True`  → `plan` 非空，且已过 validate + preview；
    · `ok=False` → `plan=None`，`issues` / `settle_error` 说明为什么。
    """

    ok: bool
    plan: Optional[Plan] = None
    issues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    preview: Optional[dict] = None
    settle_error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "issues": list(self.issues),
            "notes": list(self.notes),
            "preview": dict(self.preview) if self.preview else None,
            "settle_error": self.settle_error,
        }


# ══════════════════════════════════════════════════════════════════
# 输入规整
# ══════════════════════════════════════════════════════════════════


def _iter_builds(raw: Any) -> list[Any]:
    """`builds` 允许 list，也容忍以 plot_id 为键的 dict。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        out = []
        for pid, item in raw.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("plot_id", pid)
                out.append(item)
        return out
    return []


def _normalize_builds(
    raw_builds: Any,
    state: GameState,
    player_id: str,
    cfg: Config,
    notes: list[str],
    *,
    auto_place: bool = True,
) -> tuple[list[BuildOrder], dict[str, float]]:
    """把模型给的 `builds` 规整成 `BuildOrder` 列表 + “应改为开工”的地块。

    返回 `(orders, run_u)`：

      · `orders` —— 只保留真正的新建/升级；
      · `run_u`  —— 模型写在 builds 里、但其实指向“自己已建成同级地块”的，
                   语义改成“本季开工”，这里给出 `{plot_id: 1.0}`。

    `auto_place=True`（默认）时，**空地上的新建**若落在零邻接加成的格子上，
    会按 `recommend_build_cells()`（引擎真实规则）换到推荐格，并留下 note。
    已有加成的不动；升级不搬；推荐格与同计划其它 build 冲突时取下一个，
    都冲突就保持原格。

    ⚠️ 这是本模块最重要的一段：真实模型 100% 会把已有地块写进 builds。
    """
    player = state.player(player_id)
    orders: list[BuildOrder] = []
    run_u: dict[str, float] = {}
    seen: set[str] = set()
    converted: list[str] = []
    dropped: list[str] = []

    # 同计划里**别的**新建原格：自动选址时不去抢，免得两份 build 撞在一起
    # （只收「当前不是自己的地块」的坐标 = 空地新建的意图；升级不会进这里）。
    reserved_new: set[str] = set()
    for item in _iter_builds(raw_builds):
        if not isinstance(item, dict):
            continue
        raw_pid = item.get("plot_id", item.get("id", item.get("plot")))
        if isinstance(raw_pid, str) and raw_pid.strip():
            candidate = raw_pid.strip()
            if candidate not in player.plots:
                reserved_new.add(candidate)

    for idx, item in enumerate(_iter_builds(raw_builds)):
        if not isinstance(item, dict):
            dropped.append(f"第{idx + 1}条(非对象)")
            continue

        pid_raw = item.get("plot_id", item.get("id", item.get("plot")))
        if not isinstance(pid_raw, str) or not pid_raw.strip():
            dropped.append(f"第{idx + 1}条(无坐标)")
            continue
        pid = pid_raw.strip()

        try:
            r, c = parse_plot_id(pid)
        except ValueError:
            dropped.append(f"{pid}(坐标格式)")
            continue
        if not (0 <= r < cfg.rows and 0 <= c < cfg.cols):
            dropped.append(f"{pid}(越界)")
            continue
        if _other_player_owns(state, player_id, pid):
            dropped.append(f"{pid}(别人的地)")
            continue

        raw_type = item.get("plot_type", item.get("type"))
        existing = player.plots.get(pid)
        # 模型对「已有地块」常常省略 plot_type（它以为只要 id 就够）；
        # 这时用**这块地自己的类型**补，别因为缺一个字段把整条意图丢掉。
        ptype = _norm_type(raw_type) or (
            existing.plot_type if existing is not None else None
        )
        if ptype is None:
            dropped.append(f"{pid}(类型 {raw_type!r})")
            continue

        level_raw = _num(item.get("target_level", item.get("level", 1)))
        target_level = int(level_raw) if level_raw is not None else 1
        if target_level < 1:
            target_level = 1
        if target_level > 3:
            target_level = 3

        if existing is not None:
            if existing.status is not PlotStatus.RUNNING:
                dropped.append(f"{pid}(在建)")
                continue

            # ── 已有、且目标等级 ≤ 当前等级 → 这不是建造，是“想让它开工” ──
            if target_level <= existing.level:
                run_u[pid] = 1.0
                converted.append(pid)
                if item.get("product") not in (None, "", existing.product):
                    _append_note(
                        notes,
                        f"地块 {pid} 已建成，产物/路线建后不可更换；"
                        f"模型写的 {item.get('product')!r} 已忽略。",
                    )
                continue

            # ── 升级：类型不能变；目标等级只能 +1 ──
            if existing.plot_type is not ptype:
                dropped.append(f"{pid}(类型不符)")
                continue
            if target_level > existing.level + 1:
                _append_note(
                    notes,
                    f"地块 {pid} 当前 {existing.level} 级，升级一次只能到 "
                    f"{existing.level + 1} 级；模型写的 {target_level} 级已压回。",
                )
                target_level = existing.level + 1

            product = _norm_product(item.get("product"), ptype, target_level)
            if product not in _allowed_products(ptype, target_level):
                fallback = _default_product(ptype, target_level, existing=existing)
                if product is not None:
                    _append_note(
                        notes,
                        f"地块 {pid} 的路线 {product!r} 不在 {ptype.value} "
                        f"{target_level} 级可选范围；已改为 {fallback!r}。",
                    )
                product = fallback

            # ★ 建后不可更换（§5.1）：农业升级必须继续种原作物；
            #   工业 2→3 必须沿当前路线（机械 H-* → H-*+，研究线 → I）。
            #   模型经常在这里“顺手换个产物”，引擎的 start_work 会照单全收，
            #   等于把一条规则悄悄绕过去 —— 编译器必须先归位。
            expected = _expected_upgrade_product(existing, ptype, target_level)
            if expected is not None and product != expected:
                _append_note(
                    notes,
                    f"地块 {pid} 当前产出 {existing.product!r}，升级后只能继续 "
                    f"{expected!r}（建后不可更换）；模型写的 {product!r} 已改为 "
                    f"{expected!r}。",
                )
                product = expected

            if pid in seen:
                dropped.append(f"{pid}(重复)")
                continue
            seen.add(pid)
            orders.append(BuildOrder(
                plot_id=pid, plot_type=ptype,
                target_level=target_level, product=product,
            ))
            continue

        # ── 空地：只能新建 1 级（工程表只有 0→1）──
        if target_level != 1:
            _announce_level_clamp(notes, pid, target_level)
            target_level = 1
        product = _norm_product(item.get("product"), ptype, target_level)
        if ptype in (PlotType.AGRICULTURE, PlotType.INDUSTRY):
            if product not in _allowed_products(ptype, target_level):
                fallback = _default_product(ptype, target_level)
                if product is not None:
                    _append_note(
                        notes,
                        f"地块 {pid} 的产物 {product!r} 不在 {ptype.value} "
                        f"{target_level} 级可选范围；已改为 {fallback!r}。",
                    )
                product = fallback
        else:
            product = None

        # ★ 自动选址（2026-10-18）：零邻接加成的空地新建 → 换到推荐格。
        #   已有加成的不动；升级走上面的分支，永远不会搬。
        if auto_place:
            pid = _relocate_for_adjacency(
                state, player_id, cfg, pid, ptype,
                blocked=seen, reserved=reserved_new, notes=notes,
            )

        if pid in seen:
            dropped.append(f"{pid}(重复)")
            continue
        seen.add(pid)
        orders.append(BuildOrder(
            plot_id=pid, plot_type=ptype,
            target_level=target_level, product=product,
        ))

    if converted:
        _append_note(
            notes,
            f"{len(converted)} 条建造指向你**已有**的地块（{_join_ids(converted)}），"
            f"已改为「本季开工」——建后不可更换，已有地块不能再建一次。",
        )
    if dropped:
        _append_note(notes, f"丢弃 {len(dropped)} 条无效建造：{_join_ids(dropped)}。")
    return orders, run_u


def _relocate_for_adjacency(
    state: GameState,
    player_id: str,
    cfg: Config,
    plot_id: str,
    plot_type: PlotType,
    *,
    blocked: set[str],
    reserved: set[str],
    notes: list[str],
) -> str:
    """空地新建若**零邻接加成**，按引擎规则换到推荐格；不划算就原样返回。

    规则（2026-10-18，用户反馈 B）：

      · 模型已经选了**有加成**的格子 → 不动（它的判断可能比排序更细）；
      · 零加成 → 从 `recommend_build_cells(plot_type=...)` 里取第一个
        **对该类型真有加成**、且不与同计划其它 build 冲突的格子；
      · 推荐格都冲突 / 没有推荐 → 保持原格（不硬搬）；
      · 只对「空地新建」调用 —— 升级不搬，已有地块的 `plot_plans` 不碰。

    ⚠️ 加成与邻居明细都来自 `ai/plan_context`（其上游是引擎的
       `compute_adjacency`），本函数不自己算公式。
    """
    bonus, _neighbors = adjacency_bonus_for_new_plot(
        state, player_id, cfg, plot_id, plot_type,
    )
    if bonus > 1e-12:
        return plot_id               # 模型已经选了有加成的格子 —— 不动

    # 同计划里已排上的、以及别的 build 自己看中的原格，都不要抢
    avoid = set(blocked) | (set(reserved) - {plot_id})
    for cell in recommend_build_cells(
        state, player_id, cfg, limit=5, plot_type=plot_type,
    ):
        target = cell["plot_id"]
        if target == plot_id or target in avoid:
            continue
        _append_note(
            notes,
            f"为吃邻接加成，把 {plot_id} 的建造改到 {target}"
            f"（邻 {format_neighbors(cell.get('neighbors') or [])}）。",
        )
        return target
    return plot_id


def _announce_level_clamp(notes: list[str], pid: str, target_level: int) -> None:
    _append_note(
        notes,
        f"空地 {pid} 新建只能建 1 级（工程表只有 0→1）；"
        f"模型写的 {target_level} 级已压回 1 级。",
    )


def _normalize_plot_plans(
    raw: Any,
    player: PlayerState,
    notes: list[str],
    ignore_missing: frozenset[str] = frozenset(),
) -> dict[str, tuple[bool, float]]:
    """`plot_plans` → `{plot_id: (active, u)}`；未知/未运行的地块丢弃。

    `ignore_missing` 是本季要新建的格子 —— 模型常把新地块也写进 plot_plans，
    它们本期本来就不能开工；**静默忽略，不要报「丢弃」噪音**。
    """
    out: dict[str, tuple[bool, float]] = {}
    dropped: list[str] = []

    items: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(None, x) for x in raw]

    for key, item in items:
        if not isinstance(item, dict):
            dropped.append(str(key))
            continue
        pid = item.get("plot_id", key)
        if not isinstance(pid, str) or not pid.strip():
            dropped.append(str(key))
            continue
        pid = pid.strip()

        plot = player.plots.get(pid)
        if plot is None:
            if pid in ignore_missing:
                continue          # 本季新建的格子，不能开工；静默忽略
            dropped.append(f"{pid}(不存在)")
            continue
        if not _is_running(plot):
            dropped.append(f"{pid}(在建)")
            continue

        u = _num(item.get("u", item.get("rate", item.get("开工率", 1.0))))
        if u is None:
            u = 1.0
        active = item.get("active", True)
        active = bool(active) if isinstance(active, bool) else bool(_num(active))
        u = max(0.0, min(1.0, u))
        if not active:
            u = 0.0
        out[pid] = (u > 0.0, u)

    if dropped:
        _append_note(notes, f"丢弃 {len(dropped)} 条无效开工安排：{_join_ids(dropped)}。")
    return out


def _normalize_goods_map(
    raw: Any, cfg: Config, *, edible_only: bool, notes: list[str], label: str,
) -> dict[str, float]:
    """`purchases` / `food` 归一：商品必须存在（口粮还须可吃），数量 > 0。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    dropped: list[str] = []
    edible = set(cfg.food.baseline_quota) | set(cfg.food.good_quota)

    for goods, qty_raw in raw.items():
        code = _norm_goods(goods, cfg)
        if code is None:
            dropped.append(str(goods))
            continue
        if edible_only and code not in edible:
            dropped.append(f"{code}(不能吃)")
            continue
        qty = _num(qty_raw)
        if qty is None or qty <= 0:
            dropped.append(f"{code}(数量 {qty_raw!r})")
            continue
        out[code] = out.get(code, 0.0) + qty

    if dropped:
        _append_note(notes, f"丢弃 {len(dropped)} 项无效{label}：{_join_ids(dropped)}。")
    return out


# ══════════════════════════════════════════════════════════════════
# ★ 2026-10-18 R2「完工后人口」—— 编译侧与结算侧的唯一人口口径
# ══════════════════════════════════════════════════════════════════
#
# 结算的第 1 步是**先完工**（`pipeline._advance_construction`：在建地块
# `construction_left -= 1`，减到 0 就 `status→RUNNING`、`level→target_level`），
# 第 4 步才 `compute_population()` —— 所以结算里的 L/N/F 全是**完工后**的。
#
# 而编译发生在**季初**：`construction_left == 1` 的地块还写着 CONSTRUCTING，
# 直接调 `compute_population()` 会把它们的岗位漏掉 → 口粮买少 → H 掉档。
# 实测 `saves-live/8550.json`（4 块本季完工）：应填 A=69.83，只填 66.41，H≈0.94。


def population_after_completion(
    state: GameState,
    player_id: str,
    cfg: Config,
) -> PopulationResult:
    """本季**结算第 4 步**会看到的人口（= 第 1 步完工之后的人口）。

    做法：**不另写一套人口公式** —— 只把「本季会完工」的地块按引擎
    `_advance_construction` 的**同一语义**投影成完工态，再原样调用
    `compute_population()`。所以它与第 1 步之后结算看到的数逐位相同。

    哪些算「本季会完工」（与引擎第 1 步逐行对应）：

        CONSTRUCTING 且 `construction_left <= 1`
            → 第 1 步先 `left -= 1`，减到 `<= 0` 就完工（含 `left == 0` 的边界）；
              完工后 `status = RUNNING`、`level = target_level`。

    ⚠️ **当季新建的地块不算完工**：新建由第 6 步 `_invest_construction`
       才创建（季初根本不在 `player.plots` 里），所以这里天然看不到它们。
    ⚠️ 纯函数：只用 `copy.copy` 拼一个**读视图**，不改 `state` / `player` / `plot`。
    ⚠️ 为什么必须复用 `compute_population()`：`recipe_for()` 会按**等级**查配方
       （研究地块、工业三级的 d 都随等级变），自己乘一遍必然对不上。

    `8550` 实测：完工前 N=550 / F_优秀=166.03 → 完工后 N=575 / F_优秀=174.57
    （4 块：`3,4` `3,5` `4,3` `5,6`，都是 `left==1`）。
    """
    player = state.players[player_id]

    view_plots: dict[str, Plot] = {}
    completing = 0
    for pid, plot in player.plots.items():
        will_complete = (
            plot.status is PlotStatus.CONSTRUCTING
            and plot.construction_left <= 1
        )
        if not will_complete:
            view_plots[pid] = plot
            continue
        projected = copy.copy(plot)
        projected.status = PlotStatus.RUNNING
        projected.level = plot.target_level
        projected.construction_left = 0
        projected.running_quarters = 0     # 与引擎一致：磨合从完工本季起算
        view_plots[pid] = projected
        completing += 1

    if completing == 0:
        # 没有本季完工的地块 → 与结算完全同一条路径（省掉视图拷贝）
        return compute_population(state, player_id, {}, cfg)

    view_player = copy.copy(player)
    view_player.plots = view_plots
    view_state = copy.copy(state)
    view_state.players = dict(state.players)
    view_state.players[player_id] = view_player
    return compute_population(view_state, player_id, {}, cfg)


def _baseline_food(state: GameState, player_id: str, cfg: Config) -> dict[str, float]:
    """按“基准档”算一份口粮需求（人口只跟已建成地块走，与 u 无关）。

    ★ 2026-10-18 R2：人口走 `population_after_completion()` —— 结算先完工再算
      人口，本季 `construction_left == 1` 的地块本季就要吃饭。
    """
    pop = population_after_completion(state, player_id, cfg)
    if pop.baseline_food <= 0:
        return {}
    quotas = cfg.food.baseline_quota
    return {g: ratio * pop.baseline_food for g, ratio in quotas.items() if ratio > 0}


def _fill_baseline_food(
    food: dict[str, float],
    state: GameState,
    player_id: str,
    cfg: Config,
    notes: list[str],
) -> dict[str, float]:
    """把模型**漏掉的基准口粮**补齐（A、B 至少都要有）。

    2026-10-18 真跑：模型只回 `food: {"A": 9.2}`，缺 B → 居民只吃到 A，
    基准档永远无法达标。这里只补**基准档缺口**，不强行把 C/D 加到优秀档
    （优秀档是玩家的成本选择；模型可以自己填，漏了不罚）。
    """
    base = _baseline_food(state, player_id, cfg)
    if not base:
        return food
    added: dict[str, float] = {}
    for g, qty in base.items():
        current = float(food.get(g, 0.0) or 0.0)
        if current + 1e-9 < qty:
            food[g] = round(qty, 6)
            added[g] = round(qty - current, 6)
    # ★ 第九轮：只报**真有缺口**的项（< 0.005 视为相等，见 FOOD_FLOAT_TOL）。
    added = {g: q for g, q in added.items() if q >= FOOD_FLOAT_TOL}
    if added:
        parts = "、".join(f"{g}×{q:.2f}" for g, q in sorted(added.items()))
        _append_note(notes, f"口粮缺基准档，已补：{parts}。")
    return food


def _fill_good_food(
    food: dict[str, float],
    state: GameState,
    player_id: str,
    cfg: Config,
    notes: list[str],
) -> dict[str, float]:
    """按**优秀档**把 A/B/C/D 四样口粮补齐（只补不足，不削已有）。

    2026-10-18 AI-food-default-excellent：根因修复——AI 助手不会把口粮
    补到优秀档的根因是「默认不触发」。本函数由
    `compile_draft(include_excellent_food=True)`（默认 True）在
    `_fill_baseline_food` 之后调用：按 `good_quota × F_优秀`
    （compute_population 的 good_food）把四样口粮补到优秀档需求。
    玩家明说「只要基准档/吃饱就行」时调用方才传 False 跳过本步。

    ★ 2026-10-18 R2：`F_优秀` 走 `population_after_completion()`（完工后人口），
      否则本季完工地块的胃口没算进去，买够了也掉到基准档（H=0.6）。

    ⚠️ 只补不足，不削已有；现金不够由后续 `_shrink_until_ok` 缩量，
    本函数不特殊处理。
    """
    pop = population_after_completion(state, player_id, cfg)
    if pop.good_food <= 0:
        return food
    quotas = cfg.food.good_quota or {}
    if not quotas:
        return food
    added: dict[str, float] = {}
    for g, quota in quotas.items():
        want = float(quota) * pop.good_food
        if want <= 0:
            continue
        current = float(food.get(g, 0.0) or 0.0)
        if current + 1e-9 < want:
            food[g] = round(want, 6)
            added[g] = round(want - current, 6)
    # ★ 第九轮：补 0（差 < 0.005）不发 note —— 第八轮实测的
    #   `按优秀档补口粮：A×0.00、C×0.00` 就是这条噪声。数量照补（几乎为 0），
    #   只是不再把它当成「引擎改动」播报给玩家。
    added = {g: q for g, q in added.items() if q >= FOOD_FLOAT_TOL}
    if added:
        parts = "、".join(f"{g}×{q:.2f}" for g, q in sorted(added.items()))
        _append_note(notes, f"按优秀档补口粮：{parts}。")
    return food


def _cap_food_to_need(
    food: dict[str, float],
    state: GameState,
    player_id: str,
    cfg: Config,
    notes: list[str],
) -> dict[str, float]:
    """把口粮压到“优秀档都吃不完”的上限。

    为什么必须做（2026-10-18 真跑抓到的）：

        save 1.json 人口 22，模型回了 `food: {"A": 22}` —— 它把“22 人”
        当成了“22 单位主粮”。按 A 的上季价 10 算，这一项就是 220 现金；
        而 22 人优秀档只需要 A×2.64。多买的没有仓库可放，**结算第 7.7 步
        直接丢弃**，等于白花钱。

    上限 = `good_quota[g] × F_优秀`（超过优秀档的部分不会产生任何幸福度，
    也没有仓库可留到下一季）。这不是“改规则”，只是不让 AI 把“人数”
    误当“口粮数”后白烧钱。

    ★ 2026-10-18 R2：上限同样按**完工后人口**（`F_优秀` 用
      `population_after_completion()`）—— 不然本季完工地块会把合法口粮
      误判成“超量”削掉。
    """
    if not food:
        return {}
    pop = population_after_completion(state, player_id, cfg)
    if pop.good_food <= 0:
        _append_note(
            notes,
            "当前没有需要吃饭的人口（或 F_优秀=0），模型给的口粮已全部清空；"
            "多买的口粮没有仓库可放，会被丢弃。",
        )
        return {}

    out: dict[str, float] = {}
    for g, qty in food.items():
        quota = float(cfg.food.good_quota.get(g, 0.0) or 0.0)
        cap = quota * pop.good_food
        if cap <= 0:
            continue
        # ★ 第九轮：差值 < FOOD_FLOAT_TOL 当相等 —— 不报「超过」、也不发 note。
        #   第八轮实测的 `口粮 B=35.77 超过优秀档需求 35.77`（差 0.00）就是这条。
        if qty > cap:
            if qty - cap >= FOOD_FLOAT_TOL:
                _append_note(
                    notes,
                    f"口粮 {g}={qty:.2f} 超过优秀档需求 {cap:.2f}；"
                    f"多买的部分没有仓库、结算会丢弃，已压回 {cap:.2f}。",
                )
            qty = cap
        out[g] = qty
    return out



# ══════════════════════════════════════════════════════════════════
# Plan 构造 / 检查 / 修复
# ══════════════════════════════════════════════════════════════════


def _make_plan(
    player_id: str,
    run_map: dict[str, tuple[bool, float]],
    purchases: dict[str, float],
    food: dict[str, float],
    orders: list[BuildOrder],
) -> Plan:
    return Plan(
        player_id=player_id,
        plot_plans={
            pid: PlotPlan(plot_id=pid, active=active, u=u)
            for pid, (active, u) in sorted(run_map.items())
        },
        purchases=dict(purchases),
        food=dict(food),
        builds=list(orders),
    )


def _issues_dict(issues) -> list[dict]:
    return [i.to_dict() if hasattr(i, "to_dict") else dict(i) for i in issues]


def _check(
    plan: Plan,
    state: GameState,
    player_id: str,
    cfg: Config,
) -> tuple[list[dict], Optional[dict], str]:
    """validate + preview。返回 `(issues, preview, settle_error)`。

    ⚠️ validate 不过就**不试算**（与 `report.build_state` 同口径）：
       非法计划拿去 preview 可能抛不可预测的 KeyError，错误点离犯错点太远。
    """
    player = state.player(player_id)
    try:
        issues = validate.validate_plan(plan, player, cfg, player_id)
    except Exception as e:  # noqa: BLE001 —— 校验器自己炸了，如实返回
        return [], None, f"校验器异常（{type(e).__name__}: {e}）"
    if issues:
        return _issues_dict(issues), None, ""

    try:
        rep = pipeline.preview(state, {player_id: plan}, cfg)
        return [], rep.to_dict(), ""
    except Exception as e:  # noqa: BLE001 —— 结算失败要如实报，不编
        return [], None, f"{type(e).__name__}: {e}"


def _drop_issue_targets(
    plan: Plan,
    issues: list[dict],
    notes: list[str],
) -> Plan:
    """按 issue 的 target 丢掉能定位的坏项（最多 3 轮由调用方控制）。"""
    new = copy.deepcopy(plan)
    drop_plots: set[str] = set()
    drop_builds: set[str] = set()
    drop_purchases: set[str] = set()
    drop_food: set[str] = set()

    plot_codes = {
        "unknown_plot", "plot_not_running", "u_out_of_range",
        "recipe_unavailable",
    }
    build_codes = {
        "cannot_build_level_directly", "plot_already_constructing",
        "build_not_an_upgrade", "build_type_mismatch", "bad_target_level",
        "build_product_required", "bad_build_product",
        "build_plot_out_of_bounds", "duplicate_build_plot",
        "build_work_unavailable",
    }
    purchase_codes = {"unknown_goods", "negative_purchase"}
    food_codes = {"inedible_food", "negative_food"}

    for it in issues:
        code = it.get("code")
        target = it.get("target")
        if not isinstance(target, str):
            continue
        if code in plot_codes:
            drop_plots.add(target)
        elif code in build_codes:
            drop_builds.add(target)
        elif code in purchase_codes:
            drop_purchases.add(target)
        elif code in food_codes:
            drop_food.add(target)

    for pid in drop_plots:
        new.plot_plans.pop(pid, None)
    if drop_builds:
        new.builds = [b for b in new.builds if b.plot_id not in drop_builds]
    for g in drop_purchases:
        new.purchases.pop(g, None)
    for g in drop_food:
        new.food.pop(g, None)

    changed = drop_plots or drop_builds or drop_purchases or drop_food
    if changed:
        bits = []
        if drop_plots:
            bits.append(f"开工 {_join_ids(drop_plots)}")
        if drop_builds:
            bits.append(f"建造 {_join_ids(drop_builds)}")
        if drop_purchases:
            bits.append(f"采购 {_join_ids(drop_purchases)}")
        if drop_food:
            bits.append(f"口粮 {_join_ids(drop_food)}")
        _append_note(notes, "校验不过，已丢掉：" + "；".join(bits) + "。")
    return new


def _cash_issue_only(issues: list[dict]) -> bool:
    return bool(issues) and all(
        it.get("code") == "insufficient_cash_estimate" for it in issues
    )


def _shrink_until_ok(
    plan: Plan,
    state: GameState,
    player_id: str,
    cfg: Config,
    notes: list[str],
    protected_build_ids: Optional[set[str]] = None,
) -> tuple[Optional[Plan], Optional[dict], str]:
    """资金不足时的确定性缩量。

    修复顺序（2026-10-18 调整，按“先动最不伤产出的东西”排）：

        ① **降口粮**（口粮多买的部分本来也会被丢弃）；
        ② **降开工率**（保留建造，先保地块）；
        ③ **逐条移除建造**（从最后一条开始）；
        ④ **移除采购**（生产原料由引擎按配方自动买，额外采购最不必要）。

    ⚠️ 为什么不先降 u：真跑实测里模型把“22 人”写成 `food: {"A": 22}`，
       旧顺序会把 5 块地全部停掉、却保留 220 现金的口粮 —— 计划合法但完全不成立。
       口粮上限已在 `_cap_food_to_need()` 压过；这里再从“原申报量”往下试，
       确保先动口粮、而不是先牺牲生产。

    返回 `(plan, preview, error)`；成功时 `plan` 非空。
    """
    base_u = {pid: pp.u for pid, pp in plan.plot_plans.items()}
    base_food = dict(plan.food)

    def _try(candidate: Plan) -> tuple[Optional[Plan], Optional[dict], str]:
        issues, preview, err = _check(candidate, state, player_id, cfg)
        if not issues and not err:
            return candidate, preview, ""
        if _cash_issue_only(issues) or (err and ("资金" in err or "不足" in err)):
            return None, None, err or "资金不足"
        return None, None, err or "; ".join(i.get("message", "") for i in issues)

    def _food_at(factor: float) -> dict[str, float]:
        return {
            g: round(q * factor, 6)
            for g, q in base_food.items() if q * factor > 1e-9
        }

    def _apply(candidate: Plan, u_factor: float, food_factor: float) -> None:
        for pp in candidate.plot_plans.values():
            pp.u = round(base_u.get(pp.plot_id, 0.0) * u_factor, 6)
            pp.active = pp.u > 0.0
        candidate.food = _food_at(food_factor)

    # ── 零档：已有生产 + 新建，现金不足时**先撤建造、保生产** ──
    #   真跑实测：save 1 现金 416，模型在已有 5 块运行地块上又建 1 块农业；
    #   旧顺序把 u 降到 0 才跑通 —— 计划合法，但“5 块地全停、只留一个工地”，
    #   等于为了新投资把现有产出全牺牲掉。用户要的是经营，不是停工。
    #   所以：只要计划里还有 u>0 的已建成地块，就先**撤掉新建造**，再在不带
    #   建造的底子上找“最高可承受的开工率/口粮”；找到就返回。
    if plan.builds and any(pp.u > 0.0 for pp in plan.plot_plans.values()):
        # ★ 升级工程（build 指向已有地块）不能按“新建造”撤掉：
        #   玩家/AI 要的是把 3,4 升成研究，不是把 3,4 拆掉。
        existing_ids = set(state.player(player_id).plots)
        protected = set(protected_build_ids or ()) | existing_ids
        removable = [b for b in plan.builds if b.plot_id not in protected]
        if removable:
            no_build = copy.deepcopy(plan)
            no_build.builds = [b for b in no_build.builds if b.plot_id in protected]
            removed = [b.plot_id for b in removable]
            for u_factor in _U_LADDER:
                for food_factor in _FOOD_LADDER:
                    candidate = copy.deepcopy(no_build)
                    _apply(candidate, u_factor, food_factor)
                    got, preview, _err = _try(candidate)
                    if got is not None:
                        bits = [
                            f"为保住现有生产，移除了 {len(removed)} 条新建造"
                            f"（{_join_ids(removed)}）"
                        ]
                        if u_factor < 1.0:
                            bits.append(
                                f"开工率降到 {int(round(u_factor * 100))}%"
                            )
                        if food_factor < 1.0:
                            bits.append(
                                f"口粮降到原申报的 {int(round(food_factor * 100))}%"
                            )
                        _append_note(
                            notes,
                            "现金不足：" + "、".join(bits) + " 后，试算才跑得通。",
                        )
                        return got, preview, ""

    # ── 一、二档：u × 口粮 的确定性网格 ──
    # u 从 1 往下、口粮从 1 往下；第一个跑通的组合 = 保留最多生产/口粮的方案。
    # (1.0, 1.0) 是原计划，已经在调用方试过，跳过。
    for u_factor in _U_LADDER:
        for food_factor in _FOOD_LADDER:
            if u_factor == 1.0 and food_factor == 1.0:
                continue
            candidate = copy.deepcopy(plan)
            _apply(candidate, u_factor, food_factor)
            got, preview, _err = _try(candidate)
            if got is not None:
                bits: list[str] = []
                if food_factor < 1.0:
                    bits.append(
                        f"口粮降到原申报的 {int(round(food_factor * 100))}%"
                    )
                if u_factor < 1.0:
                    bits.append(
                        f"开工率整体降到 {int(round(u_factor * 100))}%"
                    )
                _append_note(
                    notes,
                    "现金不足：" + "、".join(bits) + " 后，试算才跑得通。",
                )
                return got, preview, ""

    # ── 三档：逐条移除建造（此时 u=0、口粮=0，只保建造的现金也不够）──
    candidate = copy.deepcopy(plan)
    _apply(candidate, 0.0, 0.0)
    protected = set(protected_build_ids or ())
    removed: list[str] = []
    while True:
        removable = [b for b in candidate.builds if b.plot_id not in protected]
        if not removable:
            break
        b = removable[-1]
        candidate.builds.remove(b)
        removed.append(b.plot_id)
        got, preview, _err = _try(candidate)
        if got is not None:
            _append_note(
                notes,
                f"现金不足：在全部停工、不买口粮的底子上，又移除 "
                f"{len(removed)} 条建造（{_join_ids(removed)}）后，试算才跑得通。",
            )
            return got, preview, ""

    # ── 四档：移除采购（生产原料由引擎按配方自动买，额外采购最不必要）──
    candidate = copy.deepcopy(plan)
    _apply(candidate, 0.0, 0.0)
    candidate.builds = [b for b in candidate.builds if b.plot_id in protected]
    if candidate.purchases:
        n = len(candidate.purchases)
        candidate.purchases = {}
        got, preview, _err = _try(candidate)
        if got is not None:
            _append_note(
                notes,
                f"现金不足：移除了全部 {n} 项额外采购"
                f"（生产原料仍由引擎按配方自动买）后，试算才跑得通。",
            )
            return got, preview, ""

    return None, None, "连空计划都跑不通（这是引擎/配置问题，不是模型的问题）"


# ══════════════════════════════════════════════════════════════════
# 公开入口
# ══════════════════════════════════════════════════════════════════


def compile_draft(
    raw: Any,
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    default_run_all: bool = True,
    default_baseline_food: bool = True,
    max_validate_rounds: int = 3,
    auto_place: bool = True,
    protected_build_ids: Optional[set[str]] = None,
    # 2026-10-18 AI-food-default-excellent: 默认 True —— 口径改为
    # 「默认按优秀档补口粮，让玩家拿 H=1.0」；仅玩家明说「只要基准档/
    # 吃饱就行」时调用方才传 False（见 server.ai_host._wants_baseline_only）。
    include_excellent_food: bool = True,
) -> DraftCompileResult:
    """把模型回的 Plan 形状 JSON 编译成一份**真能跑**的 Plan。

    参数
    ----
    raw       : `parse_model_json()` 取出的对象（通常是 dict）
    state     : 当前局面（**只读**；本函数不改它一个字）
    player_id : 给谁起草；会钉死 `plan.player_id`
    cfg       : 静态配置
    default_run_all        : 模型没提的 RUNNING 地块，是否默认 u=1
    default_baseline_food  : 模型没给口粮时，是否按基准档补一份
    max_validate_rounds    : validate 不过时，按 issue target 丢坏项的最多轮数
    auto_place             : 空地新建若零邻接加成，是否自动换到推荐格
                             （默认开；关掉 = 完全照模型给的坐标）
    include_excellent_food : 默认 True（2026-10-18 改默认——AI 助手不会把
                             口粮补到优秀档的根因）。在基准档补齐之后，按
                             `good_quota × F_优秀` 把 A/B/C/D 补到**优秀档**
                             （只补不足，不削已有；现金不够由后续
                             `_shrink_until_ok` 缩量）。仅玩家明说「只要
                             基准档/吃饱就行」时调用方才传 False。

    返回 `DraftCompileResult`；`ok=True` 时保证：

        · `result.plan` 过 `validate_plan`；
        · `result.plan` 过 `pipeline.preview`（资金/结算不炸）。

    ⚠️ 本函数**不提交、不落盘、不改状态**（§11.4：AI 无 commit）。
    """
    notes: list[str] = []

    if not isinstance(raw, dict):
        return DraftCompileResult(
            ok=False,
            issues=[{
                "code": "draft_not_object", "target": "", "message":
                f"模型回的不是 JSON 对象（{type(raw).__name__}），无法编译计划。",
                "requested": None, "available": None, "shortfall": None,
            }],
            notes=notes,
        )

    try:
        player = state.player(player_id)
    except Exception as e:  # noqa: BLE001
        return DraftCompileResult(
            ok=False,
            issues=[{
                "code": "no_player_state", "target": player_id, "message":
                f"拿不到玩家状态（{type(e).__name__}: {e}），无法编译。",
                "requested": None, "available": None, "shortfall": None,
            }],
            notes=notes,
        )

    # ── 1. 建造（含“已有地块当建造”的语义归位 / 空地自动选址）──
    orders, converted_run = _normalize_builds(
        raw.get("builds"), state, player_id, cfg, notes,
        auto_place=auto_place,
    )

    # ── 2. 开工安排 ──
    new_ids = frozenset(o.plot_id for o in orders if o.plot_id not in player.plots)
    run_map = _normalize_plot_plans(
        raw.get("plot_plans"), player, notes, ignore_missing=new_ids,
    )
    # 新地块本季在“在建”，模型写进来的 plot_plans 不生效；给一条简短说明，
    # 不再逐条报“丢弃 N 条无效开工安排”。
    if new_ids:
        pp_raw = raw.get("plot_plans")
        if isinstance(pp_raw, dict):
            pp_keys = [str(k) for k in pp_raw]
        elif isinstance(pp_raw, list):
            pp_keys = [
                str(x.get("plot_id")) for x in pp_raw if isinstance(x, dict)
            ]
        else:
            pp_keys = []
        ignored = [pid for pid in pp_keys if pid in new_ids]
        if ignored:
            _append_note(
                notes,
                f"新地块本季在建，plot_plans 里的开工已忽略：{_join_ids(ignored)}。",
            )
    for pid, u in converted_run.items():
        run_map.setdefault(pid, (True, u))

    # ── 3. 默认全开工（模型常常整块不写 plot_plans）──
    if default_run_all:
        missing = [
            pid for pid, plot in sorted(player.plots.items())
            if _is_running(plot) and pid not in run_map
        ]
        for pid in missing:
            run_map[pid] = (True, 1.0)
        if missing:
            _append_note(
                notes,
                f"模型没提 {len(missing)} 块已建成地块的开工（漏写=不开工）；"
                f"已按默认全部开工：{_join_ids(missing)}。",
            )

    # ── 4. 采购 / 口粮 ──
    purchases = _normalize_goods_map(
        raw.get("purchases"), cfg, edible_only=False,
        notes=notes, label="采购",
    )
    food = _normalize_goods_map(
        raw.get("food"), cfg, edible_only=True,
        notes=notes, label="口粮",
    )
    # ★ 先把“人数当口粮数”的常见误解压回（多买的没有仓库，结算会丢弃）
    food = _cap_food_to_need(food, state, player_id, cfg, notes)
    # ★ 模型经常只给 A、忘了 B；基准档必须 A+B 同时有，否则永远吃不饱。
    if default_baseline_food:
        food = _fill_baseline_food(food, state, player_id, cfg, notes)
    # ★ 2026-10-18 AI-food-default-excellent：默认 True —— 「默认按优秀档
    #   补四样口粮 A/B/C/D」写进编译器。只补不足、不削已有；现金不够由
    #   后续 `_shrink_until_ok` × `_FOOD_LADDER` 缩量，不做特殊处理。
    #   玩家明说「只要基准档/吃饱就行」时调用方传 False 才会跳过本步。
    if include_excellent_food:
        food = _fill_good_food(food, state, player_id, cfg, notes)

    # ── 5. 模型可能带的合同 / 其它字段：单人局一律丢弃 ──
    if raw.get("contracts"):
        _append_note(
            notes,
            "合同通道是玩家对玩家（A 方案），单人局机制上不可达；已忽略模型写的合同。",
        )

    plan = _make_plan(player_id, run_map, purchases, food, orders)

    # ── 5b. 空计划：区分“模型明确说本季什么都不做”与“给的东西全被丢了” ──
    raw_had_intent = bool(
        raw.get("builds") or raw.get("plot_plans")
        or raw.get("purchases") or raw.get("food")
    )
    plan_is_empty = not (
        plan.builds or plan.plot_plans or plan.purchases or plan.food
    )
    if plan_is_empty and raw_had_intent:
        return DraftCompileResult(
            ok=False,
            issues=[{
                "code": "no_valid_intent",
                "target": player_id,
                "message": (
                    "模型给的动作全部无效（建造/开工/采购/口粮都被丢弃或压成 0），"
                    "编译后是一份空计划；这次不把它当成建议交给前端。"
                ),
                "requested": None, "available": None, "shortfall": None,
            }],
            notes=notes,
        )
    if plan_is_empty and not raw_had_intent:
        _append_note(
            notes,
            "模型没有给出任何具体动作；这是一份空计划（本季什么都不做）。",
        )

    # ── 6. validate + preview ──
    issues, preview, settle_error = _check(plan, state, player_id, cfg)

    # validate 不过 → 按 issue target 丢坏项，最多 max_validate_rounds 轮
    rounds = 0
    while issues and rounds < max_validate_rounds:
        if _cash_issue_only(issues):
            break  # 钱的问题走缩量，不要靠丢东西
        rounds += 1
        repaired = _drop_issue_targets(plan, issues, notes)
        if repaired.to_dict() == plan.to_dict():
            break  # 没有可丢的（issue 定位不到），别空转
        plan = repaired
        issues, preview, settle_error = _check(plan, state, player_id, cfg)

    # 还是钱的问题（或 preview 报资金不足）→ 缩量
    if _cash_issue_only(issues) or (not issues and settle_error and (
        "资金" in settle_error or "不足" in settle_error
    )):
        shrunk, preview2, err2 = _shrink_until_ok(
            plan, state, player_id, cfg, notes,
            protected_build_ids=protected_build_ids,
        )
        if shrunk is not None:
            plan, preview, settle_error = shrunk, preview2, ""
            issues = []          # ★ 缩量成功后，旧的现金 issue 必须清掉
        else:
            settle_error = err2 or settle_error

    # 缩量后可能产生新的 validate 问题（例如空 plan 不会）——再验一次
    if not issues and not settle_error:
        issues, preview, settle_error = _check(plan, state, player_id, cfg)

    if issues:
        return DraftCompileResult(
            ok=False, plan=None, issues=issues, notes=notes,
            preview=None, settle_error="",
        )
    if settle_error:
        return DraftCompileResult(
            ok=False, plan=None,
            issues=[{
                "code": "preview_failed", "target": player_id,
                "message": f"试算跑不通：{settle_error}",
                "requested": None, "available": None, "shortfall": None,
            }],
            notes=notes, preview=None, settle_error=settle_error,
        )

    # ── 7. H=1.0 兜底检测（只读、不改 Plan）──
    # 2026-10-18 AI-food-H-check：意图是优秀档（include_excellent_food=True）
    # 但 preview 显示 H<1.0，说明 `_fill_good_food` 补的目标量被后续缩量砍掉了
    # （多半是现金不够）。这里**只追加一条 note 暴露问题**，不改 Plan 本身——
    # 玩家/调用方有权知道"想要优秀但没拿到"。
    if include_excellent_food and preview:
        food_info = preview.get("food") or {}
        h = food_info.get("happiness")
        if h is not None and float(h) < 1.0 - 1e-9:
            # ★ 2026-10-18 R2：缺口必须按**完工后人口**算——preview 的 H 就是
            #   结算口径（先完工再算人口），按完工前人口算会把缺口算成 0，
            #   于是「H<1.0 却不加 note」，玩家永远不知道差在哪。
            pop = population_after_completion(state, player_id, cfg)
            quotas = cfg.food.good_quota or {}
            gaps: list[str] = []
            for g, quota in sorted(quotas.items()):
                want = float(quota) * pop.good_food
                if want <= 0:
                    continue
                current = float(plan.food.get(g, 0.0) or 0.0)
                if current + 1e-6 < want:
                    gaps.append(f"{g} 缺 {want - current:.2f}")
            if gaps:
                _append_note(
                    notes,
                    f"按优秀档仍缺 {('、'.join(gaps))}（H={float(h):.2f} < 1.0，"
                    "受现金约束保留低档；想拿 H=1.0 需更多现金或削减建造/采购）。",
                )

    return DraftCompileResult(
        ok=True, plan=plan, issues=[], notes=notes,
        preview=preview, settle_error="",
    )


# ══════════════════════════════════════════════════════════════════
# 给提示词/前端用的“编译器摘要”（不参与判决）
# ══════════════════════════════════════════════════════════════════


def plan_summary(plan: Plan) -> dict:
    """一份计划的可读摘要（给日志/AI 气泡用；数字都来自 plan 本身）。"""
    return {
        "builds": len(plan.builds),
        "plot_plans": len(plan.plot_plans),
        "purchases": len(plan.purchases),
        "food": dict(plan.food),
        "run_plots": sorted(
            pid for pid, pp in plan.plot_plans.items() if pp.effective_u > 0
        ),
    }
