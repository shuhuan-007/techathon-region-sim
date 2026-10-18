"""engine/finale.py + ai/narrate.py 终局入口的测试。**stdlib unittest**（不用 pytest）。

覆盖子代理 B 契约的八条：
    ① 手工 20 季 overview：build_finale_facts / chart_points 两张图各 20 点、
       gdp 点值 = history[i].value_added、升序、unit null。
    ② 抽掉某季 → gdp/happiness 该点为 null；deterministic_body 明示「第N季」缺。
    ③ deterministic_body 含四段标题 + 20 行表 + 收尾段；无「元/￥」、无禁词。
    ④ generate_finale_report + FakeClient 合法 JSON → source="model"，且
       charts/facts_used 被**引擎版**替换（断言与 chart_points 值相等）。
    ⑤ FakeClient 回 {"ok":False,...} → source="fallback"、四键齐、warning 非空。
    ⑥ FakeClient 回 {"ok":True,"content":"不是json"} → 同样落到 fallback。
    ⑦ game_over=False → 不宣称结束、fallback 含「尚不具备 / 当前进度」。
    ⑧ 复跑既有 tests/test_narrate.py（不改断言）→ 全绿。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import narrate as N                  # noqa: E402
from engine import finale as F               # noqa: E402
from server import llm_client                # noqa: E402


# ══════════════════════════════════════════════════════════════════
# 假客户端（参照 test_narrate.py 的 FakeClient；不打网络）
# ══════════════════════════════════════════════════════════════════


class FakeClient:
    """直接返回 LLMResult；content 就是"模型回的话"。"""

    def __init__(self, content="", *, ok=True, error="", detail="",
                 raise_exc=None):
        self.content = content
        self.ok = ok
        self.error = error
        self.detail = detail
        self.raise_exc = raise_exc
        self.calls: list = []

    def chat_completions(self, messages, **_kw):
        self.calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return llm_client.LLMResult(
            ok=self.ok, content=self.content, model="fake-kimi",
            error=self.error, detail=self.detail,
        )


# ══════════════════════════════════════════════════════════════════
# 手工 20 季 overview：一条 ascending GDP + 波动 happiness 的有意义轨迹
# ══════════════════════════════════════════════════════════════════


def _row(q: int, *, gdp: float, hap: float, cash: float, sales: float,
         pop: float = 40.0, policy: dict | None = None,
         prices: dict | None = None) -> dict:
    """给一行 QuarterReport.to_dict 形状（只给测试关注的键）。"""
    return {
        "quarter": q,
        "value_added": gdp,
        "sales_revenue": sales,
        "cash_before": cash - 100.0,
        "cash_after": cash,
        "operating_profit": sales - 50.0,
        "food": {
            "baseline_need": 10.0, "good_need": 15.0,
            "available": {"A": 8.0, "B": 4.0, "C": 2.0, "D": 0.5},
            "baseline_met": True, "good_met": False,
            "achieved_level": 12.0,
            "deducted": {"A": 8.0, "B": 4.0},
            "happiness": hap,
        },
        "population": {
            "labor_total": 20.0, "labor_food": 18.0, "labor_worked": 15.0,
            "population": pop, "avg_level": 2.0, "level_multiplier": 1.15,
            "baseline_food": 10.0, "good_food": 15.0, "working_plots": 5,
        },
        "policy": policy,
        "prices": prices or {},
    }


def _overview_20q() -> dict:
    """手工 20 季 overview。GDP 设计成：先升后跌再回升（有 peak/valley）。"""
    gdp_seq = [
        100.0, 120.0, 140.0, 160.0, 180.0,  # 1-5 升
        200.0, 210.0, 215.0,                # 6-8 继续升（第8峰1）
        200.0, 185.0,                       # 9-10 回落
        190.0,                              # 11 回升
        215.0,                              # 12 并列峰1！
        200.0, 210.0,                       # 13-14
        205.0, 215.0,                       # 15-16（峰3）
        180.0, 150.0, 120.0,                # 17-19 滑
        19176.0,                            # 20 终局尖峰
    ]
    hap_seq = [
        0.60, 0.62, 0.65, 0.68, 0.70,        # 1-5 升
        0.72, 0.72, 0.70,                    # 6-8 平/微降
        0.65, 0.60,                          # 9-10 降
        0.58, 0.55, 0.55, 0.58,              # 11-14 低
        0.62, 0.65, 0.70,                    # 15-17 回升
        0.72, 0.75,                          # 18-19 升
        0.75,                                # 20 与峰并列最高
    ]
    history = [
        _row(q, gdp=gdp_seq[q - 1], hap=hap_seq[q - 1],
             cash=1000.0 + q * 100.0, sales=gdp_seq[q - 1] * 1.4,
             pop=40.0 + q)
        for q in range(1, 21)
    ]
    # 第 5 季一条政策 + 第 8 季一件商品触底 + 第 12 季另一条政策
    history[4]["policy"] = {
        "policy_id": "pol_5", "policy_type": "subsidy",
        "announcement": "种植补贴", "target_goods": "A", "parameter": 0.9,
    }
    history[11]["policy"] = {
        "policy_id": "pol_12", "policy_type": "quota",
        "announcement": "机械配额", "target_goods": "G", "parameter": 1.1,
    }
    history[7]["prices"] = {
        "G": {"price": 11.0, "P0": 110.0, "at_floor": True},
        "A": {"price": 20.0, "P0": 22.0, "at_floor": False},
    }
    final_report = history[-1]
    final_report["cash_after"] = 28440.0
    final_report["value_added"] = 19176.0
    return {
        "scope": "player",
        "player_id": "p1",
        "quarter": 21,
        "total_quarters": 20,
        "game_over": True,
        "quarters_completed": 20,
        "missing_quarters": [],
        "history_complete": True,
        "final_report": final_report,
        "history": history,
    }


def _legal_finale_json(facts: dict) -> str:
    """合法的模型 finale JSON：四键齐具 + charts 20 点升序。
    **故意给 charts 填脏数据**（模型编造），证明最后取的是引擎版。"""
    total = int(facts["total_quarters"])
    pts_gdp = [{"quarter": q, "value": 999.99} for q in range(1, total + 1)]
    pts_hap = [{"quarter": q, "value": 0.99} for q in range(1, total + 1)]
    return json.dumps({
        "headline": "《模型给的终局报告》",
        "body": "# 模型正文\n正文用 Markdown 写。",
        "charts": [
            {"id": "gdp", "title": "fake", "metric": "gdp",
             "unit": None, "points": pts_gdp},
            {"id": "happiness", "title": "fake", "metric": "happiness",
             "unit": None, "points": pts_hap},
        ],
        "facts_used": {"foo.bar": 1.23, "model.made.up": "garbage"},
    }, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
# ① build_finale_facts + chart_points（完整 20 季）
# ══════════════════════════════════════════════════════════════════


class FactsAndChartsTests(unittest.TestCase):

    def test_build_finale_facts_shape(self):
        ov = _overview_20q()
        facts = F.build_finale_facts(ov)
        # 顶层键齐
        for k in ("scope", "player_id", "quarter", "total_quarters",
                  "game_over", "quarters_completed", "missing_quarters",
                  "history_complete", "final", "series", "extremes",
                  "events", "definitions"):
            self.assertIn(k, facts, f"facts 缺键 {k}")
        self.assertEqual(facts["player_id"], "p1")
        self.assertEqual(facts["total_quarters"], 20)
        self.assertTrue(facts["game_over"])
        self.assertEqual(facts["quarters_completed"], 20)
        # final 块
        self.assertEqual(facts["final"]["cash_after"], 28440.0)
        self.assertEqual(facts["final"]["value_added"], 19176.0)
        self.assertEqual(facts["final"]["happiness"], 0.75)
        self.assertEqual(facts["final"]["population"], 60.0)
        # series 20 点、升序、字段齐
        self.assertEqual(len(facts["series"]), 20)
        for i, pt in enumerate(facts["series"]):
            self.assertEqual(pt["quarter"], i + 1)
            # gdp 必须等于 history[i].value_added
            self.assertEqual(pt["gdp"], ov["history"][i]["value_added"])
            self.assertEqual(pt["value_added"], ov["history"][i]["value_added"])
            self.assertEqual(pt["happiness"], ov["history"][i]["food"]["happiness"])
        # extremes：gdp max = 19176（第20季） / min = 100（第1季）
        self.assertEqual(facts["extremes"]["gdp"]["max"]["value"], 19176.0)
        self.assertEqual(facts["extremes"]["gdp"]["max"]["quarters"], [20])
        self.assertEqual(facts["extremes"]["gdp"]["min"]["value"], 100.0)
        self.assertEqual(facts["extremes"]["gdp"]["min"]["quarters"], [1])
        # happiness max 并列 0.75：第19 + 20季
        self.assertEqual(facts["extremes"]["happiness"]["max"]["value"], 0.75)
        self.assertEqual(sorted(facts["extremes"]["happiness"]["max"]["quarters"]),
                         [19, 20])
        # events：第 8 季 G 触底、第 5 / 12 季政策
        floors = [(e["quarter"], e["goods"]) for e in facts["events"]["at_floor_quarters"]]
        self.assertIn((8, "G"), floors)
        pol_qs = [e["quarter"] for e in facts["events"]["policy_quarters"]]
        self.assertIn(5, pol_qs)
        self.assertIn(12, pol_qs)
        pol_names = [e["name"] for e in facts["events"]["policy_quarters"]]
        self.assertIn("种植补贴", pol_names)
        # 合同缺口 → 空表（history 不含合同字段）
        self.assertEqual(facts["events"]["contract_shortfalls"], [])
        # definitions 口径键齐
        for k in ("gdp", "happiness_scale", "cash_vs_profit",
                  "sales_vs_gdp", "output_vs_sales", "policy_vs_result"):
            self.assertIn(k, facts["definitions"])

    def test_chart_points_two_charts_20_points_each(self):
        ov = _overview_20q()
        charts = F.chart_points(ov)
        self.assertEqual(len(charts), 2)
        gdp_ch, hap_ch = charts
        self.assertEqual(gdp_ch["id"], "gdp")
        self.assertEqual(gdp_ch["metric"], "gdp")
        self.assertIsNone(gdp_ch["unit"])
        self.assertIsNone(hap_ch["unit"])
        self.assertEqual(hap_ch["id"], "happiness")
        self.assertEqual(len(gdp_ch["points"]), 20)
        self.assertEqual(len(hap_ch["points"]), 20)
        ov_history = ov["history"]
        for i in range(20):
            self.assertEqual(gdp_ch["points"][i]["quarter"], i + 1)
            self.assertEqual(gdp_ch["points"][i]["value"],
                             ov_history[i]["value_added"])
            self.assertEqual(hap_ch["points"][i]["quarter"], i + 1)
            self.assertEqual(hap_ch["points"][i]["value"],
                             ov_history[i]["food"]["happiness"])


# ══════════════════════════════════════════════════════════════════
# ② 缺季处理：抽掉第 7 季
# ══════════════════════════════════════════════════════════════════


class MissingQuarterTests(unittest.TestCase):

    def _overview_missing_q7(self) -> dict:
        ov = _overview_20q()
        # 从 history 里抽掉第 7 季
        ov["history"] = [r for r in ov["history"] if r["quarter"] != 7]
        ov["missing_quarters"] = [7]
        ov["history_complete"] = False
        ov["quarters_completed"] = 19
        return ov

    def test_missing_quarter_gives_null_points(self):
        ov = self._overview_missing_q7()
        charts = F.chart_points(ov)
        gdp_ch, hap_ch = charts
        # 第 7 季 (quarter=7) 必须是 None
        self.assertEqual(gdp_ch["points"][6]["quarter"], 7)
        self.assertIsNone(gdp_ch["points"][6]["value"])
        self.assertIsNone(hap_ch["points"][6]["value"])
        # 其它点还是有数值（第 6 季）
        self.assertEqual(gdp_ch["points"][5]["quarter"], 6)
        self.assertIsNotNone(gdp_ch["points"][5]["value"])

    def test_deterministic_body_discloses_missing_quarter(self):
        ov = self._overview_missing_q7()
        facts = F.build_finale_facts(ov)
        body = F.deterministic_body(ov, facts)
        # 必须明示「第7季」缺（正则匹配「第7季」）
        self.assertRegex(body, r"第\s*7\s*季")
        # 必须出现「未提供」或「缺失」
        self.assertTrue(("未提供" in body) or ("缺失" in body))
        # 表格里第 7 行 GDP 位置应是「—」（em-dash）
        # 找到 | 7 | 那一行往里看
        row7 = None
        for line in body.splitlines():
            if re.match(r"\|\s*7\s*\|", line):
                row7 = line
                break
        self.assertIsNotNone(row7, "表格必须含第 7 季行")
        self.assertIn("—", row7)


# ══════════════════════════════════════════════════════════════════
# ③ deterministic_body 的四段标题、20 行表、收尾段、无禁词
# ══════════════════════════════════════════════════════════════════


class DeterministicBodyTests(unittest.TestCase):

    def setUp(self):
        self.ov = _overview_20q()
        self.facts = F.build_finale_facts(self.ov)
        self.body = F.deterministic_body(self.ov, self.facts)

    def test_four_section_headings(self):
        self.assertIn("## 一、终局概览", self.body)
        self.assertIn("## 二、GDP与幸福度曲线解读", self.body)
        self.assertIn("## 三、经营历程回顾", self.body)
        self.assertIn("## 四、完整数据回顾", self.body)

    def test_table_has_20_rows(self):
        # 第 1 季行与第 20 季行都必须在
        self.assertRegex(self.body, r"\|\s*1\s*\|")
        self.assertRegex(self.body, r"\|\s*20\s*\|")
        # 行数：表头 1 + 分隔 1 + 20 数据行
        table_lines = [ln for ln in self.body.splitlines()
                       if ln.strip().startswith("|")]
        # 至少 22（表头 1 + 分隔 1 + 数据 ≥20）
        self.assertGreaterEqual(len(table_lines), 22)

    def test_body_has_closing(self):
        # 收尾段
        self.assertIn("20个季度的经营到这里告一段落", self.body)

    def test_no_currency_unit(self):
        # 禁「元」「人民币」「¥」「￥」「$」邻近数字
        self.assertNotRegex(self.body, r"\d\s*人民币")
        self.assertNotRegex(self.body, r"\d\s*元[\s　，。、；,.]|人民币\s*\d")
        for sym in ("¥", "￥", "$"):
            self.assertNotIn(sym, self.body)

    def test_no_banned_labels(self):
        for w in ("最赚钱", "惨败", "完美", "天才"):
            self.assertNotIn(w, self.body)

    def test_body_mentions_player_id(self):
        self.assertIn("p1", self.body)

    def test_body_lists_final_results(self):
        # 正式结果：终局现金 / 终局 GDP / 终局幸福度
        self.assertIn("28440.00", self.body)
        self.assertIn("19176.00", self.body)
        # 幸福度 0.75
        self.assertRegex(self.body, r"0\.75")


# ══════════════════════════════════════════════════════════════════
# ④ generate_finale_report：合法 JSON → source=model 且 charts/facts_used
#    被**引擎版**替换
# ══════════════════════════════════════════════════════════════════


class GenerateModelPathTests(unittest.TestCase):

    def test_model_path_replaces_charts_and_facts_used_with_engine(self):
        ov = _overview_20q()
        facts_probe = F.build_finale_facts(ov)
        content = _legal_finale_json(facts_probe)
        client = FakeClient(content)
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "model")
        self.assertTrue(out["ok"])
        # 客户端确实被调了一次
        self.assertEqual(len(client.calls), 1)
        # headline/body 是模型给的
        self.assertEqual(out["headline"], "《模型给的终局报告》")
        self.assertIn("模型正文", out["body"])
        # **但** charts 必须是引擎版（不是模型编造的那组）
        engine_charts = F.chart_points(ov)
        self.assertEqual(out["charts"], engine_charts)
        # 模型编造的所有点都是 999.99 / 0.99 —— 引擎版不可能全这样
        gdp_pts = out["charts"][0]["points"]
        self.assertEqual(gdp_pts[0]["value"],
                         ov["history"][0]["value_added"],  # 100.0，不是 999.99
                         "charts 必须是引擎版")
        # facts_used 也必须是引擎版（引擎包从 facts 里抽），**不是** {"foo.bar": 1.23}
        self.assertNotIn("foo.bar", out["facts_used"])
        self.assertNotIn("model.made.up", out["facts_used"])
        # 引擎版 facts_used 必须含 player_id / final.* / extremes.*
        self.assertEqual(out["facts_used"]["player_id"], "p1")
        self.assertIn("final.cash_after", out["facts_used"])
        self.assertIn("extremes.gdp.max.value", out["facts_used"])

    def test_model_path_headline_is_stripped_of_currency(self):
        """headline 里夹带「元」必须被剥掉。"""
        ov = _overview_20q()
        facts_probe = F.build_finale_facts(ov)
        obj = json.loads(_legal_finale_json(facts_probe))
        obj["headline"] = "《期末现金 28440 元 的终局报告》"
        client = FakeClient(json.dumps(obj, ensure_ascii=False))
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "model")
        self.assertNotIn("元", out["headline"])
        self.assertIn("28440", out["headline"])


# ══════════════════════════════════════════════════════════════════
# ⑤⑥ 失败路径 → fallback
# ══════════════════════════════════════════════════════════════════


class FallbackPathTests(unittest.TestCase):

    def test_offline_gives_fallback(self):
        ov = _overview_20q()
        client = FakeClient("", ok=False, error="offline",
                            detail="模型网关没开")
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "fallback")
        self.assertTrue(out["ok"])
        for k in ("headline", "body", "charts", "facts_used"):
            self.assertIn(k, out)
        self.assertTrue(out["warning"], "warning 非空")
        self.assertIn("offline", out["warning"])
        # body 是兜底模板：含四段标题
        self.assertIn("## 一、终局概览", out["body"])
        self.assertIn("## 二、GDP与幸福度曲线解读", out["body"])
        self.assertIn("## 三、经营历程回顾", out["body"])
        self.assertIn("## 四、完整数据回顾", out["body"])
        # charts 仍是引擎版
        self.assertEqual(out["charts"], F.chart_points(ov))

    def test_bad_response_gives_fallback(self):
        """模型回 `{"ok":True,"content":"不是json"}` → 落 fallback。"""
        ov = _overview_20q()
        client = FakeClient("不是json，只是闲聊")  # 完全不是 JSON
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "fallback")
        self.assertTrue(out["ok"])
        self.assertTrue(out["warning"])
        self.assertIn("bad_response", out["warning"])
        # 兜底 body
        self.assertIn("## 一、终局概览", out["body"])

    def test_model_json_missing_key_gives_fallback(self):
        """模型 JSON 缺 charts → 落 fallback。"""
        ov = _overview_20q()
        client = FakeClient(json.dumps({
            "headline": "x", "body": "y",
            "facts_used": {},   # 缺 charts
        }, ensure_ascii=False))
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "fallback")
        self.assertTrue(out["warning"])

    def test_model_charts_wrong_length_gives_fallback(self):
        """模型 charts points 长度不对 → 落 fallback。"""
        ov = _overview_20q()
        bad_charts = [
            {"id": "gdp", "title": "x", "metric": "gdp", "unit": None,
             "points": [{"quarter": 1, "value": 1.0}]},   # 只有 1 个
            {"id": "happiness", "title": "x", "metric": "happiness",
             "unit": None, "points": []},
        ]
        client = FakeClient(json.dumps({
            "headline": "x", "body": "y",
            "charts": bad_charts, "facts_used": {},
        }, ensure_ascii=False))
        out = F.generate_finale_report(ov, client=client)
        self.assertEqual(out["source"], "fallback")


# ══════════════════════════════════════════════════════════════════
# ⑦ game_over=False → 不宣称结束
# ══════════════════════════════════════════════════════════════════


class NotGameOverTests(unittest.TestCase):

    def test_early_progress_does_not_claim_game_over(self):
        ov = _overview_20q()
        ov["game_over"] = False
        ov["quarters_completed"] = 12
        ov["quarter"] = 13
        # 只保留前 12 季
        ov["history"] = ov["history"][:12]
        ov["history_complete"] = False
        out = F.generate_finale_report(ov, client=FakeClient("{}"))
        self.assertEqual(out["source"], "fallback")
        # warning 或 body 必须含「尚不具备」/「当前进度」/「进行中」
        warn = (out.get("warning") or "") + out["body"]
        self.assertTrue(
            ("尚不具备" in warn) or ("当前进度" in warn) or ("进行中" in warn),
            f"应明说「尚未完整结算」，实得 warning={out.get('warning')!r}",
        )
        # **不宣称**结束：不应出现「20个季度的经营到这里告一段落」
        self.assertNotIn("20个季度的经营到这里告一段落", out["body"])
        # charts 仍然 20 点，未来季为 null
        self.assertEqual(len(out["charts"][0]["points"]), 20)
        self.assertIsNone(out["charts"][0]["points"][15]["value"])  # 第 16 季无
        self.assertEqual(out["charts"][0]["points"][0]["value"],
                         100.0)  # 第 1 季有


# ══════════════════════════════════════════════════════════════════
# ⑧ narrate_finale_report（ai/narrate.py 追加的入口）
# ══════════════════════════════════════════════════════════════════


class NarrateFinaleTests(unittest.TestCase):

    def test_none_facts_returns_none(self):
        self.assertIsNone(N.narrate_finale_report(None, client=FakeClient("x")))

    def test_prompt_files_registered(self):
        """finale 必须已注册进 _PROMPT_FILES（追加式，不改字面 dict）。"""
        self.assertIn("finale", N._PROMPT_FILES)
        self.assertEqual(N._PROMPT_FILES["finale"], "finale.md")

    def test_load_prompt_finale_works(self):
        """load_prompt("finale") 应能读出提示词，且关键契约词都在。"""
        p = N.load_prompt("finale")
        self.assertTrue(len(p) > 400)
        self.assertIn("终局经营报告助手", p)
        self.assertIn("charts", p)
        self.assertIn("facts_used", p)

    def test_finale_path_bad_charts_gives_bad_response(self):
        facts = F.build_finale_facts(_overview_20q())
        client = FakeClient(json.dumps({
            "headline": "h", "body": "b",
            "charts": [{"id": "gdp"}],   # 只有一个，且形状烂
            "facts_used": {},
        }, ensure_ascii=False))
        out = N.narrate_finale_report(facts, client=client)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad_response")
        self.assertIn("charts", out["detail"])


# ══════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════


if __name__ == "__main__":
    unittest.main(verbosity=2)
