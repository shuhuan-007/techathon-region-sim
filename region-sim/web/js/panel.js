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
    /* 2026-10-18 拍板：开局成功后自动收起 setup 面板（index.html 里监听） */
    window.dispatchEvent(new Event('rg:session-started'));
  }catch(e){ fail(e); }
};

$("btn-load").onclick = async () => {
  const gid = ($("sel-games").value || $("in-game").value).trim();
  if (!gid) return toast("先选一个存档");
  GAME = gid; resetPlan(); selected = null;
  AI_LOG.length = 0;             // 换档 → 秘书对话也从零开始
  $("main").classList.remove("hidden");
  /* 2026-10-18 拍板：读档成功后自动收起 setup 面板 */
  window.dispatchEvent(new Event('rg:session-started'));
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
    /* ★ 2026-10-18：兼容旧存档/旧回包 —— 如果读出来仍是「月报阶段」，
       自动切回行动阶段。玩家不需要再点「下一季」。 */
    if (STATE && STATE.phase === "report"){
      await enterNextQuarter();
    }
    if (!WORKS.length){
      const w = await api("GET", "/api/build_options");
      WORKS = w.works;
    }
    /* ★ 2026-10-18 · P0-C：行动阶段（collecting）且本地计划为空 → 默认全开。
       planEmpty() 是闸门 —— 玩家本季已手动改过的计划不会被覆盖；
       btn-new / btn-load / btn-next 都先 resetPlan() 再进这里，天然"首次"。 */
    if (STATE.phase === "collecting" && primeRunAll()){
      renderPlots();
      schedulePreview();
    }
    renderAll();
    /* ★ 2026-10-18 · 每季度进入行动阶段（collecting）后弹一次上季月报摘要。
       不阻塞 refresh 主流程；同季只弹一次（localStorage）。失败静默。 */
    try{ maybeShowQuarterReport(); }catch(_e){ /* modal 失败不影响主界面 */ }
    loadNarratives();                 // 不 await：数字先显示，叙事慢慢来
    try{ applyFinishedState(); }catch(_e){ /* ★ 终局界面自身异常不连累 refresh */ }
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
  /* ★ 2026-10-18 折叠：换档/刷新后同步"本季安排" panel 折叠状态。 */
  applyPlotsCollapseState();
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
  /* ★ 2026-10-18 平均效率兜底：plan 已填（primeRunAll 或玩家手动）后
     STATE 里的 s.efficiency 还没因新试算回写 → 用最近一次试算 SIM.last.efficiency
     顶上。STATE.efficiency（即"已经提交的那季"的效率）优先。 */
  const effShow = (s.efficiency != null) ? s.efficiency
                : (SIM && SIM.last && typeof SIM.last.efficiency === "number")
                  ? SIM.last.efficiency : null;
  $("s-eff").textContent = effShow == null ? "—" : "×" + fmt(effShow);

  const phaseName = {collecting:"行动阶段", resolving:"结算中", report:"月报阶段", finished:"🏁 终局（只读）"}[s.phase] || s.phase;
  $("s-phase").textContent = phaseName;

  /* ★ 2026-10-18：月报阶段并入行动阶段 —— 结算完自动进入下一季，
     玩家永远只需要「推进本季度」这一颗按钮，不再有「下一季」中转页。 */
  $("btn-settle").disabled = !settleEnabled(s);
  $("btn-settle").classList.remove("hidden");
  $("btn-next").classList.add("hidden");
  $("btn-settle").title = (s.settle_blockers || []).join("\n");

  /* 现金行的"本季计划扣"那一句由 renderPreviewLine 专管（不在这里动）。 */
  renderPreviewLine();
}

/* 研究地块内部是 industry L2 product='研究'；展示时按“研究”而不是“工业”。
   ★ 2026-10-18 · UI：地块列改显示完整二字 zone 名（农业/资源/工业/服务/研究），
   不再用「农」「资」单字简写。统一从 api.js 的 ZONE_CN 取。 */
function plotDisplayType(p){
  if (p && String(p.product || "") === "研究") return "研究";
  return ZONE_CN[p.plot_type] || TYPE_CN[p.plot_type] || p.plot_type || "";
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
    /* ★ 2026-10-18 · P0-A：在建/升级中的地块**锁定**——
         不渲染 slider；顺手把本地草稿里可能残留的这条开工清掉，
         并在 work-info 侧写清还剩几季（construction_left 为 null 的
         "影子"地块引擎没给工期，只写"在建中"，不编数字）。 */
    if (p.status === "constructing"){
      if (plan.plot_plans[p.plot_id]) delete plan.plot_plans[p.plot_id];
      const left = (p.construction_left != null) ? `（还剩 ${p.construction_left} 季）` : "（在建中）";
      return `<tr>
        <td>${p.plot_id}<br><span class="muted">${plotDisplayType(p)}${p.level} · ${p.product||"—"}</span></td>
        <td><span class="tag ${p.status}">在建</span></td>
        <td class="num">${p.efficiency ? "×"+fmt(p.efficiency.efficiency) : "—"}</td>
        <td><span class="muted">不可开工${left}</span></td>
      </tr>`;
    }
    const cur = plan.plot_plans[p.plot_id];
    const u = cur ? cur.u : 0;
    return `<tr>
      <td>${p.plot_id}<br><span class="muted">${plotDisplayType(p)}${p.level} · ${p.product||"—"}</span></td>
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
      <th>产物 / 路线（子类型）</th><th></th>
    </tr></thead><tbody>` +
    builds.map((b, i) => {
      const pid  = (b && b.plot_id != null) ? String(b.plot_id) : "—";
      const type = (b && String(b.product || "") === "研究")
        ? "研究"
        : (b && b.plot_type) ? (TYPE_CN[b.plot_type] || String(b.plot_type)) : "—";
      const lvl  = (b && b.target_level != null) ? String(b.target_level) : "—";
      /* ★ 2026-10-18：产物/路线不再原样堆字符；按子类型名显示。
           H-A=机械—农业 / H-F=机械—资源 / H-G=机械—工业 / H-J=机械—服务；
           H-*+=自动化—对应；I=高技术；其余（A/B/C/D/G/研究/无）走原样/占位。 */
      const raw  = (b && b.product) ? String(b.product) : null;
      const prod = raw ? `${productLabel(raw)}（${raw}）` : "—";
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
/* ★ 2026-10-18 · 口粮两档拆列：
   · 需求数据源优先级：STATE.food_need（后端并行下发
     {baseline:{A,B}, good:{A,B,C,D}, baseline_total, good_total}）
     → 否则退回旧口径 STATE.population 乘配比 {A:.4,B:.2 / A:.4,B:.2,C:.1,D:.02}；
   · 表头/行**拆成两列**：「合格需求」（只 A/B 有）与「优秀需求」（A/B/C/D 全有），
     不再挤一格 "base / good"（之前 C/D 那格要么空白要么仅斜杠，玩家读不懂）；
   · 「本季计划」输入照留（plan.food）；
   · 兜底：两边都拿不到时该格显示 0，**不能再空白**。 */
const FOOD_QUOTA_B = {A:0.4, B:0.2};
const FOOD_QUOTA_G = {A:0.4, B:0.2, C:0.1, D:0.02};

function _foodNeeds(simFood){
  /* ★ 2026-10-18 修复：有本季试算时，需求必须用**试算报告**里的
     baseline_need/good_need——它会算上本季完工的新地块（人口/胃口变大）。
     之前一直读 STATE.food_need（当前状态，未含当季完工），
     于是玩家按旧需求填满四项，结算却判“优秀未达标”。 */
  if (simFood && (simFood.baseline_need != null || simFood.good_need != null)){
    const baseF = Number(simFood.baseline_need || 0);
    const goodF = Number(simFood.good_need || 0);
    const baseline = {}, good = {};
    Object.entries(FOOD_QUOTA_B).forEach(([g, q]) => { baseline[g] = q * baseF; });
    Object.entries(FOOD_QUOTA_G).forEach(([g, q]) => { good[g]     = q * goodF; });
    return { baseline, good };
  }
  const fn = (STATE && STATE.food_need) || null;
  if (fn && (fn.baseline || fn.good)){
    return {
      baseline: (fn && fn.baseline) || {},
      good:     (fn && fn.good)     || {},
    };
  }
  /* 兜底：population 仍在 /state 给（compute_population 五项），按 config 配比自乘。 */
  const pop = STATE && STATE.population;
  const baseF = pop ? pop.baseline_food : 0;
  const goodF = pop ? pop.good_food     : 0;
  const baseline = {}, good = {};
  Object.entries(FOOD_QUOTA_B).forEach(([g, q]) => { baseline[g] = q * (baseF || 0); });
  Object.entries(FOOD_QUOTA_G).forEach(([g, q]) => { good[g]     = q * (goodF || 0); });
  return { baseline, good };
}

function renderFood(){
  /* ★ 2026-10-18 · 幸福度预览的数据源跟"本季收支预览"保持同一口径：
       SIM.last（/api/simulate 只读试算的最新 report）优先，其次 STATE.projection。
       之前只读 STATE.projection —— 提交过一次后玩家改口粮，幸福度一直停在旧值，
       玩家看不到「我这么改下季会不会更幸福」。 */
  const simFood = (SIM && SIM.last && SIM.last.food) ? SIM.last.food : null;
  const food = simFood || (STATE.projection && STATE.projection.food) || null;
  const needs = _foodNeeds(food);

  $("food-body").innerHTML = FOODS.map(g => {
    const baseNeed = needs.baseline[g];                 // A/B 有值；C/D 无 → 显示 "—"
    const goodNeed = needs.good[g];                     // A/B/C/D 都有
    const want = plan.food[g] || 0;
    return `<tr>
      <td>${g}</td>
      <td class="num">${baseNeed == null ? "—" : fmt(baseNeed)}</td>
      <td class="num">${goodNeed == null ? "—" : fmt(goodNeed)}</td>
      <td><input type="number" min="0" step="0.1" value="${want || ""}"
           data-food="${g}" placeholder="0" style="width:74px"></td>
    </tr>`;
  }).join("");

  $("food-body").querySelectorAll("[data-food]").forEach(inp => {
    inp.onchange = () => {
      const g = inp.dataset.food, v = Number(inp.value);
      if (v > 0) plan.food[g] = v; else delete plan.food[g];
      /* ★ 2026-10-18：改口粮 → 立刻重画幸福度预览（不等到 runPreview 回包）。 */
      renderFood();
      refreshPreview();
    };
  });

  /* 幸福度说明：S last / projection 二选一，Δ 用 STATE.happiness 当"当前"。 */
  const happy = food ? food.happiness : null;
  if (happy == null){
    $("food-note").textContent = "填好计划后（或试算回来）这里会显示预计下季幸福度。";
    return;
  }
  const cur = STATE && STATE.happiness;
  const deltaTxt = (cur != null && typeof cur === "number")
    ? (() => {
        const d = happy - cur;
        return `${d >= 0 ? "+" : ""}${fmt(d)}`;
      })()
    : "—";
  const baseTxt = food.baseline_met === true ? "✅ 合格"
               : food.baseline_met === false ? "❌ 未合格" : "—";
  const goodTxt = food.good_met === true ? "✅ 优秀"
               : food.good_met === false ? "❌ 未优秀" : "—";
  $("food-note").innerHTML =
    `合格档：<b>${baseTxt}</b>　优秀档：<b>${goodTxt}</b>　`
    + `→ 预计下季幸福度 <b>${fmt(happy)}</b>`
    + `（当前 ${fmt(cur)}，Δ${deltaTxt}）`;
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
/* ★ 2026-10-18 · P0-B：**实时**预览 —— 不等提交。
   数据源优先级：SIM.last（/api/simulate 只读试算的 report）→ STATE.projection。
     · 计划非空且 SIM.last 还没回来 → 明确显示「试算中…」（不是"提交后显示"）；
     · SIM.fail → 把真实原因（资金不足/校验问题）摆上行，不只是"暂不可用"；
     · 计划空 → 回到旧行为：显示 STATE.projection（上季试算/报错），或"提交计划后显示"。 */
function renderProj(){
  /* 计划空：SIM 已被 schedulePreview 清空，走 STATE.projection。 */
  if (planEmpty()){
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
    return _renderProjRows(p, false);
  }

  /* 计划非空：优先 SIM.last；没回来 → 试算中/失败原因。 */
  if (!SIM.last){
    if (SIM.fail){
      const first = String(SIM.reason || "试算没跑通").split("\n")[0].trim();
      $("proj-body").innerHTML =
        `<tr><td class="muted">预览暂不可用</td><td class="num" style="color:var(--warn)">${esc(first || "试算没跑通")}</td></tr>`;
      $("proj-note").textContent = SIM.reason || "";
      return;
    }
    $("proj-body").innerHTML = `<tr><td class="muted">试算中…</td></tr>`;
    $("proj-note").textContent = "（/api/simulate 只读试算，不提交计划）";
    return;
  }
  return _renderProjRows(SIM.last, true);
}

/* 真正摆行的那一段（SIM.last 与 STATE.projection 共用）。
   isSim=true 时来源是 QuarterReport.to_dict()：它没有 net_cash 键，
   但引擎 projection 的 net_cash 本来就是 cash_after − cash_before
   （engine/report.py:550），此处按**同一口径**补齐，公式不自创。
   ★ 2026-10-18：新增「经营盈亏」行 —— 后端 report/projection 并行给
     `operating_profit`（销售 − 工资 − 经营费 − 非建设物资款 + 工资税回流，
     即"不含建设和补贴的经营口径"）。后端还没下发时按同一公式兜底；**不**用
     total_deduction 顶（它混入了建造现金/建造物资，不是经营盈亏）。 */
function _operatingProfit(p){
  if (!p || typeof p !== "object") return null;
  if (typeof p.operating_profit === "number") return p.operating_profit;
  /* 兜底（同一口径，公式与后端一致）：
       sales_revenue − wages − operating − (materials_cost − construction_materials_cost)
       + wage_tax_refund */
  const pieces = [p.sales_revenue, p.wages, p.operating, p.materials_cost];
  if (pieces.some(v => typeof v !== "number")) return null;
  const cons = (typeof p.construction_materials_cost === "number")
    ? p.construction_materials_cost : 0;
  const refund = (typeof p.wage_tax_refund === "number") ? p.wage_tax_refund : 0;
  return p.sales_revenue - p.wages - p.operating - (p.materials_cost - cons) + refund;
}

function _renderProjRows(p, isSim){
  const netCash = (p.net_cash != null)
    ? p.net_cash
    : ((p.cash_after != null && p.cash_before != null) ? p.cash_after - p.cash_before : null);
  const opP = _operatingProfit(p);
  const rows = [
    ["物资款", fmt(p.materials_cost)],
    ["工资（税前）", fmt(p.wages)],
    ["经营费", fmt(p.operating)],
    ["一次性扣款（含建造）", "<b>" + fmt(p.total_deduction) + "</b>"],
    ["销售收入（本季价）", fmt(p.sales_revenue)],
    /* ★ 2026-10-18 · P0-F：工资税回流是**收入**，标"+"。 */
    ["工资税回流", "（收入，+" + fmt(p.wage_tax_refund) + "）"],
    ["经营盈亏（不含建设/补贴）", opP == null ? "—"
      : "<b>" + (opP >= 0 ? "+" : "") + fmt(opP) + "</b>"],
    ["期末现金", "<b>" + fmt(p.cash_after) + "</b>"],
    ["本季现金变动", (netCash != null ? (netCash >= 0 ? "+" : "") + fmt(netCash) : "—")],
    ["增加值 GDP（参考价）", fmt(p.value_added)],
  ];
  $("proj-body").innerHTML = rows.map(r =>
    `<tr><td class="muted">${r[0]}</td><td class="num">${r[1]}</td></tr>`).join("");
  $("proj-note").textContent =
    (isSim ? "只读试算（/api/simulate，未提交）。" : "") +
    "GDP 用固定参考价 P0，与销售收入（本季成交价）是两个口径。" +
    "补贴季度初已发、不计入本预览。";
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
  /* ★ 2026-10-18：报告面板与预览同一口径 —— 经营盈亏（不含建设/补贴）单独列。 */
  const opP = (typeof r.operating_profit === "number") ? r.operating_profit : (() => {
    const pieces = [r.sales_revenue, r.wages, r.operating, r.materials_cost];
    if (pieces.some(v => typeof v !== "number")) return null;
    const cons = (typeof r.construction_materials_cost === "number") ? r.construction_materials_cost : 0;
    const refund = (typeof r.wage_tax_refund === "number") ? r.wage_tax_refund : 0;
    return r.sales_revenue - r.wages - r.operating - (r.materials_cost - cons) + refund;
  })();
  $("report-body").innerHTML = `
    <table><tbody>
      <tr><td class="muted">季度</td><td class="num">第 ${r.quarter} 季</td></tr>
      <tr><td class="muted">产量</td><td class="num">${prod}</td></tr>
      <tr><td class="muted">卖出 X_t</td><td class="num">${sold}</td></tr>
      <tr><td class="muted">一次性扣款（含建造）</td><td class="num">${fmt(r.total_deduction)}</td></tr>
      <tr><td class="muted">销售收入</td><td class="num">${fmt(r.sales_revenue)}</td></tr>
      <tr><td class="muted">经营盈亏（不含建设/补贴）</td><td class="num">${opP == null ? "—" : "<b>" + (opP >= 0 ? "+" : "") + fmt(opP) + "</b>"}</td></tr>
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
/* ★ 2026-10-18 · 产物 → 中文【二级地块类型】子类型名（**显示层**，不参与计算）。
   键是引擎的 product 原值（含 '+'），值是给人看的子类型名；渲染时原值仍作 value。
     H-A=机械—农业  H-F=机械—资源  H-G=机械—工业  H-J=机械—服务
     H-*+=自动化—对应（机械路线升三级）
     I=高技术（研究线三级）
   农业 A/B/C/D、基础 G、研究、资源/服务 走原样显示。 */
const PRODUCT_SUBTYPE_CN = {
  "H-A" : "机械—农业",
  "H-F" : "机械—资源",
  "H-G" : "机械—工业",
  "H-J" : "机械—服务",
  "H-A+": "自动化—农业",
  "H-F+": "自动化—资源",
  "H-G+": "自动化—工业",
  "H-J+": "自动化—服务",
  "I"   : "I（高技术）",
  "研究" : "研究",
};
function productLabel(p){
  if (p == null || p === "") return "";
  return PRODUCT_SUBTYPE_CN[p] || String(p);
}

/* 动态决定产物行的文案（二级地块类型 / 产出）。
   opts 有候选 → 工业二级显示"二级地块类型"，农业 1 级显示"产出"。 */
function productRowLabel(w){
  if (!w || !Array.isArray(w.products) || !w.products.length) return "";
  /* 工业二级机械路线：products 全是 H-* → 玩家挑的是"二级地块类型"。
     其余有候选的（农业 A/B/C/D 之类）仍叫"产出"。 */
  const allMachine = w.products.every(p => PRODUCT_SUBTYPE_CN[p] && String(p).startsWith("H-"));
  return allMachine ? "二级地块类型" : "产出";
}
/* ★ 2026-10-18（修订）：建造/升级面板按【二级地块类型】展示，同时开放研究线。
   工业升级路线分流：
     · level1(G) → 同时给两条升级路线：ind_1_2_machine（机械四选一）
       与 ind_1_2_research（3季，不产商品 → 研究）；
     · level2 当前 p.product === "研究" → 只给 ind_2_3_high（研究→三级 I）；
     · level2 当前是机械（H-A/H-F/H-G/H-J）→ 只给 ind_2_3_machine
       （升到对应 H-*+，不可互转）。
   新建 / 农业 / 资源 / 服务照旧（route 过滤只对工业升级分支生效）。 */
function worksForCell(){
  const p = (STATE.plots || []).find(x => x.plot_id === selected);
  if (!p){
    // 空地 → 只能"新建一级"
    return WORKS.filter(w => w.from_level === 0);
  }
  if (p.status === "constructing") return [];
  const base = WORKS.filter(w =>
    w.plot_type === p.plot_type && w.from_level === p.level);
  if (p.plot_type !== "industry") return base;   // 农/资/服照旧
  if (p.level === 1) return base;                // G → 机械 + 研究 两条都给
  if (p.level === 2){
    const cur = p.product;
    if (cur === "研究"){
      return base.filter(w => w.work_id === "ind_2_3_high");
    }
    return base.filter(w => w.work_id === "ind_2_3_machine");
  }
  return base;
}

function renderBuildPanel(){
  const sel = selected;
  $("build-at").textContent = sel ? `（目标：${sel}）` : "（先在地图上点一格）";

  const list = sel ? worksForCell() : [];
  const p = (STATE.plots || []).find(x => x.plot_id === sel);

  /* ★ 2026-10-18 · 地块名置顶：选中"已建成"的地块，先亮名字（来自
     engine/naming.py 分配的 Plot.name，后端 STATE.plots[].name 直出）。
     空地/未命名回退到空串（不显示），避免误导。 */
  const nameStrip = $("plot-name-strip");
  if (nameStrip){
    if (sel && p && p.name) nameStrip.textContent = `「${p.name}」`;
    else nameStrip.textContent = "";
  }

  if (!sel){
    $("sel-work").innerHTML = `<option value="">—</option>`;
    $("btn-build").disabled = true;
    $("work-info").textContent = "点地图上任意一格开始。";
    $("row-product").classList.add("hidden");
    $("sel-product").innerHTML = "";   // 强制清空，防上一格残留
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
    $("sel-product").innerHTML = "";   // 强制清空，防上一格残留
    return;
  }

  /* ★ 2026-10-18 · 工程行的显示标签（仅显示层，不改后端 label）：
       ind_1_2_research 改写成"二级研究（3季，不产商品）"——玩家一眼知道
       这条线不是机械、本季不产商品；
       ind_2_3_high 改写成"研究→三级 I（3季）"——点明研究线的终点是 I。
       ⚠️ 括号里的季数是**速览**，权威值仍是 config 的 work.quarters
       （下面每行尾还会再拼一次「N 季」）；config 改了这里要跟着改。 */
  const WORK_LABEL_OVERRIDE = {
    ind_1_2_research: "二级研究（3季，不产商品）",
    ind_2_3_high:     "研究→三级 I（3季）",
  };
  $("sel-work").innerHTML = list.map(w => {
    const lbl = WORK_LABEL_OVERRIDE[w.work_id] || w.label;
    return `<option value="${w.work_id}">${lbl}　·　${w.cash} 金 + ${
      Object.entries(w.materials).map(([g,q])=>`${g}×${q}`).join("+")}　·　${w.quarters} 季</option>`;
  }).join("");
  $("sel-work").onchange = renderWorkDetail;
  renderWorkDetail();
}

function renderWorkDetail(){
  const wid = $("sel-work").value;
  const w = WORKS.find(x => x.work_id === wid);
  if (!w){
    $("btn-build").disabled = true;
    $("quote-info").textContent = "";
    $("row-product").classList.add("hidden");
    $("sel-product").innerHTML = "";
    return;
  }

  /* ★ 2026-10-18 · 产物/类型行按契约三态:
       ① 有 products 候选 → 显示下拉，标签按 二级地块类型/产出 动态换；
       ② products 空 + fixed_product（工业新建 G、研究 1→2"研究"、研究 2→3 I）
          → 隐藏下拉，并在 work-info 写明"固定产出：研究/I/G"；
       ③ products 空 + product_from_current=true（机械 2→3）→ 隐藏下拉，
          只显示"将升级为 H-A+（由当前 H-A 决定）"；
       ④ products 空 + fixed=null + from_current 假 → 强制隐藏并清空 sel-product，
          防止沿用上一块农业的 A/B/C/D 选项（服务/资源升级就是这条路径）。 */
  const opts = Array.isArray(w.products) ? w.products : [];
  const fromCur = w.product_from_current === true;
  const fixed = (w.fixed_product != null && w.fixed_product !== "")
              ? String(w.fixed_product) : null;
  const selEl = $("sel-product");
  /* 默认先清空：除"有下拉候选"之外的所有路径都不沿用上一格残留。 */
  selEl.innerHTML = "";
  if (opts.length){
    $("row-product").classList.remove("hidden");
    const lbl = productRowLabel(w);
    const l = document.querySelector("#row-product label");
    if (l) l.textContent = lbl || "产出 / 路线";
    selEl.innerHTML = opts.map(p =>
      `<option value="${esc(String(p))}">${esc(productLabel(p))}（${esc(String(p))}）</option>`
    ).join("");
    /* ★ 2026-10-18 · P0-E（农业 UI）：选择/切换产物后 btn-build **立即可用**
         —— 选产物是本地动作，不要求先提交计划，也没有别的门槛。 */
    selEl.onchange = () => { $("btn-build").disabled = false; };
  } else {
    selEl.onchange = null;
    /* ②③④ 全部走这里：固定产物 / 由当前决定 / 服务资源无产物 —— 统一隐藏下拉。 */
    $("row-product").classList.add("hidden");
  }

  // ★ 缺陷 3：用 <b> 强调"原价"，不再把 Markdown 的 ** 留在界面上
  let info = `${w.label}　现金 ${w.cash}（<b>原价</b>）　物资 ${
      Object.entries(w.materials).map(([g,q])=>`${g}×${q}`).join(" + ")}　工期 ${w.quarters} 季`;
  if (fixed){
    /* ② 固定产物：工业 1 级新建固定 G；研究 1→2 固定"研究"（不产商品）；
       研究 2→3 固定 I。 */
    info += `<br><span class="muted">固定产出：<b>${esc(productLabel(fixed))}</b>（${esc(fixed)}）</span>`;
  } else if (fromCur){
    /* ③ 机械 2→3：当前 product 决定目标（H-A → H-A+），不显示下拉，只写推断。 */
    const cur = (STATE.plots || []).find(x => x.plot_id === selected);
    const curProd = cur && cur.product;
    const next = curProd ? curProd + "+" : null;
    info += `<br><span class="muted">将升级为 <b>${esc(next || "（当前地块无产物）")}</b>${
      curProd ? `（由当前 ${esc(String(curProd))} 决定）` : ""}</span>`;
  } else if (!opts.length){
    /* ★ 2026-10-18 · P0-D（服务/资源 UI）：products 空 + fixed/fromCurrent 都没有
         → row-product 已在上面隐藏并清空；这里**明写**"无产物"，btn-build
         仍可提交（product=null 由 btn-build 的契约分支兜住）。
         不再出现"请选择产物"这种既不正确又点不下去的状态。 */
    info += `<br><span class="muted">无产物（服务/资源不产商品）。</span>`;
  }
  $("work-info").innerHTML = info;
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

  /* ★ 2026-10-18 · product 取值（契约）：
       ① 有 products 候选 → 用下拉原值（H-A/H-F/…/A/B/C/D）；
       ② 否则 fixed_product → 用它（工业新建 G、研究 1→2 固定"研究"、
          研究 2→3 固定"I"）；
       ③ 否则 product_from_current=true（机械 2→3）→ 当前地块 product + "+"
          （H-A → H-A+；当前为 null 时退化为 null，由后端兜校验）；
       ④ 否则 → null（资源/服务本来就不该带产物），
          且 sel-product 已被 renderWorkDetail 清空，
          不会沿用上一块农业残留的 A/B/C/D。 */
  const opts = Array.isArray(w.products) ? w.products : [];
  let product = null;
  if (opts.length){
    product = $("sel-product").value || null;
  } else if (w.fixed_product != null && w.fixed_product !== ""){
    product = String(w.fixed_product);
  } else if (w.product_from_current === true){
    const cur = (STATE.plots || []).find(x => x.plot_id === selected);
    const curProd = cur && cur.product;
    product = curProd ? String(curProd) + "+" : null;
  }

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
  toast(`已加入建设指令：${selected} ${w.label}${product ? "（" + productLabel(product) + " ＝" + product + "）" : ""}\n` +
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

/* ★ 2026-10-18 · P0-C：**默认全开** —— 把所有 status==="running" 的地块
   以 u=1 填进本地 plan.plot_plans（constructing 不填；它们本季不能排工，
   渲染层 renderPlots 的"不可开工"分支也会把残留条目清掉）。
   只在 planEmpty() 时动手 —— 玩家手动改过的计划**绝不**被覆盖。
   返回 true = 这次真填了（调用方据此补一发只读试算）。 */
function primeRunAll(){
  if (!STATE || !Array.isArray(STATE.plots)) return false;
  if (!planEmpty()) return false;
  const run = STATE.plots.filter(p => p && p.status === "running");
  if (!run.length) return false;
  run.forEach(p => plan.plot_plans[p.plot_id] = {plot_id:p.plot_id, active:true, u:1});
  return true;
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
    renderProj();                // ★ P0-B：SIM 清空 → 预览面板回到 STATE.projection
    return;
  }
  renderProj();                  // ★ P0-B：计划刚变 → 立刻先切到「试算中…」
  renderFood();                  /* ★ 2026-10-18：计划改动 → 幸福度预览也立刻切到
                                    「填好计划后…/当前值」（不等 runPreview 回包） */
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
  renderProj();                    // ★ P0-B：试算成功（SIM.last）/失败（SIM.fail+reason）都立刻上预览面板
  renderFood();                    /* ★ 2026-10-18：幸福度预览同步跟上（同一口径 SIM.last||projection） */
  renderTop();                     /* ★ 2026-10-18：让 ⚡ 平均效率跟上 SIM.last */
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
  /* ★ 2026-10-18 · P0-A 防御：提交/试算前把 status==="constructing" 的
     地块从 plot_plans 里滤掉 —— 在建/升级中不能排工（引擎 validate 也会挡，
     这里是**不把它带到服务端**的第二道闸）。
     constructing 集合 = STATE.plots ∪ MAP.virtual_plots（本季已递交的影子）。 */
  const constructing = new Set();
  ((STATE && STATE.plots) || []).forEach(p => {
    if (p && p.status === "constructing") constructing.add(String(p.plot_id));
  });
  ((MAP && MAP.virtual_plots) || []).forEach(v => {
    if (v && v.status === "constructing") constructing.add(String(v.plot_id));
  });
  const pp = {};
  Object.entries(plan.plot_plans || {}).forEach(([pid, v]) => {
    if (constructing.has(String(pid))) return;
    pp[pid] = v;
  });
  return {
    player_id: PLAYER,
    plot_plans: pp,
    purchases: plan.purchases,
    food: plan.food,
    builds: plan.builds,
    contracts: plan.contracts,
  };
}

/* ★ 2026-10-18：不再让玩家点「下一季」。
   结算后后端处于 report 阶段（月报已出、计划不可编辑），这里自动
   调一次 /api/next_quarter 把它切回 collecting，再刷新数字。
   这样每个季度只保留一个动作：「推进本季度」。 */
async function enterNextQuarter(){
  try{
    const r = await api("POST", "/api/next_quarter", {game:GAME, player:PLAYER});
    if (r && r.state) STATE = r.state;
    return true;
  }catch(_e){
    return false;
  }
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

    /* ★ 2026-10-18：月报阶段并入行动阶段 —— end_quarter 回包是 report 阶段，
       立刻自动切回 collecting。玩家不再需要点「下一季」。 */
    const enteredNext = await enterNextQuarter();

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

    /* ③ 兜底渲染：如果自动进入下一季失败，`eq.state` 还能把界面切到
       月报阶段（至少不是旧季度）；正常情况下 `enteredNext` 已经拿到
       collecting 状态，不能再被旧的 report 状态覆盖。 */
    if (!enteredNext && eq && eq.state) STATE = eq.state;
    resetPlan();                    // 计划已随结算消耗掉
    /* ★ 2026-10-18 · P0-C：进入新一季 → 默认全开（只填 running，不碰在建）。
       planEmpty 闸门保证不覆盖手填；玩家上一季手动"全部停工"的偏好
       不会跨季残留（计划随结算消耗，resetPlan 后是空）。 */
    if (STATE.phase === "collecting" && primeRunAll()){
      renderPlots();
      schedulePreview();
    }
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
      /* ★ 2026-10-18：结算后也弹一次上季月报（原先只在 refresh 里弹，
         结算路径漏了）。不阻塞；同季只弹一次。 */
      try{ maybeShowQuarterReport(); }catch(_e){ /* modal 失败不影响结算 */ }
      loadNarratives();             // 不 await：数字已显示，叙事后台来
      try{ applyFinishedState(); }catch(_e){ /* ★ 终局界面自身异常不连累结算 */ }
    }else{
      toast("已结算；界面刷新失败，请刷新页面或点「推进本季度」", "warn");
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
        toast("已结算；界面刷新失败，请刷新页面或点「推进本季度」", "warn");
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
    /* ★ 2026-10-18 · P0-C：refresh() 内部已按 collecting+planEmpty 默认全开，
       这里补一道（若 refresh 因故被跳过/未来改动，按钮本身也默认全开）。 */
    if (STATE && STATE.phase === "collecting" && primeRunAll()){
      renderPlots();
      schedulePreview();
    }
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
/* 把 AI 起草的 suggested_plan **合并**进前端既有的待提交 plan（与 /api/submit 同形）。
   ★ 2026-10-18：不再 `resetPlan()` 清空玩家已有安排 ——
     · plot_plans / food / purchases：**逐键合并**（AI 的键覆盖玩家同键；玩家其它键保留）；
     · builds：**按 plot_id 合并** —— 新条目覆盖玩家同 plot_id 的旧条；玩家没有冲突的旧条保留；
     · contracts：**仅有值才替换**（AI 没给 contracts → 玩家已有 contracts 不动）。
   之前的"应用 AI 计划 = 重置 + 覆盖"会把玩家点好的建造/开工全抹掉，不符合
   「AI 只起草，玩家持有主导权」的口径（§11.4）。 */
function applySuggestedPlan(sp){
  if (!sp || typeof sp !== "object") return;
  /* plot_plans / food / purchases：逐键合并 */
  if (sp.plot_plans && typeof sp.plot_plans === "object"){
    Object.entries(sp.plot_plans).forEach(([pid, v]) => { plan.plot_plans[pid] = v; });
  }
  if (sp.purchases && typeof sp.purchases === "object"){
    Object.entries(sp.purchases).forEach(([g, v]) => { plan.purchases[g] = v; });
  }
  if (sp.food && typeof sp.food === "object"){
    Object.entries(sp.food).forEach(([g, v]) => {
      const num = (typeof v === "number") ? v : Number(v);
      if (num > 0) plan.food[g] = num; else delete plan.food[g];
    });
  }
  /* builds：按 plot_id 合并 —— 新覆盖同 ID，其它保留。 */
  if (Array.isArray(sp.builds)){
    sp.builds.forEach(b => {
      if (!b || b.plot_id == null) return;
      const pid = String(b.plot_id);
      const idx = plan.builds.findIndex(x => x && String(x.plot_id) === pid);
      if (idx >= 0) plan.builds.splice(idx, 1, b);   // 覆盖同 plot_id
      else plan.builds.push(b);                       // 新增
    });
  }
  /* contracts：AI 给了才整组替换；没给就不动玩家已有 contracts。 */
  if (Array.isArray(sp.contracts)){
    plan.contracts = sp.contracts;
  }
}

/* ── 机制题 / 规划题 / 起草题分流（与后端同一套关键词；前端先判一版，后端仍可改判）──
   · 强动作词优先：只要出现「建/造/取消/撤销/删/安排/…/改」，就是让我动手 → plan；
   · ★ 2026-10-18 第九轮：出现「规划/该不该/值不值/要不要/建议/划算/留多少/建在哪」
     → answer（**只解释、不落表**）。第八轮实测把「帮我规划本季」「现在该不该升级」
     判成 auto（前端固定 draft:true）→ 直接回一份 Plan，玩家永远拿不到
     「先做什么 / 留多少 / 有什么取舍」的**文字规划**。这里把这几类词显式判成 answer；
     后端会按 mode 强翻 draft=False，摘要里带「目标/先后/留钱」三句（引擎算的数字）。
     ⚠️ 强动作词仍在最前面：说「帮我规划」但同时说「建两块」→ 仍按 plan 起草。
   · 否则出现机制词（怎么算/为什么/是什么/…）→ answer：只解释，不生成计划；
   · 都不沾 → auto：交给后端判断（闲聊、模糊请求都走这条）。
   返回 "answer" | "plan" | "auto"（原样塞进 /api/ai/advise 的 mode）。 */
const AI_ACTION_WORDS = ["建","造","取消","撤销","删","安排","起草","填",
                         "开工","停工","买","卖","改"];
const AI_ADVICE_WORDS = ["规划","该不该","值不值","要不要","建议",
                         "划算","留多少","建在哪","先做什么","优先级"];
const AI_MECHANISM_WORDS = ["怎么算","为什么","是什么","机制","规则","解释",
                            "原理","公式","多少","区别","能不能"];
/* ★ 2026-10-18 D3：问「口粮/粮食要多少」不是机制题，是要**填数字**。
   用户原话：「比例对了，但没法读取优秀需要多少进行准确填写。」
   只加这一条白名单（粮食词 + 问量词），其它启发式一字不动 ——
   不含粮食词的「服务区怎么加成」仍旧走 answer。 */
const AI_FOOD_WORDS = ["粮","口粮","粮食","吃"];
const AI_FOOD_ASK_WORDS = ["多少","几","够","买","要"];

function detectAiMode(q){
  const s = String(q == null ? "" : q);
  if (AI_ACTION_WORDS.some(w => s.includes(w))) return "plan";
  /* ★ 第九轮：「规划 / 该不该 / 值不值 / 建议」等**要判断**的问题走 answer
     （后端会强翻 draft=False，回来的是文字规划；不再直接甩一份 Plan 给玩家）。 */
  if (AI_ADVICE_WORDS.some(w => s.includes(w))) return "answer";
  if (AI_FOOD_WORDS.some(w => s.includes(w))
      && AI_FOOD_ASK_WORDS.some(w => s.includes(w))) return "plan";
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
  status.textContent = "秘书在想…";
  status.style.color = "var(--warn)";

  /* ★ 带上当前草稿与模式（新契约，后端可缺省忽略）：
     · current_plan：让后端「续写/只撤这一条」，而不是每次从零起草；
     · mode：前端按同一套关键词先判一版 —— answer = 机制题/规划题（只解释、不落表）、
       plan = 有强动作词（要我动手）、auto = 其余交给后端判；
     · draft：★ 第九轮不再固定 true —— answer 模式（「帮我规划本季」「该不该升级」）
       必须让后端回**文字规划**，而不是直接甩一份 Plan（后端也会按 mode 强翻，
       这里先对齐意图，读到请求体的人一眼能看出这次要不要起草）。 */
  const aiMode = detectAiMode(q);
  const body = {
    game: GAME, player: PLAYER, question: q,
    draft: aiMode !== "answer",
    current_plan: publicPlan(),
    mode: aiMode,
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
         只把 AI 的 summary / 校正说明留在气泡里给她参考。
         ★ 2026-10-18：applySuggestedPlan 改为**合并**（不 resetPlan），文案改为「合并进来」。 */
      const hadLocal = !planEmpty();
      if (hadLocal){
        const ok = window.confirm(isRemove
          ? "AI 要撤销部分建造并保留其余安排，应用吗？"
          : "你已经有本地草稿。应用 AI 的计划会把它合并进来（不覆盖你已有的建造/开工），继续吗？");
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
        (hadLocal ? `\n（提醒：AI 计划已**合并**到你已有的安排里——你自己的建造/开工仍在，新条目按地块覆盖同名老条目。）` : ""));
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

/* 输入框按键：
     · Enter           → **直接发送**（2026-10-18 起）；
     · Shift+Enter     → 换行（不发送）；
     · Ctrl/Cmd+Enter  → 也发送（老快捷键保留，不冲突）；
     · 输入法 composing（IME 选词中）→ **不发送**，避免打字到一半飞了。 */
$("ai-input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  /* IME 正在组词（中文/日文/韩文输入）：this Enter 是确认候选字，不是发送。 */
  if (e.isComposing) return;
  if (e.shiftKey) return;                        // Shift+Enter = 换行
  e.preventDefault();
  $("btn-ai-send").click();
});

/* 开局/读档后第一次在秘书框里给一句问候（没有历史时才给）。
   ★ 2026-10-18：精简到一句，第二段（"起草就直接填到界面…"）删除。 */
function aiGreet(){
  if (AI_LOG.length) return;
  aiBubble("ai",
    "你好，我是你的 AI 秘书。问问题、要建议、让我起草本季计划都行。");
}

/* ══════════════════════════════════════════════════════════
   六、季度月报摘要 modal（2026-10-18 · 同一季度只弹一次）
   ══════════════════════════════════════════════════════════
   条件三件事：
     ① STATE.report 存在（结算过）；
     ② STATE.phase === "collecting"（行动阶段，不是结算/报告页）；
     ③ STATE.report.quarter === STATE.quarter - 1（刚跨季，
        上一季报告是"新的"）；
   锁：localStorage `seen_report_<GAME>_<STATE.quarter>`，同季只弹一次。
   入口只在 refresh() 成功 renderAll 后调用一次（try/catch 静默，
   不能反过来拖住主界面）。按钮两枚：知道了＝隐藏；看完整月报＝隐藏并滚动。 */
function _reportModalSeenKey(gid, q){
  return `seen_report_${gid || ""}_${q == null ? "" : q}`;
}

function maybeShowQuarterReport(){
  const s = STATE;
  if (!s) return;
  const r = s.report;
  if (!r) return;                                        // ①
  if (s.phase !== "collecting") return;                  // ②
  if (r.quarter !== (s.quarter - 1)) return;             // ③

  const modal = $("report-modal");
  if (!modal) return;

  let key = _reportModalSeenKey(GAME, s.quarter);
  try{
    if (window.localStorage && localStorage.getItem(key)) return;
  }catch(_e){ /* localStorage 不可用时退化为"每次刷新都弹一次"决策仍安全 */ }

  /* 填字段（全部来自 STATE.report，原样显示，前端零计算） */
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set("rm-title", `第 ${r.quarter} 季月报`);
  set("rm-cash",  fmt(r.cash_after));
  set("rm-sales", fmt(r.sales_revenue));
  set("rm-ded",   fmt(r.total_deduction));
  set("rm-vadd",  fmt(r.value_added));
  set("rm-happy", r.food ? fmt(r.food.happiness) : "—");

  /* 工程开工 / 完工（来自 r.construction_events；空就显示「本季无工程变动」） */
  const events = Array.isArray(r.construction_events) ? r.construction_events : [];
  const evEl = $("rm-events");
  if (evEl){
    const parts = events.filter(e => e && e.plot_id).map(e => {
      const t = TYPE_CN[e.plot_type] || e.plot_type || "?";
      const route = e.product ? "·" + productLabel(String(e.product)) : "";
      const lv = `${t}${e.from_level != null ? e.from_level : "?"}→${e.to_level != null ? e.to_level : "?"}${route}`;
      return e.event === "completed" ? `✓ 工程完工：${e.plot_id}（${lv}）`
           : e.event === "started"   ? `✓ 工程开工：${e.plot_id}（${lv}）`
           : null;
    }).filter(Boolean);
    evEl.innerHTML = parts.length
      ? parts.map(t => `<div>${esc(t)}</div>`).join("")
      : `<div class="muted">本季无工程变动。</div>`;
  }

  /* 一条警告（取第一条命中的，不叠加）：
     · 口粮未达标（r.food.baseline_met === false）
     · 现金 < 300
     · 无产出（r.production 空） */
  const warnEl = $("rm-warn");
  if (warnEl){
    let warn = "";
    if (r.food && r.food.baseline_met === false){
      warn = "⚠️ 口粮未达标：下季可能影响幸福度。";
    } else if (r.cash_after != null && Number(r.cash_after) < 300){
      warn = "⚠️ 现金低于 300：注意下一季预算。";
    } else if (!Array.isArray(r.production) || !r.production.length){
      warn = "⚠️ 上季无产出：检查地块开工 / 建造。";
    }
    if (warn){
      warnEl.textContent = warn;
      warnEl.style.display = "";
    } else {
      warnEl.textContent = "";
      warnEl.style.display = "none";
    }
  }

  modal.classList.remove("hidden");

  /* 按钮钩子（每弹一次重挂，避免旧闭包吃到老 GAME / quarter） */
  const close = () => { modal.classList.add("hidden"); };
  const okBtn = $("rm-btn-ok"), moreBtn = $("rm-btn-more");
  if (okBtn) okBtn.onclick = () => { close(); };
  if (moreBtn){
    moreBtn.onclick = () => {
      close();
      const tgt = $("report-panel") || $("report-body");
      if (tgt && typeof tgt.scrollIntoView === "function"){
        try{ tgt.scrollIntoView({behavior:"smooth", block:"start"}); }
        catch(_e){ tgt.scrollIntoView(); }
      }
    };
  }

  /* 落锁：本次已弹过，本季不再弹。localStorage 写入失败也认了
     （最坏下场是玩家刷新后又看到一次，不会渲染炸）。 */
  try{
    if (window.localStorage) localStorage.setItem(key, "1");
  }catch(_e){ /* 静默 */ }
}

/* ══════════════════════════════════════════════════════════
   折叠：本季安排 panel（表格 + runall/stopall + 已排建设）
   ──────────────────────────────────────────────────────────
   · 按钮 #btn-plots-collapse（▾/▸ 切换）控制 #plots-collapse-body 显隐。
   · 状态写 localStorage —— key `rg_plots_collapse_<game_id>`（"1"=收起,
     未存过 / 存其他值 = 默认**展开**）。换档 GAME 变 → 各自记忆各自的。
   · 打开时机：GAME 已就绪（btn-new / btn-load 把 main 从 hidden 撤销后）
     refresh() 里挂一次 applyPlotsCollapseState() 即同步到 UI；
     按钮 onclick 只负责 toggle。 */
function _plotsCollapseKey(){
  return "rg_plots_collapse_" + (GAME != null ? String(GAME) : "_");
}
function _readPlotsCollapsed(){
  try{
    if (!window.localStorage) return false;
    return localStorage.getItem(_plotsCollapseKey()) === "1";
  }catch(_e){ return false; }
}
function _writePlotsCollapsed(collapsed){
  try{
    if (!window.localStorage) return;
    if (collapsed) localStorage.setItem(_plotsCollapseKey(), "1");
    else localStorage.removeItem(_plotsCollapseKey());
  }catch(_e){ /* 静默 */ }
}
function applyPlotsCollapseState(){
  const btn = document.getElementById("btn-plots-collapse");
  const body = document.getElementById("plots-collapse-body");
  if (!btn || !body) return;
  const collapsed = _readPlotsCollapsed();
  body.classList.toggle("collapsed", collapsed);
  btn.textContent = collapsed ? "▸" : "▾";
  btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
}
/* 挂按钮钩子（只在 DOM ready 后挂一次；GAME 变了不影响按钮钩子本身,
   只是 applyPlotsCollapseState 读到的 key 变了）。 */
(function _bindPlotsCollapse(){
  const bind = () => {
    const btn = document.getElementById("btn-plots-collapse");
    if (!btn) return;
    if (btn._plotsCollapseBound) return;
    btn._plotsCollapseBound = true;
    btn.addEventListener("click", () => {
      const collapsed = !_readPlotsCollapsed();
      _writePlotsCollapsed(collapsed);
      applyPlotsCollapseState();
    });
  };
  if (document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
