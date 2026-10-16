"""世界时钟（World Clock）—— 季度推进的唯一来源。

═══════════════════════════════════════════════════════════════════
为什么不能用 `datetime.now()`
═══════════════════════════════════════════════════════════════════
与 `engine/rng.py` 同源的理由（《项目架构-总》§七 纪律 2）：

    同一个存档，今天跑和明天跑，必须得到同一份月报。

`datetime.now()` 把**真实的挂钟时间**引进了计算。一旦某处规则依赖它：

  · 昨天跑出的 H 和今天的对不上，M1 锚点漂移；
  · 存档重放（读档 → 再算一次）结果不同，玩家会认为程序在骗人；
  · 测试不可复现，CI 上偶发失败。

所以引擎里**禁止**出现 `datetime.now()` / `time.time()`。
需要"现在是第几季"就查 `WorldState.quarter`；
需要"这一季是哪一年"就用本模块换算。

═══════════════════════════════════════════════════════════════════
本模块提供的换算
═══════════════════════════════════════════════════════════════════
游戏内只有**季度序号**这一个时间量（从 1 开始，单调递增）。
展示层（UI / AI 文案）需要"年份 + 季节"这种人话，由本模块负责，
**不反向影响引擎**：

    quarter = 1   → 第 1 年 · 春季
    quarter = 2   → 第 1 年 · 夏季
    quarter = 5   → 第 2 年 · 春季
    quarter = 7   → 第 2 年 · 秋季

⚠️ 这纯粹是**展示层换算**，不参与任何经济计算。
   换句话说：把 season 改个名字，不会影响任何数字。
"""

from __future__ import annotations

from dataclasses import dataclass

# 一年四季，按季度顺序排列。
# 注意：这是**游戏内**的季节，与现实日历无关（现实一季 = 游戏一季纯属巧合）。
SEASONS: tuple[str, ...] = ("春季", "夏季", "秋季", "冬季")

SEASON_EN: tuple[str, ...] = ("Spring", "Summer", "Autumn", "Winter")

# 起始年份。开局 quarter=1 即"第 1 年 春季"。
START_YEAR = 1

# 一局默认总季数。
# ✅ 2026-10-16 用户拍板：**20 季**（《规则基线》§11.9.2）。
#    ⚠️ 高技术路线总工期 14 季，20 季局里"开局即投"才勉强走完——**已知取舍，不再复议**。
#    这里是**缺省展示值**：真实值由 config 的 `world.total_quarters` 提供
#    （`Config.new_world()` 会把它写进 `WorldState`）。只有拿不到 config 时
#    才用它兜底，仅用于 UI 显示"第 7 / 20 季"。
DEFAULT_TOTAL_QUARTERS = 20


def season_of(quarter: int) -> str:
    """季度序号 → 中文季节名。

    quarter=1 → 春季，quarter=5 → 春季（第 2 年），依此类推。
    """
    return SEASONS[season_index(quarter)]


def season_index(quarter: int) -> int:
    """季度序号 → 0~3 的季节下标（0=春）。"""
    _require_valid_quarter(quarter)
    return (quarter - 1) % 4


def year_of(quarter: int, start_year: int = START_YEAR) -> int:
    """季度序号 → 游戏内年份（从 `start_year` 起算）。"""
    _require_valid_quarter(quarter)
    return start_year + (quarter - 1) // 4


def quarter_in_year(quarter: int) -> int:
    """季度序号 → 本年度内的第几季（1~4）。"""
    _require_valid_quarter(quarter)
    return (quarter - 1) % 4 + 1


def format_quarter(quarter: int, start_year: int = START_YEAR) -> str:
    """季度序号 → 中文展示串，如 `第 2 年 · 夏季`。

    供 UI 与 AI 文案使用；引擎内部一律用整数 quarter。
    """
    return f"第 {year_of(quarter, start_year)} 年 · {season_of(quarter)}"


def format_progress(quarter: int, total: int = DEFAULT_TOTAL_QUARTERS) -> str:
    """季度序号 → 进度串，如 `第 7 / 40 季`（UI 顶栏用）。

    `total <= 0` 视为"未设总季数"，退化为 `第 7 季`。
    """
    _require_valid_quarter(quarter)
    if total and total > 0:
        return f"第 {quarter} / {total} 季"
    return f"第 {quarter} 季"


def _require_valid_quarter(quarter: int) -> None:
    """季度序号必须是从 1 开始的整数。

    ⚠️ 这里刻意**宽容地接受** float 形式的整数（如 3.0），
       因为从 JSON 读回来的数字可能是 float；但拒绝 3.5。
    """
    if isinstance(quarter, bool) or not isinstance(quarter, (int, float)):
        raise TypeError(f"quarter 必须是整数，收到 {type(quarter).__name__}: {quarter!r}")
    if isinstance(quarter, float):
        if not quarter.is_integer():
            raise ValueError(f"quarter 必须是整数，收到 {quarter!r}")
        quarter = int(quarter)
    if quarter < 1:
        raise ValueError(f"quarter 从 1 开始，收到 {quarter}")


@dataclass(frozen=True)
class QuarterLabel:
    """一个季度的完整展示信息（不可变，可安全缓存/传递）。

    UI 想要的一切时间文案都在这里，避免每个界面各写一套换算。
    """

    quarter: int
    year: int
    season: str
    season_en: str
    quarter_in_year: int

    @classmethod
    def of(cls, quarter: int, start_year: int = START_YEAR) -> "QuarterLabel":
        _require_valid_quarter(quarter)
        idx = season_index(quarter)
        return cls(
            quarter=quarter,
            year=year_of(quarter, start_year),
            season=SEASONS[idx],
            season_en=SEASON_EN[idx],
            quarter_in_year=quarter_in_year(quarter),
        )

    def __str__(self) -> str:
        return f"第 {self.year} 年 · {self.season}"


def next_quarter(quarter: int) -> int:
    """推进一季。

    ⚠️ 这只是**纯函数式的序号 +1**，不做任何状态变更。
       真正的推进由 `advance()` 在流水线末尾完成
       （《已实现规则-流水账》第 11 步"结转"）。
       把它单独抽出来是为了让"推进"这件事只有一个写法，
       避免某处写 `quarter += 1`、另一处写 `quarter = quarter + 1`。
    """
    _require_valid_quarter(quarter)
    return quarter + 1


def is_game_over(quarter: int, total: int = DEFAULT_TOTAL_QUARTERS) -> bool:
    """是否已到达总季数（用于世界状态机：进行 → 结束）。

    `total <= 0` 表示不限季数，永远不结束。
    """
    _require_valid_quarter(quarter)
    if not total or total <= 0:
        return False
    return quarter > total
