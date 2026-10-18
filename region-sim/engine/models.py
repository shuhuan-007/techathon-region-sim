"""核心数据模型 —— 全部状态对象的定义。

═══════════════════════════════════════════════════════════════════
这是全项目最贵的一个文件，改动会牵动全身
═══════════════════════════════════════════════════════════════════
依据《规则基线》§1.2 骨架与 §1.1「五条写法红线」。

★ 五条红线里，**第一条最贵**：玩家身份永远用 `player_id` 索引。

    ✅ state.players["p1"].cash
    ❌ state.cash

第一期功能上只做单人（一局一个玩家、不联机），但**结构按多人写**。
理由：把玩家状态摊在顶层，以后加联机几乎等于重写；
守住这一条，单→多扩展的全部代价就只有**三处**：

    ① players 字典        ② player_id 参数        ③ advance 收 plans 字典

═══════════════════════════════════════════════════════════════════
共享 vs 私有：一切设计从这里分叉（红线 2）
═══════════════════════════════════════════════════════════════════
    WorldState（共享）   quarter / policies / price_history / market
                         —— 所有玩家看得见、会互相争夺
    PlayerState（私有）  cash / plots / inventory / debts / credit_flag
                         —— 每人一份，挂在 players[player_id] 下

判断一个新字段放哪，问一句：**"两个玩家会抢它吗？"**
会抢（市场价、政策）→ 共享；不会抢（自己的现金、自己的地）→ 私有。

═══════════════════════════════════════════════════════════════════
定下的四个"接口形状"（《规则基线》§10 第 0 步硬阻塞）
═══════════════════════════════════════════════════════════════════
① config schema   → 见 loader.py 与 config/schema（本文件只定义运行时对象）
② 地块 ID / 地图   → PlotId，格式 "r,c"，八邻格枚举见 neighbors()
③ Plan 结构        → 见文件后半段
④ 存档格式         → 每个类都有 to_dict / from_dict

⚠️ 数值一律**不进代码**：经营费 c、劳动 d、产量 e、价格 P0 等
   全部来自 config，由 loader 读入后塞进 Recipe / MarketCurve。
   本文件只定义"形状"，不含任何游戏平衡数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Iterator, Optional

# ══════════════════════════════════════════════════════════════════
# 一、枚举：有限的几种"状态"
# ══════════════════════════════════════════════════════════════════


class PlotType(str, Enum):
    """地块类型。决定走哪套配方、哪套邻接规则。

    ⚠️ 服务（SERVICE）是**唯一**需要特殊分支的类型：
       它 0→0 不产出商品，只**给出去**加成（按服务自身等级
       5%/7.5%/10%，§3.6）；其余类型走关联表 +5%（§3.2），
       相加后**不封顶**（第九轮起删掉 40% 上限）。
    """

    AGRICULTURE = "agriculture"  # 农业：产 A/B/C/D
    RESOURCE = "resource"        # 资源：产 F
    INDUSTRY = "industry"        # 工业：产 G / H-* / I
    SERVICE = "service"          # 服务：0→0，纯基础设施
    RESEARCH = "research"        # 研究：0→0，出效率加成

    @property
    def is_service(self) -> bool:
        """服务地块走独立分支（只给出去、自己不吃邻接，且无商品产出）。"""
        return self is PlotType.SERVICE

    @property
    def produces_goods(self) -> bool:
        """是否会产出商品进全国市场。

        服务与研究都**不产出商品**——服务换邻接加成，研究换效率加成。
        它们的产出不体现在市场，而体现在**别的地块的数字**上。
        """
        return self not in (PlotType.SERVICE, PlotType.RESEARCH)


class PlotStatus(str, Enum):
    """地块状态机（《项目架构-总》§三 的第三条）。

        空地 EMPTY ──建造──> 在建 CONSTRUCTING ──完工──> 运行 RUNNING
                                  ↑                          │
                                  └────────升级──────────────┘

    `CONSTRUCTING` 覆盖"新建"与"升级中"两种情况——两者共用同一套
    工期推进逻辑。区别只在完成后等级 +0 还是 +1。

    ⚠️ 在建/升级中的地块：
       · **不提供**邻接加成（§3.2 排除项）
       · **不提供**研究加成
       · 不进 ḡ（平均等级）的计算（§3.3.2）
    """

    EMPTY = "empty"                # 空地：地图开局就是这个状态
    CONSTRUCTING = "constructing"  # 在建 / 升级中
    RUNNING = "running"            # 正常运行


class SessionPhase(str, Enum):
    """会话状态机（《回合流程与状态机》§一）。

        COLLECTING（收计划）→ RESOLVING（结算）→ REPORT（月报）→ 回到 COLLECTING

    玩家动作**只有一个时点**：提交本季 Plan。
    单人 = 多人的特例：len(plans) == len(players) 提交即成立，无等待逻辑。
    """

    COLLECTING = "collecting"
    RESOLVING = "resolving"
    REPORT = "report"


class WorldPhase(str, Enum):
    """世界状态机：开局 → 进行 → 结束。"""

    SETUP = "setup"        # 开局摆放地块阶段（吃分类减免名额）
    RUNNING = "running"    # 正常进行
    FINISHED = "finished"  # 到达总季数


# ══════════════════════════════════════════════════════════════════
# 二、地图与地块 ID
# ══════════════════════════════════════════════════════════════════

# 地块 ID 采用 "r,c" 坐标制（0 基）。
# 例："0,0" 左上角、"9,9" 右下角（地图 10×10）。
#
# ⚠️ 为什么用 "r,c" 而不是 "x3_y5"：
#    ① 八邻格枚举时，行列各自 ±1 即可，不用做字符串拆解；
#    ② 排序后天然是"从上到下、从左到右"，UI 渲染顺序稳定；
#    ③ 存档可读性够用（规则基线提过的 "x3_y5" 只是举例）。
PLOT_ID_SEP = ","


def make_plot_id(row: int, col: int) -> str:
    """由行列号生成地块 ID。"""
    if row < 0 or col < 0:
        raise ValueError(f"地块坐标不能为负：({row}, {col})")
    return f"{row}{PLOT_ID_SEP}{col}"


def parse_plot_id(plot_id: str) -> tuple[int, int]:
    """地块 ID → (row, col)。格式错误时抛 ValueError。"""
    parts = str(plot_id).split(PLOT_ID_SEP)
    if len(parts) != 2:
        raise ValueError(f"地块 ID 格式应为 'r,c'，收到 {plot_id!r}")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"地块 ID 的行列必须是整数，收到 {plot_id!r}") from None


# 八邻格偏移（含对角）。顺序固定：从左上开始顺时针——
# 顺序固定很重要，保证遍历结果可复现（确定性要求）。
_NEIGHBOR_OFFSETS: tuple[tuple[int, int], ...] = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def neighbors(plot_id: str, rows: int, cols: int) -> list[str]:
    """返回地块的**八邻格** ID 列表（越界的自动剔除）。

    邻接是本项目第一条效率规则（§3.2）的计算基础：
    数出"落在关联表里的邻居个数 n"，`E_邻接 = 1 + 0.05n + Σ服务率`
    （第九轮起不封顶；旧值 `1 + min(0.05n, 0.40)` 已作废）。

    ⚠️ 只认**八邻格**，不是四邻格——对角相邻也算。
       用户已拍板（§11.7-3）：八邻格 + 关联表 + 5%（旧 40% 上限已删）。
    """
    row, col = parse_plot_id(plot_id)
    out: list[str] = []
    for dr, dc in _NEIGHBOR_OFFSETS:
        r, c = row + dr, col + dc
        if 0 <= r < rows and 0 <= c < cols:
            out.append(make_plot_id(r, c))
    return out


# ══════════════════════════════════════════════════════════════════
# 三、市场曲线（共享层）
# ══════════════════════════════════════════════════════════════════


@dataclass
class MarketCurve:
    """一种商品的供需曲线参数（§4.1）。

    定价公式::

        s = Es × Q0 / P0              # 供给斜率
        d = |Ed| × Q0 / P0            # 需求斜率
        P_t = P0 + (B_t − X_t)/(s + d)
        P_t = max(P_t, 0.1 × P0)      # 价格下限 P_min

    其中 `B_t` = 本季从公开市场买入量，`X_t` = 本季准备卖出的量。

    ⚠️ `Es` / `Ed` **允许为 None**（尚未由规则同学填写）。
       这不是"默认 0.5"——交接书里"先全用 0.5"只是**建议**，
       不是裁定。纪律见《规则基线》§11.6：
       **不得为填满配置而编造数据**。
       读到 None 时，`slopes()` 会明确报错，而不是偷偷用个默认值。
    """

    goods: str
    P0: float                 # 参考价
    Q0: float                 # 基准量
    Es: Optional[float] = None   # 供给弹性（15 商品已全值，见《规则基线》§8.5；**不许再用默认值**）
    Ed: Optional[float] = None   # 需求弹性（15 商品已全值；停用同左）
    P_min_ratio: float = 0.1     # 价格下限 = P_min_ratio × P0

    # 外部固定收购量（§8.5 的 30/15/10）。
    # ⚠️ **已全部停用（✅ 2026-10-16 定案）**：用户裁定"暂时没有固定采购"——
    #    `H-*` / `I` / `H+` 的固定收购**一律不生效**，所有商品**只走供需曲线**
    #    S_外(P) / D_外(P)，外部没有兜底买家（《规则基线》§8.5）。
    #    停用理由：固定收购量无条件生效 = **无限量免费买家，可直接套利**。
    #    因此本字段**应保持 0、且不参与任何定价计算**（引擎里除序列化外无引用）；
    #    `config.json` 里不必再写它。待调项：若实测"机械卖不出去"卡玩法，
    #    再加**带上限或带价格条件**的兜底买家 —— 第一期不加。
    external_purchase: float = 0.0

    def slopes(self) -> tuple[float, float]:
        """返回 (s, d)。缺弹性时**明确报错**，不编造默认值。"""
        if self.Es is None or self.Ed is None:
            raise ValueError(
                f"商品 {self.goods!r} 的弹性 Es/Ed 未填 —— 不能计算价格。\n"
                f"  请让规则同学补上（见 docs/04-交接/规则细化交接书.md §五.1）。\n"
                f"  ⚠️ 不要用默认值糊过去：弹性直接决定成交价，"
                f"捏一个值会让后面所有对账失去意义。"
            )
        if self.P0 == 0:
            raise ValueError(f"商品 {self.goods!r} 的 P0 = 0，无法计算斜率")
        s = self.Es * self.Q0 / self.P0
        d = abs(self.Ed) * self.Q0 / self.P0
        return s, d

    @property
    def P_min(self) -> float:
        """价格下限（§4.1）。

        ✅ 2026-10-16 深夜用户裁定：**触底时按 `P_min` 全量收购**
        （"就当是全国统一收购"）——所以 `P_min` **只决定最低成交价**，
        不代表"卖不掉"。旧口径"按可实现需求量分配、不保证全量收购"**已作废**。
        """
        return self.P_min_ratio * self.P0

    def price(self, bought: float = 0.0, sold: float = 0.0) -> float:
        """按供需求本季成交价（§4.1）。

        bought = B_t（本季买入量），sold = X_t（本季卖出量）。
        不触底时用线性解；触底则返回 P_min。
        """
        s, d = self.slopes()
        denom = s + d
        if denom == 0:
            raise ValueError(f"商品 {self.goods!r} 的 s + d = 0，曲线退化为水平线")
        raw = self.P0 + (bought - sold) / denom
        return max(raw, self.P_min)

    def to_dict(self) -> dict:
        return {
            "goods": self.goods, "P0": self.P0, "Q0": self.Q0,
            "Es": self.Es, "Ed": self.Ed,
            "P_min_ratio": self.P_min_ratio,
            "external_purchase": self.external_purchase,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MarketCurve":
        return cls(
            goods=data["goods"], P0=data["P0"], Q0=data["Q0"],
            Es=data.get("Es"), Ed=data.get("Ed"),
            P_min_ratio=data.get("P_min_ratio", 0.1),
            external_purchase=data.get("external_purchase", 0.0),
        )


# ══════════════════════════════════════════════════════════════════
# 四、配方（静态数据，从 config 读入）
# ══════════════════════════════════════════════════════════════════


@dataclass
class Recipe:
    """一条配方 = "基准满负荷一个季度"的投入产出（§2.1）。

    ★ 这句话是**所有数值校准的地基**，必须理解成"整批"：

        不是"生产 1 单位需要多少投入"，
        而是"一季度跑满，产出 e 单位，消耗 input 那么多、花 c 现金、占 d 劳动"。

    所以 `u`（开工比例）与 `E`（效率）改变时：
        产量     = e × u × E          ← 产出随两者变
        商品投入 = 基础投入 × u × E    ← 原料跟着一起变
        工资劳动 = d × u               ← ★ 劳动**不乘 E**（六条铁律之一）
        经营费   = 0.7c + 0.3c×u×E     ← 固定部分 + 变动部分

    ⚠️ 劳动不乘 E 是最容易写错的一条：效率提高意味着"同样的人
       处理更多批次"，而不是"雇更多人"。

    ⚠️ 2026-10-16（用户拍板）：**"劳动"被拆成两个用途，别混**——
       发工资用 `d × u`（在岗但歇工不领薪，本节就是这一条）；
       算岗位/人口/口粮用 `Σd`，**不看 `u`**（地块建成，人就在那儿，要吃饭）。
       见基线 §3.3.1 / §3.4 / §11.9.4。
    """

    plot_type: PlotType
    level: int                      # 1 / 2 / 3
    output: Optional[str]           # 产出商品；服务与研究为 None
    input: dict[str, float] = field(default_factory=dict)   # 商品 → 数量
    c: float = 0.0                  # 经营费（不含采购、不含工资）
    d: float = 0.0                  # 劳动
    e: float = 0.0                  # 产量

    @property
    def key(self) -> tuple[PlotType, int]:
        return (self.plot_type, self.level)

    def to_dict(self) -> dict:
        return {
            "plot_type": self.plot_type.value, "level": self.level,
            "output": self.output, "input": dict(self.input),
            "c": self.c, "d": self.d, "e": self.e,
        }


# ══════════════════════════════════════════════════════════════════
# 五、地块（私有层）
# ══════════════════════════════════════════════════════════════════


@dataclass
class Plot:
    """一块地（§1 私有层）。

    状态机：空地 → 在建 → 运行（见 PlotStatus）。
    磨合（`running_quarters`）从**首次可用**起算，决定 E_磨合：
        第 1 季 0.5、第 2 季 0.75、第 3 季起 1.0（§3.1）
    """

    plot_id: str
    plot_type: PlotType
    level: int = 1
    status: PlotStatus = PlotStatus.RUNNING

    # 磨合计数：正常运行了多少个季度（0 = 刚建好，本季还没跑过）
    running_quarters: int = 0

    # 在建 / 升级的剩余工期（季）。
    # 归零时由 pipeline 把 status 切到 RUNNING、level 生效。
    construction_left: int = 0

    # 升级目标等级（在建时有效）。新建时等于当前 level。
    target_level: int = 1

    # 已投入工程的物资（§11.2 工程材料分账）。
    # 材料一旦投入就**离开普通库存**，因此不会被季末清仓卖掉。
    construction_materials: dict[str, float] = field(default_factory=dict)

    # 具体产什么 / 走哪条路线（交接书 §五.4 待拍板）。
    # 例：工业 1 级产 G；工业 2 级可选 H-A / H-F / H-G / H-J；农业可选 A/B/C/D。
    # ⚠️ 该字段**留空即由 config 的默认配方决定**，等拍板后启用。
    product: Optional[str] = None

    # ★ 2026-10-18 地块显示名（ui/sky/3D 用，不参与引擎计算）。
    #   新建成时按 plot_type（研究地块读 research 列）从 config.plot_names
    #   里**全局不重复**地挑一个填进来；用尽则退化为 "类型-Lxx"。
    #   老存档没这字段 → None，UI 兜底仍显示 plot_id。
    name: Optional[str] = None

    # ── 派生属性 ────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """是否正常运行（可开工、可提供邻接加成）。"""
        return self.status is PlotStatus.RUNNING

    @property
    def is_constructing(self) -> bool:
        return self.status is PlotStatus.CONSTRUCTING

    @property
    def is_research(self) -> bool:
        """这块地是不是「研究地块」。

        研究地块**不产商品**（`Recipe.output is None`），它的收益体现在
        其它地块的效率加成（+1%/块、玩家内封顶 +3%，见 `efficiency.research_bonus`）。

        ⚠️ **最小存储改动**（2026-10-18 用户允许）：研究地块**内部仍用**
           `PlotType.INDUSTRY + level=2 + product='研究'` 编码，**不改存档格式**。
           判定必须同时认两种形态：
             ① `plot_type == PlotType.RESEARCH`（**独立的**研究地块，工程表外）
             ② `plot_type == PlotType.INDUSTRY and level == 2 and product == '研究'`
                （**嵌入**工业的研究线二级，来自工程表 `ind_1_2_research`）
        """
        if self.plot_type is PlotType.RESEARCH:
            return True
        return (
            self.plot_type is PlotType.INDUSTRY
            and self.level == 2
            and self.product == "研究"
        )

    def recipe_key(self) -> tuple[PlotType, int]:
        """本块地当前该用哪条配方。"""
        return (self.plot_type, self.level)

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "plot_type": self.plot_type.value,
            "level": self.level,
            "status": self.status.value,
            "running_quarters": self.running_quarters,
            "construction_left": self.construction_left,
            "target_level": self.target_level,
            "construction_materials": dict(self.construction_materials),
            "product": self.product,
            # ★ 2026-10-18 地块中文名（None / 中文串，UI 兜底 plot_id）
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plot":
        return cls(
            plot_id=data["plot_id"],
            plot_type=PlotType(data["plot_type"]),
            level=data["level"],
            status=PlotStatus(data["status"]),
            running_quarters=data.get("running_quarters", 0),
            construction_left=data.get("construction_left", 0),
            target_level=data.get("target_level", data["level"]),
            construction_materials=dict(data.get("construction_materials", {})),
            product=data.get("product"),
            # ★ 2026-10-18 老存档无 name 键 → data.get 兜底为 None
            name=data.get("name"),
        )


# ══════════════════════════════════════════════════════════════════
# 六、政策与债务（占位，第一期不用）
# ══════════════════════════════════════════════════════════════════


@dataclass
class Policy:
    """政策机（§7.4 八字段）。

    ⚠️ 第一期**空列表**。政策内容（3~5 条）尚未设计
       （交接书 §五.9），这里只锁形状，让存档格式稳定。
    """

    policy_id: str
    policy_type: str            # subsidy / quota / tariff
    effective_quarter: int
    duration: int
    target_goods: str
    target_player: str = "ALL"
    parameter: float = 0.0
    announcement: str = ""

    def is_active(self, quarter: int) -> bool:
        """本季是否生效（含首尾，`duration` 为持续季数）。"""
        return self.effective_quarter <= quarter < self.effective_quarter + self.duration

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id, "policy_type": self.policy_type,
            "effective_quarter": self.effective_quarter, "duration": self.duration,
            "target_goods": self.target_goods, "target_player": self.target_player,
            "parameter": self.parameter, "announcement": self.announcement,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        return cls(**data)


@dataclass
class Debt:
    """玩家间借贷（系统不放贷，§9 已声明不做）。

    ⚠️ 第一期为空。违约流程（§10）只说"形成赔偿义务"，无金额公式，
       故这里只留最小形状。
    """

    creditor: str
    amount: float
    due_quarter: int
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "creditor": self.creditor, "amount": self.amount,
            "due_quarter": self.due_quarter, "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Debt":
        return cls(**data)


# ══════════════════════════════════════════════════════════════════
# 七、共享层状态
# ══════════════════════════════════════════════════════════════════


@dataclass
class WorldState:
    """共享层 —— 所有玩家看得见、会互相争夺的部分（红线 2）。

    包含**世界时钟**、**市场曲线**、**政策**、**历史价格**。
    玩家私有的东西一律不在这里（那样等于把红线 2 踩了）。
    """

    quarter: int = 1
    rows: int = 10                          # 地图行数（已拍板 10×10）
    cols: int = 10                          # 地图列数
    # 一局总季数。✅ 2026-10-16 用户拍板：**20 季**（《规则基线》§11.9.2）。
    # ⚠️ 这里只是 dataclass 的**兜底缺省值**，真实值由 config 的
    #    `world.total_quarters` 提供（`Config.new_world()` 会覆盖它）。
    #    曾长期写着 40（旧交接书建议），与已拍板的 20 脱钩——本轮对齐。
    #    改这个数＝改规则，先改基线 §11.9.2 再改这里。
    total_quarters: int = 20
    phase: WorldPhase = WorldPhase.SETUP

    market: dict[str, MarketCurve] = field(default_factory=dict)
    policies: list[Policy] = field(default_factory=list)

    # 上期价，供**结算时按上季价记账买入**使用。
    # ★ 2026-10-16：玩家阶段只提交 Plan，**不扣钱、不锁价、不动账户**——
    #    采购的**记账与付款都发生在结算时**（与工资、经营费一起一笔扣清）。
    #    六条铁律第 3 条：**买入按上季价、卖出按本季价，但扣款全部集中在结算时**；
    #    "期初采购"是旧口语说法，落点在【用哪个价】，不在【时点】。
    #    见《规则基线》§4.1 交割时序 与 §6 第 5 步"一次性扣款"。
    # 结构：{商品: [逐季价格]}，末位即"上期价"（last_price() 取的就是它）。
    price_history: dict[str, list[float]] = field(default_factory=dict)

    # ★ 政策系统的确定性状态（§11.9.3）：{"seed": int, "mode": str, "seq": [已抽的季]}。
    # ⚠️ **这个字段是 2026-10-16 深夜补的**：政策模块最初没有字段可用，
    #    就把状态寄存在 `price_history["__policy__"]` 里 —— 结果撞上
    #    `to_dict()` 的 `{g: list(v) ...}`（那是为"价格列表"写的），
    #    读档时 dict 被拆成了 ["seed","seq","mode"]，**存档往返直接坏掉**。
    #    **教训：找不到字段就先加字段，别借用语义不同的容器。**
    policy_state: dict = field(default_factory=dict)

    # ── 价格查询 ────────────────────────────────────────

    def last_price(self, goods: str) -> float:
        """上期价。没有历史时退回参考价 P0。

        ⚠️ 开局第一季没有"上期价"，用参考价兜底——
           基线 §8.1 的 M1 初始条件明确写了"上期价格 = 新参考价"。
        """
        hist = self.price_history.get(goods)
        if hist:
            return hist[-1]
        curve = self.market.get(goods)
        if curve is None:
            raise KeyError(f"商品 {goods!r} 既无历史价、也不在市场配置里")
        return curve.P0

    def record_price(self, goods: str, price: float) -> None:
        """把本期成交价记入历史（结转时调用）。"""
        self.price_history.setdefault(goods, []).append(price)

    def active_policies(self) -> list[Policy]:
        """本季生效的政策。"""
        return [p for p in self.policies if p.is_active(self.quarter)]

    def in_bounds(self, plot_id: str) -> bool:
        """坐标是否在地图内。"""
        try:
            r, c = parse_plot_id(plot_id)
        except ValueError:
            return False
        return 0 <= r < self.rows and 0 <= c < self.cols

    def all_plot_ids(self) -> list[str]:
        """地图上全部格子 ID（按行列顺序，确定性）。"""
        return [
            make_plot_id(r, c)
            for r in range(self.rows)
            for c in range(self.cols)
        ]

    def neighbor_ids(self, plot_id: str) -> list[str]:
        """该地块的八邻格（已按地图边界裁剪）。"""
        return neighbors(plot_id, self.rows, self.cols)

    def to_dict(self) -> dict:
        return {
            "quarter": self.quarter, "rows": self.rows, "cols": self.cols,
            "total_quarters": self.total_quarters, "phase": self.phase.value,
            "market": {g: c.to_dict() for g, c in self.market.items()},
            "policies": [p.to_dict() for p in self.policies],
            "price_history": {g: list(v) for g, v in self.price_history.items()},
            "policy_state": dict(self.policy_state),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorldState":
        # ⚠️ 兼容早期存档：那一版把政策状态寄存在 price_history["__policy__"] 里。
        #    这里把它搬进正式字段，并把那个保留键从价格表里摘掉。
        hist = {g: list(v) for g, v in data.get("price_history", {}).items()}
        policy_state = dict(data.get("policy_state") or {})
        legacy = hist.pop("__policy__", None)
        if not policy_state and isinstance(legacy, dict):
            policy_state = dict(legacy)

        return cls(
            quarter=data.get("quarter", 1),
            rows=data.get("rows", 10), cols=data.get("cols", 10),
            total_quarters=data.get("total_quarters", 20),
            phase=WorldPhase(data.get("phase", "setup")),
            market={g: MarketCurve.from_dict(c) for g, c in data.get("market", {}).items()},
            policies=[Policy.from_dict(p) for p in data.get("policies", [])],
            price_history=hist,
            policy_state=policy_state,
        )


# ══════════════════════════════════════════════════════════════════
# 八、私有层状态
# ══════════════════════════════════════════════════════════════════


@dataclass
class PlayerState:
    """单个玩家的私有状态（红线 2）。

    ⚠️ 永远通过 `state.players[player_id]` 访问，**不要**摊到顶层。

    ⚠️ **`inventory` 不是"仓库"**（2026-10-16 用户拍板：本项目**没有仓库**）。
       它**只放本季买来、准备给居民吃的口粮**（A/B/C/D），别的什么都不放：

         · 产出        → 当季**全部**成为市场供给（`X_t`），**不落地**
         · 生产原料    → 生产时**当场消耗**，不进这里
         · 建设物资    → 直接进工程，不进这里
         · 没用完的口粮 → **丢弃**（不产生收入、不结转）

       ⚠️ 所以**不要**拿它当通用存货用。历史上曾有一条"季末未消耗的部分
          会被清仓卖掉（§11.1）"，那条**整节已作废** —— 没有仓库就没有清仓。
    """

    player_id: str
    cash: float = 0.0
    plots: dict[str, Plot] = field(default_factory=dict)
    inventory: dict[str, float] = field(default_factory=dict)
    debts: list[Debt] = field(default_factory=list)
    credit_flag: bool = False

    # 上一季的幸福度 H，供本季算 E_幸福（六条铁律第一条：
    # 必须用**上一季**的 H，否则与"粮食 → H → 效率"形成循环依赖）。
    # 开局 H₀ = 0.6（§3.3.5 已确认）。
    last_happiness: float = 0.6

    # 开局减免名额使用情况：{地块类型: 已用名额数}（§5.3）。
    # ⚠️ 名额数值待定，config 留空；这里只记"用掉了几个"。
    opening_quota_used: dict[str, int] = field(default_factory=dict)

    # ── 地块访问（一律带 player_id 语义：这是"这个人"的地） ──

    @property
    def running_plots(self) -> list[Plot]:
        """正常运行的地块（可开工、可提供邻接加成）。"""
        return [p for p in self.plots.values() if p.is_running]

    def running_plots_of(self, plot_type: PlotType) -> list[Plot]:
        return [p for p in self.plots.values() if p.is_running and p.plot_type is plot_type]

    def add_inventory(self, goods: str, qty: float) -> None:
        """入库。数量为负时不允许（防止偷偷清零）。"""
        if qty < 0:
            raise ValueError(f"入库数量不能为负：{goods} {qty}")
        self.inventory[goods] = self.inventory.get(goods, 0.0) + qty

    def take_inventory(self, goods: str, qty: float) -> float:
        """出库，返回**实际**取到的数量（不足则取光，不欠账）。

        这条"取光不欠账"的语义与粮食结算一致（§3.3.6）：
        扣减时有多少扣多少，不足扣光。
        """
        have = self.inventory.get(goods, 0.0)
        actual = min(have, qty)
        left = have - actual
        if left > 1e-12:
            self.inventory[goods] = left
        else:
            self.inventory.pop(goods, None)
        return actual

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "cash": self.cash,
            "plots": {pid: p.to_dict() for pid, p in self.plots.items()},
            "inventory": dict(self.inventory),
            "debts": [d.to_dict() for d in self.debts],
            "credit_flag": self.credit_flag,
            "last_happiness": self.last_happiness,
            "opening_quota_used": dict(self.opening_quota_used),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerState":
        return cls(
            player_id=data["player_id"],
            cash=data["cash"],
            plots={pid: Plot.from_dict(p) for pid, p in data.get("plots", {}).items()},
            inventory=dict(data.get("inventory", {})),
            debts=[Debt.from_dict(d) for d in data.get("debts", [])],
            credit_flag=data.get("credit_flag", False),
            last_happiness=data.get("last_happiness", 0.6),
            opening_quota_used=dict(data.get("opening_quota_used", {})),
        )


# ══════════════════════════════════════════════════════════════════
# 九、总状态
# ══════════════════════════════════════════════════════════════════


@dataclass
class Contract:
    """★ 合同账本的一条记录（§7.2）—— 挂在 `GameState` 上（不是 PlayerState：
       它天然跨两个玩家，放任何一边都表达不了"谁欠谁"）。

    一条 = 一对玩家在某季度对一种商品达成的**供货约定**，含履约进度：

        contract_id  稳定键（`"{卖家}>{买家}>{商品}>q{季度}"`，由 pipeline 生成）
        seller_id    供货方（交货、收钱）
        buyer_id     受货方（收货、付钱）
        goods        商品
        qty          约定交付总量
        price        协商单价（§7.2：双方协商、一般低于市场价；**不走 P_t**）
        quarter      该交的季度（游戏季度编号）
        delivered    已累计交付量（≤ qty）
        settled_cash 已累计按合同价转的钱（= Σ 交付量×price）
        fulfilled    是否已足额履约（delivered ≥ qty）
        shortfall    未履约量（qty − delivered；§7.3 的"未履约义务"就是它）

    ⚠️ §7.3 的完整流程（受损方接受/协商/上报 → 赔偿 → 信用状态）
       **本次只做到"记录未履约义务"这一步**（即把 shortfall 留在账上）；
       赔偿与信用扣分**尚未实现**，缺口已按基线登记，见 pipeline 注释与
       `待裁定` 标记。
    """

    contract_id: str
    seller_id: str
    buyer_id: str
    goods: str
    qty: float
    price: float
    quarter: int
    delivered: float = 0.0
    settled_cash: float = 0.0
    fulfilled: bool = False

    @property
    def shortfall(self) -> float:
        """未履约义务量（§7.3）。已足额履约时为 0。"""
        return max(0.0, self.qty - self.delivered)

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "seller_id": self.seller_id,
            "buyer_id": self.buyer_id,
            "goods": self.goods,
            "qty": self.qty,
            "price": self.price,
            "quarter": self.quarter,
            "delivered": self.delivered,
            "settled_cash": self.settled_cash,
            "fulfilled": self.fulfilled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Contract":
        return cls(
            contract_id=data["contract_id"],
            seller_id=data["seller_id"],
            buyer_id=data["buyer_id"],
            goods=data["goods"],
            qty=data["qty"],
            price=data["price"],
            quarter=data["quarter"],
            delivered=data.get("delivered", 0.0),
            settled_cash=data.get("settled_cash", 0.0),
            fulfilled=data.get("fulfilled", False),
        )


@dataclass
class GameState:
    """整个游戏状态 = 共享层 + 私有层（红线 2 的分层在这里体现）。

    ⚠️ `players` **永远是字典**，即使第一期只有一个玩家。
       这是单→多扩展三处代价里最贵的一处。
    """

    world: WorldState = field(default_factory=WorldState)
    players: dict[str, PlayerState] = field(default_factory=dict)

    # ★ 合同账本（§7.2）：天然跨两个玩家，所以挂在 GameState 而不是
    #   PlayerState。键 = `Contract.contract_id`。
    #   ⚠️ **进存档**（to_dict/from_dict 同步），单人局里它恒为空。
    contracts: dict[str, Contract] = field(default_factory=dict)

    # 最近一季的财报（**机器可读**，前端只显示、AI 只解读，都读这个）。
    # `report` 是序列化后的字典；`raw_reports` 挂着运行时对象（不进存档）。
    report: dict = field(default_factory=dict)
    raw_reports: dict = field(default_factory=dict, repr=False, compare=False)

    def player(self, player_id: str) -> PlayerState:
        """取某个玩家的私有状态。不存在时**报错**，不静默造一个。"""
        try:
            return self.players[player_id]
        except KeyError:
            raise KeyError(
                f"没有玩家 {player_id!r}。现有玩家：{sorted(self.players)}"
            ) from None

    @property
    def player_ids(self) -> list[str]:
        """全部玩家 ID（排序后返回，保证遍历确定性）。"""
        return sorted(self.players)

    def single_player(self) -> tuple[str, PlayerState]:
        """第一期的便捷入口：取出唯一的玩家。

        ⚠️ 只在**明确知道是单人局**的地方用（比如 M1 测试、单人 UI）。
           引擎主流程一律用 `players` 字典遍历，不要用它走捷径——
           那样会把多人结构写死成单人。
        """
        if len(self.players) != 1:
            raise ValueError(
                f"single_player() 要求恰好 1 个玩家，当前有 {len(self.players)} 个："
                f"{sorted(self.players)}"
            )
        pid = next(iter(self.players))
        return pid, self.players[pid]

    def deep_copy(self) -> "GameState":
        """深拷贝。用于 `preview()` 试算——**改副本，不动原件**。

        ⚠️ 这是 `preview` 与 `advance` 共用一个实现的关键：
           `preview` = `advance(deepcopy(state), plans)`，
           所以试算的数字与真实结算**必然一致**。
        """
        from copy import deepcopy

        return deepcopy(self)

    def to_dict(self) -> dict:
        return {
            "world": self.world.to_dict(),
            "players": {pid: p.to_dict() for pid, p in self.players.items()},
            # ★ 合同账本**进存档**（§7.2）：未履约义务不能因存档/读档丢失。
            "contracts": {cid: c.to_dict() for cid, c in self.contracts.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GameState":
        return cls(
            world=WorldState.from_dict(data.get("world", {})),
            players={pid: PlayerState.from_dict(p) for pid, p in data.get("players", {}).items()},
            # ⚠️ 兼容旧存档：没有 "contracts" 键 → 空账本（单人局本就为空）。
            contracts={cid: Contract.from_dict(c) for cid, c in data.get("contracts", {}).items()},
        )


# ══════════════════════════════════════════════════════════════════
# 十、计划（Plan）—— 玩家一季度的全部操作
# ══════════════════════════════════════════════════════════════════
#
# ★ 关键设计：Plan 是**纯数据**，完全可序列化。
#
#   这条不是风格问题，是 AI 边界问题：
#   AI 帮玩家"起草可执行的操作"时，产出的是**数据**（一个 Plan 对象），
#   不是函数调用。于是 AI 的 Plan 与玩家的 Plan 走**同一条 validate**，
#   AI 没有特权 —— 这就是"能算 ≠ 能改"（见 docs/03-架构/AI自主演算-架构影响.md）。
# ══════════════════════════════════════════════════════════════════


@dataclass
class PlotPlan:
    """对**一块地**本季的安排（§1.2）。"""

    plot_id: str
    active: bool = True     # 是否开工
    u: float = 1.0          # 开工比例 0–1

    def __post_init__(self) -> None:
        if not 0.0 <= self.u <= 1.0:
            raise ValueError(f"开工比例 u 必须在 0–1 之间，收到 {self.u}（地块 {self.plot_id}）")

    @property
    def effective_u(self) -> float:
        """不开工时比例为 0（避免调用方忘记判断 active）。"""
        return self.u if self.active else 0.0

    def to_dict(self) -> dict:
        return {"plot_id": self.plot_id, "active": self.active, "u": self.u}

    @classmethod
    def from_dict(cls, data: dict) -> "PlotPlan":
        return cls(plot_id=data["plot_id"], active=data.get("active", True), u=data.get("u", 1.0))


@dataclass
class BuildOrder:
    """一条建设 / 升级指令（§5.2 工程表）。

    `target_level` = 1 表示新建一级；> 当前等级表示升级。
    """

    plot_id: str
    plot_type: PlotType
    target_level: int = 1
    # 升级/建造的具体路线产物（如工业 2 级选 H-A），留空用 config 默认
    product: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id, "plot_type": self.plot_type.value,
            "target_level": self.target_level, "product": self.product,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BuildOrder":
        return cls(
            plot_id=data["plot_id"], plot_type=PlotType(data["plot_type"]),
            target_level=data.get("target_level", 1), product=data.get("product"),
        )


@dataclass
class ContractAction:
    """合同动作 —— 一方在**自己计划里**提交的供货/受货意图（§7.2）。

    ★ 玩家对玩家（2026-10-16 用户裁定，**A 方案**）：
       双方各自提交、**互相确认才生效**。`side` 区分这一方是供货还是受货：

         side="sell"  我供货给对方（我交货、我收钱）
         side="buy"   我从对方收货（对方交货、我付钱）

       **双方都提交了、且 商品/数量/单价/季度 完全一致，合同才成立**
       （pipeline 把两份意图对账、写进 `GameState.contracts`）。
       只有一方提交 → 不生效（第一期就是这种情况，见 pipeline 注释 `待裁定`）。

    ⚠️ `from_dict` 用显式字段（不用 `**data`）：旧数据没有 `side` 键也能读。
    """

    action: str                 # draft / accept / deliver / breach（保留枚举位）
    counterparty: str = ""      # 对手方玩家 ID
    goods: str = ""
    qty: float = 0.0
    price: float = 0.0
    quarter: int = 0
    side: str = "sell"          # "sell"=我方供货 / "buy"=我方受货

    def to_dict(self) -> dict:
        return {
            "action": self.action, "counterparty": self.counterparty,
            "goods": self.goods, "qty": self.qty,
            "price": self.price, "quarter": self.quarter,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContractAction":
        return cls(
            action=data.get("action", "deliver"),
            counterparty=data.get("counterparty", ""),
            goods=data.get("goods", ""),
            qty=data.get("qty", 0.0),
            price=data.get("price", 0.0),
            quarter=data.get("quarter", 0),
            side=data.get("side", "sell"),
        )


@dataclass
class Plan:
    """一个玩家一个季度的完整计划（§1.2）。

    ⚠️ **只增不改**：玩家提交后不得修改。要改就是撤回重提，
       第一期直接禁止（《回合流程与状态机》§三）。

    ⚠️ **AI 填的和玩家填的是同一个东西**，走同一个 `validate`。
    """

    player_id: str
    plot_plans: dict[str, PlotPlan] = field(default_factory=dict)

    # 生产原料 + 建设物资的采购（**不含口粮** —— 口粮走下面的 `food`）
    purchases: dict[str, float] = field(default_factory=dict)

    # ══════════════════════════════════════════════════════════════
    # ★ 居民口粮：玩家**决定这期给居民吃多少**（2026-10-16 用户口径）
    # ══════════════════════════════════════════════════════════════
    #
    # 用户原话："首先玩家会决定这期的粮食供给。然后缺了的话会在购入环节
    #           购入（此时仍然是上期价格，然后这期市场的需发生变化了呀），
    #           不然扣除"
    #
    # 结算顺序（**这就是本节的全部内容**）：
    #
    #   ① 玩家申报本季要供应给居民的口粮 `food`（A/B/C/D）
    #   ② **自产先顶**：`自产顶上[g] = min(本季产出[g], food[g])`
    #   ③ **缺口去市场买**：`缺口[g] = food[g] − 自产顶上[g]`
    #         · 按【上季结算价】记账
    #         · 并且**计入本季 `B_t`** → 会把本季价格顶高
    #   ④ 自产顶上的那部分**不进公开市场** → 从 `X_t` 里扣掉
    #   ⑤ 最后按 `food` 走粮食结算，判定两档 → 得 H
    #
    # 与"产出就是供本身"的关系（两条不矛盾）：
    #
    #   产出**默认**全部是公开市场供给；
    #   玩家唯一能"截留"的，就是这份**指定给居民的口粮**（以及合同交付）。
    #   所以：`X_t = 产出 − 自产顶上 − 合同交付`
    #
    # ⚠️ 缺口是**自动买**的（用户："会在购入环节购入"）：
    #    如果钱不够，第 5 步一次性扣款时**明确报错**（§11.3 不允许部分执行）。
    food: dict[str, float] = field(default_factory=dict)

    # ══════════════════════════════════════════════════════════════
    # ★ 没有"申报卖出"这个字段（2026-10-16 用户拍板）
    # ══════════════════════════════════════════════════════════════
    #
    # 用户原话："玩家不需要申报要卖，因为没有仓库，生产出来的就是'供'本身。"
    #
    #   · **没有仓库** → 不存在"囤货待售"，本季产出**默认全部**成为市场供给
    #   · 所以 `X_t` 不需要玩家填 —— 只有"口粮"与"合同"两处例外
    #   · 也因此**没有"季末清仓"这个动作** —— 没有库存可清
    #
    # ⚠️ 这里原先有个 `sales: dict[str, float]` 字段，已删除。

    builds: list[BuildOrder] = field(default_factory=list)

    # ══════════════════════════════════════════════════════════════
    # ★ 合同通道（**已实现**，玩家对玩家 A 方案）
    # ══════════════════════════════════════════════════════════════
    #
    # 用户原话："要保留一个额外的通道，之后玩家间可以通过协议低于市场价提供，
    #           这个需要到时候能实现。"
    #
    # 2026-10-16 用户裁定：**玩家对玩家**，双方各自提交 `ContractAction`、
    #   互相确认（商品/数量/单价/季度一致）才成立 → 写入 `GameState.contracts`。
    #   · 合同交付的货**不进公开市场** → 求价时从 `X_t` 里扣掉
    #   · 合同价**低于市场价**，由双方协商（不是程序定价），转账**不走 P_t**
    #   · 卖方：`X_t = 本季产出 − 合同交付量`
    #   · 买方：`B_t = 采购量 − 合同收货量`（收到的货不算"从公开市场买入"）
    #
    # ⚠️ 单人局里没有第二个玩家 → 双方永远对不上账 → **合同自然不生效**
    #    （不是靠报错挡着，是机制上达不成——这正是被保留的 A，被否掉的
    #    B"跟外部对手方签"不做）。
    contracts: list[ContractAction] = field(default_factory=list)

    def u_for(self, plot_id: str) -> float:
        """取某地块的开工比例；未在计划里出现 = 不开工（0）。"""
        pp = self.plot_plans.get(plot_id)
        return pp.effective_u if pp else 0.0

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "plot_plans": {pid: pp.to_dict() for pid, pp in self.plot_plans.items()},
            "purchases": dict(self.purchases),
            "food": dict(self.food),
            "builds": [b.to_dict() for b in self.builds],
            "contracts": [c.to_dict() for c in self.contracts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            player_id=data["player_id"],
            plot_plans={pid: PlotPlan.from_dict(pp) for pid, pp in data.get("plot_plans", {}).items()},
            purchases=dict(data.get("purchases", {})),
            food=dict(data.get("food", {})),
            builds=[BuildOrder.from_dict(b) for b in data.get("builds", [])],
            contracts=[ContractAction.from_dict(c) for c in data.get("contracts", [])],
        )
