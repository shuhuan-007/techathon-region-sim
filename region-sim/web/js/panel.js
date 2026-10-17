/* ══════════════════════════════════════════════════════════════
   panel.js —— 顶栏 / 本季安排 / 市场 / 口粮 / 收支预览 / 上季报告 /
   新闻+财报渲染 / 建造面板 / 提交·结算·下一季 / AI 秘书聊天侧栏。
   (renderMap/displayPlots 在 map.js; $ / STATE / MAP / plan 等全局在 api.js)

   从原 index.html `<script>` 一~(五) 各大块整体搬出,函数内部一行未改。
   ══════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════
   一、开局 / 读档
   ══════════════════════════════════════════════════════════ */
$("btn-new").onclick = async () => {
  const gid = $("in-game").value.trim() || "g1";
  try{
    const r = await api("POST", "/api/new_game", { game_id: gid, player_id: PLAYER });
    GAME = gid; resetPlan(); selected = null;
    AI_LOG.length = 0;             // 新对局 → 秘书对话也从零开始
    $("main").classList.remove("hidden");
    toast(`已开新局 ${gid}`, "ok");
    aiGreet();
    await refreshGames(); await refresh();
  }catch(e){ fail(e); }
};

$("btn-load").onclick = async () => {
  const gid = ($("sel-games").value || $("in-game").value).trim();
  if (!gid) return toast("先选一个存档");
  GAME = gid; resetPlan(); selected = null;
  AI_LOG.length = 0;             // 换档 → 秘书对话也从零开始
  $("main").classList.remove("hidden");
  aiGreet();
  await refresh();
};

async function refreshGames(){
  try{
    const r = await api("GET", "/api/games");
    $("sel-games").innerHTML = r.games.length
      ? r.games.map(g => `<option value="${g.game_id}">${g.game_id} · 第 ${g.quarter} 季</option>`).join("")
      : `<option value="">（暂无存档）</option>`;
  }catch(e){ /* 列表失败不阻塞 */ }
}

/* ══════════════════════════════════════════════════════════
   二、拉状态并整页重画
   ══════════════════════════════════════════════════════════ */
async function refresh(){
  if (!GAME) return;
  showStateLoadingDelayed();          // 只等数字；叙事已拆到 /api/narratives
  try{
    const [st, mp] = await Promise.all([
      /* ★ narratives:"0"：/api/state 不再同步生成新闻/财报（那要 1–3 分钟）。
         数字先回来；叙事由 loadNarratives() 在后台单独打 /api/narratives。 */
      api("GET", "/api/state",  null, {game:GAME, player:PLAYER, narratives:"0"}),
      api("GET", "/api/map",    null, {game:GAME, player:PLAYER}),
    ]);
    STATE = st.state; MAP = mp.map;
    if (!WORKS.length){
      const w = await api("GET", "/api/build_options");
      WORKS = w.works;
    }
    renderAll();
    loadNarratives();                 // 不 await：数字先显示，叙事慢慢来
  }catch(e){
    fail(e);
    /* 加载失败：人话已 toast；如果加载态曾显形又被撤下，
       玩家看到的不是死转圈而是"重试"按钮（下方补一次提示）。 */
    if (_slDepth <= 1 && _slTick){
      toast("状态拉取失败：再点一次「读档」或刷新页面让它重试。", "warn");
    }
  }finally{
    hideStateLoading();
  }
}

function renderAll(){
  renderTop(); renderMap(); renderPlots(); renderMarket();
  renderFood(); renderProj(); renderReport(); renderBuildPanel();
  renderBuilds();        // ★ 已排建设（本季草稿，逐条可撤销）
  renderQuota(); renderQuote();
  renderNarr();          // ★ 新闻 + 财报解读（引擎/服务层给什么就显示什么）
}

/* ── 顶栏 ─────────────────────────────────────────────── */
function renderTop(){
  const s = STATE;
  $("s-quarter").textContent = `${s.quarter} / ${s.total_quarters}`;
  $("s-cash").textContent    = fmt(s.cash);
  $("s-happy").textContent   = fmt(s.happiness);
  /* ★ 2026-10-17 后端新键（report.py:272-277）：jobs / population_count
     是「始终有值的标量」，与 u / plan 无关，未提交 plan 也拿得到 ——
     旧键 s.population 在 plan=None 时是 null，会让顶栏"—"成常驻。 */
  const jobs = (s.jobs != null) ? s.jobs : 0;
  const popc = (s.population_count != null) ? s.population_count : 0;
  $("s-pop").textContent = `${fmt(jobs,1)} / ${fmt(popc,1)}`;
  $("s-eff").textContent = s.efficiency == null ? "—" : "×" + fmt(s.efficiency);

  const phaseName = {collecting:"行动阶段", resolving:"结算中", report:"月报阶段"}[s.phase] || s.phase;
  $("s-phase").textContent = phaseName;

  const report = s.phase === "report";
  $("btn-settle").disabled = report || !settleEnabled(s);
  $("btn-settle").classList.toggle("hidden", report);
  $("btn-next").classList.toggle("hidden", !report);
  $("btn-settle").title = (s.settle_blockers || []).join("\n");

  /* 现金行的"本季计划扣"那一句由 renderPreviewLine 专管（不在这里动）。 */
  renderPreviewLine();
}

/* ── 地块安排(开工比例)───────────────────────────────── */
function renderPlots(){
  const body = $("plots-body");
  // ★ 缺陷 2b：把"在建/影子"也显出来 —— 递交后本季安排不再空着。
  //   只展示，开工比例只对 running 开放（constructing 本季不能排工）。
  const disp = displayPlots(MAP, STATE);
  const plots = Object.values(disp).sort((a,b) => String(a.plot_id).localeCompare(String(b.plot_id)));
  if (!plots.length){
    body.innerHTML = `<tr><td colspan="4" class="muted">还没有建成的地块。先在地图上建造。</td></tr>`;
    return;
  }
  body.innerHTML = plots.map(p => {
    const cur = plan.plot_plans[p.plot_id];
    const u = cur ? cur.u : 0;
    return `<tr>
      <td>${p.plot_id}<br><span class="muted">${TYPE_CN[p.plot_type]||""}${p.level} · ${p.product||"—"}</span></td>
      <td><span class="tag ${p.status}">${p.status==="running"?"运行":"在建"}</span></td>
      <td class="num">${p.efficiency ? "×"+fmt(p.efficiency.efficiency) : "—"}</td>
      <td><input type="range" min="0" max="100" step="5" value="${Math.round(u*100)}"
           data-plot="${p.plot_id}" class="u-range">
          <span class="muted">${Math.round(u*100)}%</span></td>
    </tr>`;
  }).join("");

  body.querySelectorAll(".u-range").forEach(r => {
    r.oninput = () => {
      const pid = r.dataset.plot, u = Number(r.value) / 100;
      plan.plot_plans[pid] = { plot_id: pid, active: u > 0, u };
      r.nextElementSibling.textContent = r.value + "%";
    };
    r.onchange = () => refreshPreview();
  });
}

$("btn-runall").onclick = () => {
  (STATE.plots || []).filter(p => p.status === "running")
    .forEach(p => plan.plot_plans[p.plot_id] = {plot_id:p.plot_id, active:true, u:1});
  renderPlots();
  schedulePreview();
};
$("btn-stopall").onclick = () => {
  plan.plot_plans = {};
  renderPlots();
  schedulePreview();
};

/* ── 已排建设（本季草稿，逐条可撤销）─────────────────────
   · 数据只读 `plan.builds`（本地草稿）；**不提交、不动 STATE.plots**；
   · 类型只查 TYPE_CN 显示，产物/路线原样显示；价格/工期一律不在前端算
     （那两项以 /api/simulate 与 build_quote 的回包为准）；
   · 撤销 = splice 掉这一条 → 整页重画（地图影子/建造面板一起刷新）
     → schedulePreview() 用缩小后的计划重新只读试算。 */
function renderBuilds(){
  const el = $("builds-body");
  if (!el) return;
  const builds = Array.isArray(plan.builds) ? plan.builds : [];
  if (!builds.length){
    el.innerHTML = `<p class="muted" style="margin:4px 0 0">本季还没有建造指令。</p>`;
    return;
  }
  el.innerHTML = `<table><thead><tr>
      <th>地块</th><th>类型</th><th class="num">目标等级</th>
      <th>产物 / 路线</th><th></th>
    </tr></thead><tbody>` +
    builds.map((b, i) => {
      const pid  = (b && b.plot_id != null) ? String(b.plot_id) : "—";
      const type = (b && b.plot_type) ? (TYPE_CN[b.plot_type] || String(b.plot_type)) : "—";
      const lvl  = (b && b.target_level != null) ? String(b.target_level) : "—";
      const prod = (b && b.product) ? String(b.product) : "—";
      return `<tr>
        <td>${esc(pid)}</td>
        <td>${esc(type)}</td>
        <td class="num">${esc(lvl)}</td>
        <td>${esc(prod)}</td>
        <td class="num"><button data-idx="${i}">撤销</button></td>
      </tr>`;
    }).join("") + `</tbody></table>`;

  el.querySelectorAll("button[data-idx]").forEach(btn => {
    btn.onclick = () => {
      const idx = Number(btn.dataset.idx);
      if (!Number.isInteger(idx) || idx < 0 || idx >= plan.builds.length) return;
      plan.builds.splice(idx, 1);   // 只删本地草稿这一条（不提交、不动地块）
      renderAll();                  // 地图影子 / 建造面板 / 列表自身一起刷新
      schedulePreview();            // 缩水后的计划重新只读试算（不提交）
    };
  });
}

/* ── 市场 ─────────────────────────────────────────────── */
function renderMarket(){
  const m = STATE.market || {};
  $("market-body").innerHTML = Object.keys(m).sort().map(g => {
    const x = m[g];
    const cur = x.current_price;
    const atFloor = x.at_floor === true;
    return `<tr>
      <td>${g}<br><span class="muted">${x.name||""}</span></td>
      <td class="num">${fmt(x.P0)}</td>
      <td class="num">${fmt(x.last_price)}</td>
      <td class="num" style="${atFloor?'color:var(--warn)':''}">${cur==null?"—":fmt(cur)}</td>
      <td class="num muted">${fmt(x.P_min)}</td>
    </tr>`;
  }).join("");
}

/* ── 口粮 ─────────────────────────────────────────────── */
const FOODS = ["A","B","C","D"];
function renderFood(){
  const proj = STATE.projection;
  const food = proj && proj.food;
  const pop  = STATE.population;
  const needBase = pop ? pop.baseline_food : null;
  const needGood = pop ? pop.good_food : null;

  $("food-body").innerHTML = FOODS.map(g => {
    const quotaB = g === "A" ? 0.4 : g === "B" ? 0.2 : null;
    const quotaG = {A:0.4,B:0.2,C:0.1,D:0.02}[g];
    const baseNeed = (quotaB != null && needBase != null) ? quotaB * needBase : null;
    const goodNeed = (needGood != null) ? quotaG * needGood : null;
    const have = food ? (food.deducted && food.deducted[g]) || 0 : null;
    const want = plan.food[g] || 0;
    return `<tr>
      <td>${g}</td>
      <td class="num">${baseNeed==null?"—":fmt(baseNeed)} / ${goodNeed==null?"—":fmt(goodNeed)}</td>
      <td><input type="number" min="0" step="0.1" value="${want || ""}"
           data-food="${g}" placeholder="0" style="width:74px"></td>
    </tr>`;
  }).join("");

  $("food-body").querySelectorAll("[data-food]").forEach(inp => {
    inp.onchange = () => {
      const g = inp.dataset.food, v = Number(inp.value);
      if (v > 0) plan.food[g] = v; else delete plan.food[g];
      refreshPreview();
    };
  });

  if (!food){
    $("food-note").textContent = "提交计划后才能看到判定结果。";
  } else {
    $("food-note").innerHTML =
      `基准档（吃饱）：<b>${food.baseline_met ? "✅ 达标" : "❌ 未达标"}</b>　`
      + `优秀档（吃好）：<b>${food.good_met ? "✅ 达标" : "❌ 未达标"}</b>　`
      + `→ 幸福度 <b>${fmt(food.happiness)}</b>`;
  }
}

/* ── 开局减免名额速览（★ 与报价行同源：读 build_quote.quota_by_type）──
   数据三块都来自引擎 build_quote（§5.3）：
     · used      存档跨季已用（引擎登记）
     · this_plan 本季这份计划自己会用掉几条（引擎按 plan.builds 数出）
     · remaining total − used − this_plan（引擎算好）
   前端**只展示**，不做折扣判定、不做名额累加。 */
function renderQuota(){
  const q = STATE.build_quote;
  const el = $("quota-strip");
  if (!q || !q.quota_by_type){ el.textContent = ""; return; }
  const order = ["agriculture","resource","industry","service","research"];
  const pieces = [];
  for (const t of order){
    const r = q.quota_by_type[t];
    if (!r) continue;
    if (!(r.total > 0)) continue;   // 没配名额的类型不占位
    const planUse = r.this_plan || 0;
    const off = r.remaining > 0;
    // "本类已用 used/total（含本季已下指令 planUse）· 下块打/不打折"
    const main = `${TYPE_CN[t] || t} 已用 ${r.used}/${r.total}` +
                 (planUse > 0 ? `（含本季已下指令 ${planUse}）` : "");
    pieces.push(
      `<span class="tag" style="${off?'':'opacity:.5'}">` +
      `${main}${off ? " ·下块打折" : " ·下块原价"}</span>`
    );
  }
  el.innerHTML = pieces.length
    ? `开局减免名额（§5.3，每类独立）：${pieces.join("　")}` +
      `<span class="muted">　· 折后现金以「一次性扣款」为准</span>`
    : "";
}

/* ── 收支预览 ─────────────────────────────────────────── */
function renderProj(){
  const p = STATE.projection;
  if (!p){
    $("proj-body").innerHTML = `<tr><td class="muted">提交计划后显示。</td></tr>`;
    $("proj-note").textContent = "—";
    return;
  }
  if (p.error){
    $("proj-body").innerHTML = `<tr><td class="muted">无法计算</td></tr>`;
    $("proj-note").textContent = p.error;
    return;
  }
  const rows = [
    ["物资款", fmt(p.materials_cost)],
    ["工资（税前）", fmt(p.wages)],
    ["经营费", fmt(p.operating)],
    ["一次性扣款", "<b>" + fmt(p.total_deduction) + "</b>"],
    ["销售收入（本季价）", fmt(p.sales_revenue)],
    ["工资税回流", fmt(p.wage_tax_refund)],
    ["期末现金", "<b>" + fmt(p.cash_after) + "</b>"],
    ["本季盈亏", (p.net_cash >= 0 ? "+" : "") + fmt(p.net_cash)],
    ["增加值 GDP（参考价）", fmt(p.value_added)],
  ];
  $("proj-body").innerHTML = rows.map(r =>
    `<tr><td class="muted">${r[0]}</td><td class="num">${r[1]}</td></tr>`).join("");
  $("proj-note").textContent =
    "GDP 用固定参考价 P0，与销售收入（本季成交价）是两个口径。";
}

/* ── 上季报告 ─────────────────────────────────────────── */
function renderReport(){
  const r = STATE.report;
  if (!r){
    // ★ 缺陷 4：第 1 季常驻一句（不再只靠瞬时 toast）
    $("report-body").innerHTML = `<span class="muted">还没有结算过（第 1 季结算后出现）。</span>`;
    return;
  }
  const sold = Object.entries(r.sold || {}).map(([g,q]) => `${g}×${fmt(q,1)}`).join("、") || "无";
  const prod = (r.production || []).map(o => {
    const out = o.output_goods ? `${o.output_goods}×${fmt(o.output_qty,1)}` : "（无产出）";
    return `${o.plot_id} → ${out}`;
  }).join("<br>") || "无";
  $("report-body").innerHTML = `
    <table><tbody>
      <tr><td class="muted">季度</td><td class="num">第 ${r.quarter} 季</td></tr>
      <tr><td class="muted">产量</td><td class="num">${prod}</td></tr>
      <tr><td class="muted">卖出 X_t</td><td class="num">${sold}</td></tr>
      <tr><td class="muted">一次性扣款</td><td class="num">${fmt(r.total_deduction)}</td></tr>
      <tr><td class="muted">销售收入</td><td class="num">${fmt(r.sales_revenue)}</td></tr>
      <tr><td class="muted">期末现金</td><td class="num"><b>${fmt(r.cash_after)}</b></td></tr>
      <tr><td class="muted">增加值</td><td class="num">${fmt(r.value_added)}</td></tr>
      <tr><td class="muted">幸福度</td><td class="num">${r.food ? fmt(r.food.happiness) : "—"}</td></tr>
    </tbody></table>`;
}

/* ── 新闻 + 财报渲染(esc/mdInline/miniMD/factsTable 在 api.js)── */
let NARR_INFLIGHT = false;
let NARR_PENDING = false;

/* ★ 2026-10-18：叙事生成从 /api/state 拆出来，单独在后台跑。
   · 前端**不 await** 它来渲染数字：结算/刷新先出数字；
   · 同一时刻只打一发（NARR_INFLIGHT），避免多入口重复烧模型；
   · 失败只影响新闻/财报两个面板，不影响任何数字；
   · 跨季度时旧请求的结果不写进新季度（用 quarter/news 对象身份校验）。 */
async function loadNarratives(){
  if (NARR_INFLIGHT){ NARR_PENDING = true; return; }
  const news = STATE && STATE.news;
  const hasReport = !!(STATE && (STATE.report || (STATE.quarter || 0) > 1));
  if (!news || !hasReport) return;                   // 还没结算过 → 不生成
  if (news.narrative && news.report_narrative) return; // 已有缓存
  const myQuarter = STATE.quarter;
  NARR_INFLIGHT = true;
  news.generating = true;
  renderNarr();
  try{
    const r = await api("GET", "/api/narratives", null,
                        {game:GAME, player:PLAYER});
    /* 期间换季了 → 这份结果属于上一季，丢弃；finally 会补发新季度。 */
    if (STATE && STATE.quarter === myQuarter && STATE.news === news && r && r.narratives){
      news.narrative = r.narratives.news || null;
      news.report_narrative = r.narratives.report || null;
    }
  }catch(e){
    if (STATE && STATE.quarter === myQuarter && STATE.news === news){
      const fail = {
        ok: false,
        error: "narratives_failed",
        detail: (e && e.message) || "新闻/财报生成失败（不影响本季数字）。",
      };
      if (!news.narrative) news.narrative = fail;
      if (!news.report_narrative) news.report_narrative = fail;
    }
  }finally{
    NARR_INFLIGHT = false;
    news.generating = false;
    renderNarr();
    if (NARR_PENDING){ NARR_PENDING = false; loadNarratives(); }
  }
}

function renderNarr(){
  const news = (STATE && STATE.news) || null;

  /* ① 第 1 季结算前：news 为 null（facts 为 null、不许生成）→ 空态 */
  if (!news){
    $("news-body").innerHTML = `<p class="narr-empty muted">还没有结算过（第 1 季结算后，这里出现本季新闻）。</p>`;
    $("report-narr-body").innerHTML = `<p class="narr-empty muted">还没有结算过（第 1 季结算后，这里出现财报解读）。</p>`;
    return;
  }

  const factsNull = news.facts == null;    // 诚实空态的第二层（一般同上到不了这）
  const pendingNews = news.generating
    ? "正在生成本季新闻…（约 1–2 分钟；不影响结算，数字已经出来了）"
    : "本季新闻（headline＋正文）还没生成。";
  const pendingReport = news.generating
    ? "正在生成本季财报解读…（约 1–2 分钟；不影响结算，数字已经出来了）"
    : "本季财报解读还没生成。";
  oneNarr("news-body", news.narrative, pendingNews, factsNull && !news.generating);
  oneNarr("report-narr-body", news.report_narrative, pendingReport, false);
}

/* 单个面板： narr 是 dict 或 null/undefined。 factsNull → "第 1 季"口径。 */
function oneNarr(id, narr, emptyText, factsNull){
  const el = $(id);

  /* ② 未生成（null/undefined）→ 空态，**不**留白 */
  if (!narr){
    el.innerHTML = `<p class="narr-empty muted">${
      factsNull ? "还没有结算过（第 1 季结算后出现）。" : esc(emptyText)
    }</p>`;
    return;
  }

  /* ③ 失败（ok:false）→ 人话原因（复用 toast 的措辞风格） */
  if (narr.ok === false){
    el.innerHTML =
      `<p style="color:var(--warn);margin:4px 0">⚠️ ${
        esc(narr.detail || "这次没能生成本季解读。")}</p>` +
      (narr.error ? `<p class="muted" style="margin:0"><span class="tag">${esc(narr.error)}</span>` +
       ` <span class="muted">（机器可读分类，排查用）</span></p>` : "") +
      `<p class="muted" style="margin:6px 0 0">可以刷新一次 /state 让它再试（同季只生成一次）。</p>`;
    return;
  }

  /* ④ 成功：新闻 = headline ＋ body按段；财报 = miniMD */
  const tag = narr.model ? `<div class="muted" style="margin:0 0 6px">模型：${esc(narr.model)} · 第 ${narr.quarter} 季</div>` : "";
  let html = tag;
  if (narr.kind === "news"){
    if (narr.headline) html += `<p style="font-weight:600;font-size:15px;margin:4px 0 8px">${esc(narr.headline)}</p>`;
    const paras = String(narr.body == null ? "" : narr.body).split(/\n{2,}/);
    html += paras.map(p => p.trim()).filter(p => p).map(p =>
      `<p>${mdInline(esc(p)).replace(/\n/g,"<br>")}</p>`).join("");
    if (!narr.headline && !narr.body) html += `<p class="narr-empty muted">（内容为空）</p>`;
  } else {  // report
    html += `<div>${miniMD(narr.text)}</div>`;
    if (!narr.text) html += `<p class="narr-empty muted">（内容为空）</p>`;
  }
  html += factsTable(narr.facts_used);
  el.innerHTML = html;
}

/* ══════════════════════════════════════════════════════════
   三、建造
   ══════════════════════════════════════════════════════════ */
function worksForCell(){
  const p = (STATE.plots || []).find(x => x.plot_id === selected);
  if (!p){
    // 空地 → 只能"新建一级"
    return WORKS.filter(w => w.from_level === 0);
  }
  if (p.status === "constructing") return [];
  return WORKS.filter(w => w.plot_type === p.plot_type && w.from_level === p.level);
}

function renderBuildPanel(){
  const sel = selected;
  $("build-at").textContent = sel ? `（目标：${sel}）` : "（先在地图上点一格）";

  const list = sel ? worksForCell() : [];
  const p = (STATE.plots || []).find(x => x.plot_id === sel);

  if (!sel){
    $("sel-work").innerHTML = `<option value="">—</option>`;
    $("btn-build").disabled = true;
    $("work-info").textContent = "点地图上任意一格开始。";
    $("row-product").classList.add("hidden");
    return;
  }
  if (!list.length){
    const why = p && p.status === "constructing"
      ? "这块地正在建设中，本季不能再下指令。"
      : "这一格没有可用的工程。";
    $("sel-work").innerHTML = `<option value="">—</option>`;
    $("btn-build").disabled = true;
    $("work-info").textContent = why;
    $("row-product").classList.add("hidden");
    return;
  }

  $("sel-work").innerHTML = list.map(w =>
    `<option value="${w.work_id}">${w.label}　·　${w.cash} 金 + ${
      Object.entries(w.materials).map(([g,q])=>`${g}×${q}`).join("+")}　·　${w.quarters} 季</option>`
  ).join("");
  $("sel-work").onchange = renderWorkDetail;
  renderWorkDetail();
}

function renderWorkDetail(){
  const wid = $("sel-work").value;
  const w = WORKS.find(x => x.work_id === wid);
  if (!w){ $("btn-build").disabled = true; $("quote-info").textContent = ""; return; }

  const opts = w.products || [];
  if (opts.length){
    $("row-product").classList.remove("hidden");
    $("sel-product").innerHTML = opts.map(p => `<option value="${p}">${p}</option>`).join("");
  } else {
    $("row-product").classList.add("hidden");
    $("sel-product").innerHTML = "";
  }
  // ★ 缺陷 3：用 <b> 强调"原价"，不再把 Markdown 的 ** 留在界面上
  $("work-info").innerHTML =
    `${w.label}　现金 ${w.cash}（<b>原价</b>）　物资 ${
      Object.entries(w.materials).map(([g,q])=>`${g}×${q}`).join(" + ")}　工期 ${w.quarters} 季`;
  $("btn-build").disabled = false;
  renderQuote();
}

/* ★ 折后现金报价 —— 引擎 `/state.build_quote` 给数，前端只显示。 */
function renderQuote(){
  const el = $("quote-info");
  const q = STATE.build_quote;
  if (!q){ el.textContent = ""; return; }
  if (q.error){
    el.innerHTML = `<span style="color:var(--bad)">报价失败：${q.error}</span>`;
    return;
  }
  const rows = (q.rows || []).map(r => {
    if (!r.discounted) return `${r.plot_id}：现金 ${fmt(r.cash)}（原价）`;
    return `${r.plot_id}：现金 ${fmt(r.cash)}　` +
           `<span style="color:var(--good)">↓ 减免 · 原价 ${fmt(r.list_cash)}</span>`;
  });
  el.innerHTML =
    `本季建设报价（引擎真算）：` +
    (rows.length ? `${rows.join("　·　")}　` : "") +
    `<b>合计 ${fmt(q.total)}</b>` +
    (q.discounted_any
      ? `　<span style="color:var(--good)">含减免</span>`
      : "");
}

$("btn-build").onclick = async () => {
  const wid = $("sel-work").value;
  const w = WORKS.find(x => x.work_id === wid);
  if (!w || !selected) return;
  const product = (w.products && w.products.length) ? $("sel-product").value : null;
  plan.builds.push({
    plot_id: selected,
    plot_type: w.plot_type,
    target_level: w.to_level,
    product,
  });
  /* ★ 只改本地草稿，不提交 —— 「plans 只增不改」的提交动作只在
     「▶ 推进本季度」发生一次。连续点第二块、第三块不会再被
     「本季已提交，不能修改」挡住。
     扣款数走 /api/simulate（只读试算）实时上顶栏。 */
  toast(`已加入建设指令：${selected} ${w.label}${product ? "（" + product + "）" : ""}\n` +
        `（本地草稿，点「▶ 推进本季度」才提交。可继续点其它格子。）`, "ok");
  $("btn-build").disabled = true;
  selected = null;
  renderMap(); renderBuildPanel();
  renderBuilds();            // ★ 已排建设列表同步显示刚下的这一条
  schedulePreview();
};
$("btn-cancel").onclick = () => { selected = null; renderMap(); renderBuildPanel(); };

/* ══════════════════════════════════════════════════════════
   四、提交 / 结算 / 下一季
   ══════════════════════════════════════════════════════════ */
/* ── ★ 实时预览（只读）：/api/simulate 防抖 + 串行化 ─────────
   原则（任务红线）：
     · 前端**零计算**：total_deduction / cash_after 全部来自 /api/simulate，
       这里**只显示**；不许在前端加减。
     · 计划只要变了就试算一次；连续变化时**防抖 ~250ms**，只保留**最后一次**。
     · 不并发轰炸：同一时间最多一个 simulate 在飞；飞行期间的变更排队，
       落地后只补一发最新的（陈旧结果直接丢弃）。
     · 失败（草稿不合法、网关抖动）**不弹窗**：顶栏改标"预览暂不可用"，
       玩家继续攒计划不受影响。
     · /api/simulate 是**只读**：深拷贝试算，不落存档、不改状态、不提交计划。
       「本季已提交，不能修改」只会由 /api/submit 触发，本路径与它无关。 */
const SIM = {
  timer: null,          // 防设定时器
  inflight: false,      // 当前是否有请求在飞
  pending: false,       // 飞行期间又收到了变更 → 落地后补一发
  seq: 0,               // 请求序号（陈旧结果丢弃）
  last: null,           // 最近一次试算成功拿到的 report（显示用缓存）
  fail: false,          // 上一次是否失败（顶栏标"预览不可用"）
  reason: "",           // ★ 失败原因（资金不足等业务结果，不再只显示一句“暂不可用”）
};
const SIM_DEBOUNCE_MS = 250;

function planEmpty(){
  return !Object.keys(plan.plot_plans).length
      && !Object.keys(plan.purchases).length
      && !Object.keys(plan.food).length
      && !plan.builds.length
      && !plan.contracts.length;
}

/* 计划变更的**唯一入口**：所有改造计划的代码（建造/口粮/开工/AI 填入/全部满开工…）
   都调它。它只管安排试算，不重画页面（重画由调用方自己做）。 */
function schedulePreview(){
  if (!GAME || !STATE) return;
  /* 空格计划 → 销毁待试算任务 + 清预览（顶栏回到只显示现金） */
  if (planEmpty()){
    if (SIM.timer){ clearTimeout(SIM.timer); SIM.timer = null; }
    SIM.pending = false;
    SIM.seq++;                     // 让在飞的旧请求结果作废
    SIM.last = null;
    SIM.fail = false;
    SIM.reason = "";
    renderPreviewLine();
    return;
  }
  if (SIM.inflight){ SIM.pending = true; return; }   // 在飞 → 排队即可
  if (SIM.timer) clearTimeout(SIM.timer);
  SIM.timer = setTimeout(() => { SIM.timer = null; runPreview(); }, SIM_DEBOUNCE_MS);
}

async function runPreview(){
  if (!GAME || !STATE) return;
  if (SIM.inflight){ SIM.pending = true; return; }
  const mySeq = ++SIM.seq;
  SIM.inflight = true;
  try{
    const r = await api("POST", "/api/simulate",
                        {game:GAME, player:PLAYER, plan: publicPlan()});
    if (mySeq !== SIM.seq) return;                   // 期间又有新变更 → 丢弃
    SIM.last = (r && r.report) ? r.report : null;
    SIM.fail = !SIM.last;
    /* ★ 2026-10-18：资金不足等业务结果由服务端 200 回在 blocker 里，
       不是“服务端崩了”。把原因留下来给顶栏显示。 */
    SIM.reason = SIM.fail ? ((r && r.blocker) || "试算没跑通") : "";
  }catch(e){
    if (mySeq !== SIM.seq) return;
    SIM.last = null;
    SIM.fail = true;                                 // 静默失败，只改标识
    const p = e && e.payload;
    if (p && Array.isArray(p.issues) && p.issues.length){
      /* 校验不过（如超预算/路线错）：把第一条问题原样给顶栏，title 放全部。 */
      SIM.reason = p.issues.map(i => (i && i.message) || String(i)).join("\n");
    } else {
      SIM.reason = (p && (p.detail || p.error))
                || (e && e.message) || "试算没跑通";
    }
  }finally{
    SIM.inflight = false;
  }
  renderPreviewLine();
  /* 飞行期间排队过一次 → 只补一发最新（不发两轮） */
  if (SIM.pending){ SIM.pending = false; schedulePreview(); }
}

/* 顶栏现金行的"本季计划扣"那么一句。三种状态：
     ① 无草稿 → 空（只显示现金）；
     ② 试算中 / 排队中 → "试算中…"；
     ③ 有结果 → "｜本季计划扣 X｜结算后约 Y"（X/Y 全来自 /api/simulate，原样显示）；
     ④ 失败 → "｜预览暂不可用"。 */
function renderPreviewLine(){
  const el = $("s-cash-plan");
  if (!el) return;
  if (planEmpty()){ el.textContent = ""; el.style.color = ""; return; }
  if (SIM.fail){
    /* ★ 2026-10-18：不再只显示“预览暂不可用”。资金不足是业务结果，
       第一行就写清“需扣多少 / 手上多少”，完整原因放 title。 */
    const first = String(SIM.reason || "试算没跑通").split("\n")[0].trim();
    const short = first.length > 56 ? first.slice(0, 56) + "…" : first;
    el.textContent = "｜预览：" + (short || "暂不可用");
    el.title = SIM.reason || "";
    el.style.color = "var(--warn)";
    return;
  }
  if (!SIM.last){
    el.textContent = "｜试算中…";
    el.style.color = "";
    return;
  }
  el.style.color = "";
  el.title = "";
  el.textContent =
    `｜本季计划扣 ${fmt(SIM.last.total_deduction)}｜结算后约 ${fmt(SIM.last.cash_after)}`;
}

/* 旧的"点建造即提交试算"接口保留这个名字，让所有改计划的调用点不动。
   行为已换：只排期试算，绝不走 /api/submit。 */
function refreshPreview(){ schedulePreview(); }

function publicPlan(){
  return {
    player_id: PLAYER,
    plot_plans: plan.plot_plans,
    purchases: plan.purchases,
    food: plan.food,
    builds: plan.builds,
    contracts: plan.contracts,
  };
}

$("btn-settle").onclick = async () => {
  /* ★ 2026-10-18 修「点结算后遮罩永不消失、进不了下一季」（实测 107 秒仍卡）：
     根因是下面那行把 `showStateLoadingNow` 写成了 `setStateLoadingNow`（不存在）
     → 流水推完后抛 ReferenceError（在 await 之后、Promise.all 之前），
       被外层 catch 吞成一条 toast，而当时**没有 finally** → hideStateLoading 永不执行。
     现在四道保险：
       ① 整段 try/catch/finally，finally 里 `forceHideStateLoading()`（不信任 _slDepth）；
       ② 流水带 8s watchdog —— 它只是"看头"，绝不能挡住结算；
       ③ `eq.state`（end_quarter 回包自带，phase=report）先兜底渲染 →
          「下一季 ▶」立刻可点，不依赖任何后续请求；
       ④ /api/state + /api/map 覆盖为最新，失败/12s 超时只提示、保留 eq.state。 */
  let eq = null;
  try{
    const r = await api("POST", "/api/submit", {game:GAME, player:PLAYER, plan: publicPlan()});
    STATE = r.state; renderAll();

    /* ★ ⑥号改造：结算本身就**先点亮加载框**、标题换成"结算第 N 季…"。
       玩家按下按钮立刻看到事情在发生（不等 2.5s）。 */
    _slStart = Date.now();          // 与 showStateLoadingDelayed 同源，让秒表从这里起
    showStateLoadingNow(`结算第 ${STATE.quarter} 季…`, "正在推进工程 / 生产 / 求价 / 扣款——引擎在真算，请稍候。");
    pushSettlementLine(`结算第 ${STATE.quarter} 季…`, "dim");

    eq = await api("POST", "/api/end_quarter", {game:GAME, player:PLAYER});

    /* end_quarter 一回包，立刻把 report 里的关键项**每 ~140ms 一条**逐条涌上。
       "**全部数字来自引擎**（`QuarterReport.to_dict`），前端只负责挑与摆"。
       ② 流水最多等 8 秒：抽行/推行任何一环出问题都不许卡住结算。 */
    const reportForStream = (eq && eq.report) || null;
    let lines = [];
    try{ lines = settlementLinesFromReport(reportForStream); }
    catch(_e){ lines = []; }        // 展示辅助函数出错 → 当作"没有可推行"
    try{
      await Promise.race([
        streamSettlementLines(lines.slice(1), 140),   // lines[0] 上面已 push 过
        new Promise(res => setTimeout(res, 8000)),
      ]);
    }catch(_e){ /* 流水异常不影响结算 */ }

    /* ③ 兜底渲染：`eq.state` 是 end_quarter **当场**给的（phase=report、含上季报告）。
       先把它画出来 —— 就算后面所有请求都死掉，「下一季 ▶」也已经可点。 */
    if (eq && eq.state) STATE = eq.state;
    resetPlan();                    // 计划已随结算消耗掉
    if (!WORKS.length){             // 极少数情况（没走过 refresh）补一次建造选项
      try{ const w = await api("GET", "/api/build_options"); WORKS = w.works; }catch(_e){}
    }
    try{ renderAll(); }catch(_e){ /* 兜底渲染失败也不能挡住收尾 */ }

    /* ④ 覆盖为最新数字（narratives:"0" → 不生成叙事；
       新闻/财报交给 loadNarratives() 在后台单独打 /api/narratives）。
       ⚠️ 这里绝不能再等叙事：两路模型 1–3 分钟，会把“结算”拖成两分钟。
       刷新失败/超时 → 保留上面的 eq.state，只提示，绝不留在遮罩里。 */
    showStateLoadingNow("正在刷新界面…", "结算已完成；新闻/财报在后台生成，不会拖住数字。");
    let st = null, mp = null, refreshErr = null;
    try{
      [st, mp] = await Promise.race([
        Promise.all([
          api("GET", "/api/state", null,
              {game:GAME, player:PLAYER, narratives:"0"}),
          api("GET", "/api/map", null, {game:GAME, player:PLAYER}),
        ]),
        new Promise((_res, rej) =>
          setTimeout(() => rej(new Error("界面刷新超时（12 秒没回来）")), 12000)),
      ]);
    }catch(e){ refreshErr = e; }

    if (!refreshErr && st && st.state){
      STATE = st.state;
      if (mp && mp.map) MAP = mp.map;
      try{ renderAll(); }catch(_e){ /* 渲染异常不阻塞收尾 */ }
      loadNarratives();             // 不 await：数字已显示，叙事后台来
    }else{
      toast("已结算；界面刷新失败，可点下一季", "warn");
    }
    await refreshGames();

    if (!refreshErr && STATE){
      toast(`第 ${STATE.quarter - 1} 季已结算。` +
            `期末现金 ${fmt(STATE.cash)}　幸福度 ${fmt(STATE.happiness)}` +
            (STATE.report ? `　增加值 ${fmt(STATE.report.value_added)}` : ""), "ok");
    }
  }catch(e){
    fail(e);
    /* 万一异常发生在"已经结算成功"之后（未知渲染坑等），仍用回包把界面
       切到 report 阶段 —— 宁可提示刷新失败，也不能让玩家停在旧季度。 */
    if (eq && eq.state){
      try{
        STATE = eq.state; resetPlan(); renderAll();
        toast("已结算；界面刷新失败，可点下一季", "warn");
      }catch(_e2){ /* 兜底本身失败也不能连累 finally */ }
    }
  }finally{
    forceHideStateLoading();       // ★ 所有出口（含异常/超时）都必须撤遮罩
  }
};

$("btn-next").onclick = async () => {
  try{
    await api("POST", "/api/next_quarter", {game:GAME, player:PLAYER});
    resetPlan(); selected = null;
    await refresh();
  }catch(e){ fail(e); }
};

$("btn-report").onclick = () => {
  const r = STATE && STATE.report;
  if (!r) return toast("还没有结算过。", "warn");
  toast(`第 ${r.quarter} 季：扣款 ${fmt(r.total_deduction)}，` +
        `销售 ${fmt(r.sales_revenue)}，期末现金 ${fmt(r.cash_after)}，` +
        `增加值 ${fmt(r.value_added)}`, "ok");
};

/* ══════════════════════════════════════════════════════════
   五、AI 秘书 —— **一个聊天式对话框**(§11.4:AI 只起草,提交永远是玩家的动作)
   ══════════════════════════════════════════════════════════ */
/* 把 AI 起草的 suggested_plan 落进前端既有的待提交 plan（与 /api/submit 同形）。
   地图/建造面板/本季安排/口粮/预览 全靠既有 renderAll() 显示。 */
function applySuggestedPlan(sp){
  resetPlan();
  // 只抄已知的五个键；其它键（player_id 之类）由 publicPlan() 自己补。
  plan.plot_plans = (sp && typeof sp.plot_plans === "object" && sp.plot_plans) ? sp.plot_plans : {};
  plan.purchases  = (sp && typeof sp.purchases  === "object" && sp.purchases)  ? sp.purchases  : {};
  plan.food       = (sp && typeof sp.food       === "object" && sp.food)       ? sp.food       : {};
  plan.builds     = Array.isArray(sp && sp.builds)    ? sp.builds    : [];
  plan.contracts  = Array.isArray(sp && sp.contracts) ? sp.contracts : [];
}

/* ── 机制题 / 规划题分流（与后端同一套关键词；前端先判一版，后端仍可改判）──
   · 强动作词优先：只要出现「建/造/取消/撤销/删/安排/…/改」，就是让我动手 → plan；
   · 否则出现机制词（怎么算/为什么/是什么/…）→ answer：只解释，不生成计划；
   · 都不沾 → auto：交给后端判断（闲聊、模糊请求都走这条）。
   返回 "answer" | "plan" | "auto"（原样塞进 /api/ai/advise 的 mode）。 */
const AI_ACTION_WORDS = ["建","造","取消","撤销","删","安排","起草","填",
                         "开工","停工","买","卖","改"];
const AI_MECHANISM_WORDS = ["怎么算","为什么","是什么","机制","规则","解释",
                            "原理","公式","多少","区别","能不能"];

function detectAiMode(q){
  const s = String(q == null ? "" : q);
  if (AI_ACTION_WORDS.some(w => s.includes(w))) return "plan";
  if (AI_MECHANISM_WORDS.some(w => s.includes(w))) return "answer";
  return "auto";
}

/* AI 这次是不是「撤销建造」类动作：后端 plan_action="removed"，
   或校正说明里点名撤销/移除建造。命中时确认措辞改成「撤销＋保留其余」。 */
function planRemovesBuilds(adv){
  if (!adv) return false;
  if (adv.plan_action === "removed") return true;
  const notes = Array.isArray(adv.plan_notes) ? adv.plan_notes : [];
  return notes.some(n =>
    typeof n === "string" && /撤销|移除建造/.test(n));
}

/* ── 对话气泡（历史只存内存；本页内保留）──────────────────── */
const AI_LOG = [];   // [{role:"me"|"ai", text:str, cls:""|"warn"|"bad", sys:str}]

function aiBubble(role, text, cls, sys){
  AI_LOG.push({role, text, cls: cls || "", sys: sys || ""});
  renderAiChat();
}

function renderAiChat(){
  const el = $("ai-chat");
  if (!el) return;
  el.innerHTML = AI_LOG.map(m => {
    const who = m.role === "me" ? "你" : "秘书";
    return `<div class="bubble ${m.role === "me" ? "me" : "ai"} ${m.cls}">` +
           `<span class="who">${who}</span>${esc(m.text)}` +
           (m.sys ? `<div class="sys">${esc(m.sys)}</div>` : "") +
           `</div>`;
  }).join("");
  el.scrollTop = el.scrollHeight;
}

/* 草稿落进界面后立刻显形的"秘书同页播报"（不进对话流）——
   只告诉她动了哪几样，让她知道"草稿已经替你填好了"。 */
function draftSummaryLines(sp){
  const bits = [];
  const nb = Array.isArray(sp.builds) ? sp.builds.length : 0;
  const np = sp.plot_plans && typeof sp.plot_plans === "object" ? Object.keys(sp.plot_plans).length : 0;
  const nf = sp.food       && typeof sp.food       === "object" ? Object.keys(sp.food).length       : 0;
  const npy= sp.purchases  && typeof sp.purchases  === "object" ? Object.keys(sp.purchases).length  : 0;
  if (nb) bits.push(`${nb} 条建造`);
  if (np) bits.push(`${np} 块地的开工`);
  if (nf) bits.push(`${nf} 项口粮`);
  if (npy) bits.push(`${npy} 项采购`);
  return bits.length ? bits.join("、") : "一份空计划";
}

/* plan_notes（引擎对模型草案的校正说明）→ 最多 5 行、每行前缀「· 」的文本；
   多于 5 条时收一句「…另有 N 条」。不是数组 / 全空时返回 ""（调用处据此决定加不加这一段）。 */
function planNoteLines(notes){
  if (!Array.isArray(notes)) return "";
  const items = [];
  notes.forEach(n => {
    let s;
    if (typeof n === "string") s = n;
    else if (typeof n === "number" || typeof n === "boolean") s = String(n);
    else if (n == null) s = "";
    else { try{ s = JSON.stringify(n); }catch(_e){ s = ""; } }   // 意外对象也不吐 [object Object]
    s = String(s || "").trim();
    if (s) items.push(s);
  });
  if (!items.length) return "";
  const MAX = 5;
  const out = items.slice(0, MAX).map(s => "· " + s);
  if (items.length > MAX) out.push(`…另有 ${items.length - MAX} 条`);
  return out.join("\n");
}

/* 发送按钮 —— 统一带 draft:true。 */
$("btn-ai-send").onclick = async () => {
  if (!GAME) return toast("先开局 / 读档，AI 要有局面才能出主意。", "warn");
  const input = $("ai-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  aiBubble("me", q);

  const sendBtn = $("btn-ai-send"), status = $("ai-status");
  sendBtn.disabled = true;
  status.textContent = "秘书在想…（真调模型可能要几十秒，请稍候，页面没死）";
  status.style.color = "var(--warn)";

  /* ★ 带上当前草稿与模式（新契约，后端可缺省忽略）：
     · current_plan：让后端「续写/只撤这一条」，而不是每次从零起草；
     · mode：前端按同一套关键词先判一版 —— answer = 机制题（只解释、不落表）、
       plan = 有强动作词（要我动手）、auto = 其余交给后端判。 */
  const body = {
    game: GAME, player: PLAYER, question: q, draft: true,
    current_plan: publicPlan(),
    mode: detectAiMode(q),
  };
  let resp;
  try{
    resp = await api("POST", "/api/ai/advise", body);
  }catch(e){
    status.textContent = "";
    status.style.color = "";
    sendBtn.disabled = false;
    aiBubble("ai",
      `⚠️ 这次没能联系上 AI 服务：${e.message || e}。\n可能是网关没在跑或网络抖动——再发一次就行。`,
      "bad");
    return;
  }
  sendBtn.disabled = false;
  status.style.color = "";

  const adv = (resp && resp.advice) || (resp && resp.ok === false ? resp : null);

  /* —— ok:true —— */
  if (adv && adv.ok === true && (adv.summary || "").trim()){
    const summary = String(adv.summary);
    const sp = (adv.suggested_plan && typeof adv.suggested_plan === "object")
      ? adv.suggested_plan : null;
    const planIssues = Array.isArray(adv.plan_issues) ? adv.plan_issues : null;
    const model = adv.model || "";
    const ruleKey = adv.rule_key ? "§" + adv.rule_key : "";

    /* ②a：起草过不了校验 → 只把问题说给玩家，**绝不落进表单**。 */
    if (planIssues && planIssues.length){
      status.textContent = "";
      const lines = planIssues.map(i => {
        const code = (i && i.code) || "";
        const msg  = (i && i.message) || String(i);
        return `· ${code ? "[" + code + "] " : ""}${msg}`;
      }).join("\n");
      /* summary 可能与 issues 之间已有空行 —— 先去尾再统一补一个空行，避免忽多忽少。 */
      let text = summary.replace(/\s+$/, "") + "\n\n" + lines;
      /* 若引擎尝试校正过（plan_notes），说明「救过但仍没过」——只显示，**绝不落进表单**。 */
      const tried = planNoteLines(adv.plan_notes);
      if (tried) text += "\n\n引擎尝试校正过，但这份草案仍未通过：\n" + tried;
      text += "\n\n可以换一句话再让我起草一次。";
      aiBubble("ai", text, "bad",
        model ? `模型：${model}${ruleKey ? " · " + ruleKey : ""}` : "");
      return;
    }

    /* ②b：拿到一份能用的草案 → 落表前先过「不覆盖手填草稿」这道闸。 */
    if (sp){
      /* 引擎侧的三块补充（新契约，可能缺省）：校正说明条数 / 试算摘要 / 撤销标记。 */
      const nNotes = Array.isArray(adv.plan_notes) ? adv.plan_notes.length : 0;
      const noteLines = planNoteLines(adv.plan_notes);
      const pv = (adv.plan_preview && typeof adv.plan_preview === "object")
        ? adv.plan_preview : null;
      const isRemove = planRemovesBuilds(adv);
      const why = (typeof adv.why === "string" && adv.why.trim()) ? adv.why.trim() : "";

      /* ★ 不覆盖玩家手填的草稿：本地已有任何东西 → 先问；不答应就**一个字都不落表**，
         只把 AI 的 summary / 校正说明留在气泡里给她参考。 */
      const hadLocal = !planEmpty();
      if (hadLocal){
        const ok = window.confirm(isRemove
          ? "AI 要撤销部分建造并保留其余安排，应用吗？"
          : "你已经有本地草稿。应用 AI 的计划会替换/编辑它，继续吗？");
        if (!ok){
          status.textContent = "已保留你的草稿（AI 计划未落表）";
          let kept = "已保留你的草稿；这次没有落表。\n\n" + summary;
          if (why) kept += "\n\n为什么：" + why;
          if (noteLines) kept += "\n\n引擎校正：\n" + noteLines;
          kept += "\n\n想让它生效：再发一次，在弹窗里点「确定」。";
          aiBubble("ai", kept, "warn",
            (model ? `模型：${model}${ruleKey ? " · " + ruleKey : ""}` : "") +
            `\n——AI 只起草，提交仍由你点「▶ 推进本季度」。`);
          return;
        }
      }

      applySuggestedPlan(sp);
      selected = null;
      renderAll();
      /* ★ 只触发只读试算（/api/simulate），**没有提交**。
         提交仍只有玩家手动点「▶ 推进本季度」一条路。 */
      schedulePreview();

      status.textContent = `已拿到草案（${draftSummaryLines(sp)}${isRemove ? " · 含撤销" : ""}）` +
        (model ? ` · 模型：${model}` : "") +
        (nNotes > 0 ? ` · 引擎校正 ${nNotes} 处` : "");

      /* 气泡正文：summary → 撤销确认 → 为什么 → 引擎校正逐条 → 试算；
         哪块没有就整段不出现（不写 undefined、不贴 JSON）。 */
      let text = summary;
      if (adv.plan_action === "removed") text += "\n\n已按你的要求撤销建造。";
      if (why) text += "\n\n为什么：" + why;
      if (noteLines) text += "\n\n引擎校正：\n" + noteLines;
      if (pv && pv.total_deduction != null && pv.cash_after != null){
        text += `\n\n试算：本季扣款 ${fmt(pv.total_deduction)}，结算后约 ${fmt(pv.cash_after)}`;
      }

      aiBubble("ai",
        text,
        "",
        (model ? `模型：${model}${ruleKey ? " · " + ruleKey : ""}` : "") +
        `\n——这是 AI 起草的，你可以改；提交仍由你点「▶ 推进本季度」。` +
        (hadLocal ? `\n（提醒：按你确认，之前攒的本地草稿已被这份计划替换。）` : ""));
      return;
    }

    /* ②c：只给了人话建议，没起草计划（闲聊、答疑、模型没编出 JSON 都走这）。 */
    status.textContent = model ? `已拿到建议 · 模型：${model}` : "已拿到建议";
    aiBubble("ai", summary, "",
      model ? `模型：${model}${ruleKey ? " · " + ruleKey : ""}` : "");
    return;
  }

  /* —— ok:false：AI 不可用 / 空回答 / 网关抖 —— */
  status.textContent = "";
  if (adv && adv.ok === false){
    const tag = adv.error ? `[${adv.error}] ` : "";
    aiBubble("ai",
      `${tag}${adv.detail || "AI 这次没给出可用回答。"}\n\n可以再发一次。`,
      "warn");
  } else {
    aiBubble("ai",
      "⚠️ AI 服务的应答形状出乎意料，没拿到可用的建议。可以再发一次。",
      "warn");
  }
};

/* 输入框里 Ctrl/Cmd+Enter 也发送（回车换行、快捷键提交，桌面惯例）。 */
$("ai-input").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter"){
    e.preventDefault();
    $("btn-ai-send").click();
  }
});

/* 开局/读档后第一次在秘书框里给一句问候（没有历史时才给）。 */
function aiGreet(){
  if (AI_LOG.length) return;
  aiBubble("ai",
    "你好，我是你的 AI 秘书。问问题、要建议、让我起草本季计划都行。\n" +
    "我起草的计划会**直接填到界面**上，你可以再改；" +
    "**提交永远由你点「▶ 推进本季度」**（§11.4，我无 commit）。");
}
