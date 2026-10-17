"""HTTP 冒烟 —— **一条命令证明"程序跑得起来"**。

跑法（在 region-sim/ 下）：

    python tools/smoke_http.py

它会：起一个真的 HTTP 服务（随机空闲端口）→ 开新局 → 下建设指令 →
结算一季 → 读状态 → 读存档 → 关服务，每一步都断言。

⚠️ 与 `tests/test_server.py` 的分工：
   那边直接调 `dispatch()`（不起 socket），测**路由与错误翻译**；
   这边**真起 socket、真发 HTTP**，测"整套东西装得起来"。

⚠️ 它**不碰你的 saves/**：用自己的临时目录，跑完就删。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config          # noqa: E402
from server.app import make_server             # noqa: E402

TOL = 1e-6
_ok = _fail = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  OK    {label}")
    else:
        _fail += 1
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main() -> int:
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        # 端口 0 = 让系统给一个空闲端口，不跟别的服务抢
        httpd = make_server("127.0.0.1", 0, Path(tmp) / "saves", cfg=cfg)
        host, port = httpd.server_address[:2]
        base = f"http://{host}:{port}"
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        print("=" * 62)
        print(f"HTTP 冒烟　{base}")
        print("=" * 62)
        try:
            # ── 静态前端 ──────────────────────────────────
            with urllib.request.urlopen(base + "/", timeout=10) as r:
                html = r.read().decode("utf-8")
            check("首页能打开（前端已装）", r.status == 200 and 'id="map"' in html,
                  f"status={r.status} len={len(html)}")

            # ── 开新局 ────────────────────────────────────
            code, body = call(base, "POST", "/api/new_game", {"game_id": "smoke"})
            check("开新局", code == 200, str(body))
            st = body["state"]
            check("开局现金 = 1200", abs(st["cash"] - 1200) < TOL, str(st["cash"]))
            check("开局零地块（§5.3 全是可建空地）", st["plots"] == [], str(st["plots"]))
            check("一局 20 季", st["total_quarters"] == 20, str(st["total_quarters"]))

            # ── 建造选项 ──────────────────────────────────
            code, body = call(base, "GET", "/api/build_options")
            check("工程表 14 条（§5.2）", len(body["works"]) == 14,
                  str(len(body["works"])))

            # ── 下建设指令 ────────────────────────────────
            plan = {"player_id": "p1", "builds": [
                {"plot_id": "3,4", "plot_type": "agriculture",
                 "target_level": 1, "product": "A"},
            ]}
            code, body = call(base, "POST", "/api/submit",
                              {"game": "smoke", "player": "p1", "plan": plan})
            check("提交建设指令", code == 200, str(body))
            check("可以推进（can_settle）", body["state"]["can_settle"] is True,
                  str(body["state"]["settle_blockers"]))
            check("★ 提交阶段还没有地块（第 2 步不改状态）",
                  body["state"]["plots"] == [], str(body["state"]["plots"]))

            # ── 结算 ──────────────────────────────────────
            code, body = call(base, "POST", "/api/end_quarter",
                              {"game": "smoke", "player": "p1"})
            check("结算成功", code == 200, str(body))
            st = body["state"]
            check("进入第 2 季", st["quarter"] == 2, str(st["quarter"]))
            # ★ 2026-10-16 用户裁定：开局每类 3 个减免名额 × 0.50（只打现金费）。
            #   于是这块新建一级农业实付 = 现金 40（原价 80 折半）+ 物资 64 = 104。
            # ★ 2026-10-17 用户裁定：每季固定 120 金补贴 → 1200 − 104 + 120 = 1216。
            check("★ 扣款 104 且含每季补贴 120（1200−104+120=1216）",
                  abs(st["cash"] - 1216.0) < TOL, str(st["cash"]))
            # ★ 2026-10-17 用户裁定：本季投钱投料也算一季工期 →
            #   1 季工期的新建工程**当季完工**，结算后是 running（不再是在建）。
            check("地块已创建、1 季工期当季完工（running）", len(st["plots"]) == 1
                  and st["plots"][0]["status"] == "running", str(st["plots"]))
            check("产出建造时选定 = A",
                  st["plots"][0]["product"] == "A", str(st["plots"][0]))

            # ── 报告 ──────────────────────────────────────
            code, body = call(base, "GET", "/api/report?game=smoke&player=p1")
            check("上季报告可取", body["report"] is not None)

            # ── 工期推进 ──────────────────────────────────
            call(base, "POST", "/api/next_quarter", {"game": "smoke", "player": "p1"})
            call(base, "POST", "/api/submit",
                 {"game": "smoke", "player": "p1", "plan": {"player_id": "p1"}})
            call(base, "POST", "/api/end_quarter", {"game": "smoke", "player": "p1"})
            code, body = call(base, "GET", "/api/state?game=smoke&player=p1")
            p = body["state"]["plots"][0]
            check("★ 工期到点完工、等级落地",
                  p["status"] == "running" and p["level"] == 1, str(p))

            # ── ★ 读档（模拟服务重启：直接从磁盘新建一个 registry）──
            from server.app import App, dispatch
            from server.registry import GameRegistry
            from storage import JsonFileStorage
            fresh = GameRegistry(cfg, JsonFileStorage(Path(tmp) / "saves"))
            code, body = dispatch(App(cfg, fresh), "GET", "/api/state",
                                  {"game": "smoke", "player": "p1"}, {})
            check("★ 从磁盘读档：季度/现金对得上",
                  body["state"]["quarter"] == 3, str(body["state"]["quarter"]))
            check("★ 读档后上季报告仍在（本轮修的 bug）",
                  body["state"]["report"] is not None, "报告丢了")

            # ── 错误分支 ──────────────────────────────────
            code, body = call(base, "GET", "/api/state?game=nope")
            check("不存在的局 → 404 not_found",
                  code == 404 and body["kind"] == "not_found", f"{code} {body}")

        finally:
            httpd.shutdown()
            httpd.server_close()

    print("-" * 62)
    print(f"{_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
