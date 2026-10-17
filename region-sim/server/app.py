"""HTTP 路由 —— **只转发，不放规则**（README 五条红线之五）。

依据《项目架构-总》§二（分层）与 §七（技术选型）。

═══════════════════════════════════════════════════════════════════
★ 为什么用标准库 `http.server`，而不是 Flask / FastAPI
═══════════════════════════════════════════════════════════════════

《项目架构-总》§七 写的是"FastAPI 或 Flask —— **接口只有几个，选熟的，别纠结**"。
本实现选了**标准库 `http.server`**，理由：

  ① **零依赖**。这个环境里没预装任何 Web 框架（实测 fastapi/flask/uvicorn
     全部 ModuleNotFoundError）。用标准库 = 换台机器、换个 Python 都能跑，
     "程序能初步跑起来"这件事不会被一次 pip 失败挡住。
  ② **接口确实只有几个**，而且**全部是转发**：收 JSON → 调 session → 回 JSON。
     框架的价值（路由分组、依赖注入、校验、ORM）这里一个都用不上。
  ③ 这一层**薄到可以整体替换** —— 路由表就是下面 `ROUTES` 那个 dict，
     将来真要换框架，改这一个文件即可，引擎与服务层一行不动。

⚠️ 所以这里**没有**、也**不该有**任何规则判断。
   合法/非法由 `engine.validate` 说了算，能不能结算是 `engine.report` 说了算。

═══════════════════════════════════════════════════════════════════
路由表
═══════════════════════════════════════════════════════════════════

    GET  /                        → web/index.html（前端）
    GET  /static/<路径>            → web/ 下的静态文件
    GET  /api/games                → 存档列表
    POST /api/new_game             → 开一局新的
    GET  /api/state?game=&player=  → /state（引擎算好的数字）
    GET  /api/map?game=&player=    → 整张 10×10
    POST /api/submit               → 提交计划
    POST /api/simulate             → ★ 试算（只读，AI/玩家共用）
    POST /api/validate             → ★ 只校验（只读）
    GET  /api/rules?[section=]      → ★ 规则基线原文（只读，给 AI）
    POST /api/end_quarter          → 结算这一季
    POST /api/next_quarter         → 看完月报进入下一季
    GET  /api/report?game=&player= → 上一季完整财报

═══════════════════════════════════════════════════════════════════
错误怎么回
═══════════════════════════════════════════════════════════════════

统一形状，让前端**不用解析字符串**就能提示：

    { "ok": false, "error": "计划被拒绝", "kind": "plan_rejected",
      "detail": "...", "issues": [ {code, target, message, ...} ] }

`kind` 是机器可读的分类（`conflict` / `plan_rejected` / `not_found` / ...），
`issues` 只在计划校验失败时出现（§11.3 的五要素就在里面）。

⚠️ **一个例外**：`POST /api/ai/advise` 的「AI 不可用」**不走 kind**，
   而是 200 + `ok: false` + `error`/`detail`（见 advise 端点的 docstring）——
   模型没起来是**常态不是异常**，不该让前端把它当成一次"请求失败"。
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# ══════════════════════════════════════════════════════════════════
# ★ 让 `python server/app.py` 也能跑
# ══════════════════════════════════════════════════════════════════
#
# 直接跑脚本时，`sys.path[0]` 是 `server/` 而不是项目根，
# 于是 `from engine import ...` 会 ModuleNotFoundError。
# 两种跑法（`python server/app.py` 与 `python -m server.app`）都支持，
# 免得启动方式变成一道坎。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import report  # noqa: E402
from engine.loader import Config, load_config  # noqa: E402
from engine.models import (  # noqa: E402
    BuildOrder,
    ContractAction,
    Plan,
    PlotPlan,
    PlotType,
)
from engine.validate import PlanRejected  # noqa: E402
from server.registry import GameRegistry  # noqa: E402
from server.session import Conflict  # noqa: E402
from storage import GameNotFound, JsonFileStorage, StorageError  # noqa: E402

from engine.models import SessionPhase  # noqa: E402
from server import ai_host  # noqa: E402  (AI 主机层：本文件只做转发)

# 前端根目录（region-sim/web）
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

# 《规则基线》（**规则唯一权威**，README §八 权威顺序 ②）。
# 本文件在 region-sim/server/ 下，规则在 开发特区/docs/01-规范/ 下，
# 所以从本文件往上退两层（server → region-sim → 开发特区）。
RULES_PATH = Path(__file__).resolve().parents[2] / "docs" / "01-规范" / "规则基线.md"
RULES_SOURCE = "docs/01-规范/规则基线.md"


def _read_rules_text() -> str:
    """读《规则基线》全文。**找不到就抛 `FileNotFoundError`**（不许装成功）。"""
    if not RULES_PATH.is_file():
        raise FileNotFoundError(f"规则基线不存在：{RULES_PATH}")
    return RULES_PATH.read_text(encoding="utf-8")


def _scan_sections(text: str) -> list[dict]:
    """从 Markdown 扫出 `#`/`##`/`###` 标题行。不用第三方库。"""
    sections: list[dict] = []
    for idx, line in enumerate(text.splitlines()):
        m = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if m:
            sections.append({
                "title": m.group(2),
                "level": len(m.group(1)),
                "line": idx + 1,          # 1-based，方便人去对照
            })
    return sections


def _slice_section(lines: list[str], sections: list[dict], keyword: str) -> str:
    """按关键词截出单节：从其标题到下一个**同级或更高级**标题之前。

    找不到命中的小节 → `KeyError`（调用方翻成 404）。
    """
    start_idx = None
    for i, s in enumerate(sections):
        if keyword in s["title"]:
            start_idx = i
            break
    if start_idx is None:
        raise KeyError(f"没有标题含 {keyword!r} 的小节")
    sec = sections[start_idx]
    # 结束位置 = 下一个 level ≤ 本节 的标题
    end_line = len(lines) + 1
    for s in sections[start_idx + 1:]:
        if s["level"] <= sec["level"]:
            end_line = s["line"]
            break
    return "\n".join(lines[sec["line"] - 1:end_line - 1])


# ══════════════════════════════════════════════════════════════════
# 请求解析
# ══════════════════════════════════════════════════════════════════


class BadRequest(Exception):
    """请求本身有问题（缺参数、JSON 坏了）。"""


def _need(params: dict, key: str) -> str:
    """取一个必填参数；缺了就报清楚缺哪个。"""
    val = params.get(key)
    if not val:
        raise BadRequest(f"缺少参数 {key!r}")
    return val


def plan_from_dict(data: dict) -> Plan:
    """把前端传来的 JSON 还原成 `Plan`。

    ⚠️ 这里**只做形状转换，不做合法性判断** ——
       合法与否由 `engine.validate` 说了算（避免规则散到服务层）。
    """
    ptype_map = {p.value: p for p in PlotType}

    plot_plans: dict[str, PlotPlan] = {}
    for pid, pp in (data.get("plot_plans") or {}).items():
        plot_plans[pid] = PlotPlan(
            plot_id=pid,
            active=bool(pp.get("active", True)),
            u=float(pp.get("u", 1.0)),
        )

    builds: list[BuildOrder] = []
    for b in (data.get("builds") or []):
        raw_type = b.get("plot_type")
        if raw_type not in ptype_map:
            raise BadRequest(
                f"未知地块类型 {raw_type!r}；可选：{sorted(ptype_map)}"
            )
        builds.append(BuildOrder(
            plot_id=b["plot_id"],
            plot_type=ptype_map[raw_type],
            target_level=int(b.get("target_level", 1)),
            product=b.get("product"),
        ))

    contracts = [ContractAction(**c) for c in (data.get("contracts") or [])]

    return Plan(
        player_id=data["player_id"],
        plot_plans=plot_plans,
        purchases={g: float(q) for g, q in (data.get("purchases") or {}).items()},
        food={g: float(q) for g, q in (data.get("food") or {}).items()},
        builds=builds,
        contracts=contracts,
    )


# ══════════════════════════════════════════════════════════════════
# 应用（路由与实现，**与 HTTP 解耦** —— 方便直接单测）
# ══════════════════════════════════════════════════════════════════


class App:
    """全部端点的实现。**只转发，不放规则。**

    ⚠️ 刻意不继承 `BaseHTTPRequestHandler`：
       这样端点能**脱离 socket 直接测**（`tests/test_server.py` 就是这么干的）。

    ⚠️ **所有端点签名统一为 `(self, arg: dict)`** ——
       GET 收 query 参数、POST 收 body。`dispatch()` 因此不需要
       猜测该传几个参数（曾经因为 `games()` 不收参数而炸过一次）。
       用不到参数就收下不用。
    """

    def __init__(self, cfg: Config, registry: GameRegistry) -> None:
        self.cfg = cfg
        self.registry = registry

    # ── 端点 ──────────────────────────────────────────────

    def games(self, _params: dict) -> dict:
        return {"ok": True, "games": self.registry.list_games()}

    def new_game(self, body: dict) -> dict:
        game_id = body.get("game_id") or "g1"
        player_id = body.get("player_id") or "p1"
        session = self.registry.create(game_id, player_id)
        return {
            "ok": True,
            "game_id": game_id,
            "player_id": player_id,
            "state": session.state_for(player_id),
        }

    def state(self, params: dict) -> dict:
        session = self.registry.get(_need(params, "game"))
        player_id = params.get("player", "p1")
        return {"ok": True, "state": session.state_for(player_id)}

    def map(self, params: dict) -> dict:
        session = self.registry.get(_need(params, "game"))
        player_id = params.get("player", "p1")
        return {"ok": True, "map": session.map_for(player_id)}

    def report(self, params: dict) -> dict:
        session = self.registry.get(_need(params, "game"))
        player_id = params.get("player", "p1")
        return {
            "ok": True,
            "report": session.state.report.get(player_id),
        }

    def submit(self, body: dict) -> dict:
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        plan = plan_from_dict(body.get("plan") or {})
        session.submit(player_id, plan)
        return {"ok": True, "state": session.state_for(player_id)}

    # ── ★ AI 秘书的三个只读端点（§11.4）──────────────────
    #
    # ⚠️ 三个端点**全部只读**：不落存档、不改 GameState、不动 plans。
    #    AI 可解释 / 可试算 / 可起草，但 **AI 无 commit** ——
    #    提交与结算永远走上面的 /api/submit，由玩家确认。

    def simulate(self, body: dict) -> dict:
        """试算：跑一遍真引擎的副本，返回**本季试算报告**。

        ⚠️ **时机与 /api/submit 同口径**：不在行动阶段 → 409 `conflict`。
        ⚠️ **先校验再试算**：`preview` 本身不查计划合法性（它只跑引擎），
           所以这里**显式**走 `validate_plan` —— 与 /api/submit 是同一处校验，
           非法 → 400 `plan_rejected` + 五要素 issues，不会让引擎在 KeyError 上炸成 500。
        """
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        plan = plan_from_dict(body.get("plan") or {})
        if session.phase is not SessionPhase.COLLECTING:
            raise Conflict(
                f"当前不在行动阶段（{session.phase.value}），不能试算"
            )
        issues = session.validate_only(plan, player_id)
        if issues:
            from engine.validate import PlanRejected
            raise PlanRejected(issues)
        rep = session.simulate(plan)
        return {"ok": True, "report": rep.to_dict()}

    def validate(self, body: dict) -> dict:
        """只校验，不试算、不落任何状态。

        形状与 /api/submit 完全相同：合法 → 200 `{ok, valid, issues: []}`；
        非法 → 400 `plan_rejected` + 五要素 issues（前端与 AI 用同一套解析）。
        """
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        plan = plan_from_dict(body.get("plan") or {})
        issues = session.validate_only(plan, player_id)
        if issues:
            from engine.validate import PlanRejected
            raise PlanRejected(issues)
        return {"ok": True, "valid": True, "issues": []}

    def rules(self, params: dict) -> dict:
        """把《规则基线》原样给出去（给 AI 用）。

        `?section=关键词`：给了就只回该小节（从其标题到下一个同级或更高级标题之前）；不给就回全文。
        """
        text = _read_rules_text()
        sections = _scan_sections(text)
        # 第一行（首个 `# ` 行）作为 title
        first_line = text.splitlines()[0] if text else ""
        m = re.match(r"^#\s+(.+?)\s*$", first_line)
        title = m.group(1) if m else first_line.lstrip("# ").strip()

        section = params.get("section")
        if section:
            sliced = _slice_section(text.splitlines(), sections, section)
        else:
            sliced = text
        return {
            "ok": True,
            "source": RULES_SOURCE,
            "title": title,
            "text": sliced,
            "sections": sections,
        }

    def advise(self, body: dict) -> dict:
        """AI 建议（§11.4）：把局面递给主机层，拿回一份**只读建议**。

        ⚠️ **只转发**：数字与措辞全部由 `server.ai_host.advise` 给出，
           本方法不改写、不补全、不把失败包装成建议。

        ⚠️ **AI 不可用 / 拒绝 / 试算失败 → HTTP 200 + `{ok: false}`**：
           「网关没起」是常态不是异常，不该让前端把它当成一次"请求失败"
           （500/503 会触发前端的重试与错误弹层，而这事玩家点一下重发
           就能好）。只有**请求本身不齐**（缺 game/player、局不存在）
           才按老规矩走 kind（400/404）。

        返回形状（成功与失败同一形状，前端只查 `ok`）：

            成功：{ok: true, advice: {ok: true, summary, suggested_plan,
                                       evidence, model, ...}}
            失败：{ok: false, advice: {ok: false, error, detail, ...}}
        """
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        advice = ai_host.advise(session, player_id, body.get("question") or None)
        return {"ok": bool(advice.get("ok")), "advice": advice}

    def end_quarter(self, body: dict) -> dict:
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        rep = session.end_quarter(player_id)
        return {
            "ok": True,
            "report": rep.to_dict() if rep is not None else None,
            # ⚠️ with_narratives=False：结算响应**不生成叙事**。
            #    否则这行会让「点结算」顺手调两路模型（2026-10-17 实测 +173 秒）。
            #    叙事等玩家进入下季、真正读 /api/state 时再生成
            #    （用户裁定：结算后、下季行动前）。
            "state": session.state_for(player_id, with_narratives=False),
        }

    def next_quarter(self, body: dict) -> dict:
        session = self.registry.get(_need(body, "game"))
        player_id = _need(body, "player")
        session.start_next_quarter()
        # 同上：推进季度只返数字，不在这里等模型。
        return {"ok": True,
                "state": session.state_for(player_id, with_narratives=False)}

    def build_options(self, params: dict) -> dict:
        """建造面板的下拉选项（**前端零计算**）。

        ⚠️ 选项由引擎给（`validate.buildable_products`），
           前端不许自己拼 —— 否则规则一改前端就过期。
        """
        from engine.construction import load_works
        from engine.validate import buildable_products

        works = load_works(self.cfg)
        rows = []
        for work_id, w in sorted(works.items()):
            rows.append({
                "work_id": work_id,
                "label": w.label,
                "plot_type": w.plot_type.value,
                "from_level": w.from_level,
                "to_level": w.to_level,
                "cash": w.cash,
                "materials": dict(w.materials),
                "quarters": w.quarters,
                "route": w.route,
                # 多路线类型需要玩家在下拉框里选产出（建后不可更换，§5.1）
                "products": buildable_products(w.plot_type, w.to_level),
            })
        return {"ok": True, "works": rows}


# ══════════════════════════════════════════════════════════════════
# 路由分派（把异常翻译成 HTTP）
# ══════════════════════════════════════════════════════════════════

#: 异常 → (HTTP 状态码, 机器可读的 kind)
_ERROR_MAP: list[tuple[type, int, str]] = [
    (BadRequest, 400, "bad_request"),
    (PlanRejected, 400, "plan_rejected"),
    (Conflict, 409, "conflict"),
    (GameNotFound, 404, "not_found"),
    (KeyError, 404, "not_found"),
    (ValueError, 400, "bad_input"),
    (StorageError, 500, "storage_error"),
]


def _error_payload(e: Exception) -> tuple[int, dict]:
    """把异常翻成 `(状态码, 响应体)`。"""
    for exc_type, code, kind in _ERROR_MAP:
        if isinstance(e, exc_type):
            body: dict[str, Any] = {
                "ok": False,
                "kind": kind,
                "error": type(e).__name__,
                "detail": str(e),
            }
            # 计划被拒时，把 §11.3 的结构化 issues 一起给前端
            issues = getattr(e, "issues", None)
            if issues:
                body["issues"] = [
                    i.to_dict() if hasattr(i, "to_dict") else str(i) for i in issues
                ]
            return code, body

    return 500, {
        "ok": False,
        "kind": "internal",
        "error": type(e).__name__,
        "detail": str(e),
    }


#: 路径 → (方法, 要不要 body, 调用哪个方法)
ROUTES: dict[str, tuple[str, bool, str]] = {
    "/api/games":         ("GET",  False, "games"),
    "/api/new_game":      ("POST", True,  "new_game"),
    "/api/state":         ("GET",  False, "state"),
    "/api/map":           ("GET",  False, "map"),
    "/api/report":        ("GET",  False, "report"),
    "/api/build_options": ("GET",  False, "build_options"),
    "/api/submit":        ("POST", True,  "submit"),
    "/api/simulate":      ("POST", True,  "simulate"),
    "/api/validate":      ("POST", True,  "validate"),
    "/api/rules":         ("GET",  False, "rules"),
    "/api/ai/advise":     ("POST", True,  "advise"),
    "/api/end_quarter":   ("POST", True,  "end_quarter"),
    "/api/next_quarter":  ("POST", True,  "next_quarter"),
}


def dispatch(app: App, method: str, path: str, params: dict, body: dict):
    """分派一次请求。返回 `(状态码, 响应体)`。

    ⚠️ 抽成纯函数是为了**能直接测** —— 不用起 socket。
    """
    route = ROUTES.get(path)
    if route is None:
        return 404, {
            "ok": False, "kind": "no_route",
            "detail": f"没有这个端点：{path}",
            "routes": sorted(ROUTES),
        }

    want_method, wants_body, handler_name = route
    if method != want_method:
        return 405, {
            "ok": False, "kind": "method_not_allowed",
            "detail": f"{path} 只接受 {want_method}，收到 {method}",
        }

    try:
        handler: Callable = getattr(app, handler_name)
        result = handler(body if wants_body else params)
        return 200, result
    except Exception as e:                       # noqa: BLE001
        # ★ 收网：任何异常都翻成 JSON，**不让服务端 500 空响应**
        #   （前端拿到空 body 会更难查）
        return _error_payload(e)


# ══════════════════════════════════════════════════════════════════
# HTTP 外壳
# ══════════════════════════════════════════════════════════════════


class Handler(BaseHTTPRequestHandler):
    """把 HTTP 请求翻译成 `dispatch()` 调用。**这一层不含业务。**"""

    server_version = "region-sim/0.1"
    app: App                       # 由 make_server 注入
    web_root: Path = WEB_ROOT

    # ── 工具 ──────────────────────────────────────────────

    def _send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"ok": False, "kind": "not_found",
                                  "detail": f"文件不存在：{path.name}"})
            return
        raw = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BadRequest(f"请求体不是合法 JSON：{e}") from None
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是一个 JSON 对象")
        return data

    # ── 分派 ──────────────────────────────────────────────

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # 静态资源（前端）
        if method == "GET" and not path.startswith("/api/"):
            if path in ("/", "/index.html"):
                self._send_file(self.web_root / "index.html")
                return
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                target = (self.web_root / rel).resolve()
                # ★ 防目录穿越：解析后必须仍在 web_root 里
                if not str(target).startswith(str(self.web_root.resolve())):
                    self._send_json(403, {"ok": False, "kind": "forbidden",
                                          "detail": "路径越界"})
                    return
                self._send_file(target)
                return
            self._send_json(404, {"ok": False, "kind": "no_route",
                                  "detail": f"没有这个路径：{path}"})
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._read_body() if method == "POST" else {}
        except BadRequest as e:
            self._send_json(*_error_payload(e))
            return

        code, payload = dispatch(self.app, method, path, params, body)
        self._send_json(code, payload)

    def do_GET(self) -> None:                       # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:                      # noqa: N802
        self._handle("POST")

    def log_message(self, fmt: str, *args) -> None:
        """默认日志太吵；保留一行简短的。"""
        print(f"  {self.address_string()} {fmt % args}", flush=True)


def make_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    saves_dir: str | Path = "saves",
    cfg: Config | None = None,
) -> ThreadingHTTPServer:
    """起一个服务器（**不起线程** —— 由调用方决定怎么跑）。"""
    cfg = cfg or load_config()
    storage = JsonFileStorage(saves_dir)
    registry = GameRegistry(cfg, storage)
    app = App(cfg, registry)

    # 用 type() 造子类，把 app / web_root 绑上去（避免全局变量）
    handler = type("BoundHandler", (Handler,), {"app": app, "web_root": WEB_ROOT})

    # ThreadingHTTPServer：一个请求卡住不会拖住整个界面。
    # ⚠️ 并发安全由 GameSession 的【每局一把锁】保证（§1.3.1），不是靠单线程。
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="区域发展模拟 —— 开发服务器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--saves", default="saves", help="存档目录")
    args = ap.parse_args(argv)

    httpd = make_server(args.host, args.port, args.saves)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 60)
    print("区域发展模拟 —— 服务已启动")
    print(f"  地址：{url}")
    print(f"  存档：{Path(args.saves).resolve()}")
    print("  Ctrl+C 停止")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
