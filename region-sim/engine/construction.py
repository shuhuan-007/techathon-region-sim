"""建设与升级 —— 工程表、路线解析、工期。

依据《规则基线》§5.1（物资对应 / 产出路线建后不可更换）与 §5.2（一次性工程数值）。

═══════════════════════════════════════════════════════════════════
本模块只做三件事
═══════════════════════════════════════════════════════════════════

  ① **查表**：`(类型, 当前等级, 目标等级, 路线)` → 一条 `Work`
  ② **解析路线**：工业是唯一要分岔的类型，`product` → `route`
  ③ **建工程**：把一条 `BuildOrder` 落到地块上（改状态的部分在 `pipeline`）

数值全在 `config.json` 的 `construction.works`（**14 行，来自基线 §5.2**），
本模块**不含任何字面量**。

═══════════════════════════════════════════════════════════════════
★ 工业为什么特殊：1→2 与 2→3 各有两条互斥路线
═══════════════════════════════════════════════════════════════════

其它类型是"一条线升到底"，工业不是：

    一级 G ──┬─→ 二级机械 H-*   （2 季，240 现金 + H-G×3）
             └─→ 二级研究线      （6 季，300 现金 + H-G×3）

    二级机械 ──→ 三级 H-*+      （4 季，360 现金 + H-G×3）
    二级研究 ──→ 三级 I         （8 季，500 现金 + H-G×5）

**工期差得很远（2 季 vs 6 季；4 季 vs 8 季）**，所以不能只看等级，
必须知道"这块地二级时走的是哪条线"。

⚠️ **启动循环例外**（§5.1）：建最初的高技术 / 自动化机械生产线，
   **可以用普通 `H-G`，不强制先消耗 `H-G+`** ——
   否则"I 需要 H-G+ 建厂，而 H-G+ 又需要 I 才能生产"是个死循环。
   本模块的工程表已经按例外录入（`ind_2_3_high` 用的是 `H-G×5`，不是 `H-G+`）。

═══════════════════════════════════════════════════════════════════
★ 产出 / 路线：建造时选定，建后不可更换（§5.1）
═══════════════════════════════════════════════════════════════════

    · 农业：A / B / C / D
    · 工业一级：G（只有一条）
    · 工业二级：机械 H-A/H-F/H-G/H-J，或研究线
    · 工业三级：I（从研究线来），或 H-*+（从机械来）
    · 资源 / 服务：没有多路线

建造时**必须选定**，之后 `Plot.product` 只读。想换 = 拆掉重建（第一期不做改建）。
所以 `engine/validate.py::_check_build_product` 会**强制要求**农业/工业填 `product`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.loader import Config, ConfigError
from engine.models import Plot, PlotStatus, PlotType


# ══════════════════════════════════════════════════════════════════
# 工程
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Work:
    """一条工程（基线 §5.2 的一行）。

    ⚠️ `from_level = 0` 表示**新建**（不是"零级地块" —— 那个概念已作废，
       地图上开局就是可建的空地，见 §5.3）。
    """

    work_id: str
    label: str
    plot_type: PlotType
    from_level: int
    to_level: int
    cash: float                       # 现金费（**不含物资**）
    materials: dict[str, float] = field(default_factory=dict)
    quarters: int = 1
    route: Optional[str] = None       # 仅工业：machine / research
    product: Optional[str] = None     # 新建时固定的产出（工业一级 = G）

    @property
    def is_new(self) -> bool:
        """是"新建"而不是"升级"。"""
        return self.from_level == 0


def load_works(cfg: Config) -> dict[str, Work]:
    """把 `config.construction.works` 读成 `Work` 表。

    ⚠️ **缺字段就报错，不糊默认值**（§11.6 数据纪律）。
       工程数值直接决定"能不能建得起"，猜一个数会让对账失去意义。
    """
    raw = (cfg.construction or {}).get("works") or {}
    if not raw:
        raise ConfigError(
            "建设表 construction.works 为空 —— 无法查任何工程（§5.2）。"
        )

    out: dict[str, Work] = {}
    for work_id, row in raw.items():
        if work_id.startswith("_"):
            continue
        try:
            out[work_id] = Work(
                work_id=work_id,
                label=row["label"],
                plot_type=PlotType(row["plot_type"]),
                from_level=int(row["from_level"]),
                to_level=int(row["to_level"]),
                cash=float(row["cash"]),
                materials={g: float(q) for g, q in row.get("materials", {}).items()},
                quarters=int(row["quarters"]),
                route=row.get("route"),
                product=row.get("product"),
            )
        except KeyError as e:
            raise ConfigError(
                f"建设表 {work_id!r} 缺字段 {e.args[0]!r}。\n"
                f"  一行工程必须有：label / plot_type / from_level / to_level / cash / quarters"
            ) from None
    return out


# ══════════════════════════════════════════════════════════════════
# 路线解析（工业专用）
# ══════════════════════════════════════════════════════════════════

#: 二级"机械"路线能产出的商品（H-A / H-F / H-G / H-J）
MACHINE_GOODS = ("H-A", "H-F", "H-G", "H-J")

#: 三级"自动化"路线能产出的商品（H-A+ / H-F+ / H-G+ / H-J+）
AUTOMATED_GOODS = ("H-A+", "H-F+", "H-G+", "H-J+")


def route_of_product(product: Optional[str]) -> Optional[str]:
    """把工业的 `product` 解析成工程路线：`machine` / `research` / `None`。

        产 G                → None      （一级只有一条线，不需要分岔）
        产 H-A/H-F/H-G/H-J  → machine   （二级机械）
        产 H-*+ 或 I        → 见下

    ⚠️ 三级要**看它是从哪条线升上来的** —— 但 `product` 是"目标产出"，
       而升级时 `Plot.product` 还是**当前**的产出。所以这里给三级返回 `None`，
       由 `find_work()` 用"地块当前路线"来判，别在这里猜。
    """
    if product in MACHINE_GOODS:
        return "machine"
    return None


def current_route(plot: Plot) -> Optional[str]:
    """**一块地当前**走的是哪条工业路线。

        产 H-*  → machine    （二级机械）
        产 I    → research   （三级高技术，来源是研究线）
        ⚠️ `Plot` 上没有"这条线是不是研究线"的字段，只能由产出反推。

    之所以要反推而不是加字段：`Plot.product` 是**建造时定死、建后只读**的
    （§5.1），它就是这条线的唯一标识。加一个冗余字段反而会出现两处不一致。
    """
    if plot.plot_type is not PlotType.INDUSTRY:
        return None
    if plot.product in MACHINE_GOODS or plot.product in AUTOMATED_GOODS:
        return "machine"
    return None


# ══════════════════════════════════════════════════════════════════
# 查表
# ══════════════════════════════════════════════════════════════════


def find_work(
    cfg: Config,
    plot_type: PlotType,
    from_level: int,
    to_level: int,
    route: Optional[str] = None,
) -> Work:
    """查出对应的一条工程。查不到 → `ConfigError`（**不猜**）。"""
    works = load_works(cfg)

    matches = [
        w for w in works.values()
        if w.plot_type is plot_type
        and w.from_level == from_level
        and w.to_level == to_level
    ]
    if not matches:
        raise ConfigError(
            f"没有这条工程：{plot_type.value} {from_level}→{to_level}（§5.2）。\n"
            f"  表里该类型有的："
            f"{sorted((w.from_level, w.to_level) for w in works.values() if w.plot_type is plot_type)}"
        )

    # 只有一条 → 直接给（资源/服务/农业都走这里）
    if len(matches) == 1:
        return matches[0]

    # ★ 工业：多条 → 必须靠 route 区分，**不替玩家挑**
    if route is None:
        raise ConfigError(
            f"{plot_type.value} {from_level}→{to_level} 有 {len(matches)} 条互斥路线，"
            f"必须指定走哪条：\n"
            f"  {sorted((w.route, w.label, w.quarters) for w in matches)}\n"
            f"  ⚠️ 两条路线工期差很远（如 2 季 vs 6 季），不能猜。"
        )
    for w in matches:
        if w.route == route:
            return w
    raise ConfigError(
        f"{plot_type.value} {from_level}→{to_level} 没有 {route!r} 这条路线。\n"
        f"  可选：{sorted(w.route for w in matches)}"
    )


def find_work_for_build(cfg: Config, plot: Optional[Plot], order) -> Work:
    """按一条 `BuildOrder` 找出该用哪条工程。

    参数
    ----
    plot  : 目标地块。`None` = **新建**（地上还没有这块地）。
    order : `BuildOrder`（有 `plot_type` / `target_level` / `product`）

    路线怎么定（工业专用）：

      · 新建一级 → 产出必是 G，只有一条线
      · 升级到二级 → 看 `order.product` 是 `H-*`（机械）还是空/研究
      · 升级到三级 → 看**当前**产出（`plot.product`）属于哪条线：
                      机械线（H-*）→ `ind_2_3_machine`
                      研究线        → `ind_2_3_high`
    """
    from_level = plot.level if plot is not None else 0
    target = order.target_level

    route: Optional[str] = None
    if order.plot_type is PlotType.INDUSTRY:
        if from_level == 1 and target == 2:
            # 二级：产出决定了走机械还是研究
            route = "machine" if order.product in MACHINE_GOODS else "research"
        elif from_level == 2 and target == 3:
            # 三级：由**当前**这条线决定（建后不可更换）
            route = current_route(plot) if plot is not None else None
            if route is None:
                # 二级是研究线时 plot.product 是 None（研究不产商品）
                route = "research"

    return find_work(cfg, order.plot_type, from_level, target, route)


# ══════════════════════════════════════════════════════════════════
# 落地：把 BuildOrder 应用到地块上
# ══════════════════════════════════════════════════════════════════


def start_work(plot: Plot, work: Work, product: Optional[str]) -> None:
    """把工程**启动**到地块上（改 `plot` 本身）。

    ⚠️ **只改地块状态，不扣钱、不扣物资** —— 那是流水线第 5、6 步的事。
       本函数是纯状态迁移，方便单独对账。

    新建与升级的差别只在"完工时等级 +0 还是 +1"，
    而 `_advance_construction` 统一按 `target_level` 落地，所以这里不分叉。
    """
    plot.status = PlotStatus.CONSTRUCTING
    plot.construction_left = work.quarters
    plot.target_level = work.to_level

    # ★ 产出 / 路线：建造时选定，建后不可更换（§5.1）
    #   新建时必须定下来；升级时若换了路线也要更新（如二级选机械 H-A）
    chosen = product or work.product
    if chosen:
        plot.product = chosen
    elif work.route == "research":
        # 研究线不产商品 → product 保持 None（服务/研究都是 0→0）
        plot.product = None


def build_steps(cfg: Config, plot: Optional[Plot], order) -> tuple[Work, dict[str, float]]:
    """给一条建设指令算出 `(工程, 本季要投入的物资)`。

    物资**按工程表原样返回**（不进库存、不按效率缩放 —— 工程是一次性投入，
    不像生产原料那样随 `u` 与 `E` 走）。
    """
    work = find_work_for_build(cfg, plot, order)
    return work, dict(work.materials)
