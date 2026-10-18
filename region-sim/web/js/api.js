/* ══════════════════════════════════════════════════════════════
   api.js —— HTTP 客户端 + 服务端非 JSON 兜底 + 慢请求加载态 +
   ⑥ 结算流水逐条涌现 + toast/fail + fmt 三件套 + 极简 Markdown
   (esc / mdInline / miniMD / factsTable)。
   从原 index.html `<script>` 顶部搬出,函数内部一行未改。

   本文件还集中声明了所有跨文件共享的全局(它们是非 module script 的
   顶层 let/const,在全局词法环境中,后续 <script src> 仍能读到——
   这是原单文件工作方式的延续,不是改动):
     $  GAME  PLAYER  STATE  MAP  WORKS  selected  plan  TYPE_CN
     emptyPlan()  resetPlan()
   ══════════════════════════════════════════════════════════════ */
"use strict";

const $ = id => document.getElementById(id);
let GAME = null, PLAYER = "p1";
let STATE = null, MAP = null, WORKS = [];
let selected = null;          // 选中的格子
let plan = emptyPlan();

/* 类型短码（显示层常量，不参与任何计算） */
const TYPE_CN = {agriculture:"农", resource:"资", industry:"工",
                 service:"服", research:"研"};

/* ★ 中文 zone 全名（集中定义,以后加 zone 只改这一处）——
   表格"地块"列、地图 cell 第一行、3D cell 悬浮字 都用这一份完整二字名。
   短码 TYPE_CN 仍保留,只用于紧凑行内"农 已用 2/3"之类的行内文案。 */
const ZONE_CN = {
  agriculture: "农业",
  resource:    "资源",
  industry:    "工业",
  service:     "服务",
  research:    "研究",
};

function emptyPlan(){
  return { plot_plans:{}, purchases:{}, food:{}, builds:[], contracts:[] };
}
function resetPlan(){ plan = emptyPlan(); }

/* ── HTTP ─────────────────────────────────────────────── */
async function api(method, path, body, params){
  const qs = params ? "?" + new URLSearchParams(params) : "";
  const res = await fetch(path + qs, {
    method,
    headers: body ? {"Content-Type":"application/json"} : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data;
  try { data = await res.json(); }
  catch(e){ throw new Error(`服务端返回了非 JSON（HTTP ${res.status}）`); }
  if (!res.ok || data.ok === false){
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

/* ── 慢请求加载态（首读 /api/state 触发叙事生成时才显形）────────
   机制：
     · SL_DELAY_MS = 2500：请求发出后先**不**显示，只起一个定时器。
     · 若请求在 2.5s 内回来（缓存命中 ~0.03s），定时器被清掉 ——
       加载态**根本没插入 DOM**，玩家完全无感。
     · 超过 2.5s 才插 DOM；之后每秒刷一次「已等待 N 秒」。
     · 请求结束（无论成败）→ clearTimeout + 撤 DOM + 停秒表。
   期间禁掉会重复触发的按钮（推进本季度/下一季/全部满开工/全部停工/
   下达建设指令/读档），防止连点把两路生成再点一轮。成功/失败由
   调用方收尾（finally 里 hideStateLoading + 解锁）。 */
const SL_DELAY_MS = 2500;
let _slTimer = null, _slTick = null, _slStart = 0, _slDepth = 0;

function showStateLoadingDelayed(){
  _slDepth++;
  if (_slTimer || _slTick) return;   // 已经在等/显示 → 别叠
  _slStart = Date.now();
  _slTimer = setTimeout(() => {
    _slTimer = null;
    const el = $("state-loading");
    el.classList.remove("hidden");
    $("sl-elapsed").textContent = "0";
    setActionButtonsDisabled(true);
    _slTick = setInterval(() => {
      const s = Math.floor((Date.now() - _slStart) / 1000);
      $("sl-elapsed").textContent = String(s);
    }, 1000);
  }, SL_DELAY_MS);
}

function hideStateLoading(){
  _slDepth = Math.max(0, _slDepth - 1);
  if (_slDepth > 0) return;          // 还有未完成的并发请求 → 继续显示
  if (_slTimer){ clearTimeout(_slTimer); _slTimer = null; }
  if (_slTick){ clearInterval(_slTick); _slTick = null; }
  const el = $("state-loading");
  if (el && !el.classList.contains("hidden")) el.classList.add("hidden");
  setActionButtonsDisabled(false);
}

/* ★ 2026-10-18：**强制**撤遮罩 —— 不信任 `_slDepth` 计数。
   背景：`showStateLoadingNow` 并**不**增加 `_slDepth`，而异常/超时路径可能让
   计数残留（比如某次 showStateLoadingDelayed 的 finally 没跑到），于是
   `hideStateLoading()` 会因 `_slDepth > 0` 提前 return —— 遮罩永远不收。
   「推进本季度」这种**必须收回遮罩**的出口直接调这个：计数归零 + 停表 + 收 DOM。 */
function forceHideStateLoading(){
  _slDepth = 0;
  if (_slTimer){ clearTimeout(_slTimer); _slTimer = null; }
  if (_slTick){ clearInterval(_slTick); _slTick = null; }
  const el = $("state-loading");
  if (el && !el.classList.contains("hidden")) el.classList.add("hidden");
  try{ setActionButtonsDisabled(false); }catch(_e){ /* 解锁失败不能连累撤遮罩 */ }
}

/* ══════════════════════════════════════════════════════════
   ★ ⑥ 等待时把结算细节**逐条涌现**（替代干等）
   ══════════════════════════════════════════════════════════
   数据源（**全部是引擎给的**，前端零编）：
     · POST /api/end_quarter 的回包 `report`（结算结束**立刻**有）
     · 季度结算字段：production / sold / total_deduction / sales_revenue /
       cash_before / cash_after / value_added / food.happiness / policy ……
       （《QuarterReport.to_dict》）
   流程（"推进本季度"按下时）：submit → end_quarter（**立刻**拿到 report）
     → 先把 report 里的关键项**每 ~320ms 一条**逐条推给还没显形的加载框
     → 推完报告（或根本没有报告）→ 标题切成"正在撰写新闻…"，进入 /api/state
       的首读等待（model 生成可能要 1–3 分钟）。
   ⚠️ **绝不"编"任何一条**：只在引擎真给了那个字段才上一行；
     一个字段都没有（比如还有些"在建尚未完工"）就只推"结算第 N 季"那一行，
     加载框快速滑过而**不会当成报错**。 */

/* 让加载框立刻显形（不走 2.5s 延迟），标题换成"结算第 N 季…"。
   专给"推进本季度"用；普通 refresh 还是走延迟。 */
function showStateLoadingNow(title, sub){
  /* 已经在显示（等 2.5s 那个 setTimeout 还没触发）→ 直接抢占。 */
  if (_slTimer){ clearTimeout(_slTimer); _slTimer = null; }
  const el = $("state-loading");
  el.classList.remove("hidden");
  if (title) $("sl-title").textContent = title;
  if (sub)   $("sl-sub").textContent   = sub;
  $("sl-elapsed").textContent = "0";
  const flow = $("sl-flow"); if (flow) flow.innerHTML = "";
  setActionButtonsDisabled(true);
  if (!_slTick){
    _slTick = setInterval(() => {
      const s = Math.floor((Date.now() - _slStart) / 1000);
      $("sl-elapsed").textContent = String(s);
    }, 1000);
  }
}

/* 2026-10-18：叙事已从 /api/state 拆到 /api/narratives（后台生成）。
   这里不再有“撰写新闻/财报”那档首读等待；数字先出，叙事后到。 */

/* 推一条进结算流水（只在加载框已显形时能看到；没显形就静默）。
   kind ∈ "" | "good" | "warn" | "dim"。 */
function pushSettlementLine(text, kind){
  const flow = $("sl-flow");
  if (!flow) return;
  const div = document.createElement("div");
  if (kind) div.className = kind;
  div.textContent = text;
  flow.appendChild(div);
  flow.scrollTop = flow.scrollHeight;
}

/* 从引擎的 report 提取"逐条涌现"的那几行。**一个字段一个字段地查**：
   有就上、没有就跳；绝不替引擎"总结"任何东西（那是 AI 的事）。
   行序按《基线》季度推进的顺序：建成 → 生产 → 扣款 → 收入 → 期末现金 → 幸福度 → 政策。 */
function settlementLinesFromReport(r){
  if (!r || typeof r !== "object") return [];
  const lines = [];
  /* 季度头 */
  lines.push({text:`结算第 ${r.quarter} 季…`, kind:"dim"});
  /* 建成（production 里没有"建成"行——它是"在产"；建成走 construction_left=0。
     引擎没单独给"建成"清单，所以这里**不上**建成行——只上真实存在的。） */

  /* 生产：每块地一行 */
  const prod = Array.isArray(r.production) ? r.production : [];
  const producing = prod.filter(o => o && (o.output_qty || o.u));
  if (producing.length){
    producing.forEach(o => {
      const u  = (o.u != null) ? Math.round(Number(o.u)*100) : null;
      const bits = [];
      if (u != null) bits.push(`开工 ${u}%`);
      if (o.output_goods) bits.push(`产出 ${o.output_goods} ×${fmt(o.output_qty,1)}`);
      const suffix = bits.length ? `（${bits.join(" · ")}）` : "";
      lines.push({
        text:`✓ 生产：${o.plot_id}（${TYPE_CN[o.plot_type]||o.plot_type||"?"}${o.level||""}${o.product?"·"+o.product:""}）${suffix}`,
        kind:"good",
      });
    });
  } else {
    lines.push({text:"· 本季无生产（没开工/没地块）", kind:"dim"});
  }

  /* 工程事件：本季开工 / 完工（引擎 `construction_events`，2026-10-18 新增）。
     有就逐条上；没有就跳过 —— 不替引擎补“应该建成了”这种话。 */
  const events = Array.isArray(r.construction_events) ? r.construction_events : [];
  events.forEach(e => {
    if (!e || !e.plot_id) return;
    const t = TYPE_CN[e.plot_type] || e.plot_type || "?";
    const route = e.product ? `·${e.product}` : "";
    const lv = `${t}${e.from_level != null ? e.from_level : "?"}→${e.to_level != null ? e.to_level : "?"}${route}`;
    if (e.event === "completed"){
      lines.push({text:`✓ 工程完工：${e.plot_id}（${lv}）`, kind:"good"});
    } else if (e.event === "started"){
      lines.push({text:`✓ 工程开工：${e.plot_id}（${lv}）`, kind:""});
    }
  });

  /* 工程：本季工程现金费 + 实际投入物资（r.construction 是 {商品:数量}）。
     ★ 2026-10-18 补：玩家要求结算时能看到“工程情况”。 */
  const cons = (r.construction && typeof r.construction === "object") ? r.construction : {};
  const consKeys = Object.keys(cons);
  if ((r.construction_cash || 0) > 0 || consKeys.length){
    const mats = consKeys.sort()
      .map(g => `${g}×${fmt(cons[g],1)}`).join("、") || "无";
    lines.push({
      text:`✓ 工程：现金 ${fmt(r.construction_cash || 0)} ／ 物资 ${mats}`,
      kind:"",
    });
  }

  /* 卖出：X_t 每商品一行 */
  const sold = r.sold && typeof r.sold === "object" ? r.sold : {};
  const soldKeys = Object.keys(sold);
  if (soldKeys.length){
    soldKeys.sort().forEach(g => {
      const qty = Number(sold[g]);
      if (!(qty > 0)) return;
      const price = r.prices && r.prices[g] && r.prices[g].price;
      const priceTxt = (price != null) ? ` @ ${fmt(price)}（本季终价）` : "";
      lines.push({text:`✓ 成交：卖出 ${g} ×${fmt(qty,1)}${priceTxt}`, kind:"good"});
    });
  } else {
    lines.push({text:"· 本季无卖出", kind:"dim"});
  }

  /* 扣款三件套（材料 / 工资 / 经营）+ 一次性扣款 */
  const anyCost = (r.materials_cost || r.wages || r.operating || r.total_deduction) != null;
  if (anyCost){
    lines.push({
      text:`✓ 扣款：材料 ${fmt(r.materials_cost)} ／ 工资 ${fmt(r.wages)} ／ 经营 ${fmt(r.operating)}`,
      kind:"",
    });
    if (r.total_deduction != null){
      lines.push({text:`✓ 一次性扣款：${fmt(r.total_deduction)}`, kind:""});
    }
    if (r.wage_tax_refund){
      lines.push({text:`✓ 工资税回流：+${fmt(r.wage_tax_refund)}`, kind:"good"});
    }
  }

  /* 收入 */
  if (r.sales_revenue != null){
    lines.push({text:`✓ 销售收入（本季价）：+${fmt(r.sales_revenue)}`, kind:"good"});
  }

  /* 期末现金（现金变化最直观的一行） */
  if (r.cash_before != null && r.cash_after != null){
    lines.push({
      text:`✓ 现金：${fmt(r.cash_before)} → ${fmt(r.cash_after)}`,
      kind:"good",
    });
  }

  /* 增加值（GDP 参考价口径） */
  if (r.value_added != null){
    lines.push({text:`✓ 增加值 GDP（参考价）：${fmt(r.value_added)}`, kind:""});
  }

  /* 幸福度 */
  const h = r.food && r.food.happiness;
  if (h != null){
    lines.push({text:`✓ 幸福度：${fmt(h)}`, kind:""});
  }

  /* 政策（本季抽到的） */
  if (r.policy && r.policy.announcement){
    lines.push({text:`✓ 政策：${String(r.policy.announcement)}`, kind:"warn"});
  }

  return lines;
}

/* 让 lines 每 ~320ms 上一条（"流水"感）。返回一个 Promise：推完才 resolve。
   若玩家根本就没展开加载框（report 极快返回），也会把行推完——没问题，
   下一次 hideStateLoading 时整张框被撤掉，玩家看不到"曾经滚过一堆"。

   ★ 2026-10-18 修「流水 Promise 永挂」：
     ① 推完**最后一行**就 resolve（原来要多等一个 interval 才进 `i>=length` 分支）；
     ② `pushSettlementLine` 单行抛错只跳过该行，**不让整条流水断掉**；
     ③ setTimeout 回调里的异常原本会被浏览器吞掉、Promise 永不 resolve
        （结算按钮会一直等）—— 现在统一 `finish()` 兜底 resolve。
   调用方仍建议加时间闸（见 panel.js 的 8s watchdog）。 */
function streamSettlementLines(lines, intervalMs){
  return new Promise(resolve => {
    let done = false;
    const finish = () => { if (done) return; done = true; resolve(); };
    try{
      if (!lines || !lines.length){ finish(); return; }
      const gap = (intervalMs == null) ? 320 : intervalMs;
      let i = 0;
      const step = () => {
        try{
          if (i >= lines.length){ finish(); return; }      // ① 推完 → 一定 resolve
          const l = lines[i++];
          try{ pushSettlementLine(l.text, l.kind); }        // ② 单行失败不影响流水
          catch(_e){ /* 忽略这一行 */ }
          if (i >= lines.length){ setTimeout(finish, gap); return; }  // 最后一行留一拍
          setTimeout(step, gap);
        }catch(_e){ finish(); }                             // ③ 永不悬挂
      };
      step();
    }catch(_e){ finish(); }
  });
}

/* 会重复触发"拉状态"或"上交计划"的按钮（建造按钮走自身 disabled 流，单独管）。 */
/* ★ 「推进本季度」什么时候可点（2026-10-17 修）：
 *
 *   ⚠️ 原来这里判的是 `STATE.can_settle`，而 `can_settle` 的语义是
 *      「**计划已经提交** + 过校验 + 试算跑得通」。
 *
 *   但前端现在改成「建造只改本地计划，提交由这个按钮触发」之后，
 *   `can_settle` 就永远是 false —— 于是按钮**永远禁用**，玩家根本无法进入下一季度。
 *   这是个死锁（改 A 引入 B 失效）。
 *
 *   正确口径：**只要处在行动阶段（collecting）就可点**。
 *   点了之后前端先 submit（不合法就报错、不结算），成功再 end_quarter；
 *   一个"空计划"也是合法计划（不安排＝不开工，§11.3）。
 */
function settleEnabled(s){
  if (!s) return false;
  if (s.phase === "finished") return false;   /* ★ 2026-10-18 终局：永远不可点 */
  if (s.phase !== "collecting") return false;
  /* 已经提交过（can_settle 为真）当然也能点 */
  return true;
}

/* ★ 2026-10-18 · 终局界面：拉终局总览（GET /api/finale，只读）。
   契约：{ok, finale, narrative}；narrative 可能 null / 缺失（B 线还没好）。
   失败把异常原样抛，调用方（finale.js）自己会兜"N/A 自构"。 */
async function fetchFinale(game, player){
  return api("GET", "/api/finale", null, {game, player});
}

const _SL_BTNS = ["btn-settle","btn-next","btn-runall","btn-stopall","btn-load","btn-new","btn-ai-send"];
function setActionButtonsDisabled(off){
  _SL_BTNS.forEach(id => {
    const b = $(id);
    if (!b) return;
    if (off){
      /* ★ 2026-10-18：**只在第一次上锁时记录原态**。
         原来每次上锁都覆写 `slWasDisabled`，而「推进本季度」会连着上锁两次
         （结算中 → 正在刷新界面），第二次就把"原本可点"记成了"原本禁用"
         → 解锁时「下一季 ▶」永久禁用，玩家进不了下一季。 */
      if (b.dataset.slWasDisabled === undefined) b.dataset.slWasDisabled = b.disabled ? "1" : "0";
      b.disabled = true;
    } else if (b.dataset.slWasDisabled === "0"){
      b.disabled = false;
      delete b.dataset.slWasDisabled;
    } else {
      delete b.dataset.slWasDisabled;
    }
  });
  /* 解锁后按当前 STATE 重判一次 settle 可用性（renderTop 之外最权威的来源） */
  if (!off && STATE){
    const report = STATE.phase === "report";
    const bs = $("btn-settle");
    if (bs) bs.disabled = report || !settleEnabled(STATE);
  }
}

/* ── 提示 ─────────────────────────────────────────────── */
function toast(text, cls){
  const d = document.createElement("div");
  d.className = "msg " + (cls || "");
  d.textContent = text;
  $("toast").appendChild(d);
  setTimeout(() => d.remove(), cls === "ok" ? 2600 : 9000);
}
function fail(e){
  let t = e.message || String(e);
  if (e.payload && e.payload.issues){
    t += "\n\n" + e.payload.issues.map(i =>
      `· [${i.code}] ${i.message}` + (i.shortfall != null
        ? `\n    申请 ${fmt(i.requested)} / 可用 ${fmt(i.available)} / 差额 ${fmt(i.shortfall)}`
        : "")).join("\n");
  }
  toast(t, "");
}

/* ── 显示格式（**只格式化，不计算**）───────────────────── */
function fmt(n, d){ return (n == null) ? "—" : Number(n).toFixed(d == null ? 2 : d); }
function fmtInt(n){ return (n == null) ? "—" : Math.round(Number(n)).toLocaleString(); }
function pct(n){ return (n == null) ? "—" : Math.round(Number(n) * 100) + "%"; }


/* ── 极简 Markdown / 转义(供 panel.js 的叙事渲染用)── */
/* 极简转义（本文件里插叙事的**唯一**净化点；其余字段照旧信任服务端形状） */
function esc(s){
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

function mdInline(s){
  return s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

/* 极简 Markdown：**只**渲染 段落 / `-`（含 `*` `+`、`1.`）列表 / `**粗体**` /
   `#`–`####` 标题。十几行，够用；不引任何第三方库（红线）。
   ⚠️ 数字一个都不碰：文本原样过 esc，只给结构。 */
function miniMD(text){
  if (!text) return "";
  const lines = esc(text).split(/\r?\n/);
  const out = []; let i = 0, n = lines.length;
  const isHs = l => { const m = l.match(/^(#{1,4})\s+(.*)$/); return m; };
  const isUl = l => /^\s*[-*+]\s+/.test(l);
  const isOl = l => /^\s*\d+\.\s+/.test(l);
  while (i < n){
    const l = lines[i];
    if (l.trim() === ""){ i++; continue; }
    let m;
    if ((m = isHs(l))){
      const lv = m[1].length;
      out.push(`<h${lv}>${mdInline(m[2].trim())}</h${lv}>`);
      i++; continue;
    }
    if (isUl(l) || isOl(l)){
      const tag = isUl(l) ? "ul" : "ol";
      const items = [];
      while (i < n && (isUl(lines[i]) || isOl(lines[i]))){
        const t = lines[i].replace(/^\s*(?:[-*+]|\d+\.)\s+/, "");
        items.push(`<li>${mdInline(t.trim())}</li>`);
        i++;
      }
      out.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }
    const para = [];
    while (i < n && lines[i].trim() !== "" &&
           !isHs(lines[i]) && !isUl(lines[i]) && !isOl(lines[i])){
      para.push(lines[i].trim()); i++;
    }
    out.push(`<p>${mdInline(para.join(" "))}</p>`);
  }
  return out.join("");
}

/* 溯源表（facts_used：路径 → 原值）。值原样显示 —— 是长 dict 就截断，不算。 */
function factsTable(fu){
  const ks = Object.keys(fu || {});
  if (!ks.length) return "";
  const rows = ks.sort().map(k => {
    const v = fu[k];
    const s = (v && typeof v === "object") ? JSON.stringify(v) : String(v);
    const show = s.length > 140 ? s.slice(0, 140) + "…" : s;
    return `<tr><td style="font-size:11px">${esc(k)}</td>
             <td style="font-size:11px">${esc(show)}</td></tr>`;
  }).join("");
  return `<details class="facts-src"><summary class="muted">数字来源（facts_used，${ks.length} 条）</summary>` +
    `<table><tbody>${rows}</tbody></table></details>`;
}
