/* ══════════════════════════════════════════════════════════════
   map.js —— 地图渲染相关:
     · displayPlots(): 把 STATE.plots 与 MAP.virtual_plots 合成视图
     · renderMap():    地块格子渲染(含 planned 草稿显影 + data-asset)
   从原 index.html `<script>`「── 地图 ──」块整体搬出,函数内部一行未改。
   依赖 api.js 里的全局: $  STATE  MAP  selected  plan  TYPE_CN
   依赖 panel.js 里的: renderBuildPanel()(点击格子后刷新建造面板)
   ══════════════════════════════════════════════════════════════ */
/* ── 地图 ─────────────────────────────────────────────── */
/* 把 STATE.plots 与 MAP.virtual_plots（本季已下建设指令、未结算的影子）
   合成一个"显示用"视图：pending 在先（递交后立刻显影）、结算后由真 plots 顶替。
   引擎只给数、前端只展示 —— 此层不做任何判定/折扣计算。 */
function displayPlots(MAP, STATE){
  const out = {};
  (STATE.plots || []).forEach(p => out[p.plot_id] = p);
  (MAP.virtual_plots || []).forEach(v => {
    if (!out[v.plot_id]) out[v.plot_id] = v;  // 已存在真地块 → 真地块为准
  });
  return out;
}

function renderMap(){
  const el = $("map");
  el.style.gridTemplateColumns = `repeat(${MAP.cols}, minmax(0,1fr))`;
  const byId = displayPlots(MAP, STATE);

  /* ★ ②号改造：本地 plan.builds（**只在草稿、还没递交**）也要在地图上显影。
       与"已递交的影子"（virtual_plots）分开档位：
         已递交 → 引擎给的"constructing"虚线橙；
         只在草稿 → 前端的"planned"虚线青 + 「待建」。
       玩家一眼看出："这几块是我这季打算动的"。 */
  const plannedById = {};
  (plan.builds || []).forEach(b => {
    if (b && b.plot_id) plannedById[b.plot_id] = b;
  });

  el.innerHTML = MAP.cells.map(c => {
    // ★ 缺陷 2：cell 直接带 status（引擎 build_map 已补）。优先用 cell 自己的
    //   status；空地上叠的"前影"（virtual_plots）也会带 status=constructing，
    //   不用再去 STATE.plots 里找。
    const cellStatus = c.status;             // 引擎给的；空地无此字段
    const p = byId[c.plot_id];               // STATE.plots / virtual_plots
    const status = cellStatus || (p && p.status);
    const isConstructing = status === "constructing";
    const isRunning = status === "running";
    const planned = plannedById[c.plot_id];  // 只在草稿里的待建指令

    const cls = ["cell"];
    if (p || cellStatus){
      // 有真地块或有 pending 影子就高亮；类型色优先看 cell/c 实有
      const pt = c.plot_type || (p && p.plot_type);
      if (pt) cls.push("mine", pt);
      if (isConstructing) cls.push("constructing");
      if (isRunning) cls.push("running");
    } else if (planned){
      /* 只在草稿里的待建（引擎还没影子）—— 不上"mine/类型色"
         （那是"已经是你的"的信号），只上 planned（待建青）。 */
      cls.push("planned");
    }
    if (selected === c.plot_id) cls.push("sel");
    let inner;
    if (!p && !cellStatus && !planned){
      inner = `+`;
    } else if (planned && !p && !cellStatus){
      /* 待建格：类型完整中文名 + "待建"。产物不再显示
         （★ 2026-10-18 两段式拍板：上一行 zone 中文名、下一行 N 级）。 */
      const pt = planned.plot_type;
      const zoneName = ZONE_CN[pt] || TYPE_CN[pt] || "?";
      inner = `<span class="lt">${zoneName}</span>`
            + `<span class="sub">待建</span>`;
    } else {
      /* ★ 2026-10-18 · 两段式重构：
           · 上一行(.lt): zone 完整中文名（**农业** / **资源** / **工业** / **服务** / **研究**）,
             不再是单字简写；
           · 下一行(.sub): 等级（**1级** / **2级** / **3级**）,
             施工中也照常显示（比如"1级→2级"），不再显示产物。
           · research 是 industry L2 product='研究' 的特殊化，
             展示层归到"研究"zone（与 plotDisplayType 同一口径）。 */
      const pt = c.plot_type || (p && p.plot_type);
      const prod = (p && p.product) || c.product;
      const isResearch = (String(prod || "") === "研究");
      const zoneName = isResearch ? ZONE_CN.research
                                  : (ZONE_CN[pt] || TYPE_CN[pt] || "?");
      const lvl = (p && p.level != null) ? p.level : (c.level != null ? c.level : 0);
      const tgt = (p && p.target_level != null) ? p.target_level : lvl;
      const lvlTxt = isConstructing ? `${lvl}级→${tgt}级` : `${lvl}级`;
      inner = `<span class="lt">${zoneName}</span>`
            + `<span class="sub">${lvlTxt}</span>`;

      /* ★ S6 E 四分量悬停 tooltip —— 引擎给就显示、没给就不渲染。
           数据源：STATE.plots[i].efficiency（engine/efficiency.py:86-100 to_dict()），
           字段名原样用：adjacency / happiness / ramp_up / research_multiplier，
           合计 = .efficiency（派生乘积，引擎算的）。
           - engine/report.py:395 只在 plot.is_running 时填 → 在建/升级中
             efficiency=null，**前端不造公式**（红线），所以那里不渲染 tooltip。
           - 空地/待建草稿自然也不渲染。
           - esc() 数字字段虽是 number，按惯例 esc 防 XSS。 */
      const eff = (p && p.efficiency) || null;
      /* cell-tooltip 仅在 eff 存在时挂 —— eff=null 的格子（在建/升级/无拆解）
         保持原样，没有 .cell-tooltip 子节点，CSS 的 .cell:hover .cell-tooltip
         规则找不到目标，自然不会浮出。 */
      if (eff && typeof eff === "object"){
        const tt = `<div class="cell-tooltip">`
          + `<div class="tooltip-title">效率拆解（×连乘）</div>`
          + `<div class="kv"><span class="k">邻接</span><span class="v">×${esc(fmt(eff.adjacency))}</span></div>`
          + `<div class="kv"><span class="k">幸福</span><span class="v">×${esc(fmt(eff.happiness))}</span></div>`
          + `<div class="kv"><span class="k">磨合</span><span class="v">×${esc(fmt(eff.ramp_up))}</span></div>`
          + `<div class="kv"><span class="k">研究</span><span class="v">×${esc(fmt(eff.research_multiplier))}</span></div>`
          + `<div class="kv total"><span class="k">合计</span><span class="v">×${esc(fmt(eff.efficiency))}</span></div>`
          + `</div>`;
        inner += tt;
      }
    }
    /* glB 资产键（引擎给，前端只挂到 DOM 上 —— 不拼键、不加载模型）。
       c.asset 为 null = 空地/无模型 → 没有 data-asset 属性，保持现有 2D 渲染。
       将来接 three.js 时按 data-asset 查 MAP.assets 得 .glb 路径；
       文件缺失（fetch/Loader 失败）就保持这个 2D 格子不动，即回退。 */
    const asset = c.asset ? ` data-asset="${c.asset}"` : "";
    return `<div class="${cls.join(" ")}" data-id="${c.plot_id}"${asset} title="${c.plot_id}">${inner}</div>`;
  }).join("");

  el.querySelectorAll(".cell").forEach(c => {
    c.onclick = () => { selected = c.dataset.id; renderMap(); renderBuildPanel(); };
  });

}

/* ══════════════════════════════════════════════════════════════
   2026-10-18 · S5 3D 渲染层**整段已删除**（用户拍板：主界面纯 2D，
   3D 只在 city-modal 弹窗里看建模 —— 弹窗由另一子代理维护）。
   删除范围：原 glbKeyForPlot（@deprecated）+ APP.map3d IIFE（_ensureRenderer
   / _resize / _render / _loadGlb / _placeGlb / _sync / _requestSync /
   APP.map3d = {...}），以及 renderMap() 末尾的 requestSync() 调用。
   tiles.css 里 #map3d 样式也已同步删除；#map{position:relative} 保留
   （.cell-tooltip 悬停定位要用）。
   后端 /static/assets/plots/*.glb 未动；想找回此层看 git 历史。
   ══════════════════════════════════════════════════════════════ */

