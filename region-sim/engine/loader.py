"""配置加载器 —— 把 `config/config.json` 读成运行时对象。

═══════════════════════════════════════════════════════════════════
本文件的核心职责不是"解析 JSON"，而是**守住数据纪律**
═══════════════════════════════════════════════════════════════════
《规则基线》§11.6 写得很明确：

    不得为填满配置而编造数据。
    缺失规则或数据必须明确报出，不得以语言推测代替正式计算。

所以本模块的默认行为是——**缺值就报错，绝不用默认值糊过去**：

    ❌ 弹性没填 → 偷偷用 0.5
    ✅ 弹性没填 → raise ConfigError("商品 F 的 Es 未填 …")

这条纪律看起来"麻烦"，但它省掉的是**最难查的一类 bug**：
程序跑出一个数，你不知道它是"按规则算的"还是"按某个默认值算的"。
M1 对账时，这种模糊会让排查时间成倍增加。

⚠️ 唯一的例外是**结构性参数**（地图 10×10、r=2、工资税率 10%），
   这些已经在《规则基线》里拍板定案，不算"缺失数据"，可以给默认值。
   判据：**它是否已被文档明确裁定**。裁定了 → 可以兜底；没裁定 → 必须报错。

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════
    from engine.loader import load_config

    cfg = load_config()                      # 默认读 config/config.json
    cfg = load_config("path/to/config.json") # 指定路径

    cfg.market          # {商品: MarketCurve}
    cfg.recipe(PlotType.RESOURCE, 1)         # 取配方
    cfg.wage_for(PlotType.RESOURCE)          # 取工资
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from engine.models import (
    MarketCurve,
    Plot,
    PlotType,
    Recipe,
    WorldState,
)

# 默认配置路径：region-sim/config/config.json
# __file__ = region-sim/engine/loader.py → 上溯两级到 region-sim/
_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.json"


class ConfigError(Exception):
    """配置错误。

    刻意单独定义一个异常类型，而不是用裸 `KeyError`/`ValueError`：
    调用方（服务层 / AI 层）需要能区分"**配置没填好**"和"**代码有 bug**"，
    前者要转给规则同学，后者要自己修。
    """


# ══════════════════════════════════════════════════════════════════
# 配方键的映射：config 里的键名 → (PlotType, level)
# ══════════════════════════════════════════════════════════════════
#
# config 里农业/资源按"产出商品"分节（A/B/C/D/F），工业按"路线+等级"
# 分节（G_L1 / H-A_L2 …）。这是为了让规则同学填表时贴近原始配方表。
# 代码侧需要的是统一的 (类型, 等级) 索引，所以在这里做一次映射。

_INDUSTRY_KEY_TO_OUTPUT: dict[str, tuple[int, str]] = {
    "G_L1":    (1, "G"),
    "H-A_L2":  (2, "H-A"),
    "H-F_L2":  (2, "H-F"),
    "H-G_L2":  (2, "H-G"),
    "H-J_L2":  (2, "H-J"),
    "I_L3":    (3, "I"),
    "H-A+_L3": (3, "H-A+"),
    "H-F+_L3": (3, "H-F+"),
    "H-G+_L3": (3, "H-G+"),
    "H-J+_L3": (3, "H-J+"),
}

# 工业"默认路线"：一块二级工业地若未指定 product，默认产什么。
# ⚠️ 待拍板（交接书 §五.4）：作物/路线是否建造时选定、建后可否更换。
#    现在给一个**明确的默认**（H-G），并在取用时要求调用方知道这回事。
_INDUSTRY_DEFAULT_ROUTE: dict[int, str] = {
    1: "G_L1",
    2: "H-G_L2",
    3: "I_L3",
}


def _require(data: dict, key: str, where: str) -> Any:
    """取一个**必填**字段，缺失时给出可操作的报错。"""
    if key not in data or data[key] is None:
        raise ConfigError(
            f"{where} 缺少必填字段 {key!r}。\n"
            f"  → config/config.json 里该处为 null 或不存在。\n"
            f"  → 这属于【待填数据】，请规则同学补上（见 docs/04-交接/规则细化交接书.md）。\n"
            f"  ⚠️ 不要用默认值绕过：程序报错是设计如此，不是 bug。"
        )
    return data[key]


# ══════════════════════════════════════════════════════════════════
# 配置对象
# ══════════════════════════════════════════════════════════════════


@dataclass
class EfficiencyConfig:
    """效率四分量参数（§3.1–§3.3.5 / §3.6）。

    ★ 2026-10-16 改动：服务『给出去』的加成由"任何邻居一律 +10%"
       改为**按服务自身等级取率**（1级 5% / 2级 7.5% / 3级 10%）。
       原写法导致一级与三级服务收益完全相同、升级无意义。
       `service_rate_by_level` 从 config 读，代码里不写死数字。
    """

    adjacency_per_neighbor: float = 0.05
    adjacency_cap: float = 0.40

    # 服务给邻居的加成率，按【服务自身等级】索引。
    # 例：{1: 0.05, 2: 0.075, 3: 0.10}
    service_rate_by_level: dict[int, float] = field(default_factory=dict)
    service_cap: Optional[float] = None   # None = 不封顶（初始估值）

    adjacency_table: dict[PlotType, set[PlotType]] = field(default_factory=dict)

    happiness_baseline_h: float = 0.6
    happiness_slope: float = 0.5
    happiness_floor: float = 0.90
    initial_happiness: float = 0.6

    ramp_up: dict[int, float] = field(default_factory=dict)
    ramp_up_default: float = 1.0

    research_bonus_per_plot: float = 0.01
    research_bonus_cap: float = 0.03

    def service_rate(self, service_level: int) -> float:
        """服务地块『给出去』的邻接加成率（§3.6）。

        ⚠️ 缺等级时**报错**，不退回一个默认率——
           退回默认值会让"三级服务"和"一级服务"悄悄算出同一个数，
           正是本次要修掉的那个毛病。
        """
        try:
            return self.service_rate_by_level[service_level]
        except KeyError:
            raise ConfigError(
                f"服务等级 {service_level} 没有配置加成率。\n"
                f"  已配置：{sorted(self.service_rate_by_level)}\n"
                f"  → 请检查 config.json 的 efficiency.adjacency.service_rate_by_level"
            ) from None

    def service_adjacency(self, service_level: int, neighbor_count: int) -> float:
        """服务地块的 E_邻接（§3.6）。

            E_邻接(服务) = 1 + rate(服务等级) × 相邻地块数
            rate: 1级 0.05 / 2级 0.075 / 3级 0.10

        `service_cap` 为 None 时**不封顶**（8 邻居、三级服务理论上限 +80%）。
        """
        bonus = self.service_rate(service_level) * neighbor_count
        if self.service_cap is not None:
            bonus = min(bonus, self.service_cap)
        return 1.0 + bonus

    def ramp_factor(self, running_quarters: int) -> float:
        """磨合系数：第 1 季 0.5、第 2 季 0.75、第 3 季起 1.0。

        `running_quarters` = 已经正常运行过的季度数（0 表示刚建好）。
        所以"第 1 季"对应 running_quarters == 0。
        """
        return self.ramp_up.get(running_quarters + 1, self.ramp_up_default)

    def happiness_factor(self, h: float) -> float:
        """H → E_幸福（§3.3.5）。

            H ≥ 0.6 → 0.90 + (H − 0.6) × 0.5         （旧公式不变：H=.6→.9、H=.8→1.0）
            H < 0.6 → 0.90 × max(0, H) / 0.6         （★ 2026-10-18 S4：线性下探）

        ★ 旧公式在 H < baseline 时有断点式下限 0.90 —— 玩家挨饿（H 掉到 0.4）
           效率只从 1.0 降到 0.9，惩罚太钝。改为**线性下探**：
           H=0 → 0，H=0.6 → 0.90，中间连续过渡，低幸福区才有真实代价。

        ⚠️ H ≥ baseline 的原式一字未动 —— M1 的 H=0.8 → 1.0 不受影响（M1 不动）。

        ⚠️ 用的是**上一季**的 H（六条铁律第一条），否则与
           "粮食 → H → 效率"形成循环依赖。
        """
        if h < self.happiness_baseline_h:
            return self.happiness_floor * max(0.0, h) / self.happiness_baseline_h
        return self.happiness_floor + (h - self.happiness_baseline_h) * self.happiness_slope

    def neighbors_in_table(self, plot_type: PlotType, neighbor_types: list[PlotType]) -> int:
        """数出落在关联表里的邻居个数（§3.2）。

        ⚠️ 服务**不查这张表**——它对任何邻居都加成（§3.6），
           调用方应对服务走独立分支。这里若传 SERVICE 会报错，
           防止误用。
        """
        if plot_type is PlotType.SERVICE:
            raise ValueError(
                "服务地块不走关联表，应走 §3.6 的『任何邻居 + 按等级取率』分支。"
                " 这是邻接计算里唯一需要按类型分支的地方。"
            )
        allowed = self.adjacency_table.get(plot_type, set())
        return sum(1 for t in neighbor_types if t in allowed)


@dataclass
class PopulationConfig:
    """人口与粮食需求参数（§3.3）。

    ★ 2026-10-16：**取消 round**。`N = L × r` 直接保留小数。
    ★ 2026-10-16（稍后，用户拍板）：**`L = Σd`（岗位跟已建成地块走，不再乘 `u`）**。
       所以 `L` 带小数只剩一个来源：`d` 本身就是小数（三级地块 `d=1.5`）。
       见基线 §3.3.1 与 §11.9.4；工资仍是 `d × u`（`production.py`）。
       引擎存小数，显示层负责取整。**全文计算链不再有任何四舍五入。**
    """

    r: float = 2.0
    level_multiplier_slope: float = 0.15
    base_baskets_per_person: float = 5.0
    good_tier_multiplier: float = 1.5

    def population(self, labor: float) -> float:
        """`N = L × r`。**不取整**（§3.3.1）。

        ⚠️ 这里刻意**没有** `round()` / `ceil()`。
           取整会抹掉 `d=1.5`、`u=0.5` 这些粒度，让账对不上。
        """
        return labor * self.r

    def level_multiplier(self, avg_level: float) -> float:
        """`m = 1 + slope × (ḡ − 1)`（§3.3.2）。"""
        return 1.0 + self.level_multiplier_slope * (avg_level - 1.0)

    def baseline_food(self, population: float, avg_level: float) -> float:
        """`F_基准 = (N / 5) × m`（§3.3.3）。"""
        return (population / self.base_baskets_per_person) * self.level_multiplier(avg_level)

    def good_food(self, baseline_food: float) -> float:
        """`F_优秀 = 1.5 × F_基准`（§3.3.3）。"""
        return self.good_tier_multiplier * baseline_food


@dataclass
class FoodConfig:
    """两档粮食配比（§3.3.3）。"""

    baseline_quota: dict[str, float] = field(default_factory=dict)
    good_quota: dict[str, float] = field(default_factory=dict)
    replaceable_goods: list[str] = field(default_factory=list)


@dataclass
class TradeConfig:
    """交割口径（§4.1 / §6 —— 2026-10-16 用户裁定的"足够权威"版本）。

    三件事说清：

        ① 买入按【上一季度结算价】记账   buy_price_source = "last_settlement"
        ② 卖出按【本季求出的 P_t】        sell_price_source = "current"
        ③ 物资款 + 工资 + 经营费在【结算时一次性】扣
                                          deduct_timing = "settlement"

    ⚠️ 玩家阶段（期初）**不扣钱、不锁价、不动账户**，只提交 Plan。

    `buy_volume_includes` 决定 `B_t` 由哪些段构成——三段全部计入本季需求侧，
    也就是**买入动作本身会把本季价格顶高**：

        production_input  生产原料
        construction      建设物资（一次性买入，但同样扰动市场）
        household_food    居民口粮
    """

    buy_price_source: str = "last_settlement"
    sell_price_source: str = "current"
    deduct_timing: str = "settlement"
    buy_volume_includes: list[str] = field(
        default_factory=lambda: ["production_input", "construction", "household_food"]
    )
    first_quarter_price_fallback: str = "P0"


@dataclass
class Config:
    """全部静态数据。

    ⚠️ 本对象**只增不改**：引擎是纯函数，配置属于"输入"的一部分。
    """

    raw: dict = field(default_factory=dict)

    # 世界
    rows: int = 10
    cols: int = 10
    total_quarters: int = 40
    opening_cash: Optional[float] = None   # None = 未拍板

    # 商品与配方
    market: dict[str, MarketCurve] = field(default_factory=dict)
    goods_name: dict[str, str] = field(default_factory=dict)
    recipes: dict[tuple[PlotType, int], Recipe] = field(default_factory=dict)
    # 工业按路线索引：(等级, 产出商品) → Recipe
    industry_routes: dict[tuple[int, str], Recipe] = field(default_factory=dict)
    # 农业按产出索引：(等级, 产出商品) → Recipe
    # ⚠️ 农业 4 种产出共用 (类型, 等级) 索引会互相覆盖，所以单独存一份。
    agriculture_routes: dict[tuple[int, str], Recipe] = field(default_factory=dict)

    wages: dict[str, float] = field(default_factory=dict)

    efficiency: EfficiencyConfig = field(default_factory=EfficiencyConfig)
    population: PopulationConfig = field(default_factory=PopulationConfig)
    food: FoodConfig = field(default_factory=FoodConfig)
    trade: TradeConfig = field(default_factory=TradeConfig)

    # 经营费拆分（§3.4）：0.7c + 0.3c·u·E。**规则常量**，不是平衡数值。
    production_operating_fixed_share: float = 0.7
    production_operating_variable_share: float = 0.3

    wage_tax_rate: float = 0.10

    construction: dict = field(default_factory=dict)
    opening_quota: dict[str, Optional[int]] = field(default_factory=dict)
    opening_discount_rate: Optional[float] = None

    # ★ 2026-10-17 用户拍板：每季固定补贴（转移支付，不是产值，不进 GDP、不进市场求价）。
    #   默认 300.0；config.economy.quarterly_subsidy 可覆盖。
    #   ★ 2026-10-18 S1：改为**季初发放**（server/session.py），结算不再入账。
    quarterly_subsidy: float = 300.0

    # ★ 2026-10-18 E1 用户拍板：开局期加发补贴。
    #   前 `early_subsidy_quarters` 季（含边界，即 `quarter <= early_subsidy_quarters`）
    #   发 `early_quarterly_subsidy`；之后回落到 `quarterly_subsidy`。
    #   缺省 0/0 → 没有任何一季能命中，early 档自动失效（每季发基准 `quarterly_subsidy`）。
    #   ⚠️ `early_subsidy_quarters` 必须 ≥0 整数；= 0 等价于"关掉早期补贴"。
    #   ⚠️ 与 `quarterly_subsidy` 同口径：转移支付，不是产值，不进 GDP / 市场求价。
    #   ★ 2026-10-18 S1 最终以 config.json 为准（Q1–Q7 各 1200、Q8 起 300）；
    #      这里仍是"未加载 config 时"的兜底缺省。
    early_quarterly_subsidy: float = 0.0
    early_subsidy_quarters: int = 0

    # ★ 政策（§11.9.3）：开局模式 + 乘数数值覆盖。见 policies.py。
    policy_mode: Optional[str] = None
    policy_multipliers: dict = field(default_factory=dict)

    # ★ 2026-10-18 地块名字库：{plot_type_key: [名字…]}。
    #   引擎**只用来查、不参与计算**；新建成时 Plot.name 从这里分配。
    #   缺省空 dict → 分配函数退化到默认后缀策略（"类型-Lxx"）。
    plot_names: dict[str, list[str]] = field(default_factory=dict)

    # ── 访问器 ──────────────────────────────────────────

    def recipe(self, plot_type: PlotType, level: int,
               product: Optional[str] = None) -> Recipe:
        """取配方。

        工业地块需要 `product` 指定路线（H-A/H-F/…）：
          · 传了 product → 查对应路线；
          · 没传 → 用默认路线（并**不**静默：默认值是明确的表）。

        ⚠️ `industry_routes` 的索引是 **(等级, 产出商品)**，例如 `(2, "H-A")`，
           不是 config 里的键名 `"H-A_L2"`。两者混用会让工业配方永远查不到。
        """
        if plot_type is PlotType.INDUSTRY:
            output = self._industry_output(level, product)
            try:
                return self.industry_routes[(level, output)]
            except KeyError:
                raise ConfigError(
                    f"工业配方缺失：等级 {level}、产出 {output!r}。\n"
                    f"  该等级可用产出："
                    f"{sorted(k[1] for k in self.industry_routes if k[0] == level)}"
                ) from None

        if plot_type is PlotType.AGRICULTURE:
            # ⚠️ 农业**必须**指定产出：4 种产出（A/B/C/D）各有自己的配方，
            #    用 (类型, 等级) 查会撞车。产出已在建造时选定、建后不可换。
            if product is None:
                raise ConfigError(
                    f"农业配方必须指定产出（A/B/C/D）：等级 {level}。\n"
                    f"  该等级可选："
                    f"{sorted(k[1] for k in self.agriculture_routes if k[0] == level)}\n"
                    f"  → 农业 4 种产出共用『类型+等级』索引会互相覆盖，"
                    f"不能猜一个默认值。"
                )
            try:
                return self.agriculture_routes[(level, product)]
            except KeyError:
                raise ConfigError(
                    f"农业配方缺失：等级 {level}、产出 {product!r}。\n"
                    f"  该等级可选："
                    f"{sorted(k[1] for k in self.agriculture_routes if k[0] == level)}"
                ) from None

        try:
            return self.recipes[(plot_type, level)]
        except KeyError:
            raise ConfigError(
                f"配方缺失：类型 {plot_type.value}、等级 {level}。\n"
                f"  已加载的配方：{sorted((t.value, lv) for t, lv in self.recipes)}"
            ) from None

    def _industry_output(self, level: int, product: Optional[str]) -> str:
        """把 (等级, 产出商品) 规范化成产出商品编码。

        `product=None` 时取该等级的**默认路线**（明确的表，不是猜的）。
        """
        if product is None:
            try:
                default_key = _INDUSTRY_DEFAULT_ROUTE[level]
            except KeyError:
                raise ConfigError(f"工业等级 {level} 没有默认路线，请显式指定 product") from None
            return _INDUSTRY_KEY_TO_OUTPUT[default_key][1]

        for lv, out in _INDUSTRY_KEY_TO_OUTPUT.values():
            if lv == level and out == product:
                return product
        raise ConfigError(
            f"工业等级 {level} 没有产出 {product!r} 的路线。\n"
            f"  该等级可选："
            f"{sorted(out for lv, out in _INDUSTRY_KEY_TO_OUTPUT.values() if lv == level)}"
        )

    def wage_for(self, plot_type: PlotType, level: int = 1,
                 product: Optional[str] = None) -> float:
        """取工资（§3.5 工资表）。

        工资按**活动**而非按地块类型：
          农业 8 / 资源 12 / 基础工业 G 15 / 机械 H,H+ 20 / 高技术 I,研究 28 / 服务 16
        """
        key = self._wage_key(plot_type, level, product)
        try:
            return self.wages[key]
        except KeyError:
            raise ConfigError(
                f"工资表缺少 {key!r}（类型 {plot_type.value}、等级 {level}、产出 {product}）。"
            ) from None

    @staticmethod
    def _wage_key(plot_type: PlotType, level: int, product: Optional[str]) -> str:
        if plot_type is PlotType.AGRICULTURE:
            return "agriculture"
        if plot_type is PlotType.RESOURCE:
            return "resource"
        if plot_type is PlotType.SERVICE:
            return "service"
        if plot_type is PlotType.RESEARCH:
            return "high_tech"
        if plot_type is PlotType.INDUSTRY:
            if level == 1:
                return "industry_g"
            if level == 2:
                return "machine"          # H-* 机械
            # 三级：I 是高技术；H-*+ 是自动化机械
            return "high_tech" if (product == "I") else "machine"
        raise ConfigError(f"未知地块类型：{plot_type}")

    # ── 研究地块的「被子」helper（★ 2026-10-18 最小存储改动）─────────────
    #
    # 研究地块内部**仍编码为** `PlotType.INDUSTRY + level=2 + product='研究'`
    # （用户允许的**最小存储改动**：不改存档格式、不引入新枚举值）。
    # 但它的**行为**必须等同研究地块：配方走 `research.maintain`、
    # 工资按 `high_tech`、不产商品、参与 `efficiency.research_bonus`。
    # 这两条 helper 是这套行为的**唯一入口**：所有取配方/工资的地方都改走它，
    # 避免 pipeline / population / report / validate 各处散写 if-else。
    #
    # ⚠️ 独立的 `PlotType.RESEARCH`（工程表外的研究地块）也走它——
    #   `industry L2 '研究'` 是【研究线二级】、`PlotType.RESEARCH L1` 是【独立
    #   研究地块】，两者共用同一条 `research.maintain` 配方（Research Recipe
    #   只按 (RESEARCH, 1) 注册）。

    def recipe_for(self, plot: Plot) -> Recipe:
        """**按地块**取配方 —— 研究地块重定向到 `research.maintain`，其它走原 `recipe()`。

        ⚠️ **唯一入口**：生产（pipeline）、人口（population）、报表（report）、
           校验（validate）**都要走它**，不再各自调 `cfg.recipe(...)`。
           研究地块**不产商品**（`Recipe.output is None`），所以这条也存在
           「研究地块的配方」这个语义上——尽管存储上研究地块是
           `PlotType.INDUSTRY + level=2 + product='研究'`。
        """
        if plot.is_research:
            try:
                return self.recipes[(PlotType.RESEARCH, 1)]
            except KeyError:
                raise ConfigError(
                    "config 缺失研究配方：recipes.research.maintain 未定义。\n"
                    "  研究地块（含 industry L2 product='研究'）共用 research.maintain。\n"
                    f"  已加载的研究配方：{sorted((t.value, lv) for t, lv in self.recipes if t is PlotType.RESEARCH)}"
                ) from None
        return self.recipe(plot.plot_type, plot.level, plot.product)

    def wage_for_plot(self, plot: Plot) -> float:
        """**按地块**取工资 —— 研究地块按 `high_tech`（§3.5），其它走原 `wage_for()`。

        ⚠️ **工资 = d × u** 在 `production.py` 里逐地块按 `d` 计算；
           本函数只给【单位劳动工资】。研究地块 d 来自 `research.maintain`
           （d=2），乘上这里给的 28 就是"研究人员的工资率"。
        """
        if plot.is_research:
            try:
                return self.wages["high_tech"]
            except KeyError:
                raise ConfigError("工资表缺少 'high_tech'（研究地块按它结算工资）") from None
        return self.wage_for(plot.plot_type, plot.level, plot.product)

    def price_of(self, goods: str) -> float:
        """参考价 P0。"""
        try:
            return self.market[goods].P0
        except KeyError:
            raise ConfigError(f"商品 {goods!r} 不在市场配置里") from None

    # ── 补贴（2026-10-18 E1 拍板）─────────────────────

    def subsidy_for_quarter(self, quarter: int) -> float:
        """**纯函数**：本季应发多少补贴（每个玩家）。

        E1 口径（2026-10-18 用户拍板）：
            quarter ≤ early_subsidy_quarters → early_quarterly_subsidy
            quarter >  early_subsidy_quarters → quarterly_subsidy

        ⚠️ **`early_subsidy_quarters = 0` 时 early 档不可达**（`quarter ≥ 1 > 0`），
           即"early 未启用"。两个字段任一缺省 0 → 永远都走基准档。

        ⚠️ 是**转移支付**而不是产值：pipeline 只能把它进 cash 与
           `report.subsidy`，**不进 value_added / sales_revenue / 市场求价**。
           这条纪律看得见落点 —— 见 `pipeline.py` 第 8.5 步注释。

        ⚠️ 明确写成方法而不是字段拼接，是**为了让 pipeline 与测试共用同一条
           公式**，避免"代码改了但断言还在拼 `quarterly_subsidy`"。
        """
        if quarter <= self.early_subsidy_quarters:
            return float(self.early_quarterly_subsidy)
        return float(self.quarterly_subsidy)

    def all_goods(self) -> list[str]:
        """全部商品编码（排序，保证确定性）。"""
        return sorted(self.market)

    def new_world(self) -> WorldState:
        """造一个开局世界状态（第 1 季、SETUP 阶段、市场曲线就位）。

        ★ 政策模式：本方法**不**开启随机抽取，一切按 `engine/policies.py`
          的缺省（`regular` = 恒常规环境）走 —— 因为 M1 夹具与单点对账
          都用 `new_world()` 造世界，锚点必须钉死。
          正式对局在开局入口显式调 `policies.set_policy_mode(world, "random")`，
          或直接调 `policies.apply_config_mode(world, cfg)`。
        """
        from engine.models import WorldPhase

        return WorldState(
            quarter=1,
            rows=self.rows,
            cols=self.cols,
            total_quarters=self.total_quarters,
            phase=WorldPhase.SETUP,
            market={g: MarketCurve.from_dict(c.to_dict()) for g, c in self.market.items()},
            policies=[],
            price_history={},
        )

    def new_player(self, player_id: str):
        """造一个开局玩家状态（§8.1 初始条件）。

        ⚠️ 开局现金未拍板时**报错**，不编造。
           测试里请显式传 cash。
        """
        from engine.models import PlayerState

        if self.opening_cash is None:
            raise ConfigError(
                "开局现金（world.opening_cash）未拍板 —— 不能自动选一个数。\n"
                "  · M1 测试用的是 1000（测试值，非正式值）\n"
                "  · 交接书 §五.4 建议 1200\n"
                "  → 请在 config.json 里填入正式值，或调用时显式传 cash。"
            )
        return PlayerState(player_id=player_id, cash=float(self.opening_cash))


# ══════════════════════════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════════════════════════


def load_config(path: str | Path | None = None) -> Config:
    """读取并校验 config.json。

    校验策略：**结构性问题立即报错，待填数值留到使用时报错**。
    理由：M1 只需要 F/G 两条配方，不应该因为"建设表还没填"就跑不起来。
    """
    p = Path(path) if path else _DEFAULT_CONFIG
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}")

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"配置文件不是合法 JSON：{p}\n  {e}\n"
            f"  ⚠️ JSON 不允许注释和尾逗号，说明文字请放进 _comment 字段。"
        ) from None

    cfg = Config(raw=raw)

    # ── 世界 ────────────────────────────────────────────
    world = raw.get("world", {})
    cfg.rows = int(world.get("rows", 10))
    cfg.cols = int(world.get("cols", 10))
    cfg.total_quarters = int(world.get("total_quarters", 40))
    cfg.opening_cash = world.get("opening_cash")  # 允许 None

    # ── 商品 / 市场 ─────────────────────────────────────
    goods = raw.get("goods")
    if not goods:
        raise ConfigError("config 里必须有 'goods' 节")
    for gid, g in goods.items():
        if gid == "_comment":
            continue
        P0 = _require(g, "P0", f"goods.{gid}")
        Q0 = _require(g, "Q0", f"goods.{gid}")
        cfg.market[gid] = MarketCurve(
            goods=gid,
            P0=float(P0),
            Q0=float(Q0),
            # ⚠️ 刻意允许 None：弹性未填是"待填数据"，不是结构错误。
            #    真正算价时 MarketCurve.slopes() 会报错。
            Es=(float(g["Es"]) if g.get("Es") is not None else None),
            Ed=(float(g["Ed"]) if g.get("Ed") is not None else None),
            P_min_ratio=float(g.get("P_min_ratio", 0.1)),
            external_purchase=float(g.get("external_purchase", 0.0) or 0.0),
        )
        cfg.goods_name[gid] = g.get("name", gid)

    # ── 配方 ────────────────────────────────────────────
    recipes = raw.get("recipes", {})
    _load_recipes(cfg, recipes)

    # ── 工资 ────────────────────────────────────────────
    cfg.wages = {k: float(v) for k, v in raw.get("wages", {}).items() if k != "_comment"}

    # ── 效率 ────────────────────────────────────────────
    eff = raw.get("efficiency", {})
    adj = eff.get("adjacency", {})
    cfg.efficiency = EfficiencyConfig(
        adjacency_per_neighbor=float(adj.get("per_neighbor", 0.05)),
        adjacency_cap=float(adj.get("cap", 0.40)),
        service_rate_by_level={
            int(k): float(v)
            for k, v in (adj.get("service_rate_by_level") or {}).items()
        },
        service_cap=(float(adj["service_cap"]) if adj.get("service_cap") is not None else None),
        happiness_baseline_h=float(eff.get("happiness", {}).get("baseline_h", 0.6)),
        happiness_slope=float(eff.get("happiness", {}).get("slope", 0.5)),
        happiness_floor=float(eff.get("happiness", {}).get("floor", 0.90)),
        initial_happiness=float(eff.get("happiness", {}).get("initial_h", 0.6)),
        # ⚠️ ramp_up 里除 "_comment" 外还有一个 "default" 键（不是数字等级），
        #    必须用 isdigit() 过滤，否则 int("default") 会炸。
        ramp_up={int(k): float(v) for k, v in eff.get("ramp_up", {}).items()
                 if str(k).isdigit()},
        ramp_up_default=float(eff.get("ramp_up", {}).get("default", 1.0)),
        research_bonus_per_plot=float(eff.get("research_bonus", {}).get("per_plot", 0.01)),
        research_bonus_cap=float(eff.get("research_bonus", {}).get("cap", 0.03)),
    )
    _build_adjacency_table(cfg, adj.get("table", {}))

    # ★ 2026-10-16：服务的等级加成率表**必须**存在。
    #    缺了会让服务退化成"不分等级"，正是本次要修掉的毛病。
    if not cfg.efficiency.service_rate_by_level:
        raise ConfigError(
            "config 缺少 efficiency.adjacency.service_rate_by_level（服务按等级取率）。\n"
            "  → 这是 2026-10-16 定案的规则（§3.6：1级5% / 2级7.5% / 3级10%）。\n"
            "  → 不能省略：省略会让一级与三级服务算出同一个数。"
        )

    # ── 人口 / 粮食 ─────────────────────────────────────
    pop = raw.get("population", {})
    cfg.population = PopulationConfig(
        r=float(pop.get("r", 2)),
        level_multiplier_slope=float(pop.get("level_multiplier_slope", 0.15)),
        base_baskets_per_person=float(pop.get("base_baskets_per_person", 5)),
        good_tier_multiplier=float(pop.get("good_tier_multiplier", 1.5)),
    )

    fd = raw.get("food", {})
    cfg.food = FoodConfig(
        baseline_quota={k: float(v) for k, v in fd.get("baseline_quota", {}).items()},
        good_quota={k: float(v) for k, v in fd.get("good_quota", {}).items()},
        replaceable_goods=list(fd.get("replaceable_goods", ["A", "B"])),
    )

    # ── 经营费拆分（§3.4）────────────────────────────────
    prod = raw.get("production", {})
    cfg.production_operating_fixed_share = float(prod.get("operating_fixed_share", 0.7))
    cfg.production_operating_variable_share = float(prod.get("operating_variable_share", 0.3))

    # ── 税 / 交割 / 建设 / 开局 ──────────────────────────
    cfg.wage_tax_rate = float(raw.get("tax", {}).get("wage_tax_rate", 0.10))

    tr = raw.get("trade", {})
    cfg.trade = TradeConfig(
        buy_price_source=tr.get("buy_price_source", "last_settlement"),
        sell_price_source=tr.get("sell_price_source", "current"),
        deduct_timing=tr.get("deduct_timing", "settlement"),
        buy_volume_includes=list(tr.get(
            "buy_volume_includes",
            ["production_input", "construction", "household_food"],
        )),
        first_quarter_price_fallback=tr.get("first_quarter_price_fallback", "P0"),
    )

    cfg.construction = raw.get("construction", {})

    opening = raw.get("opening", {})
    cfg.opening_quota = dict(opening.get("discount_quota", {}))
    cfg.opening_discount_rate = opening.get("discount_rate")

    # ── 经济（§2026-10-17 补贴 / 2026-10-18 E1 早期补贴）─────────────
    # ★ 只加读取，不改既有字段语义（红线：loader 只增不改）。
    #   缺省时用 Config 字段默认 120.0；显式 null / 0 也走默认（视为"未配置"）。
    #   ⚠️ 它是转移支付：不是产值，不进 value_added，不进 sales_revenue，
    #      不改市场求价。它只影响玩家现金账面（注入源头）与 report.subsidy。
    eco = raw.get("economy", {}) or {}
    sub = eco.get("quarterly_subsidy")
    if sub is not None:
        # ⚠️ 包含 0.0：显式写 0 = 关掉补贴，不回退默认。
        try:
            cfg.quarterly_subsidy = float(sub)
        except (TypeError, ValueError):
            raise ConfigError(
                f"economy.quarterly_subsidy 必须是数值，实得 {sub!r}。"
            ) from None
    # `sub is None` → 保持 Config 字段的默认 120.0（2026-10-17 用户拍板）。

    # ★ 2026-10-18 E1：开局期加发。规则同 quarterly_subsidy：
    #    `early_subsidy_quarters = 0` → early 档不可达，等价于关掉早期补贴。
    #    两个字段任一缺省 → Config 字段默认 0/0 → 永远走基准档（行为与旧代码一致）。
    early_sub = eco.get("early_quarterly_subsidy")
    if early_sub is not None:
        try:
            cfg.early_quarterly_subsidy = float(early_sub)
        except (TypeError, ValueError):
            raise ConfigError(
                f"economy.early_quarterly_subsidy 必须是数值，实得 {early_sub!r}。"
            ) from None

    early_q = eco.get("early_subsidy_quarters")
    if early_q is not None:
        # ⚠️ 必须是 ≥0 整数。float 整数（JSON 读进来的 4 vs 4.0）也放行，
        #    但要真的按 int 存出去，避免后续 isinstance 判断坑。
        if isinstance(early_q, bool) or not isinstance(early_q, (int, float)):
            raise ConfigError(
                f"economy.early_subsidy_quarters 必须是 ≥0 整数，实得 {early_q!r}。"
            )
        qi = int(early_q)
        if qi < 0 or float(early_q) != float(qi):
            raise ConfigError(
                f"economy.early_subsidy_quarters 必须是 ≥0 整数，实得 {early_q!r}。"
            )
        cfg.early_subsidy_quarters = qi

    # ── 政策（§11.9.3）──────────────────────────────────
    # ⚠️ 四项政策本身、25% 概率、作用范围都是**规则**，不在 config 里增删；
    #    这里只读两样：开局模式（random/regular）与乘数数值覆盖。
    #    `mode` 作用见 `engine/policies.py`：正式对局用 "random"（每季抽），
    #    "regular" 恒为常规环境（测试/单点对账用）。
    pol = raw.get("policies") or {}
    cfg.policy_mode = pol.get("mode")
    cfg.policy_multipliers = dict(pol.get("multipliers") or {})

    # ── 地块名字库（2026-10-18）─────────────────────────
    # 只读取、不校验内容；{plot_type_key: [名字…]}，名字全当中文串用。
    pn = raw.get("plot_names") or {}
    names_map: dict[str, list[str]] = {}
    for key, names in pn.items():
        if key.startswith("_"):
            continue
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            names_map[key] = list(names)
    cfg.plot_names = names_map

    _validate(cfg)
    return cfg


def _load_recipes(cfg: Config, recipes: dict) -> None:
    """把 config 的三节配方（农业/资源/工业/服务/研究）铺成索引。"""

    # 农业：{ "A": {"1": {...}} }  → (AGRICULTURE, level), output=A
    #
    # ⚠️ 农业有 4 种产出 × 3 个等级 = 12 条配方，但主索引 `recipes` 只能按
    #    (类型, 等级) 存一条 —— 四个产出会**互相覆盖**，最后一个（D）胜出。
    #    所以另建 `agriculture_routes[(等级, 产出)]`，与工业路线同款处理。
    #    （产出已在建造时选定、建后不可换，见《规则基线》§5.1。）
    for output, levels in recipes.get("agriculture", {}).items():
        if output.startswith("_"):
            continue
        for lv_str, r in levels.items():
            level = int(lv_str)
            rec = Recipe(
                plot_type=PlotType.AGRICULTURE, level=level, output=output,
                input={k: float(v) for k, v in r.get("input", {}).items()},
                c=float(r["c"]), d=float(r["d"]), e=float(r["e"]),
            )
            cfg.agriculture_routes[(level, output)] = rec
            # 主索引也存一份（默认取第一个产出，仅作兼容；
            # 真正取配方请走 cfg.recipe(..., product=...)）
            cfg.recipes.setdefault((PlotType.AGRICULTURE, level), rec)

    # 资源：同农业，产出 F
    for output, levels in recipes.get("resource", {}).items():
        if output.startswith("_"):
            continue
        for lv_str, r in levels.items():
            level = int(lv_str)
            cfg.recipes[(PlotType.RESOURCE, level)] = Recipe(
                plot_type=PlotType.RESOURCE, level=level, output=output,
                input={k: float(v) for k, v in r.get("input", {}).items()},
                c=float(r["c"]), d=float(r["d"]), e=float(r["e"]),
            )

    # 工业：按路线键（G_L1 / H-A_L2 …）
    for key, r in recipes.get("industry", {}).items():
        if key.startswith("_"):
            continue
        if key not in _INDUSTRY_KEY_TO_OUTPUT:
            raise ConfigError(
                f"未知的工业配方键 {key!r}。\n"
                f"  合法键：{sorted(_INDUSTRY_KEY_TO_OUTPUT)}"
            )
        level, output = _INDUSTRY_KEY_TO_OUTPUT[key]
        rec = Recipe(
            plot_type=PlotType.INDUSTRY, level=level, output=output,
            input={k: float(v) for k, v in r.get("input", {}).items()},
            c=float(r["c"]), d=float(r["d"]), e=float(r["e"]),
        )
        cfg.industry_routes[(level, output)] = rec
        # 默认路线同时进主索引，方便不关心路线的调用方
        if _INDUSTRY_DEFAULT_ROUTE.get(level) == key:
            cfg.recipes[(PlotType.INDUSTRY, level)] = rec

    # 服务：0→0，output 为 None
    for lv_str, r in recipes.get("service", {}).items():
        if lv_str.startswith("_"):
            continue
        level = int(lv_str)
        cfg.recipes[(PlotType.SERVICE, level)] = Recipe(
            plot_type=PlotType.SERVICE, level=level, output=None,
            input={}, c=float(r["c"]), d=float(r["d"]), e=0.0,
        )

    # 研究：维护项
    res = recipes.get("research", {}).get("maintain")
    if res:
        cfg.recipes[(PlotType.RESEARCH, 1)] = Recipe(
            plot_type=PlotType.RESEARCH, level=1, output=None,
            input={}, c=float(res["c"]), d=float(res["d"]), e=0.0,
        )


def _build_adjacency_table(cfg: Config, table: dict) -> None:
    """关联表：地块类型 → 哪些邻居类型算"关联"（§3.2）。

    ⚠️ 表里的 "high_tech" 不是一个 PlotType（没有"高技术地块"），
       它指的是**研究地块**（research）。这里做一次显式换算。
    """
    alias = {"high_tech": PlotType.RESEARCH}

    for tname, allowed in table.items():
        if tname.startswith("_"):
            continue
        try:
            ptype = PlotType(tname)
        except ValueError:
            raise ConfigError(
                f"关联表里有未知地块类型 {tname!r}。合法值：{[t.value for t in PlotType]}"
            ) from None

        converted: set[PlotType] = set()
        for a in allowed:
            if a in alias:
                converted.add(alias[a])
                continue
            try:
                converted.add(PlotType(a))
            except ValueError:
                raise ConfigError(
                    f"关联表 {tname!r} 里有未知邻居类型 {a!r}。"
                ) from None
        cfg.efficiency.adjacency_table[ptype] = converted


def _validate(cfg: Config) -> None:
    """结构性校验 —— 只查"形状"，不查"数值填没填"。"""
    # 关联表必须覆盖 5 个类型（交接书自检清单要求）
    expected = set(PlotType)
    missing = expected - set(cfg.efficiency.adjacency_table)
    if missing:
        raise ConfigError(
            f"关联表缺少类型：{sorted(t.value for t in missing)}\n"
            f"  关联表必须 5 个类型齐全（见交接书 §七 自检清单）。"
        )

    # 两档粮食配比的商品必须在市场里有
    for tier, quota in (("baseline_quota", cfg.food.baseline_quota),
                        ("good_quota", cfg.food.good_quota)):
        for g in quota:
            if g not in cfg.market:
                raise ConfigError(f"食物 {tier} 里的商品 {g!r} 不在市场配置中")

    # 配方的 input 商品必须在市场里有
    all_recipes = {**cfg.recipes, **cfg.industry_routes, **cfg.agriculture_routes}
    for key, rec in all_recipes.items():
        for g in rec.input:
            if g not in cfg.market:
                raise ConfigError(
                    f"配方 {key} 的投入商品 {g!r} 不在市场配置中"
                )
        if rec.output is not None and rec.output not in cfg.market:
            raise ConfigError(
                f"配方 {key} 的产出商品 {rec.output!r} 不在市场配置中"
            )


def validate_config_ready(cfg: Config, goods: list[str] | None = None) -> list[str]:
    """检查"能不能真的开始算"，返回缺失项清单（不抛异常）。

    用途：给队友一个**自检工具** —— 想知道还差什么，跑这个；
    而真正计算时缺值会直接抛 ConfigError。
    """
    problems: list[str] = []

    targets = goods if goods is not None else cfg.all_goods()
    for g in targets:
        curve = cfg.market.get(g)
        if curve and (curve.Es is None or curve.Ed is None):
            problems.append(f"商品 {g} 的弹性 Es/Ed 未填（无法算价）")

    if cfg.opening_cash is None:
        problems.append("开局现金 opening_cash 未填")

    if not cfg.construction.get("works"):
        problems.append("建设/升级表 construction.works 为空（M1 之后需要）")

    # 开局减免名额已裁定（2026-10-16：每类 3 个 × 折扣 0.50）。
    # 只有「整个 quota 缺失」或「全部为 None」才算真没填、才报；
    # 只要有一类填了数，就当配置齐了，不再报。
    quota = cfg.opening_quota or {}
    if not quota or all(v is None for v in quota.values()):
        problems.append("开局分类减免名额 discount_quota 未填")

    return problems
