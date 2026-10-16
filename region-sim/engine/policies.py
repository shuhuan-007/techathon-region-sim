"""政策系统 —— 每季随机抽一项，仅当季有效（《规则基线》§11.9.3）。

═══════════════════════════════════════════════════════════════════
规则（基线 §11.9.3 定死，本模块只是它的直译）
═══════════════════════════════════════════════════════════════════

  · 一局 20 季；**每季开始**抽一项（第一季也抽）；
  · **每季恰好一项**、**四项各 25%**、**允许连续抽到相同政策**；
  · **仅当季有效、不累计**（duration = 1，到期即失效）；
  · **读档 / 重开界面不重抽** —— 抽取结果落进 `world.policies`，
    读档时从存档恢复，只对 `quarter` 不在存档里的季才补抽。

四项政策（基线原表）：

  | # | 政策       | 效果                                             |
  |---|-----------|--------------------------------------------------|
  | 1 | 常规环境   | 无额外修正                                        |
  | 2 | 生产激励   | **商品生产地块**效率 ×1.10；工资 ×1.15             |
  | 3 | 精益运营   | **商品生产地块**效率 ×0.95；经营费 ×0.90           |
  | 4 | 民生优先   | **全部运行地块**工资 ×1.10                         |

**作用范围**（基线原文）：

  · **商品生产地块** = 使用**有可交易商品产出**的生产配方的地块
    （**不含**研究与本地服务 `J`）；
  · **全部运行地块** = 商品生产 ＋ 按原规则正常运行的、并支付工资的服务/研究。

⚠️ **停工（`u = 0`）且按原规则不发工资的地块，不因政策凭空产生工资。**
   本模块的 `wage_multiplier()` 只在"本来就有工资"（`labor > 0`）时才返回非 1 的
   乘数；接入点（`pipeline`）也只对 `u > 0` 的地块乘。两道保险。

**计算位置**（基线原文）：

    E_base          = 原有效率公式的计算结果
    E_effective     = E_base × 政策效率乘数
    wage_payment    = 原工资计算结果 × 政策工资乘数
    operating_cost  = (原经营费公式用 E_effective 算出的结果) × 政策经营费乘数

  · 生产产量和商品投入使用 `E_effective`；**劳动不乘 `E_effective`**；
  · **经营费减免不作用于**：原料采购费、工资、建设现金费、建设材料费；
  · **政策不改变邻接分项的 40% 上限**；效率乘数在**原有效率结果之后**应用，
    **不回写**邻接参数。

═══════════════════════════════════════════════════════════════════
确定性
═══════════════════════════════════════════════════════════════════

抽取用 `engine.rng.Rng`（**不用 Python 的 `random`**，见 `rng.py` 模块头）。
种子直接取存档里的 `rng_state(world)["seed"]`；每个季度按 `(seed, quarter)`
派生一个**独立子发生器**（`rng_for`），于是：

  · 第 `q` 季抽到什么，只由 `(seed, q)` 决定，与"前面抽过几次"无关；
  · 读档不重抽天然成立 —— 即使 `state` 漂了，`(seed, q)` 仍给出同一结果。

═══════════════════════════════════════════════════════════════════
怎么关掉政策（M1 锚点）
═══════════════════════════════════════════════════════════════════

`engine.policies.rng_state(world)`：

  · `{"mode": "random"}`  —— 每季按 25% 抽（★ **正式对局**，
    由 `Config.new_world()` 显式开启）；
  · `{"mode": "regular"}` —— 恒为**常规环境**（乘数全 1）。
    ★ **安全缺省**：手搓的 `WorldState()`（M1 夹具、单点对账、试算回归）
    拿到的是这个，锚点因此不随种子漂。

★ M1 锚点（甲 66 / 乙 215 / 合计 176、结算价 23.52 / 55.222222、期末现金
  1031.68 / 1065.611111）由这条缺省钉死。见 `DEFAULT_POLICY_MODE`
  与 `pipeline._ensure_season_policy()` 的说明。

⚠️ **种子 / 模式存在哪**：`WorldState.policy_state`（正式字段）：

    world.policy_state = {"seed": int, "mode": str, "seq": [季]}

★ **2026-10-16 深夜修正**：本模块最初**没有字段可用**，于是把状态寄存在
   `price_history["__policy__"]` 里 —— 那个容器的序列化是
   `{g: list(v) for g, v in ...}`（为"价格列表"写的），读档时
   `{"seed":…, "seq":[…], "mode":…}` 被 `list()` 拆成了
   `["seed", "seq", "mode"]`，**存档往返直接坏掉**（test_storage 抓到）。

   ⇒ **教训：找不到字段就先加字段，别借用语义不同的容器。**
   现已在 `models.WorldState` 加 `policy_state` 字段并纳入 to_dict/from_dict，
   `from_dict` 还兼容把旧存档里的 `price_history["__policy__"]` 搬进新字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine.models import Policy, WorldState
from engine.rng import DEFAULT_SEED, Rng, rng_for

# ─────────────────────────────────────────────────────────────
# 政策标识
# ─────────────────────────────────────────────────────────────

#: 常规环境 —— 无额外修正
REGULAR = "regular_environment"
#: 生产激励 —— 效率 ×1.10、工资 ×1.15
PRODUCTION_INCENTIVE = "production_incentive"
#: 精益运营 —— 效率 ×0.95、经营费 ×0.90
LEAN_OPERATION = "lean_operation"
#: 民生优先 —— 全部运行地块工资 ×1.10
LIVELIHOOD_FIRST = "livelihood_first"

#: 全部四项（固定顺序 → 抽取与展示都确定）
POLICY_IDS: tuple[str, ...] = (
    REGULAR,
    PRODUCTION_INCENTIVE,
    LEAN_OPERATION,
    LIVELIHOOD_FIRST,
)

#: 每项的概率（基线：四项各 25%）
POLICY_PROBABILITY = 1.0 / len(POLICY_IDS)

#: 政策有效期：**仅当季**（duration 1 季，不累计）
POLICY_DURATION = 1

#: 政策类型标签（沿用 `Policy.policy_type` 字段；旧枚举 subsidy/quota/tariff
#: 是"政策机"占位，与 §11.9.3 的四项对不上，这里用统一标签）。
POLICY_TYPE = "baseline_11_9_3"


@dataclass(frozen=True)
class PolicyDef:
    """一项政策的静态定义（基线 §11.9.3 原表的一行）。"""

    policy_id: str
    name: str
    announcement: str
    efficiency_multiplier: float = 1.0   # 作用于**商品生产地块**
    wage_multiplier: float = 1.0         # 作用于工资
    operating_multiplier: float = 1.0    # 作用于经营费
    wage_scope_all_running: bool = False  # True = 全部运行地块；False = 仅商品生产地块

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "announcement": self.announcement,
            "efficiency_multiplier": self.efficiency_multiplier,
            "wage_multiplier": self.wage_multiplier,
            "operating_multiplier": self.operating_multiplier,
            "wage_scope_all_running": self.wage_scope_all_running,
        }


#: 政策表 —— 数值来自基线 §11.9.3；`config.json` 的 `policies.multipliers`
#: 可以覆盖（`apply_multiplier_overrides()`），但**四项本身不可增删**。
POLICY_TABLE: dict[str, PolicyDef] = {
    REGULAR: PolicyDef(
        policy_id=REGULAR,
        name="常规环境",
        announcement="本季无额外政策修正。",
        # ⚠️ 三个乘数**显式写 1.0**，不靠字段缺省 —— 缺省值一旦被改动，
        #    "常规环境"就会悄悄带上修正（本轮就踩过一次：wage 缺省曾是 1.1，
        #    导致 M1 甲区现金从 1031.68 变 1028.44）。
        efficiency_multiplier=1.0,
        wage_multiplier=1.0,
        operating_multiplier=1.0,
    ),
    PRODUCTION_INCENTIVE: PolicyDef(
        policy_id=PRODUCTION_INCENTIVE,
        name="生产激励",
        announcement="商品生产地块效率 ×1.10、工资 ×1.15。",
        efficiency_multiplier=1.10,
        wage_multiplier=1.15,
    ),
    LEAN_OPERATION: PolicyDef(
        policy_id=LEAN_OPERATION,
        name="精益运营",
        announcement="商品生产地块效率 ×0.95、经营费 ×0.90。",
        efficiency_multiplier=0.95,
        operating_multiplier=0.90,
    ),
    LIVELIHOOD_FIRST: PolicyDef(
        policy_id=LIVELIHOOD_FIRST,
        name="民生优先",
        announcement="全部运行地块工资 ×1.10。",
        wage_multiplier=1.10,
        wage_scope_all_running=True,
    ),
}

#: 政策模式（存在 `rng_state(world)["mode"]`）
MODE_RANDOM = "random"      # 每季按 25% 抽（正式玩法）
MODE_REGULAR = "regular"    # 恒为常规环境（M1 锚点、确定性回归用）

#: ⚠️ 缺省是 **`MODE_REGULAR`（恒常规环境）**，这是**刻意的安全缺省**：
#:
#:   · 基线 §11.9.3 要求正式玩法"每季抽"，但**抽取是玩法，不是数值前提** ——
#:     一个没显式开局的世界（M1 夹具、单点对账、`preview` 回归）不该因为
#:     抽到"精益运营"就少产 5%，那样锚点会随种子漂、且极难查。
#:   · 正式对局由**开局入口**显式 `set_policy_mode(world, MODE_RANDOM)`
#:     （`Config.new_world()` 里已经这么做，见 `loader.py`），
#:     所以"一局 20 季、每季抽"照样成立。
#:   · 直接 `WorldState()` 手搓的世界 = 测试/试算场景 → 安全缺省。
#:
#: ⚠️ 这是一处**判断**，不是基线原话。若用户裁定"缺省就该随机"，
#:    把这里改成 `MODE_RANDOM` 即可（M1 需另配种子，见 tests/test_policies.py）。
DEFAULT_POLICY_MODE = MODE_REGULAR

#: ~~保留键~~ **已废弃**（2026-10-16 深夜）：政策状态已迁到
#: `WorldState.policy_state` 正式字段（见模块头）。这个常量只剩溯源价值，
#: 名字仍被 `models.WorldState.from_dict()` 用来搬运旧存档，**不要再写它**。
_LEGACY_STATE_KEY = "__policy__"


# ─────────────────────────────────────────────────────────────
# 一、乘数查询（纯函数）
# ─────────────────────────────────────────────────────────────


def def_for(policy_id: str) -> PolicyDef:
    """取政策定义。未知 ID **报错**，不静默当成常规环境。"""
    try:
        return POLICY_TABLE[policy_id]
    except KeyError:
        raise KeyError(
            f"未知政策 {policy_id!r}。合法值：{list(POLICY_IDS)}"
        ) from None


def _active_defs(world: WorldState) -> list[PolicyDef]:
    """本季生效的政策定义列表。

    ⚠️ 用 `Policy.is_active(quarter)` 判定，所以"仅当季有效、不累计"
       由 `duration = 1` 直接保证：第 `q` 季抽的政策在第 `q+1` 季自动失效。
    """
    return [def_for(p.policy_id) for p in world.active_policies()]


def active_policy_id(world: WorldState) -> Optional[str]:
    """本季生效的政策 ID；本季还没抽（或已过期）返回 None。

    ⚠️ `None` 与 `REGULAR`（常规环境）**不是一回事**：
       `None` = 还没抽；`REGULAR` = 抽到了"无修正"。两者的乘数都是 1。
    """
    for p in world.active_policies():
        return p.policy_id
    return None


def efficiency_multiplier(world: WorldState, is_commodity_plot: bool) -> float:
    """政策效率乘数（基线 §11.9.3「计算位置」第一式）。

    ⚠️ **只作用于商品生产地块**（`is_commodity_plot=True`）。
       研究（R）与本地服务（J）**不受**效率乘数影响。
    """
    if not is_commodity_plot:
        return 1.0
    m = 1.0
    for d in _active_defs(world):
        m *= d.efficiency_multiplier
    return m


def wage_multiplier(world: WorldState, is_commodity_plot: bool) -> float:
    """政策工资乘数。

    作用范围（基线原文）：
      · 商品生产地块 → 政策表里的 `wage_multiplier`；
      · **全部运行地块**类政策（民生优先）→ 服务与研究也吃；
      · ⚠️ 两者**相乘**而不是取大 —— 规则说"四项各 25%、每季恰好一项"，
        同季不会同时命中两条工资政策，相乘与取大在合法状态里等价，
        但相乘在"存档被人手改坏"时是**乘性叠加**（可解释），不静默吞掉一条。
    """
    m = 1.0
    for d in _active_defs(world):
        if is_commodity_plot or d.wage_scope_all_running:
            m *= d.wage_multiplier
    return m


def operating_multiplier(world: WorldState, is_commodity_plot: bool) -> float:
    """政策经营费乘数。

    ⚠️ **减免不作用于**原料采购费、工资、建设现金费、建设材料费 ——
       所以这个乘数只能乘在 `production.operating_cost()` 的结果上，
       绝不能顺手乘到 `buy_bill` 或建设费上。
    """
    if not is_commodity_plot:
        return 1.0
    m = 1.0
    for d in _active_defs(world):
        m *= d.operating_multiplier
    return m


def is_commodity_plot(recipe_output: Optional[str], cfg=None) -> bool:
    """**商品生产地块**判定（基线 §11.9.3「作用范围」原文）。

    "使用**有可交易商品产出的生产配方**的地块（**不含**研究与本地服务 `J`）"

    判据 = 配方有产出商品，且该商品**在市场配置里**（可交易）。
    服务/研究配方 `output` 为 None → False。
    """
    if not recipe_output:
        return False
    if cfg is None:
        return True          # 没有市场配置时按"有产出商品"判
    return recipe_output in cfg.market


# ─────────────────────────────────────────────────────────────
# 二、抽取（用 engine.rng，确定性）
# ─────────────────────────────────────────────────────────────


def draw_policy(rng: Rng) -> str:
    """从四项中等概率抽一项（各 25%）。**纯抽取，不改任何状态。**"""
    return rng.choice(list(POLICY_IDS))


def make_policy(policy_id: str, quarter: int) -> Policy:
    """按 §11.9.3 的形状造一条**本季政策对象**，**不碰世界状态**。

    ⚠️ 单独抽出来，是为了让"**抽签**"与"**落盘**"能分开（见
       `draw_season_policy()` 与 `pipeline._run()` 的两阶段做法）。
    """
    return Policy(
        policy_id=policy_id,
        policy_type=POLICY_TYPE,
        effective_quarter=quarter,
        duration=POLICY_DURATION,          # 仅当季，不累计
        target_goods="ALL",                # 作用于全部商品
        target_player="ALL",               # 全体玩家（共享层，红线 2）
        parameter=1.0,
        announcement=def_for(policy_id).announcement,
    )


def existing_season_policy(world: WorldState, quarter: Optional[int] = None) -> Optional[Policy]:
    """本季**已经落盘**的政策；没有则 `None`。**只读，不写。**"""
    q = world.quarter if quarter is None else quarter
    for p in world.policies:
        if p.effective_quarter == q:
            return p
    return None


def draw_season_policy(world: WorldState, quarter: Optional[int] = None) -> tuple[Policy, bool]:
    """★ **只抽不落盘**：给出本季政策对象，**世界状态一个字节都不改**。

    返回 `(政策, 是否新抽)`：

      · `(已有那条, False)` —— 本季已经抽过（读档 / 重开界面）→ **不重抽**；
      · `(新抽的,  True)` —— 本季还没抽，这是**新抽**的结果，**仅存在于内存**。

    ══════════════════════════════════════════════════════════════
    ★ 为什么需要它（2026-10-16 修的真实缺陷）
    ══════════════════════════════════════════════════════════════

    `pipeline._run()` 的第 0 步要抽政策（§11.9.3「每季开始」），但**扣款
    在第 5 步**。原先第 0 步直接调 `ensure_season_policy()` **当场落盘**，
    于是"资金不足"抛错时，世界已经被改了：

        world.policies       [] → [本季政策]
        world.policy_state   {} → {seed, mode, seq:[本季]}

    这违反基线 §11.3「**不产生部分状态变更**」（扣款失败不该留下痕迹）。

    ⇒ 现在第 0 步只调**本函数**（纯内存），政策状态由
      `commit_season_policy()` 在**第 5 步扣款成功之后**才提交。

    ⚠️ **两个副作用都要堵住**，它们是两条独立路径：

      ① `world.policies.append(...)` + `_bump_seq(...)`（老代码在此落盘）
      ② `rng_state()` 会**就地补缺省值** → 光是"读一下种子"就已经
         把 `{seed, seq, mode}` 写进了 `world.policy_state`

    所以下面**不走 `rng_state()`**，改用只读的 `_read_seed()`。

    ⚠️ **幂等仍由本函数保证**（红线：重开界面 / 读档不得重抽）：
       `(seed, q)` 派生的子发生器是确定性的，"本季已抽过就返回那条"
       也原样保留 —— 重构前后**抽到的政策完全相同**。
    """
    q = world.quarter if quarter is None else quarter

    existing = existing_season_policy(world, q)
    if existing is not None:
        # ① 本季已有 → 读档/重开界面，**不重抽**
        return existing, False

    mode = _read_mode(world)
    pid = REGULAR if mode == MODE_REGULAR else _draw_for_quarter(world, q)
    return make_policy(pid, q), True


def commit_season_policy(world: WorldState, policy: Optional[Policy]) -> Optional[Policy]:
    """★ **把抽好的政策提交进世界状态**（写 `world.policies` + `policy_state`）。

    由 `pipeline._run()` 在**第 5 步扣款成功之后**调用 ——
    这样任何一步抛错都不会留下"抽了签却没结算"的痕迹（§11.3）。

    ⚠️ **幂等**：本季已经落过盘（`policy.effective_quarter` 那一季已有记录）
       → 不重复登记、`seq` 里也不会出现两条同样的季号。
    """
    if policy is None:
        return None
    q = policy.effective_quarter

    # ⚠️ 必须**先落盘再补 seq**，而且不能简单地"看到本季已有记录就返回"：
    #    流水线第 0 步已经把这条政策**临时**挂进 `world.policies` 了
    #    （本季的效率/工资乘数要按它算），所以走到这里时
    #    `existing_season_policy()` **必然找得到它**。
    #    若在此时 return，`_bump_seq()` 就永远不执行 → `seq` 恒为空列表。
    #    ⇒ 判据改成"**是不是同一条对象**"：
    #       是它本人 → 只补 seq；是别的记录 → 才是真的重复登记，什么都不做。
    existing = existing_season_policy(world, q)
    if existing is None:
        world.policies.append(policy)
    elif existing is not policy:
        # 另一条记录已经占住本季 → 保持原样（真正的重复提交）
        return existing

    _bump_seq(world, q)
    return policy


def ensure_season_policy(world: WorldState, quarter: Optional[int] = None) -> Optional[Policy]:
    """★ **每季开始**抽政策并写进 `world.policies`。返回本季政策（可能为 None）。

    ＝ `draw_season_policy()` + `commit_season_policy()`（**一步到位**）。
    对外语义与重构前**逐字节相同**：调它就会落盘。

    ⚠️ **`pipeline._run()` 不要再用它** —— 流水线里抽签与落盘必须分开
       （抽签在第 0 步、落盘在第 5 步扣款成功之后），否则扣款失败会留下
       状态变更（§11.3）。流水线请用上面那两个函数。

    三条要点（基线 §11.9.3）：

      ① **幂等**：本季已经抽过 → 直接返回已有的那条，**不重抽**。
         这就是"重新打开界面或读取同一季度存档不重新抽取"的落点。
      ② **确定性**：第 `q` 季抽什么只由 `(seed, q)` 决定（独立子发生器），
         与"已经抽过几次"无关 → 读档后序列不会漂。
      ③ **不允许连续抽到相同政策**？——**基线说允许**，所以这里不做去重。
         （测试 `test_policy_allows_consecutive_same` 会固定验证这一点。）

    `policy_mode == "regular"` 时恒为**常规环境**（M1 锚点用）。
    """
    pol, _drawn = draw_season_policy(world, quarter)
    return commit_season_policy(world, pol)


def _read_seed(world: WorldState) -> int:
    """**只读**种子，缺省不落盘（对照 `rng_state()` 的就地补缺省）。

    ⚠️ 这里刻意不复用 `rng_state()`：那个函数会写
       `world.policy_state`（`setdefault`），在"只抽不落盘"的路径上
       它本身就是一处状态泄漏。
    """
    st = world.policy_state
    if isinstance(st, dict):
        seed = st.get("seed")
        if seed is not None:
            return int(seed)
    return DEFAULT_SEED


def _read_mode(world: WorldState) -> str:
    """**只读**政策模式，缺省不落盘（同 `_read_seed`）。"""
    st = world.policy_state
    if isinstance(st, dict):
        mode = st.get("mode")
        if mode in (MODE_RANDOM, MODE_REGULAR):
            return mode
    # 兼容早期存档：状态曾寄存在 price_history["__policy__"]
    legacy = world.price_history.get(_LEGACY_STATE_KEY)
    if isinstance(legacy, dict) and legacy.get("mode") in (MODE_RANDOM, MODE_REGULAR):
        return legacy["mode"]
    return DEFAULT_POLICY_MODE


def _draw_for_quarter(world: WorldState, quarter: int) -> str:
    """第 `quarter` 季抽到的政策 ID。**只读世界状态，不写。**"""
    seed = _read_seed(world)
    sub = rng_for(Rng(seed=seed), "policy", f"q{quarter}")
    return draw_policy(sub)


def rng_state(world: WorldState) -> dict:
    """取（必要时初始化）政策系统的确定性状态。

    返回的 dict 形状::

        {"seed": 20261016, "mode": "random"|"regular", "seq": [已抽的季]}

    ⚠️ 缺键时**就地补上缺省值**（`seed = rng.DEFAULT_SEED`、
       `mode = DEFAULT_POLICY_MODE`），所以对同一个世界反复调用是幂等的。

    ★ 2026-10-16 深夜：状态**已迁到 `WorldState.policy_state` 正式字段**。
      此前寄存在 `price_history["__policy__"]`，会被 `to_dict()` 的
      `{g: list(v) ...}` 拆成键名列表 —— 存档往返坏掉。别搬回去。
    """
    st = world.policy_state
    if not isinstance(st, dict):
        st = {}
        world.policy_state = st
    st.setdefault("seed", DEFAULT_SEED)
    st.setdefault("seq", [])
    if st.get("mode") not in (MODE_RANDOM, MODE_REGULAR):
        st["mode"] = DEFAULT_POLICY_MODE
    return st


def policy_mode(world: WorldState) -> str:
    """取政策模式（`"random"` 每季抽 / `"regular"` 恒常规环境）。"""
    return rng_state(world)["mode"]


def set_policy_mode(world: WorldState, mode: str) -> None:
    """显式设置政策模式（`"random"` / `"regular"`）。

    ⚠️ **只影响"以后还没抽的季"** —— 已经抽过并落进 `world.policies`
       的季**不会**被重抽（否则 M1 这种跑过的局面会被追溯改数）。
    """
    if mode not in (MODE_RANDOM, MODE_REGULAR):
        raise ValueError(
            f"未知政策模式 {mode!r}，合法值：{MODE_RANDOM!r} / {MODE_REGULAR!r}"
        )
    rng_state(world)["mode"] = mode


def set_seed(world: WorldState, seed: int) -> None:
    """显式设置本局种子（不传则用 `rng.DEFAULT_SEED`）。

    ⚠️ 同 `set_policy_mode`：**不追溯**已抽过的季。
    """
    rng_state(world)["seed"] = int(seed)


def _bump_seq(world: WorldState, quarter: int) -> None:
    """记下"抽到第几季了"（存档往返、报告展示用）。"""
    seq = rng_state(world)["seq"]
    if not seq or seq[-1] != quarter:
        seq.append(quarter)


# ─────────────────────────────────────────────────────────────
# 开局接入（把 config 的政策设置装到世界上）
# ─────────────────────────────────────────────────────────────


def apply_config_mode(world: WorldState, cfg) -> Optional[str]:
    """把 `config.json` 的 `policies.mode` 装到世界上。**正式对局开局调它。**

        from engine import policies as POL
        world = cfg.new_world()
        POL.apply_config_mode(world, cfg)   # ← 之后每季都会抽政策

    ⚠️ `cfg.policy_mode` 为 None（配置里没写）时**什么都不做**，
       于是落到 `policies.py` 的安全缺省 `regular`（恒常规环境）。
       这是刻意的：**没配就是安全**，绝不静默替玩家开一个随机的局。
    """
    apply_multiplier_overrides(getattr(cfg, "policy_multipliers", None))
    mode = getattr(cfg, "policy_mode", None)
    if mode:
        set_policy_mode(world, mode)
    return mode


def new_game_world(cfg) -> WorldState:
    """★ **正式开局**的世界：每季抽政策（§11.9.3）。

    与 `cfg.new_world()` 的唯一差别就是这个 —— 把"玩法世界"和
    "测试/对账世界"分开，免得锚点随抽签漂。
    """
    world = cfg.new_world()
    apply_config_mode(world, cfg)
    return world


def draw_count(world: WorldState) -> int:
    """本局已经抽过几季（= `world.policies` 的条数）。"""
    return len(world.policies)


# ─────────────────────────────────────────────────────────────
# 三、配置覆盖（只覆盖乘数数值，不增删政策）
# ─────────────────────────────────────────────────────────────


def apply_multiplier_overrides(raw: Optional[dict]) -> list[str]:
    """把 `config.json` 里 `policies.multipliers` 的数值覆盖到 `POLICY_TABLE`。

    ⚠️ **只允许覆盖乘数**（0.95 / 0.90 / 1.10 / 1.15 这类平衡数值）。
       四项政策本身、作用范围、概率都是**规则**，改它们要先改基线 §11.9.3。

    返回被覆盖的 `"政策.字段"` 列表（供 `validate` 打印）。
    """
    if not raw:
        return []
    changed: list[str] = []
    for pid, fields in raw.items():
        if pid.startswith("_"):
            continue
        if pid not in POLICY_TABLE:
            raise KeyError(
                f"config.policies.multipliers 里有未知政策 {pid!r}。\n"
                f"  合法值：{list(POLICY_IDS)}\n"
                f"  ⚠️ §11.9.3 的四项是规则，不能在配置里增删。"
            )
        base = POLICY_TABLE[pid]
        if not isinstance(fields, dict):
            raise ValueError(f"config.policies.multipliers.{pid} 应是对象")
        new = PolicyDef(
            policy_id=base.policy_id,
            name=base.name,
            announcement=base.announcement,
            efficiency_multiplier=float(
                fields.get("efficiency_multiplier", base.efficiency_multiplier)),
            wage_multiplier=float(fields.get("wage_multiplier", base.wage_multiplier)),
            operating_multiplier=float(
                fields.get("operating_multiplier", base.operating_multiplier)),
            wage_scope_all_running=base.wage_scope_all_running,
        )
        POLICY_TABLE[pid] = new
        changed.append(pid)
    return changed


def reset_table() -> None:
    """把 `POLICY_TABLE` 恢复成基线 §11.9.3 的原值（测试用）。"""
    POLICY_TABLE[REGULAR] = PolicyDef(
        policy_id=REGULAR, name="常规环境",
        announcement="本季无额外政策修正。",
        efficiency_multiplier=1.0, wage_multiplier=1.0, operating_multiplier=1.0,
    )
    POLICY_TABLE[PRODUCTION_INCENTIVE] = PolicyDef(
        policy_id=PRODUCTION_INCENTIVE, name="生产激励",
        announcement="商品生产地块效率 ×1.10、工资 ×1.15。",
        efficiency_multiplier=1.10, wage_multiplier=1.15,
    )
    POLICY_TABLE[LEAN_OPERATION] = PolicyDef(
        policy_id=LEAN_OPERATION, name="精益运营",
        announcement="商品生产地块效率 ×0.95、经营费 ×0.90。",
        efficiency_multiplier=0.95, operating_multiplier=0.90,
    )
    POLICY_TABLE[LIVELIHOOD_FIRST] = PolicyDef(
        policy_id=LIVELIHOOD_FIRST, name="民生优先",
        announcement="全部运行地块工资 ×1.10。",
        wage_multiplier=1.10, wage_scope_all_running=True,
    )


def table_snapshot() -> list[dict]:
    """政策表快照（前端 / 报告展示用）。"""
    return [POLICY_TABLE[pid].to_dict() for pid in POLICY_IDS]
