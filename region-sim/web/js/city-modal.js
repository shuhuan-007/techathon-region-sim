/* APP.cityModal —— 全屏 3D 城市预览 modal（多角度 + 阴影 + 蓝黑背景）
   ─────────────────────────────────────────────────────────────
   与 APP.map3d 完全独立的第二份 three.js 实例：自己的 canvas /
   scene / camera / renderer，互不共享,避免互相污染。

   v7（2026-10-18，拍板 A）稀疏行列压缩
   ─────────────────────────────────────────────────────────────
   用户原话："如果三块分得很开，就把中间空了很多格的剪掉。不能完全
   打乱排序（主排序不变）。例如 PIZZA 中分成了三大块，那么就是要让
   这三大块间的空隙少一点，但是这三大块本身的排序不变。"
   落点：
   · _compactAxisCoords(usedIdxList) → {map, size}
     扫 sorted 去重 used 列表，相邻 gap>1 视为"分段"，段间在 newIdx
     中留 1 空缝（让视觉上"这是被压缩的地方"），段内连续。原始顺序
     保留（主排序不变，只剪中间空格）。
   · _renderCity 每次重算 rowMap/colMap（不缓存，防竞态）；用
     新 (newX,newZ) 摆模型、用新 (newRow,newCol) 算 bbox 与视野。
   · 荒地：原始地图上"跳过行/列（空缝）"整行/列留白；原始
     "存在该行/列但该 (row,col) 没人占"才堆 0x4a4036 荒地。LOD
     side ≤ 6 按**压缩后 side** 评估。
   · 点击/数据保护：plot 的 plot_id 与 STATE.plots/MAP.cells 映射
     完全不动；压缩只是显示层。
   · city-modal-sky.js: rotation.x 从 -30° 改 +30°（屏正面朝相机
     那一侧往上倾 30°，进入视锥）。

   v2 改造（2026-10-17，用户原话）
   ─────────────────────────────────────────────────────────────
   用户：「预览一定要做出来，能从多角度预览，背景不能是墨绿色，
   每一个地块应该有 3*3=9 个原本的建模……做鼠标旋转/缩放/平移。」
   落点：
   · OrbitControls（vendor 手工转 UMD，挂在 THREE.OrbitControls）
     - enableDamping = true，dampingFactor = 0.06
     - minDistance = 4，maxDistance = 40
     - maxPolarAngle = π / 2.1（不让用户钻到地下）
   · 相机：透视 FOV 45、near 0.1 far 200，position (10, 11, 10)、
     lookAt (5, 0, 5)。
   · cell_spacing = 1.0 → plot (col, row) 摆到 (col+0.5, 0, row+0.5)。
     Box3 实测水平跨距后 scale 到 (0.95 * cell_spacing) / horizSpan，
     让 glb 几乎贴满 1×1 网格。
   · glb 本身已经是 3×3 烘焙阵（拍板 v7 §3），摆 1 次就是 9 个建模，
     不在这里 clone 9 次。
   · 背景 = var(--bg) ≈ #12161c → renderer.setClearColor(0x12161c)。
   · 底盘已删（v6 拍板）：整个空间一个颜色,无底面漂浮平面。
      （原 v2 底盘 PlaneGeometry(20,20) 蓝黑 0x1a2130 段已整段删除。）
   · 灯光三件套：Ambient 0.55 + Directional 0.85 + castShadow （位置
     (20, 30, 15), shadow.camera ±15, mapSize 2048²) + Hemisphere 0.3。
   · 阴影：renderer.shadowMap.enabled = true, type PCFSoftShadowMap，
     每个模型 traverse 上 cast/receiveShadow=true。
   · Esc / ✕ / 点遮罩关闭：打开时启动 requestAnimationFrame 循环
     （让 OrbitControls damping 生效、模型在动画中持续渲染），关闭时
     停止循环（_rafId）。
   · constructing 2026-10-18 拍板 v3：用专用 `在建` 模型（在建.glb），
     不再用灰化 variant。running/constructing 都走彩色原色（pbr 资产真色）。
   ───────────────────────────────────────────────────────────── */
window.APP = window.APP || {};
(function (APP) {
  /* 内部状态 */
  let _canvas = null;
  let _renderer = null;
  let _scene = null;
  let _camera = null;          // THREE.PerspectiveCamera
  let _controls = null;        // THREE.OrbitControls
  let _plotGroup = null;
  let _wasteGroup = null;      // 简陋荒地（一块薄片 box，不占 gltf）
  let _roadGroup = null;       /* v15 拍板：平面道路（地块 mesh 间隙下） */
  let _labelGroup = null;      /* ★ 2026-10-18 地块名悬浮牌（canvas→texture→plane 躺平） */
  let _labelVisible = true;    /* 开关：默认开；checkbox 可关 */
  let _enabled = true;
  let _failedUrls = {};
  let _rafId = 0;              // animation frame id
  let _isOpen = false;

  const _gltfCache = {};       // name → Promise<GLTF>（独立缓存）
  let _gltfLoader = null;

  /* canvas 固定尺寸（1200×750 / 本次约定） */
  const CANVAS_W = 1200, CANVAS_H = 750;
  const CELL_SPACING = 1.0;
  const GRID_CENTER = 5.0;

  /* 已拍板（2026-10-18 v4）：视野 = 已有地块 bbox 扩到正方形并加 padding。
     埋 DISPLAY bbox = [minRow..maxRow] × [minCol..maxCol] 追加到正方形：
     side = max(w, h) + 2 * PAD_CELLS （PAD_CELLS=1）；
     side 至少 = 3（太小形态沒；最多 = 10（全图）。
     输入：cells = [{row, col}, ...]。无 cell 返回 null。 */
  function _cityViewBBox(cells){
    if (!cells || !cells.length) return null;
    let minR = Infinity, maxR = -Infinity, minC = Infinity, maxC = -Infinity;
    cells.forEach(({ row, col }) => {
      if (row < minR) minR = row;
      if (row > maxR) maxR = row;
      if (col < minC) minC = col;
      if (col > maxC) maxC = col;
    });
    const w = maxC - minC + 1, h = maxR - minR + 1;
    const PAD = 0;   /* 2026-10-18 v6 拍板：最小容纳现有地块的正方形,不加 padding */
    const side = Math.min(10, Math.max(1, Math.max(w, h) + PAD * 2));
    const cx = (minC + maxC + 1) / 2;
    const cz = (minR + maxR + 1) / 2;
    /* 方形中心不动 */
    return { cx, cz, side };
  }

  /* v7 拍板 A：稀疏行列压缩 helper。
     usedIdxList 是 sorted 去重的 index 数组（原 row 或 col）。
     返回 { map, size }：
       · map[oldIdx] = newIdx — 原始 idx → 压缩后 idx 的映射
       · size = 压缩后总长度（含段间 1 空缝）
     规则：扫 usedIdxList，prev 与当前 idx 相差 > 1 视为"分段"，
     段间在 newIdx 中多空 1 格（视觉上"这是被剪掉的空格"）；
     段内连续编号，顺序完全保留。
     空数组返回 { map:{}, size:0 }。 */
  function _compactAxisCoords(usedIdxList){
    if (!usedIdxList || !usedIdxList.length) return { map: {}, size: 0 };
    const map = {};
    let next = 0;
    let prev = null;
    usedIdxList.forEach(idx => {
      if (prev !== null && idx - prev > 1) next += 1;  /* 段间留 1 空缝 */
      map[idx] = next;
      next += 1;
      prev = idx;
    });
    return { map, size: next };
  }

  function _open(){
    const m = document.getElementById('city-modal');
    if (!m) return;
    m.classList.remove('hidden');
    _isOpen = true;
    _ensureRenderer();
    window.__cityScene = _scene;  /* 供 citySky 挂屏 */
    _renderCity();
    _startLoop();
    /* 数据飞字层：只读 STATE、零侵入 */
    if (window.APP && APP.cityTicker && APP.cityTicker.start) APP.cityTicker.start();
    /* 天上报表层 —— v14（2026-10-18）：整套代码保留，开关默认关。
       city-modal-sky.js 里设 _SKY_ENABLED=false 才不在此触发。
       恢复 = 改 city-modal-sky.js:78 那个 const。 */
    if (window.APP && APP.citySky && APP.citySky._SKY_ENABLED && APP.citySky.open) APP.citySky.open();
  }

  function _close(){
    const m = document.getElementById('city-modal');
    if (m) m.classList.add('hidden');
    _isOpen = false;
    _stopLoop();
    if (window.APP && APP.cityTicker && APP.cityTicker.stop) APP.cityTicker.stop();
    /* 屏开着的才需要 close；开关本身是 false 时调 close 也无害（_opened=false 直接 return） */
    if (window.APP && APP.citySky && APP.citySky._SKY_ENABLED && APP.citySky.close) APP.citySky.close();
    /* 不 destroy gltf cache——下次打开还能复用；不清 _plotGroup，让
       下次打开瞬间可以一次性把旧场面替换掉而不是空白闪烁。 */
  }

  function _startLoop(){
    if (_rafId) return;
    const tick = () => {
      if (!_isOpen){ _rafId = 0; return; }
      _rafId = requestAnimationFrame(tick);
      if (_controls) _controls.update();
      _renderFrame();
    };
    _rafId = requestAnimationFrame(tick);
  }
  function _stopLoop(){
    if (_rafId){
      cancelAnimationFrame(_rafId);
      _rafId = 0;
    }
  }

  function _ensureRenderer(){
    if (_renderer || !_enabled) return;
    if (typeof THREE === 'undefined' || !THREE.GLTFLoader){
      console.warn('[cityModal] THREE 或 GLTFLoader 未加载 — modal 关闭');
      _enabled = false; return;
    }
    if (typeof THREE.OrbitControls === 'undefined'){
      console.warn('[cityModal] THREE.OrbitControls 未加载 — modal 关闭');
      _enabled = false; return;
    }
    _canvas = document.getElementById('city-canvas');
    if (!_canvas){ _enabled = false; return; }
    try {
      _renderer = new THREE.WebGLRenderer({
        canvas: _canvas, antialias: true, alpha: false,
        powerPreference: 'low-power',
      });
      _renderer.setPixelRatio(1);
      _renderer.setSize(CANVAS_W, CANVAS_H, /*updateStyle*/false);
      _renderer.setClearColor(0x12161c, 1);
      _renderer.shadowMap.enabled = true;
      _renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      /* 不启用 sRGBEncoding —— glb 材质已经按 r140 默认 linear-space
         显示正确,强开反而出现"整屏高亮钢灰"的偏色。 */
    } catch(e){
      console.warn('[cityModal] WebGLRenderer 构造失败:', e);
      _enabled = false; return;
    }

    _scene = new THREE.Scene();

    /* 灯光三件套：Ambient（主环境） + Directional（主阴影） + Hemisphere（氛围） */
    _scene.add(new THREE.AmbientLight(0xffffff, 0.55));
    const dir = new THREE.DirectionalLight(0xffffff, 0.85);
    dir.position.set(20, 30, 15);
    dir.castShadow = true;
    dir.shadow.mapSize.set(2048, 2048);
    dir.shadow.camera.left = -15;
    dir.shadow.camera.right = 15;
    dir.shadow.camera.top = 15;
    dir.shadow.camera.bottom = -15;
    dir.shadow.camera.near = 1;
    dir.shadow.camera.far = 80;
    dir.shadow.bias = -0.0005;
    _scene.add(dir);
    _scene.add(new THREE.HemisphereLight(0x87ceeb, 0x5c4a3a, 0.3));

    /* 透视相机 */
    _camera = new THREE.PerspectiveCamera(45, CANVAS_W / CANVAS_H, 0.1, 200);
    /* 比拍板的 (10,11,10) 略近略低一点,让 1×1 cell 里的 3×3 子模型
       有足够像素可辨。距离≈10.6,极角≈40°（俯视但不顶视）。 */
    _camera.position.set(10.5, 7.5, 10.5);
    _camera.up.set(0, 1, 0);
    _camera.lookAt(GRID_CENTER, 0, GRID_CENTER);

    /* OrbitControls：阻尼、限制滚出地面 */
    _controls = new THREE.OrbitControls(_camera, _canvas);
    _controls.target.set(GRID_CENTER, 0, GRID_CENTER);
    _controls.enableDamping = true;
    _controls.dampingFactor = 0.06;
    /* 2026-10-18 反馈 1：minDistance 4 → 2（用户想"再多放大一点"）。
       pbr 模型单件 span≈1.87，min 2.0 时单件贴脸但仍不穿透 (near=0.1)。 */
    _controls.minDistance = 2;
    _controls.maxDistance = 40;
    _controls.maxPolarAngle = Math.PI / 2.1;
    _controls.update();

    _plotGroup = new THREE.Group();
    _scene.add(_plotGroup);
    _wasteGroup = new THREE.Group();
    _scene.add(_wasteGroup);
    _roadGroup = new THREE.Group();
    _scene.add(_roadGroup);
    _labelGroup = new THREE.Group();
    _labelGroup.visible = _labelVisible;      // checkbox 可关
    _scene.add(_labelGroup);
    _gltfLoader = new THREE.GLTFLoader();
  }

  /* ★ 2026-10-18 CanvasTexture 厂：把 plot.name 画成躺平的悬浮牌
     - canvas 256×64；名字越长字号越小（2→44px, 3→40px, 4→34px, ≥5→26px）。
     - 返回 MeshBasicMaterial + PlaneGeometry(0.6, 0.15) 的 Mesh。
     - rotation.x = -PI/2 → 平行于地面；plot 放 model 顶上方 0.22。
     - depthTest: true → 走近的建筑会自然挡住后面的牌子（用户要"悬浮"不"UI条"）。
     - 字号命名规则是"愿意密一点就小一点"，写死生效即好；不必做自适应真拟合。 */
  function _makeLabelMesh(name, plotColor){
    if (!name) return null;
    const W = 256, H = 64;
    const canvas = document.createElement('canvas');
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    /* 半透明圆角底（略暗，方便白字看清） */
    const radius = 12;
    const rx = 4, ry = 6, rw = W - 8, rh = H - 12;
    ctx.fillStyle = 'rgba(12, 16, 24, 0.62)';
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(rx, ry, rw, rh, radius);
    else ctx.rect(rx, ry, rw, rh);
    ctx.fill();

    const n = String(name).length;
    const fontSize = n <= 2 ? 44 : n === 3 ? 40 : n === 4 ? 34 : 26;
    ctx.font = `600 ${fontSize}px "PingFang SC", "Microsoft YaHei", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = plotColor || '#f0f4ff';
    ctx.fillText(name, W / 2, H / 2 + 1);

    const tex = new THREE.CanvasTexture(canvas);
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      depthTest: true,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    const geo = new THREE.PlaneGeometry(0.6, 0.15);
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = -Math.PI / 2;   // 躺平，平行于地面
    return mesh;
  }

  /* 按 plot.plot_type 给标签配色（淡色按类） */
  function _labelColorFor(plot){
    const t = plot && plot.plot_type;
    const prod = plot && plot.product;
    if (prod === '研究' || (t === 'industry' && plot.level === 2 && prod === '研究')) return '#b5e3ff';  // 研究 淡蓝
    switch (t){
      case 'agriculture': return '#c6f6c9';   // 淡绿
      case 'resource':    return '#ffd9a8';   // 淡橙
      case 'industry':    return '#ffbfbf';   // 淡红
      case 'service':     return '#e3d0ff';   // 淡紫
      default:            return '#f0f4ff';
    }
  }

  /* 异步加载一份 glb（独立缓存 + 15s timeout 防悬挂）。 */
  function _loadGlb(name){
    if (_gltfCache[name]) return _gltfCache[name];
    const url = '/static/assets/plots/' + encodeURIComponent(name) + '.glb';
    _gltfCache[name] = new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        if (!_failedUrls[url]){
          console.warn('[cityModal] glb 加载超时(15s)', url);
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
            console.warn('[cityModal] glb 加载失败', url, err && err.message ? err.message : err);
            _failedUrls[url] = true;
          }
          reject(err);
        });
    });
    return _gltfCache[name];
  }

  /* 把 gltf 实例摆到 (x, 0, z)。
     glb 必须 y 朝上（外层 wrapper 不翻 -π/2——作者建模时 +y 就是上，
     底盘也在 y=0 平面）；
     scale = (0.95 * CELL_SPACING) / horizSpan。
     2026-10-18 拍板：constructing 用专用 `在建.glb`（_renderCity 选 key），
     不再走"灰化同款"分支，asGray 参数与 MeshBasicMaterial 0x888888 已删。 */
  function _placeGlb(gltf, x, z){
    const inst = gltf.scene.clone(true);
    /* 阴影开关（全模型 cast+receive）。 */
    inst.traverse(o => {
      if (o.isMesh){
        o.castShadow = true;
        o.receiveShadow = true;
      }
    });
    /* ★ 提亮近纯黑 Standard/Lambert/Phong 材质（沿用 03ca40b 的修复策略——
       本仓库 plots/*.glb 的 MeshStandardMaterial color 多是 (0,0,0) 近纯黑，
       照三盏灯也输出近零。凡 color RGB 和 < 0.05 且无 base color 贴图
       (m.map == null)，把 color 强制改写成浅灰 0.85,既保留金属度/粗糙度
       质感,又能让灯光真正起作用；有贴图的材质不动,贴图颜色会乘上来。 */
    {
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
            c.setRGB(0.85, 0.85, 0.85);
            m.needsUpdate = true;
          }
        });
      });
    }

    /* identity 下量 bbox。本仓库 plots/*.glb 实测：单件 bbox ≈ 1.87×0.98×1.89
       （一个建筑的尺寸），即 glb 文件本身**不含** 3×3 烘焙阵。按拍板 v3
       （"glb 已烘 9 件则 add 一次否则 clone×9"）和用户原话（"每一个地块应该
       有 3*3=9 个原本的建模"），单个 glb 需要在前端**克隆 9 次**摆出 3×3。
       判定：如果 bbox horizSpan > 2.5,就认为 glb 自带 3x3,不再克隆。
       基础教育 1.87 < 2.5,所以会进克隆分支。
       克隆的 3×3 阵列为 plot 内聚簇,子 cell 间距 = cell_span/3,单个模型缩到
       plot_span * 0.95/3。 */
    const bbox = new THREE.Box3().setFromObject(inst);
    const size = new THREE.Vector3();
    bbox.getSize(size);
    const center = new THREE.Vector3();
    bbox.getCenter(center);
    const horizSpan = Math.max(size.x, size.z) || 1;
    /* 是否自带 3×3：horizSpan 显著大于典型单件 span(约2),即已烘焙多件套 */
    const IS_BAKED_3X3 = horizSpan > 2.5;

    if (IS_BAKED_3X3){
      /* glb 自带 3×3 → 单摆。0.95 缩到 plot_span。 */
      const scale = (0.95 * CELL_SPACING) / horizSpan;
      inst.position.set(-center.x, -center.y, -center.z);
      const wrapper = new THREE.Group();
      wrapper.add(inst);
      wrapper.scale.setScalar(scale);
      wrapper.position.set(x, size.y * scale / 2, z);
      _plotGroup.add(wrapper);
    } else {
      /* glb 只一件 → 克隆 9 件摆 3×3。子 cell 间距 = CELL_SPACING/3,
         每件缩到子 cell 物理宽 ×0.95 几乎贴边。同时把整体均匀放大,
         让用户能看清：cell 宽 1,子 cell 宽 1/3 ≈ 0.333,模型水平跨距
         取该宽的 0.95 ≈ 0.317。考虑模型在屏幕上的可读性,把所有 plot 都
         缩到只占 cell 的 0.9,3×3 子阵每子格 0.3,单件渲染尺寸按 0.285。 */
      const SUB = CELL_SPACING / 3;
      const SUB_TARGET = SUB * 0.95;
      const scale = SUB_TARGET / horizSpan;
      /* 子中心相对 plot 中心 (0,0) 的偏移：3×3 阵两轴各 -SUB, 0, +SUB */
      const offsets = [-SUB, 0, SUB];
      /* 共用：把 glb 中心拿到原点的"内层"逻辑独立做 */
      inst.position.set(-center.x, -center.y, -center.z);
      const innerWrapper = new THREE.Group();
      innerWrapper.add(inst);
      innerWrapper.scale.setScalar(scale);
      /* 9 件：除了第一件用 innerWrapper,其余 clone */
      for (let ii = 0; ii < 3; ii++){
        for (let jj = 0; jj < 3; jj++){
          let subRoot;
          if (ii === 1 && jj === 1){
            subRoot = innerWrapper;
          } else {
            subRoot = innerWrapper.clone(true);
          }
          subRoot.position.set(
            x + offsets[ii],
            size.y * scale / 2,
            z + offsets[jj]
          );
          _plotGroup.add(subRoot);
        }
      }
    }

  }

  /* 主渲染：清空 → 摆全部 plot → 起循环 */
  async function _renderCity(){
    if (!_enabled || !_renderer) return;
    if (typeof STATE === 'undefined' || typeof MAP === 'undefined'){
      console.warn('[cityModal] STATE/MAP 未全局可用');
      return;
    }
    if (!STATE || !MAP){ return; }
    /* 清空 */
    while (_plotGroup.children.length){
      _plotGroup.remove(_plotGroup.children[0]);
    }
    if (_wasteGroup){
      while (_wasteGroup.children.length){
        _wasteGroup.remove(_wasteGroup.children[0]);
      }
    }
    if (_roadGroup){
      while (_roadGroup.children.length){
        _roadGroup.remove(_roadGroup.children[0]);
      }
    }
    if (_labelGroup){
      while (_labelGroup.children.length){
        const m = _labelGroup.children[0];
        _labelGroup.remove(m);
        if (m.material && m.material.map) m.material.map.dispose();
        if (m.material) m.material.dispose();
        if (m.geometry) m.geometry.dispose();
      }
    }

    const cellById = {};
    (MAP.cells || []).forEach(c => { if (c && c.plot_id) cellById[c.plot_id] = c; });

    /* 副标题：第 N 季 · X 块地块 */
    const subtitleEl = document.getElementById('cm-subtitle');
    if (subtitleEl){
      const q = STATE.quarter !== undefined ? STATE.quarter : '—';
      const n = (STATE.plots || []).length;
      subtitleEl.textContent = `第 ${q} 季 · ${n} 块地块`;
    }

    const tasks = [];
    /* 2026-10-18 反馈 2：跟踪"已有地块的 bbox 中心"代替全局 GRID_CENTER=5。
       收集所有 plot 的 (row,col)，结束后把 controls.target / camera 跟过去。 */
    const placedCenters = [];  // [{x, z}] 每个 plot 中心（世界坐标）
    const placedCells   = [];  // [{row, col}] 每个 plot 的"压缩后"网格坐标（_cityViewBBox 用）

    /* ── v7 拍板 A：稀疏行列压缩（每次都重算,不缓存）────────────
       1. 从 STATE.plots ∪ MAP.cells(c.asset 非空) 收集已占 (row,col)；
       2. rowsUsed / colsUsed 排序去重;
       3. _compactAxisCoords → {map, size};段间留 1 空缝,段内连续,主排序不变;
       4. 摆模型 / bbox / 荒地 全部用 newRow/newCol（显示层）;
       5. STATE.plots 与 MAP.cells 的 plot_id / 键一毫不改（数据层）。*/
    const occupiedRowsSet = new Set();
    const occupiedColsSet = new Set();
    (STATE.plots || []).forEach(p => {
      if (!p || !p.plot_id) return;
      const parts = String(p.plot_id).split(',');
      const r = parseInt(parts[0], 10);
      const c = parseInt(parts[1], 10);
      if (isNaN(r) || isNaN(c)) return;
      occupiedRowsSet.add(r);
      occupiedColsSet.add(c);
    });
    (MAP.cells || []).forEach(c => {
      if (!c || !c.plot_id || !c.asset) return;
      const parts = String(c.plot_id).split(',');
      const r = parseInt(parts[0], 10);
      const cc = parseInt(parts[1], 10);
      if (isNaN(r) || isNaN(cc)) return;
      occupiedRowsSet.add(r);
      occupiedColsSet.add(cc);
    });
    const rowsUsedList = Array.from(occupiedRowsSet).sort((a,b)=>a-b);
    const colsUsedList = Array.from(occupiedColsSet).sort((a,b)=>a-b);
    const rc = _compactAxisCoords(rowsUsedList);   /* row 方向压缩 */
    const cc = _compactAxisCoords(colsUsedList);   /* col 方向压缩 */
    const rowMap = rc.map, rowSize = rc.size;
    const colMap = cc.map, colSize = cc.size;
    /* 反向:压缩后 newIdx → 原 idx。"段间空缝"指 newIdx 不在 reverse 表里。 */
    const rowReverse = {};   /* newR → oldR(存在),不存在即空缝 */
    const colReverse = {};   /* newC → oldC(存在),不存在即空缝 */
    rowsUsedList.forEach(oldR => { rowReverse[rowMap[oldR]] = oldR; });
    colsUsedList.forEach(oldC => { colReverse[colMap[oldC]] = oldC; });

    (STATE.plots || []).forEach(p => {
      const pid = p.plot_id;
      const cell = cellById[pid];
      if (!cell || !cell.asset) return;
      /* 2026-10-18 拍板：constructing 改用专用 `在建` 模型（在建.glb），
         不再用灰化同款；running 走原 cell.asset。 */
      const isConstructing = (p.status === 'constructing') || (cell.status === 'constructing');
      const key = isConstructing ? '在建' : cell.asset;
      const parts = pid.split(',');
      const row = parseInt(parts[0], 10);
      const col = parseInt(parts[1], 10);
      if (isNaN(row) || isNaN(col)) return;
      /* v7 显示层:走 rowMap / colMap 拿压缩后坐标。原 plot_id 不动。 */
      const newR = rowMap[row];
      const newC = colMap[col];
      if (newR === undefined || newC === undefined) return;
      const x = newC * CELL_SPACING + CELL_SPACING / 2;
      const z = newR * CELL_SPACING + CELL_SPACING / 2;
      placedCenters.push({ x, z });
      /* bbox 用压缩后坐标 → 视野方形才会"压缩",贴合 v7 拍板 */
      placedCells.push({ row: newR, col: newC });
      tasks.push(
        _loadGlb(key).then(gltf => {
          _placeGlb(gltf, x, z);
        }).catch(() => {/* 已 warn，静默 */})
      );
      /* ★ 2026-10-18 悬浮地块名：躺平 Plane + canvas→texture；
         constructing 也亮（名字在新建时就分配，旧档读不到时留白）。 */
      if (_labelGroup && p.name){
        const labelMesh = _makeLabelMesh(p.name, _labelColorFor(p));
        if (labelMesh){
          labelMesh.position.set(x, 0.55, z);
          _labelGroup.add(labelMesh);
        }
      }
    });

    if (tasks.length){
      await Promise.allSettled(tasks);
    }

    /* ── 视野方形 + 荒地填充（2026-10-18 v6 拍板;v7 跟进压缩后坐标）───
       1. 用 _cityViewBBox 拿压缩后 {cx, cz, side}；
       2. OrbitControls target 跟到 (cx, 0, cz)，camera 平行平移；
       3. side ≤ 6（压缩后评估）时,对 bbox 方形内:
            · newR/newC 落在"段间空缝"（reverse 表里不存在）→ 整行/列留白,不堆；
            · newR/newC 对应原始 (row,col) 但该格既不在 STATE.plots 也无 asset
              → 堆简陋荒地（0x4a4036,0.95×0.04×0.95 薄 Box）。
       底盘已删（v6 拍板），不再对底面做缩放/平移。 */
    const view = _cityViewBBox(placedCells);
    if (view){
      const { cx, cz, side } = view;
      /* 1) controls target / camera */
      if (_controls){
        const dx = cx - _controls.target.x;
        const dz = cz - _controls.target.z;
        _controls.target.set(cx, 0, cz);
        _camera.position.x += dx;
        _camera.position.z += dz;
        _camera.lookAt(cx, 0, cz);
        _controls.update();
      }
      /* ★★ 2026-10-18 v16 拍板（推翻 v15b）★★
         ──────────────────────────────────────────────────────────
         视觉正确的"马路" = 相邻两个【已建成 plot】之间的**交界窄缝**，
         不是"段间空缝"（那个是 v7 视觉剪掉的空格，里面什么都没有）。

         每个 plot 占 1×1 世界单位，中心 (c+0.5, r+0.5)；模型本体宽
         0.95，两边各留 0.025。两个相邻 plot（水平相邻 (r,c)+(r,c+1)
         或垂直相邻 (r,c)+(r+1,c)）的"间隔"= 单位边长 1 − 0.95 = 0.05，
         中心线 = 两边格子的几何交界（中心偏移 ±0.5）。
         所以路宽 0.05 是物理准确的，不会有越出缝的部分。

         做法：
           1. 扫已建成 plot 集合（压缩后/原始坐标一一对应）；
           2. 对每个 plot 向右看一格、向下看一格，若都建成 → 在压缩后
              对应格边线上画一条窄路；
           3. 同时若 (r,c)/(r,c+1)/(r+1,c)/(r+1,c+1) 四格都建成 → 在
              它们共享的角落点画一个小圆/方块作"路口"。

         与"未建房空地"无关——空地根本不画路。 */
      if (_roadGroup){
        const asphaltMat = new THREE.MeshStandardMaterial({
          color: 0x22262e, roughness: 0.98, metalness: 0.0,
        });
        const stripeMat = new THREE.MeshBasicMaterial({
          color: 0xf5edaf,
        });
        /* 路宽严格 = 两模型之间的物理缝（不可加宽，会盖住模型边缘 0.025/边）*/
        const ROAD_W = 0.05 * 0.92;    /* 略缩 8%，避免与模型边缘"刚好相切"看起来脏 */
        const roadY = 0.004;            /* 略高于底盘 0，低于 Plot 模型底 0.02 */
        const roadH = 0.02;
        const stripeY = roadY + roadH + 0.002;
        const stripeH = 0.002;

        /* 已经建成的压缩后格子集合（用 placedCells 即可，它就是真实 plot 坐标）*/
        const placedSet = new Set();
        placedCells.forEach(({ row, col }) => placedSet.add(row + ',' + col));

        /* 辅助：在压缩后坐标 (r,c) 与 (r,c+1) 的共享竖线上画路（水平方向的路段）*/
        const drawHRoad = (r, c) => {
          const z1 = r * CELL_SPACING + CELL_SPACING / 2;
          const xMid = (c + 1) * CELL_SPACING;      /* 两格之间的 x（格子 c 的右沿 = 格子 c+1 的左沿）*/
          const road = new THREE.Mesh(
            new THREE.BoxGeometry(ROAD_W, roadH, CELL_SPACING * 0.96),
            asphaltMat
          );
          road.position.set(xMid, roadY, z1);
          road.receiveShadow = true;
          _roadGroup.add(road);
          /* 中心虚线：两段 */
          [-0.22, 0.22].forEach(dz => {
            const stripe = new THREE.Mesh(
              new THREE.BoxGeometry(0.008, stripeH, 0.10),
              stripeMat
            );
            stripe.position.set(xMid, stripeY, z1 + dz);
            _roadGroup.add(stripe);
          });
        };
        /* 在 (r,c) 与 (r+1,c) 的共享横线上画路（垂直方向的路段）*/
        const drawVRoad = (r, c) => {
          const x1 = c * CELL_SPACING + CELL_SPACING / 2;
          const zMid = (r + 1) * CELL_SPACING;
          const road = new THREE.Mesh(
            new THREE.BoxGeometry(CELL_SPACING * 0.96, roadH, ROAD_W),
            asphaltMat
          );
          road.position.set(x1, roadY, zMid);
          road.receiveShadow = true;
          _roadGroup.add(road);
          [-0.22, 0.22].forEach(dx => {
            const stripe = new THREE.Mesh(
              new THREE.BoxGeometry(0.10, stripeH, 0.008),
              stripeMat
            );
            stripe.position.set(x1 + dx, stripeY, zMid);
            _roadGroup.add(stripe);
          });
        };

        /* 扫所有已建成格子 */
        placedSet.forEach(key => {
          const [r, c] = key.split(',').map(x => parseInt(x, 10));
          if (placedSet.has(r + ',' + (c + 1))) drawHRoad(r, c);
          if (placedSet.has((r + 1) + ',' + c)) drawVRoad(r, c);
        });

        /* 十字路口：4 格都建成 → 在共享角落画小方块路口 */
        placedSet.forEach(key => {
          const [r, c] = key.split(',').map(x => parseInt(x, 10));
          const four =
            placedSet.has(r + ',' + (c + 1)) &&
            placedSet.has((r + 1) + ',' + c) &&
            placedSet.has((r + 1) + ',' + (c + 1));
          if (!four) return;
          const xCorner = (c + 1) * CELL_SPACING;
          const zCorner = (r + 1) * CELL_SPACING;
          const junction = new THREE.Mesh(
            new THREE.BoxGeometry(ROAD_W * 1.05, roadH * 0.9, ROAD_W * 1.05),
            new THREE.MeshStandardMaterial({ color: 0x1a1e26, roughness: 0.98, metalness: 0.0 })
          );
          junction.position.set(xCorner, roadY, zCorner);
          junction.receiveShadow = true;
          _roadGroup.add(junction);
        });
      }

      /* 3) 荒地：side ≤ 6 才堆（压缩后评估） */
      if (side <= 6 && _wasteGroup){
        /* 原始"被占用"字典:STATE.plots ∪（MAP.cells 里 c.asset 非空） */
        const occupied = {};
        (STATE.plots || []).forEach(p => { if (p && p.plot_id) occupied[p.plot_id] = true; });
        (MAP.cells || []).forEach(c => {
          if (c && c.plot_id && c.asset) occupied[c.plot_id] = true;
        });
        /* 压缩后方形的左下角（压缩后 coord 空间）*/
        const colStart = Math.round(cx - side / 2);
        const rowStart = Math.round(cz - side / 2);
        for (let newR = rowStart; newR < rowStart + side; newR++){
          for (let newC = colStart; newC < colStart + side; newC++){
            /* 段间空缝:newR/newC 不在 reverse 表 → 整行/列留白 */
            if (rowReverse[newR] === undefined) continue;
            if (colReverse[newC] === undefined) continue;
            const oldR = rowReverse[newR];
            const oldC = colReverse[newC];
            const id = oldR + ',' + oldC;
            if (occupied[id]) continue;
            /* 原始"空地"（地图上有这行/列,但该格没人占）→ 堆简陋荒地 */
            const w = CELL_SPACING * 0.95;
            const h = 0.04;
            const geo = new THREE.BoxGeometry(w, h, w);
            const mat = new THREE.MeshStandardMaterial({
              color: 0x4a4036, roughness: 1.0, metalness: 0.0,
            });
            const m = new THREE.Mesh(geo, mat);
            m.position.set(newC * CELL_SPACING + CELL_SPACING / 2, 0.02, newR * CELL_SPACING + CELL_SPACING / 2);
            m.receiveShadow = true;
            m.castShadow = false;
            _wasteGroup.add(m);
          }
        }
      }
    }
    /* 无 plot：底盘已删（v6 拍板,整个空间一个颜色 0x12161c）,无额外回退逻辑 */
  }

  function _renderFrame(){
    if (_renderer && _scene && _camera){
      _renderer.render(_scene, _camera);
    }
  }

  function _bind(){
    const btn = document.getElementById('btn-city-view');
    if (btn) btn.onclick = _open;
    const closeBtn = document.getElementById('btn-city-close');
    if (closeBtn) closeBtn.onclick = _close;
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') _close();
    });
    /* 点击 modal 背景也关闭（除了 canvas box） */
    const m = document.getElementById('city-modal');
    if (m){
      m.addEventListener('click', (e) => {
        if (e.target === m) _close();
      });
    }
    /* ★ 2026-10-18 悬浮名开关 */
    const lt = document.getElementById('cm-label-toggle');
    if (lt){
      lt.checked = _labelVisible;
      lt.addEventListener('change', () => {
        _labelVisible = !!lt.checked;
        if (_labelGroup) _labelGroup.visible = _labelVisible;
      });
    }
  }

  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _bind);
  } else {
    _bind();
  }

  APP.cityModal = { open: _open, close: _close };
})(window.APP);
