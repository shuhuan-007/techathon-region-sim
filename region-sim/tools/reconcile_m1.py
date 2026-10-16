"""M1 对账脚本（临时）——按当前规则算出每一步，与文档锚点逐项比对。

⚠️ 这不是 pytest 测试，是一次**人工对账**：把中间量摊开看。
   跑法：python tools/reconcile_m1.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 让脚本能直接 `python tools/reconcile_m1.py` 跑起来（把 region-sim/ 加进 path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from engine.models import Plot, PlotStatus, PlotType
from engine import production as P

cfg = load_config()
P0 = {g: c.P0 for g, c in cfg.market.items()}

# 文档《规则基线》§8.1 的 M1 锚点
DOC = {
    "甲": {"产出": "F x 4", "原料费": 0, "工资": 36, "经营费": 30,
           "生产总支出": 66, "增加值": 66, "期末现金": 1033.12},
    "乙": {"产出": "G x 5", "原料费": 72, "工资": 45, "经营费": 40,
           "生产总支出": 157, "增加值": 93, "期末现金": 1049.65278},
}


def mk(pid, t, lv, prod=None):
    return Plot(plot_id=pid, plot_type=t, level=lv,
                status=PlotStatus.RUNNING, product=prod)


CASES = [
    ("甲", mk("0,0", PlotType.RESOURCE, 1)),
    ("乙", mk("0,1", PlotType.INDUSTRY, 1)),
]

print("=" * 68)
print("M1 对账（按当前《规则基线》+ config.json 实算）")
print("=" * 68)

for name, plot in CASES:
    rec = cfg.recipe(plot.plot_type, plot.level, plot.product)
    w = cfg.wage_for(plot.plot_type, plot.level, plot.product)
    o = P.run_plot(plot, rec, u=1.0, efficiency=1.0, cfg=cfg, wage_rate=w)
    mat = o.material_cost(P0)
    total = mat + o.cash_out_before_materials
    d = DOC[name]

    print(f"\n【{name}】{plot.plot_type.value} L{plot.level}  u=1  E=1")
    print(f"  产出         实算 {o.output_goods} x {o.output_qty:<8} 文档 {d['产出']}")
    print(f"  商品投入     实算 {o.inputs}")
    print(f"  原料费 @P0   实算 {mat:<8} 文档 {d['原料费']}"
          f"{'   <<< 不一致' if abs(mat - d['原料费']) > 1e-9 else ''}")
    print(f"  工资         实算 {o.wage:<8} 文档 {d['工资']}"
          f"{'   <<< 不一致' if abs(o.wage - d['工资']) > 1e-9 else ''}")
    print(f"     └ 劳动 {o.labor} x 单价 {o.wage_rate}")
    print(f"  经营费       实算 {o.operating_cost:<8} 文档 {d['经营费']}"
          f"{'   <<< 不一致' if abs(o.operating_cost - d['经营费']) > 1e-9 else ''}")
    print(f"  生产总支出   实算 {total:<8} 文档 {d['生产总支出']}"
          f"{'   <<< 不一致' if abs(total - d['生产总支出']) > 1e-9 else ''}")

print("\n" + "=" * 68)
print("价格一步：需要 Es/Ed")
print("=" * 68)
for g in ("F", "G"):
    curve = cfg.market[g]
    print(f"  {g}: P0={curve.P0} Q0={curve.Q0} Es={curve.Es} Ed={curve.Ed}")
try:
    cfg.market["F"].slopes()
    print("  -> 弹性已填，可求价")
except Exception as e:
    print(f"  -> 求价被挡：{type(e).__name__}（这是设计如此，不是 bug）")
