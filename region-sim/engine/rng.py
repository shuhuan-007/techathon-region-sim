"""确定性随机数发生器（Deterministic RNG）。

═══════════════════════════════════════════════════════════════════
为什么不能用 Python 自带的 `random`
═══════════════════════════════════════════════════════════════════
本项目是**确定性的账本机器**（《规则基线》§0）：

    同样的输入 必须 得到同样的输出。

`random` 模块的全局状态会被任何一处调用推进，于是：

  · 今天跑出 215，明天某个函数多抽一次随机数，就变成别的值；
  · M1 的验收锚点（乙区生产总支出 215 / 增加值合计 176）会莫名其妙对不上；
  · 复盘 bug 时无法重放，因为你不知道当时抽了什么。

所以全项目**禁止** `import random`（纪律见《项目架构-总》§七）。

用本模块代替：

    from engine.rng import Rng

    rng = Rng(seed=20261016)     # 种子写死 → 永远同一串数
    rng.random()                 # 0.0 ~ 1.0
    rng.randint(1, 6)            # 1 ~ 6，含两端
    rng.choice(["甲", "乙"])

═══════════════════════════════════════════════════════════════════
实现选择：为什么不直接用 `random.Random(seed)`
═══════════════════════════════════════════════════════════════════
`random.Random(seed)` 本身是可复现的（它不碰全局状态），但它有两个问题：

  1. **跨版本不保证稳定**。CPython 的 Mersenne Twister 初始化算法
     曾变动过；而且 `random.shuffle` 等方法的实现细节属于版本内部
     行为。升级 Python 后历史存档可能重放不出同样的结果。
  2. **不能脱离存档携带**。多人/存档场景下，我们希望"把 RNG 状态
     存进 JSON，下一季接着抽"——`random.Random` 的状态是内部元组，
     不可序列化。

这里用一个**自己实现的小型发生器**（PCG 风格，64 位线性同余 +
XSH-RR 输出置换），全部状态就是一个整数：

  · 状态 = 一个 int → 直接进 JSON 存档，天然可序列化；
  · 算法由本文件写死 → 换 Python 版本结果不变；
  · 统计质量足够游戏使用（远好于"线性同余直接用高位"的朴素写法）。

⚠️ 这是**统计用途**的发生器，不是密码学安全的（不要拿它生成密钥）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────
# 常量：取自 PCG 系列的推荐参数（64 位 LCG 乘子与增量）
# ─────────────────────────────────────────────────────────────

_MULTIPLIER = 6364136223846793005
_INCREMENT = 1442695040888963407

# 64 位掩码：Python 的 int 无限精度，必须手动截断到 64 位，
# 否则状态会无限增长（且与 C 实现的行为不一致）。
_MASK64 = (1 << 64) - 1

# 2^32，用于把 64 位输出折叠成 32 位
_MASK32 = (1 << 32) - 1

DEFAULT_SEED = 20261016


@dataclass
class Rng:
    """可序列化的确定性随机数发生器。

    用法::

        rng = Rng(seed=1)
        rng.random()          # 浮点
        rng.randint(0, 9)     # 整数
        rng.choice(["a", "b"])  # 取一个

        data = rng.to_dict()  # 存档
        rng2 = Rng.from_dict(data)   # 读档，后续序列完全一致

    注意：`state` 是**唯一**的可变状态。只要两个 Rng 的 state 相同，
    之后产生的序列就逐位相同。
    """

    seed: int = DEFAULT_SEED
    state: int = field(default=0)

    def __post_init__(self) -> None:
        # 约定：state == 0 表示"尚未推进"，用种子做一次初始化打散。
        # 这样 Rng(seed=5) 与 Rng(seed=5, state=0) 等价，
        # 而从存档恢复时 state 必为非 0，不会重复初始化。
        if self.state == 0:
            self.state = self._scramble(self.seed)

    # ── 内部：状态推进与输出 ──────────────────────────────

    @staticmethod
    def _scramble(seed: int) -> int:
        """把任意整数种子打散成一个"看起来随机"的非 0 初始状态。

        连续种子（1, 2, 3...）若直接用作状态，前几个输出会高度相关。
        这里先做两轮 LCG 混合，让相邻种子彻底分开。
        """
        s = (seed + _INCREMENT) & _MASK64
        s = (s * _MULTIPLIER + _INCREMENT) & _MASK64
        s = (s * _MULTIPLIER + _INCREMENT) & _MASK64
        return s if s != 0 else _INCREMENT

    def _next_u32(self) -> int:
        """推进状态一次，返回一个 32 位无符号整数。

        两步（PCG 的标准做法）：
          1. 线性同余推进 64 位状态；
          2. 输出置换（XSH-RR）：把高位异或到低位，再按高位旋转。
             直接取低位会有明显周期性问题，必须做这步置换。
        """
        self.state = (self.state * _MULTIPLIER + _INCREMENT) & _MASK64
        x = self.state

        # XSH-RR 输出函数
        xorshifted = (((x >> 18) ^ x) >> 27) & _MASK32
        rot = (x >> 59) & 31  # 取高 5 位作为旋转量（0~31）
        return ((xorshifted >> rot) | (xorshifted << ((-rot) & 31))) & _MASK32

    # ── 公开：取随机数 ────────────────────────────────────

    def random(self) -> float:
        """返回 [0.0, 1.0) 的浮点数。

        用 32 位输出除以 2^32。注意**不是**除以 2^32 - 1：
        后者会产生恰好等于 1.0 的结果，破坏半开区间约定。
        """
        return self._next_u32() / float(_MASK32 + 1)

    def randint(self, low: int, high: int) -> int:
        """返回 [low, high] 的整数，**两端都含**（与 `random.randint` 一致）。

        用取模会引入偏置（modulo bias）：2^32 不能整除区间长度时，
        靠前的数会略多一点。这里用**拒绝采样**剔除偏置——
        对游戏数值影响虽小，但确定性项目里"同样的输入同样的输出"
        要求我们不能有这种隐式偏差。
        """
        if low > high:
            raise ValueError(f"randint: low({low}) 不能大于 high({high})")
        span = high - low + 1
        if span <= 0:
            raise ValueError("randint: 区间长度溢出")

        # 拒绝阈值：丢弃 >= limit 的样本，使剩余部分是 span 的整数倍
        limit = (_MASK32 + 1) - ((_MASK32 + 1) % span)
        while True:
            v = self._next_u32()
            if v < limit:
                return low + (v % span)

    def choice(self, seq):
        """从非空序列中等概率取一个元素。"""
        if not seq:
            raise ValueError("choice: 不能从空序列中取元素")
        return seq[self.randint(0, len(seq) - 1)]

    def shuffle(self, seq) -> None:
        """**就地**打乱序列（Fisher–Yates）。

        ⚠️ 就地修改，与 `random.shuffle` 行为一致，不返回新列表。
        """
        for i in range(len(seq) - 1, 0, -1):
            j = self.randint(0, i)
            seq[i], seq[j] = seq[j], seq[i]

    # ── 存档 ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        """导出为可 JSON 序列化的字典。整数在 JSON 里是安全的。"""
        return {"seed": self.seed, "state": self.state}

    @classmethod
    def from_dict(cls, data: dict) -> "Rng":
        """从存档恢复。恢复后产生的序列与原对象完全一致。"""
        return cls(seed=int(data["seed"]), state=int(data["state"]))

    def clone(self) -> "Rng":
        """复制一份，用于"试算"（preview）时不污染真实序列。"""
        return Rng(seed=self.seed, state=self.state)


def rng_for(rng: Rng, *labels: str) -> Rng:
    """按标签派生一个**独立**的子发生器（可选工具）。

    用途：不希望某个模块的抽取次数影响另一个模块。例如

        weather_rng = rng_for(rng, "weather", "q3")
        market_rng  = rng_for(rng, "market", "q3")

    两者互不干扰：天气多抽一次，不影响市场序列。
    标签用稳定哈希（不用内置 `hash()`——它带随机盐，跨进程不稳定）。
    """
    h = 1469598103934665603  # FNV-1a 64 位偏移基准
    for label in labels:
        for ch in label:
            h ^= ord(ch)
            h = (h * 1099511628211) & _MASK64  # FNV 质数
    return Rng(seed=(h ^ rng.state) & _MASK64)
