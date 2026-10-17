/* APP.cityModal —— 全屏 3D 城市预览 modal（多角度 + 阴影 + 蓝黑背景）
   ─────────────────────────────────────────────────────────────
   与 APP.map3d 完全独立的第二份 three.js 实例：自己的 canvas /
   scene / camera / renderer，互不共享,避免互相污染。

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
   · 底盘 PlaneGeometry(20, 20) × MeshStandardMaterial(0x1a2130)，
     中心 (5, -0.01, 5)，receiveShadow = true。**不是墨绿**。
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
  let _ground = null;
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

  function _open(){
    const m = document.getElementById('city-modal');
    if (!m) return;
    m.classList.remove('hidden');
    _isOpen = true;
    _ensureRenderer();
    _renderCity();
    _startLoop();
    /* 数据飞字层：只读 STATE、零侵入 */
    if (window.APP && APP.cityTicker && APP.cityTicker.start) APP.cityTicker.start();
  }

  function _close(){
    const m = document.getElementById('city-modal');
    if (m) m.classList.add('hidden');
    _isOpen = false;
    _stopLoop();
    if (window.APP && APP.cityTicker && APP.cityTicker.stop) APP.cityTicker.stop();
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

    /* 底盘：平面（20×20）中心 (5, -0.01, 5)，蓝黑，收影 */
    const groundGeo = new THREE.PlaneGeometry(20, 20);
    const groundMat = new THREE.MeshStandardMaterial({
      color: 0x1a2130, roughness: 1.0, metalness: 0.0,
    });
    _ground = new THREE.Mesh(groundGeo, groundMat);
    _ground.rotation.x = -Math.PI / 2;
    _ground.position.set(GRID_CENTER, -0.01, GRID_CENTER);
    _ground.receiveShadow = true;
    _scene.add(_ground);

    _plotGroup = new THREE.Group();
    _scene.add(_plotGroup);
    _gltfLoader = new THREE.GLTFLoader();
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
    const placedCenters = [];  // [{x, z}] 每个 plot 中心
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
      const x = col * CELL_SPACING + CELL_SPACING / 2;
      const z = row * CELL_SPACING + CELL_SPACING / 2;
      placedCenters.push({ x, z });
      tasks.push(
        _loadGlb(key).then(gltf => {
          _placeGlb(gltf, x, z);
        }).catch(() => {/* 已 warn，静默 */})
      );
    });

    if (tasks.length){
      await Promise.allSettled(tasks);
    }

    /* 把 OrbitControls target / camera 跟到已有地块 bbox 中心。
       只有 1 块时 target 就是该地块中心；多块时取均值（≈bbox 中点）。
       无 plot 时回退 GRID_CENTER。 */
    if (placedCenters.length && _controls){
      let cx = 0, cz = 0;
      placedCenters.forEach(c => { cx += c.x; cz += c.z; });
      cx /= placedCenters.length;
      cz /= placedCenters.length;
      /* 把 camera 平移等价量，保持"控制视角不变"手感 */
      const dx = cx - _controls.target.x;
      const dz = cz - _controls.target.z;
      _controls.target.set(cx, 0, cz);
      _camera.position.x += dx;
      _camera.position.z += dz;
      _camera.lookAt(cx, 0, cz);
      _controls.update();
    }
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
  }

  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _bind);
  } else {
    _bind();
  }

  APP.cityModal = { open: _open, close: _close };
})(window.APP);
