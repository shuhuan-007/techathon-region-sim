"""税收与 GDP —— 从 `pipeline.py` 抽出来的两块独立账。

依据《规则基线》§7.1（工资税）与 §7.5（GDP / 增加值）。

═══════════════════════════════════════════════════════════════════
为什么单独成文件
═══════════════════════════════════════════════════════════════════

这两块原先**散在 `pipeline.py` 里**（`_value_added()` 与第 8 步的三行税）。
抽出来有两个好处：

  ① `pipeline.py` 短一截 —— 它是"季度流水线"，该讲**顺序**，
     不该顺带讲"什么算中间投入"这种**口径**；
  ② 这两块**能单独写测试**。以前要验一个 GDP 口径，
     得先造一个完整季度跑完流水线，成本高到没人愿意写。

⚠️ **抽出来不是重写**：算式一字未改，就是把同一个函数挪个地方，
   再加一层"口径说明"。抽完后 M1 锚点（甲 66 / 乙 110 / 合计 176）必须原样对上。

═══════════════════════════════════════════════════════════════════
★ §7.1 工资税 —— 一个容易看错的口径
═══════════════════════════════════════════════════════════════════

    工资税 = W × 10%

账面口径（§7.1 原文）：
  · 工资支出记**税前总额**      ← 第 5 步扣的是 `W`，**不是** `W × 0.9`
  · 工资税记**回流收入**        ← 第 8 步 `player.cash += 税`
  · 净支出 = 税后工资
  · **不生成居民消费订单**

所以在本项目第一期，**工资的实际成本就是 `W × 90%`** ——
但账面上**先扣全额、再回流**，两笔都记，**不是直接扣 90%**。

⚠️ **不要把这两步合并成"扣 0.9W"**：账面口径是规则的一部分，
   多人时"回流"很可能变成进公共账户（见 D4 待拍板项）。
   现在合并了，将来引入公共账户就要重写。

═══════════════════════════════════════════════════════════════════
★ §7.5 GDP / 增加值 —— 什么算"中间投入"
═══════════════════════════════════════════════════════════════════

    增加值 = 有效产出价值 − 实际消耗的中间投入价值

**固定统计价：用参考价 `P0`，不随季度成交价变。**
（理由：GDP 是统计口径，若随成交价浮动，玩家卖得便宜就等于"GDP 变低"，
  那是把价格波动混进了产出核算。）

| 项 | 算不算中间投入 | 本文件怎么处理 |
|---|:--:|---|
| 工资 `W` | ❌ **不**再扣一次 | 完全不出现 —— 工资是**增加值的一部分**（劳动者报酬） |
| 原料 | ✅ 要扣 | `rep.bought_breakdown["production_input"]` |
| 经营费 `c`（维护/能源） | ✅ 要扣 | `rep.operating` 整块扣 |
| 粮食扣减 | ✅ 要扣 | `rep.food.deducted` |
| 贷款、拨款、税款 | ❌ 不计入 | 不出现 |
| 建设与研发成果 | **单独报告** | **不硬塞进 GDP** |

⚠️ **"工资不扣"是最容易写反的一条。**
   把工资也当中间投入扣掉，甲区增加值会从 66 变成 30（66 − 36），
   而 §8.1 的锚点写的是 **66**。
   GDP 的"增加值"法里，工资属于增加值（劳动者报酬），不是中间消耗。

⚠️ **原料只算"实际消耗的"** —— 取 `rep.bought_breakdown["production_input"]`，
   那是**按配方算出来的实际投料**，不是玩家申报的采购量。
   申报了却没用上的，不该扣。
"""

from __future__ import annotations

from engine.loader import Config


# ══════════════════════════════════════════════════════════════════
# §7.1 工资税
# ══════════════════════════════════════════════════════════════════


def wage_tax(wages: float, cfg: Config) -> float:
    """工资税 = `W × wage_tax_rate`（§7.1，测试参数 10%）。

    ⚠️ **返回的是"税"本身**，不是税后工资。
       调用方要按账面口径**分两笔**记：

          扣款时：`cash -= wages`        （税前总额）
          结算时：`cash += wage_tax(wages, cfg)`   （回流收入）

       **不要**写成 `cash -= wages * 0.9` —— 见模块头的说明。
    """
    return wages * cfg.wage_tax_rate


def wage_tax_net(wages: float, cfg: Config) -> float:
    """税后净支出 = `W − 税`（第一期等于 `W × 90%`）。

    ⚠️ 这**只是给显示/试算用的一个派生量**，
       流水线**不要**用它来做扣款 —— 扣款必须走上面那个"两笔"的口径。
       它的用途是：前端顶栏想直接显示"工资实际花了我多少"。
    """
    return wages - wage_tax(wages, cfg)


# ══════════════════════════════════════════════════════════════════
# §7.5 GDP / 增加值
# ══════════════════════════════════════════════════════════════════


def output_value(production: list, cfg: Config) -> float:
    """有效产出价值 —— 按**固定参考价 `P0`** 算（§7.5）。

    ⚠️ 服务与研究地块 `output_goods is None`，**跳过产出侧**（★ 2026-10-18：
       服务的**服务收入**由 `value_added()` 单独加，不混进产出价值）：
       它们 0→0 不产商品，其价值已经体现在**别的地块的效率**上，
       在这里再算一遍等于同一份价值收两次。
    """
    total = 0.0
    for o in production:
        if o.output_goods is None:
            continue
        total += o.output_qty * cfg.price_of(o.output_goods)
    return total


def material_value(report, cfg: Config) -> float:
    """实际消耗的中间投入价值 = 原料 + 经营费 + 粮食扣减（§7.5）。

    ⚠️ **原料取"按配方实际投料"**（`production_input`），
       不是玩家申报的采购量 —— 买了没用上的不该扣。
    """
    material = 0.0
    for g, qty in (report.bought_breakdown.get("production_input") or {}).items():
        material += qty * cfg.price_of(g)

    # 经营费 c：外购维护/能源等经营服务，算中间投入
    material += report.operating

    # 粮食扣减：居民吃掉的那部分也算中间投入（§4.3.4）
    if report.food:
        for g, qty in report.food.deducted.items():
            material += qty * cfg.price_of(g)

    return material


def value_added(report, cfg: Config) -> float:
    """GDP 增加值（§7.5）＝ 有效产出价值 + 服务收入 − 中间投入价值。

    ★ 2026-10-18：服务地块新增**服务收入**（§3.6）。它属于服务业的
      增加值（`output_value` 那边服务仍为 0 —— 服务不产商品），所以
      在这里**加**进分子，而不是塞进产出价值里（那会混淆两种口径）。

    ⚠️ 用 `getattr(report, "service_revenue", 0.0)` 兜底旧报告/旧存档：
        在本字段加入前生成的 `QuarterReport` 没有它，不能让 GDP 计算炸掉。

    ⚠️ **工资不在里面** —— 它属于增加值（劳动者报酬），不是中间消耗。
       见模块头表格。写错这条，甲区会从 66 变成 30。

    ⚠️ 用**固定统计价 `P0`**，与 `report.sales_revenue`（按本季成交价）
       **是两个不同的数**，不要混：一个是统计口径，一个是现金流。
    """
    return (
        output_value(report.production, cfg)
        + getattr(report, "service_revenue", 0.0)
        - material_value(report, cfg)
    )


# ══════════════════════════════════════════════════════════════════
# 给 UI 的一整块（前端零计算）
# ══════════════════════════════════════════════════════════════════


def finance_summary(report, cfg: Config) -> dict:
    """把工资/税/GDP 打包成一块，供 `/state` 与月报直接给前端。

    ⚠️ **只读、只汇总**，不重算任何流水线里的数 ——
       所有输入都来自已经算好的 `report`。
    """
    return {
        "wages": report.wages,
        "wage_tax": wage_tax(report.wages, cfg),
        "wage_net": wage_tax_net(report.wages, cfg),
        "operating": report.operating,
        "materials_cost": report.materials_cost,
        "total_deduction": report.total_deduction,
        "sales_revenue": report.sales_revenue,
        "service_revenue": getattr(report, "service_revenue", 0.0),
        # GDP 用固定参考价，与 sales_revenue 口径不同，两个都给
        "output_value_at_p0": output_value(report.production, cfg),
        "material_value_at_p0": material_value(report, cfg),
        "value_added": report.value_added,
    }
