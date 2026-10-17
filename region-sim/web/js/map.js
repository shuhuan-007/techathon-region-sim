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
      /* 待建格：类型短码 + "待建"，产物也写上（比如"农·A"）。 */
      const pt = planned.plot_type;
      const prod = planned.product;
      inner = `<span class="lt">${TYPE_CN[pt] || "?"}·待建</span>`
            + `<span class="sub">${prod || "（未选产出）"}</span>`;
    } else {
      const pt = c.plot_type || (p && p.plot_type);
      const lvl = (p && p.level != null) ? p.level : (c.level != null ? c.level : 0);
      const tgt = (p && p.target_level != null) ? p.target_level : lvl;
      const lvlTxt = isConstructing ? `${lvl}→${tgt}` : `${lvl}`;
      const prod = (p && p.product) || c.product;
      inner = `<span class="lt">${TYPE_CN[pt] || "?"}${lvlTxt}</span>`
            + `<span class="sub">${prod || (isConstructing ? "在建" : "—")}</span>`;

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

  /* ★ S5 3D 叠加层：渲染完 2D cell DOM 后通知 3D 层同步（防抖）。
       失败（THREE 未加载、glb 404、WebGL 不支）时 3D 层静默隐藏，
       2D cell 依然完整 —— 这就是"保留 2D 作 fallback"的含义。 */
  if (window.APP && window.APP.map3d) window.APP.map3d.requestSync();
}

/* ══════════════════════════════════════════════════════════════
   S5 · 3D 渲染层（方案 B 单大 canvas 覆盖 #map 上）
   ══════════════════════════════════════════════════════════════
   落地方案（自主拍板，记录在交付报告里）：
     · 相机：屏幕像素正交（Screen-space ortho，纯俯视 top-down）。
       相机 left/right/top/bottom 与 #map 容器像素宽高对齐，cell (r,c) 的
       offsetLeft/offsetTop/offsetWidth 直接读出 → glb 摆到对应屏幕像素
       位置；resize 时只需 resize renderer + 重设 frustum。
     · 视野深度：near=-1000, far=1000，相机在 z=500 朝 -z 看。
       (x 向右、y 向上即屏幕向上 → three.js y 轴对应屏幕 -y,
       因此投影时 y_3d = mapHeight - (offsetTop + offsetHeight/2))。
     · 大小：glb 局部 ±0.95,映射成 cell 内部 90%（父代拍板的 90% 边距方案）。
       即 1 world unit = (cell.innerWidth * 0.9) / 1.9 ≈ cellW * 0.4737。
       用 mesh.scale.setScalar(scale) 实现。
     · 朝向：把 glb y-up 转成 z-up 显示 → mesh.rotation.x = -Math.PI/2
       （让 glb 的 +y 顶视后显示朝屏幕上方即 +y_screen）；three.js 相机
       俯视时屏幕上方对应 three.js 的 +y,所以 mesh 局部 +y 投影后对到
       屏幕 +y 正好。
     · constructing 灰色模型（用户拍板 20:50）：
       克隆的 gltf.scene.traverse,把每个 mesh.material 替换成
       new THREE.MeshBasicMaterial({color: 0x888888})。
       简单 5 行，按用户原话"方便就选这个" → 选了这个。
   ══════════════════════════════════════════════════════════════ */

/* ★ S5: plot(plot_type, level, product) → glb 文件名（中文目录名）。
     与 engine/assets.py 的 asset_key 等价，前端复刻版。
     返回 null：无对应建模 → 该 cell 走 2D SVG/CSS 渲染（原样）。

     @deprecated 2026-10-17 —— 前端 _sync() 已改为直接用 MAP.cells[pid].asset
     （engine 已算好的中文 glb 键），本函数不再被调用，仅作保留兜底。
     ⚠️ 历史 bug：constructing 且 level=0 时 `if (!t || !lv)` 把 lv=0 当 falsy
     永远 return null → constructing 0 级地块 3D 不渲染（已由此修复）。 */
function glbKeyForPlot(plot) {
  if (!plot) return null;
  const t = plot.plot_type, lv = plot.level, p = plot.product;
  // ⚠️ 2026-10-17 真修：原来是 `if (!t || !lv)` —— 而**在建地块 level 恰好是 0**，
  //    0 是 falsy，于是所有在建地块都 return null、3D 永远不渲染。
  //    这里必须判 `lv == null`（只挡"没有这个字段"），不能判 falsy。
  if (!t || lv == null) return null;
  if (t === 'agriculture') return '农业' + lv;
  if (t === 'resource')    return '资源' + lv;
  if (t === 'service')     return '服务' + lv;
  if (t === 'research')    return (lv === 1) ? '研究' : null;
  if (t === 'industry') {
    if (lv === 1) return '工业1';
    const base = (p || '').replace(/\+$/, '');   // 去掉 "+" 后缀
    const m = {
      'H-A,2': '工业农业2', 'H-A,3': '工业农业3',
      'H-G,2': '工业工业2', 'H-G,3': '工业工业3',
      'H-F,2': '工业资源2', 'H-F,3': '工业资源3',
      'H-J,2': '工业服务2', 'H-J,3': '工业服务业3',  // ⚠️ 美术笔误名,但以实际 glb 目录名为准
      'I,3':  '工业高技术3',
    };
    return m[base + ',' + lv] || null;
  }
  return null;
}

/* ★ S5: APP.map3d 单例。/static/assets/plots/<encode(name)>.glb 一文件一 fetch,
     结果挂到 _gltfCache（值为 Promise<GLTF>）保证同一 glb 全程只请求一次。 */
window.APP = window.APP || {};
(function (APP) {
  /* 内部状态 */
  let _canvas = null;          // <canvas id="map3d">
  let _renderer = null;        // THREE.WebGLRenderer
  let _scene = null;           // THREE.Scene
  let _camera = null;          // THREE.OrthographicCamera
  let _mapEl = null;           // #map 容器
  let _plotLayer = null;       // THREE.Group,每次 sync 清空重摆
  let _enabled = true;         // 真表示 WebGL 不可用/构造失败时 _ensureRenderer 会改为 false 永久关闭
  let _syncScheduled = false;  // 防抖：renderMap 每调一次就 requestSync 一次太频繁
  let _failedUrls = {};        // 已 404 的 glb → 下次直接跳过不再 warn 同一 URL

  const _gltfCache = {};       // name → Promise<GLTF>（永久缓存,包括失败）
  let _gltfLoader = null;      // THREE.GLTFLoader 实例

  /* ── 相机与几何（2026-10-17 重写）─────────────────────────────
     重写原因：旧方案 `OrthographicCamera(0,1,0,1)` + `_resize()` 改
     left/right/top/bottom + `lookAt(0,0,0)` + `inst.rotation.x=-π/2`
     组合，在一个渲染管线里同时塞了 4 个互相矛盾的约定：
       1. `top=0, bottom=h` 与 three.js "top > bottom" 的顺序相反；
       2. 相机在 (0,0,500) 看 (0,0,0)，视线 -z，但 frustum 的
          left/right/top/bottom 是按**相机本地**坐标系配的，lookAt
          之后的旋转会跟"屏幕像素 = 本地 (x,y)"这件事有交互；
       3. `inst.rotation.x = -π/2` 把 glb +y 转到 +z，但相机视线是
          -z，所以从相机看过去看到的是 glb **底面**，不是顶面；
       4. scale 假设 glb 是 ±0.95 本地单位，不 <Box3 实测>。
     结果：即便 glb 200 加载成功，三个致命几何错误叠加 → 屏幕上看不到
     任何模型。

     新方案（清晰版，CSS 像素 = 世界单位）：
     ─────────────────────────────────────
     坐标系约定
       · 地图 CSS 像素 [0,W]×[0,H]，cell 中心 (cx,cy) 由
         offsetLeft/offsetTop + offsetWidth/offsetHeight/2 计算。
       · 世界坐标 = CSS 像素，x 向右、y 向上（OpenGL 惯例）。
         CSS 的 cy（向下）≡ 世界 y，关系：y_world = H - cy。
       · z 朝屏幕外（朝相机），z=0 落在 cell 平面上。
     相机（每次 _resize 全量重设，不留"相对"状态）
       · OrthographicCamera(-W/2, W/2, H/2, -H/2, 0.1, 4000)：
         frustum 中心即世界 (W/2, H/2)，本地 +x=屏右 +y=屏上 +z=朝相机。
       · position = (W/2, H/2, 1500)，up = (0,1,0)，
         lookAt(W/2, H/2, 0)。相机在地图正上方往下看。
         1500 的取值是随意的，只要满足 0.1 < 1500 < 4000，
         near/far 把 cell 平面（z=0）夹在内部即可。
     模型摆放（_placeGlb）
       · 先在 identity 变换下用 Box3.setFromObject(inst) 量出 glb 原始
         bounding box；取 max(size.x, size.z) 作为"水平跨距"（顶视下
         屏幕 x 对应 glb x，屏幕 y 对应 glb z）。
       · scale = (cellW * 0.9) / 水平跨距 → glb 最宽处恰好占 cell 内宽
         的 90%（父代拍板的 90% 边距）。
       · Box3.getCenter() 得原始中心 c；把模型平移 −c，让 glb 的
         **几何中心**而不是原点对齐 cell 中心（不然模型本身偏心的话，
         看到的会偏出 cell）。
       · rotation.x = -π/2 把 glb +y（建筑"朝上"）转到 +z（朝相机）：
         绕 +x 转 -90° 时 R_x(-π/2)·ŷ = +ẑ ✓，ŝ → -ŷ，即 glb 前方 (z+)
         投到屏幕下方，这是建筑"顶视"图的标准朝向。
       · position = (cx, H - cy, 0) 做 CSS 向下 → 世界向上 的翻转。
       · 注意变换应用顺序：three.js 是 M = T·R·S，即先 scale、再
         rotate、最后 translate；我们把"减中心"放在实例的 children
         前面作为内嵌的 Group，这样不用手动算复合矩阵。
     constructing 灰化
       · 同旧版：clone 后 traverse，把每个 mesh.material 换成
         共享的 MeshBasicMaterial({color:0x888888})（Basic 材质
         不受光，灰得均匀）。
     ───────────────────────────────────── */
  function _ensureRenderer(){
    if (_renderer || !_enabled) return;
    if (typeof THREE === 'undefined' || !THREE.GLTFLoader){
      console.warn('[map3d] THREE 或 GLTFLoader 未加载 — 3D 层关闭');
      _enabled = false; return;
    }
    _mapEl = $('map');
    if (!_mapEl){ _enabled = false; return; }
    try {
      _canvas = document.createElement('canvas');
      _canvas.id = 'map3d';
      // 用 antialias:false 与 pixelRatio:1,父代拍板的性能约束
      _renderer = new THREE.WebGLRenderer({
        canvas: _canvas, antialias: false, alpha: true,
        powerPreference: 'low-power',
      });
      _renderer.setPixelRatio(1);
    } catch(e){
      console.warn('[map3d] WebGLRenderer 构造失败 — 3D 层关闭:', e);
      _enabled = false; return;
    }
    _scene = new THREE.Scene();
    /* 光照（2026-10-17 调亮）——首次截图发现模型可见但 RGB 全在 30 以下,
       相机正对 +z 顶视时旧方向光 dir.position=(200,400,600) 几乎正射
       +z 面,只 Lev 0.35 → glb PBR 材质顶面反射率被压得很暗。
       新方案：AmbientLight 1.0 提供均匀底光 + 两盏方向光 0.6 各从
       两侧 45° 照射,让 PBR 材质的色彩纹理能真正出来。
       MeshBasicMaterial（constructing 灰） 不看光,不受影响。 */
    _scene.add(new THREE.AmbientLight(0xffffff, 1.0));
    const dirA = new THREE.DirectionalLight(0xffffff, 0.6);
    dirA.position.set(1, 1, 1.5);       // 右上稍偏相机,照亮屋顶+右侧
    _scene.add(dirA);
    const dirB = new THREE.DirectionalLight(0xffffff, 0.4);
    dirB.position.set(-1, -1, 1);       // 左下补光,让背光侧不死黑
    _scene.add(dirB);
    // 相机本体在 _resize() 里全量配置——构造时随便放一个标准 frustum，
    // 等 _resize() 拿到真实 W/H 再全部覆盖。
    _camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 4000);
    _plotLayer = new THREE.Group();
    _scene.add(_plotLayer);
    _gltfLoader = new THREE.GLTFLoader();
    // 插到 #map 内部第一个孩子之前,使 cell DOM 位于 canvas 之后但
    // 因 canvas 用 absolute+z-index:5,实际在 cell 上方（视觉）。
    // ⚠️ #map 必须 position:relative（已在 tiles.css 里加）。
    _mapEl.appendChild(_canvas);
    // ResizeObserver 监听 #map 尺寸变化（窗口拉拽、侧栏开合）
    if (typeof ResizeObserver !== 'undefined'){
      new ResizeObserver(() => _resize()).observe(_mapEl);
    } else {
      window.addEventListener('resize', _resize);
    }
    _resize();
  }

  /* 每次 resize 全量重设相机：frustum 中心 W/2,H/2，position/lookAt
     都指向同一中心——这样 cell 世界坐标 (x, y, 0) 投影后正好落在
     屏幕像素 (x, H - y)，cell (cx, cy) 对应世界 (cx, H - cy, 0)。
     不需要在摆放模型时再加任何偏移。 */
  function _resize(){
    if (!_renderer || !_mapEl) return;
    const w = _mapEl.clientWidth, h = _mapEl.clientHeight;
    if (w <= 0 || h <= 0) return;
    _renderer.setSize(w, h, /*updateStyle*/true);
    _camera.left   = -w / 2;
    _camera.right  =  w / 2;
    _camera.top    =  h / 2;
    _camera.bottom = -h / 2;
    _camera.near   = 0.1;
    _camera.far    = 4000;
    _camera.position.set(w / 2, h / 2, 1500);
    _camera.up.set(0, 1, 0);
    _camera.lookAt(w / 2, h / 2, 0);
    _camera.updateProjectionMatrix();
    _render();
  }

  function _render(){
    if (_renderer && _scene && _camera){
      _renderer.render(_scene, _camera);
    }
  }

  /* 异步加载一份 glb（带缓存 + 超时防悬挂）。
     ⚠️ 2026-10-17 追加情报（子代理 83329531 侦察）：3/4 glb 请求虽然
        HTTP 200 全字节下来，netstate 是 ERR_ABORTED → GLTFLoader 的
        Promise 可能悬挂（既不 onLoad 也不 onError），缓存了一份永远
        pending 的 Promise 会让 await Promise.all(tasks) 永远不返回,
        _render() 永远不被调用 → 屏幕零 3D。
        解法：包一层 setTimeout 强制 reject（15 秒,考虑 glb 大 32MB），
        胜出方先入 _gltfCache,Timeout 后到的 resolve 会被忽略（Promise
        race 语义：第一个 settle 的算数）。 */
  function _loadGlb(name){
    if (_gltfCache[name]) return _gltfCache[name];
    const url = '/static/assets/plots/' + encodeURIComponent(name) + '.glb';
    _gltfCache[name] = new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        if (!_failedUrls[url]){
          console.warn('[map3d] glb 加载超时(15s) — 怀疑 ERR_ABORTED 悬挂', url);
          _failedUrls[url] = true;
        }
        reject(new Error('glb load timeout: ' + url));
      }, 15000);
      _gltfLoader.load(url,
        (gltf) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(gltf);
        },
        undefined,
        (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (!_failedUrls[url]){
            console.warn('[map3d] glb 加载失败', url, err && err.message ? err.message : err);
            _failedUrls[url] = true;
          }
          reject(err);
        });
    });
    return _gltfCache[name];
  }

  /* 把已加载好的 gltf 克隆一份实例,摆到 cell (cx, cy_css) 中心,
     让 glb 水平跨距（x/z 较大者）恰好 = cellPxW * 0.9。
     asGray=true 时把所有 mesh 换成共享的 Basic 灰色材质（constructing）。

     几何细节：
       1. 先在 identity 变换下 Box3.setFromObject(inst) 量原始 bbox,
          取 max(size.x, size.z) 作水平跨距 → scale。
       2. 用 bbox 的几何中心（getCenter）而不是 glb 原点对齐 cell 中心——
          glb 作者建模时如果把地基画在 0..2 而不是 -1..1,用原点对齐会
          让整栋楼偏出 cell。
       3. 内嵌一个 Group：translate(−c) 放里层（先把 glb 中心掰到自己
          原点），rotate/scale/translate 放外层。three.js 的
          Object3D 矩阵是 M = T·R·S,外层"先 scale 再 rotate 再
          translate",内层已经被减过中心,正好是想要的效果。 */
  function _placeGlb(gltf, centerX, centerYcss, cellPxW, asGray){
    const inst = gltf.scene.clone(true);
    if (asGray){
      const gray = new THREE.MeshBasicMaterial({ color: 0x888888 });
      inst.traverse(o => { if (o.isMesh) o.material = gray; });
    } else {
      /* ★ 2026-10-18 实锤修复：本仓库 plots/*.glb 的 MeshStandardMaterial
         baseColorFactor 写成 (0,0,0,1) —— mat.color 是纯黑。
         PBR 光照下 color × light = 输出 → 任何强度的光打上去都是 (0,0,0)。
         为什么"3D 模型加载成功、零 console 错误"屏幕却黑乎乎一片,就是这里。
         修复策略：traverse 一次,凡 color 几乎全黑（RGB 和 < 0.05）且无 base
         color 贴图 (m.map == null) 的 Standard/Lambert/Phong 材质,把 color
         强制改写为浅灰 (0.85)。这样既保留金属度/粗糙度质感,又能在三盏
         灯下输出非零亮度；有贴图的材质不动,贴图本身的颜色会乘上来。 */
      const seen = new Set();
      inst.traverse(o => {
        if (!o.isMesh) return;
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        mats.forEach(m => {
          if (!m || seen.has(m)) return;
          seen.add(m);
          const c = m.color;
          if (!c) return;
          const sum = c.r + c.g + c.b;
          if (sum < 0.05 && !m.map){
            // 近纯黑 & 无贴图 → 提亮
            c.setRGB(0.85, 0.85, 0.85);
            m.needsUpdate = true;
          }
        });
      });
    }
    /* identity 下量 bbox & 中心 */
    const bbox = new THREE.Box3().setFromObject(inst);
    const size = new THREE.Vector3();
    bbox.getSize(size);
    const center = new THREE.Vector3();
    bbox.getCenter(center);
    /* 水平跨距（顶视下屏幕 x 对应 glb x,屏幕 y 对应 glb z）；
       防御 ||1：万一 bbox 算出 0（空 glb）就当 1,避免 scale=∞ */
    const horizSpan = Math.max(size.x, size.z) || 1;
    const scale = (cellPxW * 0.9) / horizSpan;

    /* 内层把 glb 中心掰到原点 */
    inst.position.set(-center.x, -center.y, -center.z);

    /* 外层：先 scale、再 rotate（绕 +x -90°,glb +y → +z 朝相机）、
       最后 translate 到世界 (centerX, H - cy_css, 0)。 */
    const wrapper = new THREE.Group();
    wrapper.add(inst);
    wrapper.rotation.x = -Math.PI / 2;
    wrapper.scale.setScalar(scale);
    const mapH = _mapEl.clientHeight;
    wrapper.position.set(centerX, mapH - centerYcss, 0);
    _plotLayer.add(wrapper);
  }

  /* 一次同步：清空 → 遍历 → 摆入 → 渲染 */
  async function _sync(){
    _syncScheduled = false;
    if (!_enabled) return;
    _ensureRenderer();
    if (!_enabled) return;
    if (!MAP || !_mapEl) return;

    const mapW = _mapEl.clientWidth, mapH = _mapEl.clientHeight;
    if (mapW <= 0 || mapH <= 0) return;
    // 与 frustum 对齐（renderMap 调用后 cell 已上 DOM,尺寸准确）
    _resize();

    // 清空上一帧的所有 plot 实例（gltf.scene.clone 出来的 child)
    // 不 dispose 共享 geometry/material —— glb 都在 _gltfCache 里活
    // 整局,克隆实例只用 child.geometry 引用,不 own。
    while (_plotLayer.children.length){
      _plotLayer.remove(_plotLayer.children[0]);
    }

    // 当前 #map 内每个 cell 的像素位置：用 cellDOM.querySelector 读
    const cellEls = _mapEl.querySelectorAll('.cell');
    const cellPos = {};     // plot_id(r,c 字符串) → {cx, cy, w}
    cellEls.forEach(el => {
      const id = el.dataset && el.dataset.id;
      if (!id) return;
      cellPos[id] = {
        cx: el.offsetLeft + el.offsetWidth / 2,
        cy: el.offsetTop + el.offsetHeight / 2,
        w:  el.offsetWidth,
      };
    });

    const byId = displayPlots(MAP, STATE);
    /* 2026-10-17 修复：直接用 engine 算好的 MAP.cells[pid].asset 作为 glb 键,
       不再在前端调 glbKeyForPlot（那个函数在 constructing && level=0 时
       因 lv=0 是 falsy 而永远 return null,导致 3D 不渲染）。
       建一个 pid → cell 的索引,循环里 O(1) 查。 */
    const cellById = {};
    (MAP.cells || []).forEach(c => { if (c && c.plot_id) cellById[c.plot_id] = c; });
    // 逐 plot 摆 3D。异步加载 glb 之间不互相等待（Promise.all 较浪费渲染次数,
    // 但全图只有 ~20 种 glb、都缓存,首次加载完后同步快如函数调用）。
    const tasks = [];
    for (const pid in byId){
      if (!Object.prototype.hasOwnProperty.call(byId, pid)) continue;
      const p = byId[pid];
      const pos = cellPos[pid];
      if (!pos) continue;
      const cell = cellById[pid];
      const key = cell && cell.asset ? cell.asset : null;
      if (!key) continue;             // 无 glb（asset=null）→ 2D 已有 SVG fallback
      /* constructing 灰化：优先用 p.status，但 cell.status 也已由引擎补齐
         （含 virtual_plots 影子），二者任一给 'constructing' 就算灰。
         这样和 2D 的 isConstructing 判断完全同源。 */
      const asGray = (p && p.status === 'constructing')
                  || (cell && cell.status === 'constructing');
      /* scale 不再在前端假设 glb 是 ±0.95,而是把 cell 像素宽 pos.w 传进
         _placeGlb,由它 Box3 实测后算 scale。
         屏幕 y 向下 → 世界 y 向上 的翻转也完整移到 _placeGlb 内部
         （用 _mapEl.clientHeight 在内部翻转）,这里只传原始 CSS cy。 */
      tasks.push(
        _loadGlb(key).then(gltf => {
          _placeGlb(gltf, pos.cx, pos.cy, pos.w, asGray);
          /* 每块模型摆上就立刻 render 一次,不 await 其它 glb —
             万一某 glb Promise 悬挂/失败,其它已经摆上的仍然显示。
             这样做也是"304 缓存命中后第二次 sync 依然能出模型"
             的关键：第一次 sync 摆上的模型在第二次 sync 时被清空,
             必须等新的 _placeGlb 完成后 _render,才有东西。 */
          _render();
        }).catch(() => {/* 已 warn,此处静默保持 2D cell */})
      );
    }
    /* 等待所有任务 settle（resolve 或 reject 都算）,但**不依赖**它才能
       渲染——上面每个 task 内部已各自的 _render。allSettled 只是为了让
       _sync() 返回一个可 await 的 Promise,外部调 sync() 能拿到同步
       完成的时机；不阻塞渲染本身。 */
    if (tasks.length){
      await Promise.allSettled(tasks);
    }
    /* 最终再 render 一次（保险:某些 glb 走到 catch,清空后的 scene
       至少留下一次干净的空帧）。 */
    _render();
  }

  function _requestSync(){
    if (_syncScheduled) return;
    _syncScheduled = true;
    // 让浏览器完成 layout（特别是 cell offsetWidth） 后再跑
    requestAnimationFrame(() => { _sync(); });
  }

  APP.map3d = {
    requestSync: _requestSync,
    /* 便捷：外部强制立刻同步一次（主代理调试用） */
    sync: () => { _syncScheduled = false; return _sync(); },
    /* 便捷：销毁（不在当前任务范围,但暴露出来便于将来切换视图/清场） */
    detach: () => {
      _enabled = false;
      if (_canvas && _canvas.parentNode) _canvas.parentNode.removeChild(_canvas);
      if (_renderer){ try { _renderer.dispose(); } catch(e){} }
      _canvas = null; _renderer = null; _scene = null; _camera = null;
      _mapEl = null; _plotLayer = null;
    },
  };
})(window.APP);
