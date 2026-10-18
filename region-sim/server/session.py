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
import copy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:                       # 只为类型标注，运行时不 import
    from storage.base import Storage

from engine import report, validate
from engine.clock import is_game_over
from engine.loader import Config
from engine.models import GameState, Plan, PlotType, SessionPhase, WorldPhase
from engine.pipeline import QuarterReport, SettlementError, advance, preview
from server import slots as _slots

# ⚠️ `PlanRejected` 在这里**故意显式重导出**：调用方只需要 import
#    `server.session` 就能同时 catch `Conflict` 与 `PlanRejected`，
#    不必再知道它来自 `engine.validate`（服务层要转发的东西越少越好）。
from engine.validate import PlanRejected  # noqa: F401  (re-export)


class Conflict(Exception):
    """请求与当前会话状态冲突（不在行动阶段、已提交过等）。"""


def _report_view_from_dict(d: dict):
    """把 `state.report` 的 dict 适配成 `engine.news.news_facts` 需要的只读对象。

    读档后 `raw_reports` 为空，但 `state.report` 一直在；没有这层适配时
    `/api/narratives` 的新闻路会永远拿不到 facts（ok:false 或 null）。
    这里只做形状转换，**不补、不改任何数字**。
    """
    if not isinstance(d, dict):
        return None
    prod = []
    for item in d.get("production") or []:
        if not isinstance(item, dict):
            continue
        try:
            ptype = PlotType(item.get("plot_type"))
        except (TypeError, ValueError):
            ptype = PlotType.AGRICULTURE
        prod.append(SimpleNamespace(
            plot_id=item.get("plot_id"),
            plot_type=ptype,
            level=int(item.get("level") or 0),
            product=item.get("product"),
            output_goods=item.get("output_goods"),
            output_qty=float(item.get("output_qty") or 0.0),
        ))
    food = d.get("food")
    return SimpleNamespace(
        quarter=int(d.get("quarter") or 0),
        player_id=d.get("player_id") or "",
        prices=d.get("prices") or {},
        production=prod,
        food=SimpleNamespace(**food) if isinstance(food, dict) else None,
        policy=d.get("policy"),
        cash_before=float(d.get("cash_before") or 0.0),
        cash_after=float(d.get("cash_after") or 0.0),
        total_deduction=float(d.get("total_deduction") or 0.0),
        materials_cost=float(d.get("materials_cost") or 0.0),
        wages=float(d.get("wages") or 0.0),
        operating=float(d.get("operating") or 0.0),
        construction_cash=float(d.get("construction_cash") or 0.0),
        sales_revenue=float(d.get("sales_revenue") or 0.0),
        wage_tax_refund=float(d.get("wage_tax_refund") or 0.0),
        value_added=float(d.get("value_added") or 0.0),
        production_incentive=float(d.get("production_incentive") or 0.0),
        net_cash=float(d.get("net_cash") or 0.0),
    )


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

    # ── ★ AI 叙事（新闻 + 财报解读）按季缓存 ─────────────────
    # (quarter, kind) → 结果 dict（kind ∈ {"news", "report"}）。
    #
    # 三条承诺（用户 2026-10-17 拍板：生成时机＝**结算后**进下一季、
    # 玩家行动**前**生成；不是结算当中）：
    #   ① 同一季**只调一次**模型——生成过就缓存住，刷新不重复烧配额；
    #   ② **不在结算锁里**调模型——生成发生在 `/state` 的**锁外**路径
    #      （见 `state_for` / `_narratives_for`），结算永不被对话模型卡；
    #   ③ 失败**如实标出**——dict 里带 ok:false + error + detail，前端显示。
    #
    # ⚠️ 这是**易失缓存**：只在内存里，**不进存档**（正文是生成物不是事实，
    #    读档后由 `/state` 按既有 facts 重新按需生成）。
    narrative_cache: dict = field(default_factory=dict, repr=False)
    # 单独一把小锁**只**守上面那张缓存表（与结算锁互不嵌套、不会死锁）。
    narrative_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.player_ids:
            self.player_ids = self.state.player_ids
        if self.storage is not None and not self.game_id:
            raise ValueError(
                "给了 storage 就必须给 game_id —— 否则不知道往哪一局存。"
            )
        # ★ 2026-10-18 第七轮：**市场曲线属于 config，不是玩家进度**。
        #   存档里带的是**建局时**的那份曲线；config 改了 Q0/弹性之后，
        #   读旧档若继续用存档曲线 → "改了市场"对旧档完全不生效
        #   （实测：8550 读到 C Q0=200 / H-A Q0=30，新配置 260/150）。
        #   所以在会话建立时一律用 config 覆盖；新建局的 `new_world()`
        #   本来就用 cfg.market，这里对它是恒等操作。
        if getattr(self.cfg, "market", None):
            self.state.world.market = copy.deepcopy(self.cfg.market)

        # ★ 2026-10-19 修（终局验收抓到的真 bug）：**读档后 phase 没跟着世界状态走**。
        #
        #   现象：读一个已终局（`world.phase == FINISHED`）的存档，
        #         session.phase 却是默认的 `COLLECTING` —— 于是界面上
        #         ① 不弹终局 overlay（前端只在 finished 时弹）、
        #         ② 「推进本季度」居然可点、
        #         ③ 顶栏季度显示成 `3 / 2`（超过总局数）。
        #   根因：`registry.get()` 读档时只传 `state/cfg/storage/game_id`，
        #         没有传 phase；而 dataclass 默认值是 COLLECTING。
        #   修法：在这里按**世界状态**推断一次 —— 存档是世界状态的一部分，
        #         `WorldPhase.FINISHED` 是它的权威终局标记。
        #   ⚠️ 只做「FINISHED → FINISHED」这一条单向推断：
        #      其他 phase（RUNNING 等）一律保持默认 COLLECTING，
        #      因为"行动阶段/结算阶段"是**会话内**的临时状态，不该从存档恢复。
        if getattr(self.state.world, "phase", None) is WorldPhase.FINISHED:
            self.phase = SessionPhase.FINISHED

    # ── ★ 补贴（2026-10-18 S1 用户拍板：季度初发放、不进结算）────────

    def credit_opening_subsidy(self) -> float:
        """**幂等**：给本局所有玩家发**当季度**补贴（季初现金注入，不进结算）。

        ★ 用户拍板（S1）：
          · 新开局（Q1）→ 发一次；
          · 每次 `advance` 结算完（`end_quarter` 已推进 `quarter`）→ 发下一季补贴；
          · **不在 `_run` 里发**（那里现在只算市场/税/GDP，`rep.subsidy` 恒 0）；
          · **加载旧档不补发** —— 本函数只在"创建"与"推进"两个时点被调用，
            旧档的现金里**含**它历次已发的补贴，不重复。

        ⚠️ 幂等性来自**调用点**而非内部记账：每个时点**恰好**调一次。
           每次**重复调用**（同锁内）都会再发一份 —— 由 server/registry/
           `_resolve_unlocked` 的顺序保证"只会调一次"（见那两处的注释）。
        """
        amount = float(self.cfg.subsidy_for_quarter(self.state.world.quarter))
        if amount == 0.0:
            return 0.0
        for player in self.state.players.values():
            player.cash += amount
        return amount

    # ── 读 ────────────────────────────────────────────────

    def state_for(self, player_id: str, *, with_narratives: bool = False) -> dict:
        """给前端的只读状态快照（**已算好的数字**，前端零计算）。

        ✅ 2026-10-16：改为调用 `engine.report.build_state()`。
        此前这里手搓了一个六字段的小字典，既盖不住《界面要显示的变量清单》
        要的 40 项，也**绕开了"只转发"的原则**（数字形状写在服务层里）。
        现在服务层只做一件事：把**已提交的计划**递进去。

        ⚠️ 读也要拿锁：否则可能读到"结算到一半"的状态。

        ★ 2026-10-18：`with_narratives` **默认改为 False**。
           叙事（新闻/财报解读）两路模型调用实测 1–3 分钟；它挂在
           `/api/state` 上会让“结算后刷新界面”看起来像结算要两分钟。
           现在默认只回数字（叙事字段为 null），前端先渲染；
           要生成叙事走单独的 `GET /api/narratives`（见 `narratives_for`）。
           `with_narratives=True` 仍保留旧行为，给确实要一步到位的调用方。
        """
        # ── 锁外：要不要生成本季叙事（一次拿公平快照，决定后调模型）──
        narratives = self.narratives_for(player_id) if with_narratives else None

        with self.lock:
            return report.build_state(
                self.state,
                player_id,
                self.cfg,
                plan=self.plans.get(player_id),      # 没提交 → None（不编数字）
                phase=self.phase.value,
                narratives=narratives,               # 引擎只转发，不调 AI
            )

    def narratives_for(self, player_id: str) -> Optional[dict]:
        """本季「新闻 + 财报解读」两份叙事。**锁外、按季缓存、可能很慢。**

        这是 `/api/narratives` 的入口，也是 `state_for(with_narratives=True)`
        的底层。**不要在结算请求里调它**——两路模型实测 1–3 分钟。
        返回 `None`（还没结算/生成器未就绪）或
        `{"news": dict|None, "report": dict|None}`。
        """
        return self._narratives_for(player_id)

    # ── ★ AI 叙事：按季缓存 + 缺失即生成（全部**锁外**） ────────

    def _narratives_for(self, player_id: str) -> Optional[dict]:
        """本季"新闻 + 财报解读"两份叙事。**锁外。**（也许生成，也许只取缓存）

        做法（用户拍板，二选一里选了**A 同步生成**）：

            A. `/state` 发现本季叙事**还没有**且**已结算过** → **当场调一次模型**，
               直到出结果（或失败）才返回。选它的理由见 `_try_generate_narrative`。

        返回::

            None                       —— 本季还没结算（models.quarter 从 1 起：
                                           `quarter <= 1` = 第 1 季、一次都没结过），
                                           **不许生成**（用户红线：facts 为 null 不生成）；
            {"news": dict|None, "report": dict|None}
                                         —— 每键是 ai.narrate 的结果 dict；键为 None =
                                           该路生成器尚未落盘（见下）。

        ⚠️ `ai.narrate` 是**另一路并行在写**的模块，此刻**可能还没落盘**。
           靠 `getattr` 探测：函数不在 → 该键老老实实 None + 一行日志（不编造）。
        """
        # 公平快照（`self.lock` 拿的，判断"该不该生成"要用一致的状态）。
        with self.lock:
            quarter = self.state.world.quarter
            rep_obj = self.state.raw_reports.get(player_id) \
                if self.state.raw_reports else None
            rep_dict = self.state.report.get(player_id) \
                if self.state.report else None

        if quarter <= 1 or (rep_obj is None and rep_dict is None):
            return None

        out: dict[str, Any] = {}
        for kind in ("news", "report"):
            out[kind] = self._get_or_generate(
                quarter, kind, player_id, rep_obj, rep_dict)
        return out

    def _get_or_generate(
        self,
        quarter: int,
        kind: str,
        player_id: str,
        rep_obj,
        rep_dict,
    ):
        """一个 `kind` 的"缓存取 / 缺失生成"。**锁外调模型、小锁写缓存。**

        · 缓存命中 → 直接返回（**同一季只调一次**由此保证）。
        · 生成器未落盘 → None（探测见 `_generator_for`）。
        · 真调模型 → **不在任何锁里**（`narrative_lock` 只在"登记到缓存表"
          那一瞬间拿一下）。
        · 生成抛穿 → 包成 `{"kind","quarter",ok:False,error,"detail"}`（§11.4 不编造）。
        """
        key = (quarter, kind)
        with self.narrative_lock:
            if key in self.narrative_cache:
                return self.narrative_cache[key]

        gen = self._generator_for(kind)
        if gen is None:
            return None          # 另一路的 ai/narrate.py 还没落盘 —— 诚实未生成

        # ── 准备参数（**锁外**纯搬运，无判定）────────────────
        if kind == "news":
            facts = self._facts_for(player_id, rep_obj, rep_dict)
        else:
            facts = None
        try:
            if kind == "news":
                # 统一形状铁律：facts 为 null 时不许生成（这里 quarter>1 必非空，
                # 但仍兜底）。
                if facts is None:
                    return None
                result = gen(facts, cfg=self.cfg)
            else:
                # 财报解读吃 **report_dict**：优先 `state.report`（dict，读档也在），
                # 内存里刚结算的则 `rep_obj.to_dict()`。
                rd = rep_dict if rep_dict is not None else (
                    rep_obj.to_dict() if rep_obj is not None else None)
                if rd is None:
                    return None
                result = gen(rd, cfg=self.cfg)
        except Exception as e:  # noqa: BLE001
            result = {
                "kind": kind,
                "quarter": quarter,
                "ok": False,
                "error": "generator_crashed",
                "detail": f"叙事生成器抛了没接住的异常（{type(e).__name__}：{e}）。"
                          "这是 bug，但绝不能用编造文本顶替（§11.4）。",
            }

        with self.narrative_lock:
            # 另一线程可能刚好也生成完 → 已存在就用现成的（generate-once）。
            existing = self.narrative_cache.get(key)
            if existing is not None:
                return existing
            self.narrative_cache[key] = result
            return result

    def _generator_for(self, kind: str):
        """按 kind 取 `ai.narrate` 里的生成函数。**它可能还没落盘。**

        ⚠️ 本项目"一套接缝 + 两份提示词"——news 与 report **共用**
        `ai.narrate` 里同一个「装提示词→调模型→校验 JSON」的核心，
        只是提示词文件不同。入口签名（另一路承诺）：

            ai.narrate.narrate_news(facts, cfg=...)
            ai.narrate.narrate_report(report_dict, cfg=...)

        未就绪 → None + 一行 stderr（**不**当作失败缓存——等它落盘后自然接上）。
        """
        import sys

        try:
            from ai import narrate as _narrate  # type: ignore
        except Exception:
            print(f"[narrate] ai/narrate.py 尚未就绪（{kind} 路这次跳过）",
                  file=sys.stderr)
            return None
        fn = getattr(_narrate, f"narrate_{kind}", None)
        if not callable(fn):
            print(f"[narrate] ai.narrate.narrate_{kind} 不存在（跳过）",
                  file=sys.stderr)
            return None
        return fn

    def _facts_for(self, player_id: str, rep_obj, rep_dict=None):
        """新闻事实包（直接走引擎 `news_facts`；`rep_obj`/`rep_dict` 都 None → None）。

        ⚠️ 读档后 `raw_reports` 通常为空，但 `state.report` 是 dict；
          这里把 dict 适配成 news_facts 需要的只读视图，避免「读档后新闻永远
          生成不出来」。
        """
        if rep_obj is None and rep_dict is not None:
            rep_obj = _report_view_from_dict(rep_dict)
        if rep_obj is None:
            return None
        from engine import news as _news_mod

        try:
            return _news_mod.news_facts(
                rep_obj,
                self.cfg,
                contracts=list(self.state.contracts.values())
                if self.state.contracts else None,
                previous_happiness=None,   # 状态层无"上季 H"号位（engine/report.py 同口径）
            )
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[narrate] news_facts 抛错（{type(e).__name__}: {e}），"
                  "这次按未生成处理", file=sys.stderr)
            return None

    # 设计说明（为什么不另开线程"先返 null 再补"——即做法 B）：
    #   ① /state 是"刷新即拿全量"的只读端，前端只显示一次到位的东西，
    #      不必做"null → 轮询 → 补一次"的两段渲染；
    #   ② 生成全在锁外，几十秒里这个 /state 请求挂着，但结算锁不被占，
    #      提交/结算照常串行（红线②不阻塞结算满足）；
    #   ③ 失败就地写进缓存（ok:false）——同季不再重烧配额（红线①满足）。
    #   做法 B（后台线程）省去首刷等待，却要把"生成中/失败"中间态引入前端，
    #   且 http.server 线程模型下后台线程生命周期没有保证——更稳的是 A。

    def map_for(self, player_id: str) -> dict:
        """整张地图（含别人的格子）—— 前端画地图用。**只读。**

        ★ 2026-10-17：递入**已提交的本季计划**（`self.plans`，只读）。
           它只驱动 `build_map` 在 cells 上叠"前影"（在建虚影），
           方便玩家在递交后立刻看到哪几格已经下过指令；
           **不改真状态**、不进 `self.plans`。
        """
        with self.lock:
            return report.build_map(
                self.state,
                player_id,
                pending=self.plans.get(player_id),
            )

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
            # ★ 终局只读守卫（2026-10-18「20 季终局」）：
            #   已结束的局**不接受任何写操作**——可看地图/上季报告，无操作入口。
            if self.phase is SessionPhase.FINISHED:
                raise Conflict(
                    "本局已到第 20 季终局，只读回望，不再接受操作。"
                )
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
            # ★ 终局只读守卫（同 `submit`）：FINISHED 之后**不再**有下一季。
            if self.phase is SessionPhase.FINISHED:
                raise Conflict(
                    "本局已到第 20 季终局，只读回望，不再接受操作。"
                )
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
        except Exception:
            # 结算失败 → 退回行动阶段，让玩家改计划重来。
            #
            # ★ 2026-10-17 修：原来只捕获 `SettlementError`。
            #   实测有的坏计划会抛**别的**异常（ValueError / KeyError 之类），
            #   于是 phase 永远停在 `resolving` —— 这一局就变成**僵尸局**：
            #   既不能提交（不在行动阶段）、也不能结算（不在收集阶段），只能重开档。
            #   现在捕获 `Exception`（保留原异常类型往上抛，不影响错误展示），
            #   保证**任何**失败都把 phase 退回 collecting。
            #
            #   ⚠️ 状态是否干净取决于 advance 自己的事务性：它在扣款前抛错时状态是干净的；
            #      若是更晚的步骤出错，advance 内部的快照/回滚（policies 那套）会兜住。
            self.phase = SessionPhase.COLLECTING
            raise

        self.plans.clear()
        self.phase = SessionPhase.REPORT

        # ★ 2026-10-18「20 季终局」：advance 已把 quarter 从 N 推到 N+1。
        #   `is_game_over(quarter, total)` ⇔ `quarter > total`（clock.py）。
        #   到终点 → 世界状态切 FINISHED、会话状态切 FINISHED：
        #   **终局只读**——可看地图与上季报告，没有 submit/end/next 任何操作入口。
        if is_game_over(self.state.world.quarter, self.state.world.total_quarters):
            self.state.world.phase = WorldPhase.FINISHED
            self.phase = SessionPhase.FINISHED

        # ★ 2026-10-18 S1：季度推进完（`advance` 已把 `quarter +1`）→
        #   立刻发**新一季度**的季初补贴。锁内、结算之后、存档之前，
        #   保证"落盘的现金"含这笔。幂等来自"每次 _resolve 恰好这一行调一次"。
        self.credit_opening_subsidy()

        self._save_unlocked()          # ★ 结算完立刻全量快照（锁内）

        # ★ 2026-10-18 多档自动存档：把"上一季末"快照另存一份为
        #   `<game_id>@q<N-1>`（N-1 是**结算前**那季，`advance` 已 `+1`）。
        #   这是玩家"想回到某一季再试一次"的最低成本兜底 ——
        #   静默失败：slot 落不下来不影响主档，只留一行 stderr。
        if self.storage is not None:
            settle_quarter = self.state.world.quarter - 1
            if settle_quarter >= 0:
                auto_slot_id = _slots.auto_quarter_slot_id(
                    self.game_id, settle_quarter
                )
                try:
                    self.storage.save(auto_slot_id, self.state)
                except Exception as e:  # noqa: BLE001
                    import sys
                    print(
                        f"[slots] 自动季度档 {auto_slot_id!r} 落盘失败 "
                        f"（{type(e).__name__}: {e}）—— 主档不受影响。",
                        file=sys.stderr,
                    )

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
            # ★ 终局只读守卫：已经 FINISHED 就**没有下一季**。
            if self.phase is SessionPhase.FINISHED:
                raise Conflict("本局已终局，没有下一季。")
            if self.phase is not SessionPhase.REPORT:
                raise Conflict(f"只有月报阶段才能进入下一季（当前 {self.phase.value}）")
            self.phase = SessionPhase.COLLECTING

    # ── ★ 终局只读查询（2026-10-18「20 季终局」）─────────────────

    def history_for(self, player_id: str) -> list:
        """该玩家**逐季**财报 dict 列表（按季升序）。

        原样转发 `state.history`，**不补、不插、不造数** ——
        缺季如实缺着（由 `finale_overview.missing_quarters` 显式披露）。
        """
        with self.lock:
            return list(self.state.history.get(player_id, []) or [])

    def is_game_over(self) -> bool:
        """当前局是否已到总季数（clock.is_game_over 的锁内只读封装）。"""
        with self.lock:
            return is_game_over(
                self.state.world.quarter, self.state.world.total_quarters
            )

    def finale_overview(self, player_id: str) -> dict:
        """终局总览（**只读聚合**，全部由存档/状态真实值组成，**不新增派生指标**）。

        `/api/finale` 的引擎概览就是从这里秒出；模型正文由另一路接，
        本方法不生成任何叙事。

        `missing_quarters` / `history_complete` 是**披露**而不修饰：
        有缺季就明示出来，让玩家/前端知道这局的历史断在哪。
        """
        with self.lock:
            history = list(self.state.history.get(player_id, []) or [])
            gdp_quarters = [
                r.get("quarter")
                for r in history
                if isinstance(r.get("value_added"), (int, float))
            ]
            completed = len(gdp_quarters)
            game_over = is_game_over(
                self.state.world.quarter, self.state.world.total_quarters
            )
            # 规则：没打完的局**不能**自称"历史完整"；打完的局要求至少
            # total_quarters 行（即 1..total 每季都有一行）。
            full = (not game_over) or completed >= self.state.world.total_quarters
            missing = [
                q
                for q in range(1, self.state.world.total_quarters + 1)
                if q not in set(gdp_quarters)
            ]
            return {
                "scope": "player",
                "player_id": player_id,
                "quarter": self.state.world.quarter,
                "total_quarters": self.state.world.total_quarters,
                "game_over": game_over,
                "quarters_completed": completed,
                "missing_quarters": missing,   # 缺失季明示，供报告披露
                "history_complete": full,
                "final_report": (
                    self.state.report.get(player_id)
                    if isinstance(self.state.report, dict)
                    else None
                ),
                "history": history,
            }

    # ── ★ 终局叙事（2026-10-18 B 单接力）─────────────────────

    def finale_narrative(self, player_id: str) -> dict:
        """终局报告叙事。**`/api/finale` 的并联键；走错不开就模板兜底。**

        三步（**锁内**拿总览，**锁外**调模型/兜底——模型可能 1–3 分钟，
        占着锁会让结算/提交/状态全卡住）：
          1. `overview = self.finale_overview(player_id)`（只读，锁内）；
          2. 惰性 `from engine.finale import generate_finale_report` 并调用
             （`generate_finale_report` 内部先试 `narrate_finale_report`，
             败了/离线在引擎侧已兜底为规则化模板）；这一步**不在锁里**；
          3. 自己再兜一层异常——generate_finale_report 本身就该不抛，
             但万一它抛了，这里**绝不能让 /api/finale 跟着 500**：
             原样返回一个 `{"ok": True, "finale": None, "warning": ...}`
             **不够好**（B 单红线：四键必须齐）。所以这里拼一份
             **极简兜底 dict**：headline 用 player_id + total_quarters，
             body 明说「叙事生成器未就绪」+ 贴 overview.game_over /
             quarters_completed 两个原值，charts/facts_used 给 []({})
             （**不是四键齐的 fail-safe**： B 单钉的是 `engine/finale.py`
             的 `generate_finale_report` —— 它在，才是正常路径；这里
             只是「generate_finale_report 炸了我们还能挤出什么」的
             最后一道兜底，宁缺毋滥：缺键就明说）。"
        """
        overview = self.finale_overview(player_id)   # 锁内只读

        # —— 锁外：模型/兜底。generate_finale_report 契约不抛，
        #    但**绝不信**，兜一层让它也炸不坏 /api/finale。
        try:
            from engine.finale import generate_finale_report  # 惰性
        except Exception as e:  # noqa: BLE001
            return {
                "ok": True,
                "headline": f"《{player_id}的终局报告（生成器未就绪）》",
                "body": (
                    "终局报告生成器（`engine/finale.py`）尚未落盘；本次无法生成"
                    "正文。引擎层终局概览（`/api/finale` 的 `finale` 键）依然"
                    "可用。"
                ),
                "charts": [],
                "facts_used": {},
                "source": "error_no_engine",
                "warning": f"finale_narrative._import_failed: {type(e).__name__}: {e}",
            }
        try:
            # client=None：走 narrate 内部「离线闸 → _default_client」；
            # 离线（NARRATE_OFFLINE 默认 False 之外的任意开启值）时
            # narrate_finale_report 返回 ok:False → 引擎侧自动走
            # deterministic_body 兜底（终局界面必须永远能开）。
            return generate_finale_report(overview, self.cfg, client=None)
        except Exception as e:  # noqa: BLE001
            total = int(overview.get("total_quarters") or 20)
            q_done = int(overview.get("quarters_completed") or 0)
            game_over = bool(overview.get("game_over"))
            pid = overview.get("player_id") or player_id
            return {
                "ok": True,
                "headline": f"《{pid}的{total}季经营终局报告（兜底）》",
                "body": (
                    "终局报告正文生成器中途异常；以下由原样搬运的"
                    f"引擎概览原值给出：已结算 {q_done}/{total} 季，"
                    f"game_over={game_over}。详情请查看"
                    " `/api/finale` 的 `finale` 键（完整原值）。"
                ),
                "charts": [],
                "facts_used": {
                    "player_id": pid,
                    "quarters_completed": q_done,
                    "total_quarters": total,
                    "game_over": game_over,
                },
                "source": "error_generate_crashed",
                "warning": f"generate_finale_report crashed: {type(e).__name__}: {e}",
            }
