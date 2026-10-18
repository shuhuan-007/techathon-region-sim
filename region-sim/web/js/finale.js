/* ══════════════════════════════════════════════════════════════
   finale.js —— 20 季终局结算界面（全屏 overlay + 只读看地图 + 禁全部操作）

   离开「三单并行」后独立落盘的**纯新增**前端件：
     · 不依赖、不修改 city-modal / map / panel 的任何现物大改；
     · 只在 panel.js 预留的**两个触发点**被一行调起（applyFinishedState）；
     · 数据源严格走 `GET /api/finale` 的契约（finale / narrative 两个并联键，
       narrative 可能 null/残缺 → 全部走自构兜底）。

   内部结构（自上而下）：
     ① 小工具（finaleEsc / 判断是否 finished / 读 history 数值）
     ② 图表数据装配（优先 narrative.charts，缺则从 finale.history 自构）
     ③ SVG 双折线绘制（缺值=null → **真断开**，不平滑 / 不插值 / 不预测）
     ④ overlay DOM 构建（headline + miniMD body + 双曲线 + 数据表 + 按钮）
     ⑤ applyFinishedState() —— 面板在 refresh / 结算成功两个时机各调一次
     ⑥ 只读锁（禁按钮 / 输入 / 选择 / 滑杆 / 撤销）
   ══════════════════════════════════════════════════════════════ */

/* ── ① 小工具 ────────────────────────────────────────────── */

/* esc 的别名，防止别脚本把全局 esc 覆盖了（本文件的**唯一**净化点）。 */
function _finaleEsc(s){
  try{
    if (typeof esc === "function") return esc(s);
  }catch(_e){}
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

/* STATE 是否已终局：后端契约 phase==="finished" + game_over:true。
   ★ 兼容读档路径：SessionPhase 是内存层、不进存档——读档入的局 phase 还原成
      "collecting"，但 world_phase="finished" + game_over=true 已实现。
      两种相位都认（任一真就锁）。 */
function _isFinished(s){
  if (!s) return false;
  if (s.game_over !== true) return false;
  return s.phase === "finished" || s.world_phase === "finished";
}

/* history[i] 真字段直取。不接受派生值、不做 || 0 归 0。 */
function _histNum(row, key){
  if (!row || typeof row !== "object") return null;
  const v = row[key];
  return (typeof v === "number" && isFinite(v)) ? v : null;
}
function _histNested(row, parent, key){
  if (!row || typeof row !== "object") return null;
  const p = row[parent];
  if (!p || typeof p !== "object") return null;
  const v = p[key];
  return (typeof v === "number" && isFinite(v)) ? v : null;
}

/* 从 history 里按 quarter 找行（行可能乱序/缺季）。 */
function _histRowByQuarter(history, q){
  if (!Array.isArray(history)) return null;
  for (const r of history){
    if (r && typeof r === "object" && r.quarter === q) return r;
  }
  return null;
}

/* localStorage key：同 game 只弹一次。换 game 清 game 重弹。 */
function _finaleShownKey(game){
  return "finaleShown:" + String(game || "");
}

/* ── ② 数据装配：charts 优先，history 兜底 ──────────────────
   返回 **恰好两个** chart 对象 [{id,title,metric,unit,points:[{quarter,value}]}]
   顺序固定：gdp 在前。points 覆盖 1..total_quarters 升序；缺季 value=null。 */
function _assembleFinaleCharts(finale, narrative){
  const ov = (finale && typeof finale === "object") ? finale : {};
  const total = (typeof ov.total_quarters === "number" && ov.total_quarters > 0)
    ? Math.floor(ov.total_quarters) : 20;
  const history = Array.isArray(ov.history) ? ov.history : [];

  /* —— 优先：narrative.charts 里有完整同形状的两张表 —— */
  const nCharts = (narrative && typeof narrative === "object"
                   && Array.isArray(narrative.charts)) ? narrative.charts : null;
  let gdpPts = null, hapPts = null;
  if (nCharts){
    for (const c of nCharts){
      if (!c || typeof c !== "object") continue;
      const id = String(c.id || "");
      const pts = Array.isArray(c.points) ? c.points : null;
      if (!pts) continue;
      if (id === "gdp" && gdpPts === null)       gdpPts = pts;
      if (id === "happiness" && hapPts === null) hapPts = pts;
    }
  }

  /* —— 兜底：finale.history 自构（两路同形状处理是底线） —— */
  function _fillFromHistory(fetcher){
    const out = [];
    for (let q = 1; q <= total; q++){
      const row = _histRowByQuarter(history, q);
      out.push({quarter:q, value: row ? fetcher(row) : null});
    }
    return out;
  }
  if (gdpPts === null) gdpPts = _fillFromHistory(r => _histNum(r, "value_added"));
  if (hapPts === null) hapPts = _fillFromHistory(r => _histNested(r, "food", "happiness"));

  /* —— 归一到"恰好覆盖 1..total"（防 narrative 给的点少季/多季）—— */
  function _normalize(pts){
    const byQ = {};
    (pts || []).forEach(p => {
      if (p && typeof p === "object" && typeof p.quarter === "number")
        byQ[p.quarter] = (typeof p.value === "number" && isFinite(p.value)) ? p.value : null;
    });
    const out = [];
    for (let q = 1; q <= total; q++)
      out.push({quarter: q, value: (q in byQ) ? byQ[q] : null});
    return out;
  }

  const titleRange = `第1—${total}季度`;
  return [
    { id:"gdp",       title:`${titleRange} 本季GDP 变化`, metric:"gdp",
      unit:null, points: _normalize(gdpPts) },
    { id:"happiness", title:`${titleRange} 幸福度 变化`,  metric:"happiness",
      unit:null, points: _normalize(hapPts) },
  ];
}

/* ── ③ SVG 双折线 ──────────────────────────────────────────
   · 缺值 null → **真断开**（分多段 polyline，不跨季插值）。
   · 极小非零值**绝不按 || 0 归 0**（只信 typeof === number）。
   · GDP 纵轴自适应；happiness 纵轴**固定 0—1**。
   · 不平滑（直线段 polyline）、不预测（点数就是契约点数）。 */

function _svgEl(tag, attrs){
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in (attrs || {})) el.setAttribute(k, String(attrs[k]));
  return el;
}

/* points: [{quarter, value|null}]；opts: {width,height,yMin,yMax,stroke,xLabelEvery} */
function _drawLineSVG(points, opts){
  const o = opts || {};
  const W = o.width || 520, H = o.height || 180;
  const padL = 44, padR = 14, padT = 12, padB = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const svg = _svgEl("svg", {viewBox:`0 0 ${W} ${H}`, class:"finale-chart-svg"});
  svg.setAttribute("role","img");
  svg.setAttribute("aria-label", o.ariaLabel || "趋势曲线");

  /* 收集真值（对 yMin/yMax 自适应时要靠真值；null 不参与） */
  const real = points.filter(p => p.value !== null && typeof p.value === "number");
  let yMin, yMax;
  if (typeof o.yMin === "number" && typeof o.yMax === "number"){
    yMin = o.yMin; yMax = o.yMax;            /* happiness 固定 0—1 走这里 */
  }else if (real.length){
    yMin = Math.min.apply(null, real.map(p => p.value));
    yMax = Math.max.apply(null, real.map(p => p.value));
    if (yMin === yMax){ yMin = yMin - 1; yMax = yMax + 1; }
    else { const m = (yMax - yMin) * 0.08; yMin -= m; yMax += m; }
  }else{
    yMin = 0; yMax = 1;
  }

  /* X：quarter ∈ [1..total] 线性 */
  const total = points.length ? points[points.length-1].quarter : 1;
  const qMin = 1, qMax = Math.max(1, total);
  const xOf = q => (qMax === qMin) ? (padL + plotW/2)
                                   : padL + (q - qMin) * plotW / (qMax - qMin);
  const yOf = v => padT + (yMax - v) * plotH / (yMax - yMin);

  /* 网格 + 轴 */
  const gGrid = _svgEl("g", {class:"finale-grid"});
  const hLines = 4;
  for (let i = 0; i <= hLines; i++){
    const v = yMin + (yMax - yMin) * i / hLines;
    const y = yOf(v);
    gGrid.appendChild(_svgEl("line", {
      x1:padL, x2:padL+plotW, y1:y, y2:y,
    }));
    const lbl = _svgEl("text", {x:padL-6, y:y+4, class:"finale-ylbl", "text-anchor":"end"});
    lbl.textContent = _finaleFmt(v, o.yDigits != null ? o.yDigits : 1);
    gGrid.appendChild(lbl);
  }
  /* X 轴刻度（默认每 5 季） */
  const every = o.xLabelEvery || Math.max(1, Math.round(qMax / 8));
  for (let q = qMin; q <= qMax; q += every){
    const x = xOf(q);
    gGrid.appendChild(_svgEl("line", {
      x1:x, x2:x, y1:padT, y2:padT+plotH, class:"finale-grid-v",
    }));
    const lbl = _svgEl("text", {x:x, y:padT+plotH+16, class:"finale-xlbl", "text-anchor":"middle"});
    lbl.textContent = String(q);
    gGrid.appendChild(lbl);
  }
  svg.appendChild(gGrid);

  /* 折线：把 points 按 null 切段，每段一条 polyline */
  const gLine = _svgEl("g", {class:"finale-line"});
  let run = [];
  const flush = () => {
    if (run.length >= 2){
      const ptsAttr = run.map(p => `${xOf(p.quarter).toFixed(2)},${yOf(p.value).toFixed(2)}`).join(" ");
      gLine.appendChild(_svgEl("polyline", {
        points: ptsAttr, class:"finale-stroke",
        style:`stroke:${o.stroke || "#65a9ff"}`,
      }));
    }
    run = [];
  };
  points.forEach(p => {
    if (p.value === null || typeof p.value !== "number"){ flush(); return; }
    run.push(p);
  });
  flush();
  svg.appendChild(gLine);

  /* 数据点（含 null 的小圆断点提示，可选；这里只画真值点） */
  const gDot = _svgEl("g", {class:"finale-dots"});
  real.forEach(p => {
    gDot.appendChild(_svgEl("circle", {
      cx:xOf(p.quarter), cy:yOf(p.value), r:2.6, class:"finale-dot",
      style:`fill:${o.stroke || "#65a9ff"}`,
    }));
  });
  svg.appendChild(gDot);

  return svg;
}

/* 数字格式（**只显示**，不计算）：null → "—"；默认 1 位小数（曲线纵轴刻度）。 */
function _finaleFmt(v, digits){
  if (v === null || typeof v !== "number" || !isFinite(v)) return "—";
  return Number(v).toFixed(digits == null ? 1 : digits);
}

/* ── ④ overlay DOM ──────────────────────────────────────── */

let _finaleOverlay = null;     /* 单例；避免重复 build */
let _finaleLock    = false;    /* 正在构建中（幂等锁） */

function _finaleTotalOf(finale){
  if (finale && typeof finale.total_quarters === "number" && finale.total_quarters > 0)
    return Math.floor(finale.total_quarters);
  return 20;
}

/* 主入口：拉数 + 构建 + 弹。同 game 只弹一次（localStorage）。 */
async function maybeShowFinaleOverlay(){
  if (!_isFinished(STATE)) return false;
  if (!GAME) return false;
  if (_finaleLock) return false;
  if (localStorage.getItem(_finaleShownKey(GAME))) return false;

  _finaleLock = true;
  try{
    /* 后端契约：{ok, finale, narrative}；narrative 可能 null / 缺失 */
    let finale = null, narrative = null;
    try{
      const r = await fetchFinale(GAME, PLAYER);
      if (r && typeof r === "object"){
        if (r.finale    && typeof r.finale    === "object") finale    = r.finale;
        if (r.narrative && typeof r.narrative === "object") narrative = r.narrative;
      }
    }catch(_e){
      /* /api/finale 挂了也不挡界面 —— 后面用 STATE.history 自构兜底 */
    }

    /* /api/finale 没回时，用 STATE.history 凑一份最小 finale（自构底线） */
    if (!finale){
      const total = (STATE && typeof STATE.total_quarters === "number" && STATE.total_quarters > 0)
        ? STATE.total_quarters : 20;
      finale = {
        scope: "player", player_id: PLAYER, quarter: STATE && STATE.quarter,
        total_quarters: total, game_over: true,
        quarters_completed: total,
        missing_quarters: [], history_complete: true,
        final_report: (STATE && STATE.report) || null,
        history: (STATE && Array.isArray(STATE.history)) ? STATE.history : [],
      };
    }

    _buildFinaleOverlay(finale, narrative);
    localStorage.setItem(_finaleShownKey(GAME), "1");
    return true;
  }finally{
    _finaleLock = false;
  }
}

/* 组装 DOM（**所有用户可见文本都过 esc**；数字契约字段例外，走 fmt）。 */
function _buildFinaleOverlay(finale, narrative){
  /* 已有就撤（防御） */
  if (_finaleOverlay && _finaleOverlay.parentNode) _finaleOverlay.parentNode.removeChild(_finaleOverlay);
  _finaleOverlay = null;

  const total = _finaleTotalOf(finale);
  const pid = finale && finale.player_id ? String(finale.player_id) : (PLAYER || "玩家");

  /* —— 骨架 —— */
  const ov = document.createElement("div");
  ov.id = "finale-overlay";
  ov.setAttribute("role","dialog");
  ov.setAttribute("aria-modal","true");
  ov.setAttribute("aria-labelledby","finale-title");

  const box = document.createElement("div");
  box.className = "finale-box";
  ov.appendChild(box);

  /* 顶部条：标题 + ✕ */
  const top = document.createElement("div");
  top.className = "finale-top";
  const h = document.createElement("h2");
  h.id = "finale-title";
  h.textContent = "🏁 终局结算";
  top.appendChild(h);
  const btnClose = document.createElement("button");
  btnClose.id = "finale-btn-close";
  btnClose.className = "finale-x";
  btnClose.title = "关闭终局结算（地图仍可只读查看）";
  btnClose.textContent = "✕";
  btnClose.onclick = _closeFinaleOverlay;
  top.appendChild(btnClose);
  box.appendChild(top);

  /* 标头一行（narrative.headline 缺则降级） */
  const headline = (narrative && typeof narrative.headline === "string"
                    && narrative.headline.trim())
    ? narrative.headline
    : `🏁 第 ${total} 季终局 · 经营总结`;
  const head = document.createElement("div");
  head.className = "finale-headline";
  head.textContent = headline;
  box.appendChild(head);

  /* 摘要正文（miniMD） */
  if (narrative && typeof narrative.body === "string" && narrative.body.trim()){
    const body = document.createElement("div");
    body.className = "finale-body";
    try{
      body.innerHTML = (typeof miniMD === "function")
        ? miniMD(narrative.body)
        : `<p>${_finaleEsc(narrative.body)}</p>`;
    }catch(_e){
      body.innerHTML = `<p>${_finaleEsc(narrative.body)}</p>`;
    }
    box.appendChild(body);
  }else{
    const body = document.createElement("div");
    body.className = "finale-body finale-body-fallback";
    body.innerHTML = `<p class="muted">本局已完成全部 ${total} 季结算。` +
      `终局叙事尚未生成（或正在跑），下面给出基于账本的两条真曲线与核心指标表。</p>`;
    box.appendChild(body);
  }

  /* ── 双曲线（单位不同 → 两张独立小图，绝不共用纵轴）── */
  const chartsWrap = document.createElement("div");
  chartsWrap.className = "finale-charts";
  const charts = _assembleFinaleCharts(finale, narrative);
  charts.forEach(c => {
    const card = document.createElement("div");
    card.className = "finale-chart-card";
    const ttl = document.createElement("div");
    ttl.className = "finale-chart-title";
    ttl.textContent = String(c.title || c.id || "");
    card.appendChild(ttl);
    const isHap = (c.id === "happiness");
    const svg = _drawLineSVG(c.points, {
      width: 560, height: 200,
      yMin: isHap ? 0 : undefined,
      yMax: isHap ? 1 : undefined,
      stroke: isHap ? "#f2c14e" : "#65a9ff",
      yDigits: isHap ? 2 : 1,
      ariaLabel: c.title || c.id,
    });
    card.appendChild(svg);
    chartsWrap.appendChild(card);
  });
  box.appendChild(chartsWrap);

  /* ── 兜底数据表（narrative 缺失时必出；列**全从 history 直取**，不派生）── */
  const tblWrap = document.createElement("div");
  tblWrap.className = "finale-table-wrap";
  const tblTitle = document.createElement("div");
  tblTitle.className = "finale-chart-title";
  tblTitle.textContent = "完整数据回顾（逐季直取，不派生）";
  tblWrap.appendChild(tblTitle);

  const history = (finale && Array.isArray(finale.history)) ? finale.history : [];
  const tbl = document.createElement("table");
  tbl.className = "finale-table";
  tbl.innerHTML = `<thead><tr>
      <th>季度</th>
      <th class="num">GDP (value_added)</th>
      <th class="num">幸福度</th>
      <th class="num">期末现金</th>
      <th class="num">销售收入</th>
      <th class="num">人口</th>
    </tr></thead>`;
  const tb = document.createElement("tbody");
  for (let q = 1; q <= total; q++){
    const row = _histRowByQuarter(history, q);
    const gdp   = row ? _histNum(row, "value_added")               : null;
    const hap   = row ? _histNested(row, "food", "happiness")      : null;
    const cash  = row ? _histNum(row, "cash_after")                : null;
    const sales = row ? _histNum(row, "sales_revenue")             : null;
    const pop   = row ? _histNested(row, "population", "population") : null;
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="num">${q}</td>` +
      `<td class="num">${_finaleTdNum(gdp, 2)}</td>` +
      `<td class="num">${_finaleTdNum(hap, 2)}</td>` +
      `<td class="num">${_finaleTdNum(cash, 2)}</td>` +
      `<td class="num">${_finaleTdNum(sales, 2)}</td>` +
      `<td class="num">${_finaleTdNum(pop, 1)}</td>`;
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  tblWrap.appendChild(tbl);
  box.appendChild(tblWrap);

  /* 警告（若后端带了 warning） */
  if (narrative && typeof narrative.warning === "string" && narrative.warning.trim()){
    const w = document.createElement("div");
    w.className = "finale-warn";
    w.textContent = "⚠️ " + narrative.warning;
    box.appendChild(w);
  }

  /* 底部按钮行：回看地图 */
  const foot = document.createElement("div");
  foot.className = "finale-foot";
  const btnMap = document.createElement("button");
  btnMap.id = "finale-btn-map";
  btnMap.className = "primary";
  btnMap.textContent = "🗺️ 回看地图";
  btnMap.title = "关闭结算面板，回到只读地图";
  btnMap.onclick = _closeFinaleOverlay;
  foot.appendChild(btnMap);
  box.appendChild(foot);

  /* Esc 关闭（一次绑定） */
  ov.addEventListener("keydown", _finaleEscKey);
  ov.tabIndex = -1;

  document.body.appendChild(ov);
  _finaleOverlay = ov;
  /* 聚焦到 overlay 让 Esc 生效 */
  try{ ov.focus(); }catch(_e){}
}

function _finaleEscKey(e){
  if (e && e.key === "Escape") _closeFinaleOverlay();
}

function _closeFinaleOverlay(){
  if (_finaleOverlay && _finaleOverlay.parentNode)
    _finaleOverlay.parentNode.removeChild(_finaleOverlay);
  _finaleOverlay = null;
}

/* 表格数字格：null → "—"；**绝不 || 0**（0.003 不能被吞）。 */
function _finaleTdNum(v, digits){
  if (v === null || typeof v !== "number" || !isFinite(v)) return "—";
  return Number(v).toFixed(digits == null ? 2 : digits);
}

/* ── ⑤ applyFinishedState：refresh / 结算成功两个时机都调 ─── */

/* 真入口（panel.js 只调这一行）。同步部分**绝不抛错**，
   防止终局界面把 refresh / 结算主流程拖死。 */
function applyFinishedState(){
  try{
    if (!_isFinished(STATE)) return;
    _applyFinishedLock(true);
    /* 弹 overlay 是异步（要拉 /api/finale），不阻塞主流程 */
    Promise.resolve().then(() => maybeShowFinaleOverlay()).catch(_e => { /* 静默 */ });
  }catch(_e){ /* 防御兜底：终局界面异常不能连累 refresh */ }
}

/* ── ⑥ 只读锁（禁全部操作）────────────────────────────────
   · 提交类按钮：disabled + 视觉灰化（加 .finale-locked class）
   · 输入/选择/滑杆：disabled
   · 开工/停工/满开工/建造撤销：disabled
   · 已渲染的"已排建设撤销"按钮（动态生成）：用事件代理拦截，禁点
   · 顶栏统计 / 地图 / 市场 / 上季报告 / 新闻 / 财报解读：**保持只读可见**，不动 */

const _FINALE_LOCK = {
  /* 明确禁用的按钮 id（含提交类） */
  btnIds: [
    "btn-settle", "btn-next", "btn-build",
    "btn-runall", "btn-stopall", "btn-ai-send",
    "btn-new", "btn-load",
  ],
  /* 输入/选择类 id */
  inputIds: [
    "ai-input",
    "sel-work", "sel-product",
    "in-game",
  ],
  /* 表格 / 区 域内**动态生成**可点的小按钮（靠 querySelectorAll） */
  selector: [
    "#plots-body input[type=range]",
    "#plots-body button",
    "#food-body input[type=checkbox]",
    "#food-body button",
    "#builds-body button",
    "#proj-body button",
    "#market-body button",
    "#report-body button",
  ],
};

function _applyFinishedLock(on){
  const cls = "finale-locked";
  _FINALE_LOCK.btnIds.forEach(id => {
    const b = $(id);
    if (!b) return;
    if (on){ b.disabled = true; b.classList.add(cls); }
    else   { b.classList.remove(cls); }
  });
  _FINALE_LOCK.inputIds.forEach(id => {
    const el = $(id);
    if (!el) return;
    if (on) el.disabled = true;
  });
  _FINALE_LOCK.selector.forEach(sel => {
    document.querySelectorAll(sel).forEach(el => {
      if (on) el.disabled = true;
    });
  });

  /* 顶栏阶段 pill：补一个"🏁 终局（只读）"提示（renderTop 已把 phaseName
     翻成中文，这里再保险一次—— pill 元素就在 index.html 里，随时可写） */
  if (on){
    const sp = $("s-phase");
    if (sp && sp.textContent.indexOf("终局") < 0) sp.textContent = "🏁 终局（只读）";
  }
}

/* 全局点击守卫：finished 时**拦所有涉及"提交/建造/计划"的点击**。
   防御动态渲染块里残留的 onclick。地图 / 报表 / 市场点击**放行**。
   策略：只拦截"明确在 _FINALE_LOCK 列表里"或带 `.finale-locked` 的元素。 */
document.addEventListener("click", function _finaleClickGuard(ev){
  if (!_isFinished(STATE)) return;
  let el = ev.target;
  while (el && el !== document.body){
    if (el.id && _FINALE_LOCK.btnIds.indexOf(el.id) >= 0){
      ev.preventDefault(); ev.stopPropagation(); return;
    }
    if (el.classList && el.classList.contains("finale-locked")){
      ev.preventDefault(); ev.stopPropagation(); return;
    }
    el = el.parentElement;
  }
}, true);

/* ── 自检用导出（调试面板可查） ─────────────────────────── */
window.FINALE = {
  applyFinishedState,
  maybeShowFinaleOverlay,
  _assembleFinaleCharts,
  _isFinished,
};
