"""会话 —— 三状态机 + **每局一把串行锁**。

依据《规则基线》§1.3.1（限制并发）与《项目架构-总》§三 / §七。

═══════════════════════════════════════════════════════════════════
★ 限制并发（2026-10-16 定案）
═══════════════════════════════════════════════════════════════════

**同一局同一时刻只允许一个请求在跑，全部串行。**

    game_id → Lock       一个请求进 → 拿锁 → 算完 → 释放 → 下一个才能进

为什么必须这样：

  · `advance()` 会**整体改写状态**。两个请求同时跑 = 状态被交叉覆盖，账必错。
  · 结算必须"**一次算完**"。中途被打断会留下半个季度，比崩溃更难查。

四条落地要点（§1.3.1）：

  1. 锁的粒度是**每局一把**（按 game_id），不是全局一把 —— 不同局互不阻塞
  2. **提交计划也走串行**：`plans` 只增不改，配合锁天然满足"只能提交一次"
  3. 单人期 `len(plans) == 1` 立即结算，**锁内**完成，不留等待态
  4. 锁在 `try/finally` 里释放 —— 异常也必须放锁，否则整局卡死

═══════════════════════════════════════════════════════════════════
⚠️ 锁**只活在服务层**
═══════════════════════════════════════════════════════════════════
`engine/` 是纯函数、**不知道锁的存在**。这样引擎仍能脱离 Web 单独跑。

⚠️ 这**不是**"防并发写库"那类问题（《项目架构-总》§八已澄清：
   行动阶段是收集、结算是单点触发）。这里挡的是
   **同一个 HTTP 端点被重复点两次 / 重放**，属于最朴素的一类并发。

⚠️ 与"plans 只增不改"的关系：§3.4 的"只能提交一次"是**业务层**约束，
   本节是**传输层**约束，两者互不替代。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                       # 只为类型标注，运行时不 import
    from storage.base import Storage

from engine import report, validate
from engine.loader import Config
from engine.models import GameState, Plan, PlotType, SessionPhase
from engine.pipeline import QuarterReport, SettlementError, advance, preview

# ⚠️ `PlanRejected` 在这里**故意显式重导出**：调用方只需要 import
#    `server.session` 就能同时 catch `Conflict` 与 `PlanRejected`，
#    不必再知道它来自 `engine.validate`（服务层要转发的东西越少越好）。
from engine.validate import PlanRejected  # noqa: F401  (re-export)


class Conflict(Exception):
    """请求与当前会话状态冲突（不在行动阶段、已提交过等）。"""


@dataclass
class GameSession:
    """一局游戏的会话。**每个 game_id 一个实例，每个实例一把锁。**

    ⚠️ 单人 = 多人的特例：`len(plans) == len(players)` 在单人时
       提交即成立，**没有任何等待逻辑**。
    """

    state: GameState
    cfg: Config
    player_ids: list[str] = field(default_factory=list)
    plans: dict[str, Plan] = field(default_factory=dict)
    phase: SessionPhase = SessionPhase.COLLECTING

    # ★ 每局一把锁。`repr=False` 避免打印时出现无意义的锁对象。
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── 存档（可选）────────────────────────────────────────
    # 给了就在**结算完、锁内**自动存一次（《项目架构-总》§八）。
    # ⚠️ 不给也能跑（测试与 M1 对账就不需要磁盘）。
    storage: Optional["Storage"] = field(default=None, repr=False)
    game_id: str = ""

    def __post_init__(self) -> None:
        if not self.player_ids:
            self.player_ids = self.state.player_ids
        if self.storage is not None and not self.game_id:
            raise ValueError(
                "给了 storage 就必须给 game_id —— 否则不知道往哪一局存。"
            )

    # ── 读 ────────────────────────────────────────────────

    def state_for(self, player_id: str) -> dict:
        """给前端的只读状态快照（**已算好的数字**，前端零计算）。

        ✅ 2026-10-16：改为调用 `engine.report.build_state()`。
        此前这里手搓了一个六字段的小字典，既盖不住《界面要显示的变量清单》
        要的 40 项，也**绕开了"只转发"的原则**（数字形状写在服务层里）。
        现在服务层只做一件事：把**已提交的计划**递进去。

        ⚠️ 读也要拿锁：否则可能读到"结算到一半"的状态。
        """
        with self.lock:
            return report.build_state(
                self.state,
                player_id,
                self.cfg,
                plan=self.plans.get(player_id),      # 没提交 → None（不编数字）
                phase=self.phase.value,
            )

    def map_for(self, player_id: str) -> dict:
        """整张地图（含别人的格子）—— 前端画地图用。**只读。**"""
        with self.lock:
            return report.build_map(self.state, player_id)

    def build_quote(self, player_id: str, builds: list) -> dict:
        """一组**未提交**建设指令的报价（引擎公式真算 → 前端零计算）。

        原则：前端只发形状（`plan.builds` 的原生 dict），**一切判定都在引擎**：
        能报价 = 引擎 `build_cash_plan` 真跑；报不了 = 原样
        转发异常消息（缺产出/在建中……），前端只显示、再也不拦。

        ⚠️ 这是**试算**，与结算第 5 步**不是同一次扣款**，不登记名额、
           不扣任何钱。真实额度仍以结算时 `pipeline` 算的为准（基线 §11.3）。
        """
        from engine import construction as _C
        from engine.models import BuildOrder

        with self.lock:
            player = self.state.player(player_id)
            orders: list[BuildOrder] = []
            for b in builds or []:
                orders.append(BuildOrder(
                    plot_id=b["plot_id"],
                    plot_type=PlotType(b["plot_type"]),
                    target_level=int(b["target_level"]),
                    product=b.get("product"),
                ))
            plan = Plan(player_id=player_id, builds=orders)

            quote_rows: list[dict] = []
            total = 0.0
            error: Optional[str] = None
            discounted_any = False
            try:
                rows = _C.build_cash_plan(plan, player, self.cfg)
                for r in rows:
                    this_cash = float(r["cash"])
                    quote_rows.append({
                        "plot_id": r["plot_id"],
                        "plot_type": r["plot_type"].value,
                        "list_cash": float(r["list_cash"]),
                        "cash": this_cash,
                        "discounted": bool(r["discounted"]),
                        "consumed_after": int(r["consumed_after"]),
                    })
                    total += this_cash
                    if r["discounted"]:
                        discounted_any = True
            except Exception as e:  # noqa: BLE001
                # 引擎抛什么就原样转给玩家（不能屏蔽真原因）；到这里
                # 的都是**业务错误**（缺产出、在建中、没这工程……）
                error = f"{type(e).__name__}: {e}"

            return {
                "rows": quote_rows,
                "total": round(total, 6),
                "discount_rate": _C._discount_rate(self.cfg),
                "discounted_any": discounted_any,
                "error": error,
            }

    def validate_only(self, plan: Plan, player_id: str) -> list:
        """只校验，不试算、不落任何状态（给 `POST /api/validate`）。

        ⚠️ **只读**：调 `engine.validate.validate_plan` 拿问题列表就返回。
           不提交、不入 `plans`、不改 `phase`、不落存档。
        """
        with self.lock:
            return validate.validate_plan(
                plan, self.state.player(player_id), self.cfg, player_id
            )

    # ── 写（全部串行） ────────────────────────────────────

    def submit(self, player_id: str, plan: Plan) -> None:
        """提交计划。**只增不改** —— 第二次提交直接拒绝。

        ⚠️ **提交时就要校验计划**（2026-10-16 补上的一环）。
        在此之前 `submit()` 只查"是不是重复提交"，于是一个地块 ID 不存在、
        或买了不存在商品的计划能一路走到结算第 5 步才炸 ——
        **报错点离犯错点太远**，玩家不知道是哪一步错了。

        两类错误**刻意分开**：

          · `Conflict`      —— 与**会话状态**冲突（不在行动阶段 / 已提交过）
          · `PlanRejected`  —— **计划本身**不合法（带结构化 `issues` 列表）

        分开的理由：前者要玩家"等/换个时机"，后者要玩家"改计划"，
        服务层与前端要做的事完全不同。
        """
        with self.lock:                       # ★ 提交也串行
            if self.phase is not SessionPhase.COLLECTING:
                raise Conflict(
                    f"当前不在行动阶段（{self.phase.value}），不能提交计划"
                )
            if player_id in self.plans:
                raise Conflict(
                    f"{player_id} 本季已提交，不能修改。"
                    f"（plans 只增不改：要改只能等下一季）"
                )
            if plan.player_id != player_id:
                raise Conflict(
                    f"计划里的 player_id ({plan.player_id!r}) "
                    f"与提交者 ({player_id!r}) 不一致"
                )

            # ★ 计划校验（§11.3）。**只读**，不改状态；不合格就不入库。
            validate.require_valid(
                plan, self.state.player(player_id), self.cfg, player_id
            )

            self.plans[player_id] = plan

    def end_quarter(self, player_id: str) -> Optional[QuarterReport]:
        """结束季度。**收齐所有计划才结算**（红线 3）。"""
        with self.lock:                       # ★ 整个结算在锁内
            if self.phase is not SessionPhase.COLLECTING:
                raise Conflict(f"当前不在行动阶段（{self.phase.value}）")
            if player_id not in self.plans:
                raise Conflict(f"{player_id} 还没提交计划，不能结束季度")
            if len(self.plans) < len(self.player_ids):
                # 多人：等其他人。单人时这个分支永远不会走到。
                return None
            return self._resolve_unlocked()

    def simulate(self, plan: Plan) -> QuarterReport:
        """试算（= AI 与 UI 用的 `preview`）。**不改状态。**

        ⚠️ 拿锁是为了读到一致的状态快照，不是为了串行化试算本身
           （试算在副本上跑，本来就不冲突）。
        """
        with self.lock:
            return preview(self.state, {plan.player_id: plan}, self.cfg)

    # ── 内部 ──────────────────────────────────────────────

    def _resolve_unlocked(self) -> QuarterReport:
        """真结算。⚠️ **调用方必须已持有 `self.lock`。**

        命名带 `_unlocked` 后缀是刻意的：看到这个名字就知道
        "这里没有自己拿锁"，避免以后有人直接调用而漏掉锁。
        """
        self.phase = SessionPhase.RESOLVING
        try:
            advance(self.state, self.plans, self.cfg)
        except SettlementError:
            # 结算失败 → 退回行动阶段，让玩家改计划重来。
            # ⚠️ 不改状态：advance 在扣款前就会抛错，所以状态是干净的。
            self.phase = SessionPhase.COLLECTING
            raise

        self.plans.clear()
        self.phase = SessionPhase.REPORT
        self._save_unlocked()          # ★ 结算完立刻全量快照（锁内）
        first = sorted(self.state.raw_reports)[0]
        return self.state.raw_reports[first]

    def _save_unlocked(self) -> None:
        """落盘。⚠️ **调用方必须已持有 `self.lock`**（与 `_resolve_unlocked` 同理）。

        ⚠️ 放在**锁内**是《项目架构-总》§八 的要求：
           两个请求同时结算 → 交叉覆盖 → 存档写成一锅粥。
           原子替换只能保证"文件不半截"，**保证不了"内容对应哪一季"**。

        ⚠️ 没配 `storage` 时**静默跳过** —— 测试与 M1 对账不需要磁盘。
        """
        if self.storage is None:
            return
        self.storage.save(self.game_id, self.state)

    def save_now(self) -> None:
        """手动存一次（给"不结算也想存"的场景，如退出前）。**拿锁。**"""
        with self.lock:
            self._save_unlocked()

    def start_next_quarter(self) -> None:
        """月报看完 → 回到行动阶段（§三 的循环）。"""
        with self.lock:
            if self.phase is not SessionPhase.REPORT:
                raise Conflict(f"只有月报阶段才能进入下一季（当前 {self.phase.value}）")
            self.phase = SessionPhase.COLLECTING
