"""新闻 / 财报叙述 —— **骨架与接缝**（2026-10-17）。

用户原话：「新闻财报这种，留一个地方就行，提示词明天给你。」
所以本模块**只做两件事**：

  ① `news_facts()` —— 从既有财报 `QuarterReport` **搬运事实**，
     把"明天喂给模型的那一包数字"一次性抽出来。
  ② `narrate()`    —— **显式空位**。签名与返回形状定死，正文留占位，
     等用户 2026-10-18 把提示词交来，接在 `server/ai_host.py` 的
     同一条模型通道上。

══════════════════════════════════════════════════════════════════
★ 红线：引擎不涉 AI
══════════════════════════════════════════════════════════════════

  · `news_facts()` 是**纯搬运**：每个数字都直接取自 `report` 字段或
    `cfg` 的静态表，**没有一行新公式**，禁止解释/评价/编造。
    "价格变化"用的是引擎已经记下的两个数（本季结算价 / 上季结算价），
    变化本身交给 AI 去说，引擎只递数。
  · `narrate()` **不调任何模型**、**不含提示词正文**。它存在的意义是
    让"接 AI 的缝"有形：调用方（`report.py` 构想的新闻块、前端的
    `#ai-panel`）今天就能按这个返回形状写代码，明天只填一个函数体。
  · 可验证性：facts 里每个数值都要能在 `report.to_dict()` 或 `cfg`
    里找到同值 —— `tests/test_news.py` 就是照这条写的。
"""

from __future__ import annotations

from typing import Optional

from engine.loader import Config
from engine.models import Contract
from engine.pipeline import QuarterReport


# ══════════════════════════════════════════════════════════════════
# 一、事实搬运（今天就能看到）
# ══════════════════════════════════════════════════════════════════

def news_facts(
    report: QuarterReport,
    cfg: Config,
    contracts: Optional[list[Contract]] = None,
    previous_happiness: Optional[float] = None,
) -> dict:
    """从一季财报抽出**可叙述要点**（纯数据、零解释）。

    参数
    ----
    report : `engine.pipeline.QuarterReport` —— 结算完毕的那一份
    cfg    : 静态配置（拿商品中文名）。

    返回
    ----
    形状（键名与类型**定死**，前端与明天的 AI 都按它写）::

        {
          "quarter": int,               # 第几季（report.quarter）
          "player_id": str,             # 谁的账（report.player_id）
          "prices": {                   # 本季参与交易的商品，按代码排序
            "<G>": {"name": str,         # cfg.goods_name
                    "last_price": float | None,   # 上季结算价
                    "price": float,      # 本季结算价
                    "at_floor": bool},   # 是否触底
            ...
          },
          "production": [               # 每块地一行（report.production 原样）
            {"plot_id": str, "plot_type": str, "level": int,
             "product": str | None, "output_goods": str | None,
             "output_qty": float},
            ...
          ],
          "happiness": float | None,            # 本季 H（report.food.happiness）
          "previous_happiness": float | None,   # 上季 H（cfg 无此字段 → 见下注）
          "cash_before": float,
          "cash_after": float,
          "total_deduction": float,     # 一次性扣款
          "materials_cost": float, "wages": float, "operating": float,
          "construction_cash": float,   # 工程现金
          "sales_revenue": float,
          "wage_tax_refund": float,
          "value_added": float,         # 增加值（GDP）
          "policy": dict | None,        # `_policy_report()` 整块原样搬
                                        # （含 announcement/乘数/
                                        #  multipliers_applied；policy_type
                                        #  恒 null 是废案，随块走）
          "unfulfilled": {"has_unfulfilled": bool,
                          "contracts": [{"contract_id": str, "goods": str,
                                         "shortfall": float}, ...]},
        }

    ⚠️ 两个**来自状态层**的数（`previous_happiness`、未履约明细）不在
       `QuarterReport` 里 —— 财报只记本季。调用方是"状态口"（`report.py`
       握有 `GameState`），由它**原样**递进来；不给就老老实实 `None`，
       **宁可缺席，不可编造**（§11.4 同一条红线）。
    """
    prices: dict[str, dict] = {}
    for goods in sorted(report.prices):
        pr = report.prices[goods]
        prices[goods] = {
            "name": cfg.goods_name.get(goods, goods),
            "last_price": pr.get("last_price"),
            "price": pr.get("price"),
            "at_floor": bool(pr.get("at_floor")),
        }

    production = [
        {
            "plot_id": o.plot_id,
            "plot_type": o.plot_type.value,
            "level": o.level,
            "product": o.product,
            "output_goods": o.output_goods,
            "output_qty": o.output_qty,
        }
        for o in report.production
    ]

    # 政策**整块原样搬**（提示词要求 policy.announcement / 乘数 /
    # multipliers_applied 都在场）。⚠️ `policy_type` 恒为 null 是既有
    # 废案（用户裁定不修、提示词不依赖），属块内一员，随块搬运。
    policy = dict(report.policy) if report.policy is not None else None

    # 未履约义务：合同账本在 `state.contracts`（§7.2/§7.3 记录位），
    # 调用方从状态层原样递来；只挑 **shortfall > 0** 的（没交齐的）。
    # 不给账本 = 单人局/没合同这一说 → 空清单，**不可编造**。
    breaches = [
        {
            "contract_id": c.contract_id,
            "goods": c.goods,
            "shortfall": c.shortfall,
        }
        for c in (contracts or [])
        if c.shortfall > 0.0
    ]
    unfulfilled = {
        "has_unfulfilled": bool(breaches),
        "contracts": breaches,
    }

    food = report.food
    happiness = food.happiness if food is not None else None

    return {
        "quarter": report.quarter,
        "player_id": report.player_id,
        "prices": prices,
        "production": production,
        "happiness": happiness,
        "previous_happiness": previous_happiness,  # 状态层递；不给 = 诚实 None
        "cash_before": report.cash_before,
        "cash_after": report.cash_after,
        "total_deduction": report.total_deduction,
        "materials_cost": report.materials_cost,
        "wages": report.wages,
        "operating": report.operating,
        "construction_cash": report.construction_cash,
        "sales_revenue": report.sales_revenue,
        "wage_tax_refund": report.wage_tax_refund,
        "value_added": report.value_added,
        "policy": policy,
        "unfulfilled": unfulfilled,
    }


# ══════════════════════════════════════════════════════════════════
# 二、AI 叙事 —— 显式空位（明天接模型，今天就有形状）
# ══════════════════════════════════════════════════════════════════

def narrate(facts: dict, cfg: Optional[Config] = None) -> dict:
    """把 `news_facts()` 的事实包**讲成一段人话新闻**。

    ⚠️ **提示词由用户 2026-10-18 提供，届时接在
       `server/ai_host.py` 的同一条模型通道上。**
       本函数**现在不实现、也绝不偷偷返回编造文本**（§11.4：
       AI 只只解释、不编造 —— 没有提示词出的任何话都是编造）。

    参数
    ----
    facts : `news_facts()` 的返回（只读，不改）。
    cfg   : 静态配置（可选；将来喂提示词时拼商品名用）。

    返回（**形状定死**，实现后必须长这样）::

        {
          "headline": str,   # 一句话头条，如「Q3 产值翻倍」
          "body": str,       # 一段新闻正文
          "facts_used": dict # 回传用了哪些事实（溯源，§11.4 可解释）
        }

    ⚠️ `NotImplementedError` 是**公开承诺**，不是偷懒：
       前端 `#ai-panel`、`/state` 的新闻块今天就能「捕获 →
       显示"AI 建议（待接）"」，明天把函数体换上提示词+一次
       `llm_client` 调用即可，**调用方一行不用改**。
    """
    raise NotImplementedError(
        "narrate() 待 2026-10-18 用户提示词到位后实现；"
        "事实包见 news_facts()，通道见 server/ai_host.py。"
    )
