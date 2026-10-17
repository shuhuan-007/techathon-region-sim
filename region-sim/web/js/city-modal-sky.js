/* APP.citySky —— 3D 天上"静止"报表屏(2026-10-18 v5 拍板)
   ─────────────────────────────────────────────────────────────
   用户原话:"总之他就挂在那里,不会动,就像是'静止'。"一块物理存在的
   面板,相机绕着它能看到侧面、透视变形。**不是 billboard、不是 HUD/跟随**。
   ─────────────────────────────────────────────────────────────
   技术形态
     Canvas 2D 报表 → THREE.CanvasTexture → PlaneGeometry mesh,
     挂在 city-modal 的 _scene 上空。不调 lookAt、不动 rotation。
     相机 OrbitControls 走一圈,就能看到这块屏的边/透视拉伸。
   ─────────────────────────────────────────────────────────────
   位置 / 尺寸 / 朝向(用户拍板)
     · 场景尺寸宽 5 × 高 2.8(scene 中 cell ≈ 1,这块屏大约是 5×3 格)
     · y = 8.5(远高于模型,模型高约 1)
     · xz = 视野 bbox 中心(沿用 city-modal._cityViewBBox 算法)
     · rotation.x = -π/2 × (30/90) = -π/6 ≈ 向下微倾 30°(v6 拍板"斜 30 度";
       缺省 rotation 面朝 +z 完全竖直,相机几乎只能看到一条边)
   ─────────────────────────────────────────────────────────────
   与 city-modal 的契约
     · city-modal _open() 在 _ensureRenderer() 之后写
       window.__cityScene = _scene; citySky.open() 依赖该引用拿 scene。
     · city-modal _close() 会调 APP.citySky.close();那时 __cityScene
       还引用已销毁前的 scene,这里 remove 后立即把 __cityScene 置 null。
   ─────────────────────────────────────────────────────────────
   数据源(沿用 v4)
     fetch('/api/report?game=<game>&player=p1') → report;
     失败/4xx → _report=null,绘制走 STATE fallback。
     _fetchPromise 去重,反复 open 不重复 fetch。refresh() 只重画纹理。 */
window.APP = window.APP || {};
(function (APP) {

  /* 屏幕物理尺寸(场景单位,1 = 1 cell) */
  const SKY_W = 5.0;
  const SKY_H = 2.8;
  /* 屏幕中心高度 */
  const SKY_Y = 8.5;
  /* canvas 像素尺寸(固定;微糊效果是有意的,呼应"掛在天上"的氛围) */
  const CVS_W = 1280;
  const CVS_H = 720;

  let _opened = false;
  let _report = null;         // 一次成功 fetch 后凕 save
  let _fetchPromise = null;   // 去重
  let _mesh = null;           // THREE.Mesh
  let _tex = null;            // THREE.CanvasTexture
  let _geo = null;            // THREE.PlaneGeometry
  let _mat = null;            // THREE.MeshBasicMaterial
  let _canvas = null;         // offscreen canvas
  let _ctx = null;

  function _findGame(){
    try {
      return new URLSearchParams(location.search).get('game') || 'default';
    } catch(_){ return 'default'; }
  }

  async function _fetchReport(){
    if (_fetchPromise) return _fetchPromise;
    const game = encodeURIComponent(_findGame());
    _fetchPromise = fetch('/api/report?game=' + game + '&player=p1')
      .then(r => r.ok ? r.json() : null)
      .then(j => (j && j.ok && j.report) ? j.report : null)
      .catch(() => null);
    return _fetchPromise;
  }

  /* ── bbox 中心(与 city-modal._cityViewBBox 相同算法)── */
  function _viewCenter(){
    if (typeof STATE === 'undefined' || !STATE || !Array.isArray(STATE.plots)){
      return { cx: 5, cz: 5 };  /* 地图 GRID_CENTER 兜底 */
    }
    const cells = [];
    STATE.plots.forEach(p => {
      if (!p || !p.plot_id) return;
      const parts = String(p.plot_id).split(',');
      const row = parseInt(parts[0], 10);
      const col = parseInt(parts[1], 10);
      if (isNaN(row) || isNaN(col)) return;
      cells.push({ row, col });
    });
    if (!cells.length) return { cx: 5, cz: 5 };
    let minR = Infinity, maxR = -Infinity, minC = Infinity, maxC = -Infinity;
    cells.forEach(({ row, col }) => {
      if (row < minR) minR = row;
      if (row > maxR) maxR = row;
      if (col < minC) minC = col;
      if (col > maxC) maxC = col;
    });
    const cx = (minC + maxC + 1) / 2;
    const cz = (minR + maxR + 1) / 2;
    return { cx, cz };
  }

  /* ── 数字格式化(沿用 v4)── */
  function _fmtSec(n){ return (n != null && isFinite(n)) ? (Math.round(n * 100) / 100).toFixed(2) : null; }
  function _fmtPct01(x){ return (x != null && isFinite(x)) ? Math.round(x * 100) + '%' : null; }

  /* ── Canvas 绘制辅助 ── */
  function _roundRect(ctx, x, y, w, h, r){
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h); ctx.arcTo(x, y + h, x, y + h - r, r);
    ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r);
    ctx.closePath();
  }

  /* 表格段:[[k,v],...] 缺啥跳啥;返回结束 y。 */
  function _drawSection(ctx, title, rows, x, y, w){
    rows = (rows || []).filter(r => r && r[1] != null && r[1] !== '');
    if (!rows.length) return y;
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    ctx.font = '600 14px ui-monospace, monospace';
    ctx.fillText(String(title).toUpperCase(), x, y);
    y += 20;
    ctx.font = '13px ui-monospace, monospace';
    rows.forEach(([k, v]) => {
      ctx.fillStyle = 'rgba(255,255,255,0.65)';
      ctx.fillText(String(k), x, y);
      ctx.fillStyle = 'rgba(255,255,255,0.92)';
      const s = String(v);
      const tw = ctx.measureText(s).width;
      ctx.fillText(s, x + w - tw, y);
      y += 18;
    });
    return y + 8;
  }

  /* ── 主绘制:把当前 _report(或 STATE fallback)画到 canvas 上 ── */
  function _paint(){
    if (!_ctx || !_canvas) return;
    const ctx = _ctx, W = CVS_W, H = CVS_H;
    /* 清屏 + 半透明深蓝底 */
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = 'rgba(10, 16, 24, 0.92)';
    _roundRect(ctx, 0, 0, W, H, 14);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.18)';
    ctx.lineWidth = 2;
    _roundRect(ctx, 1, 1, W - 2, H - 2, 14);
    ctx.stroke();

    const r = _report;

    /* 标题 */
    let q = null;
    if (r && r.quarter != null) q = r.quarter;
    else if (typeof STATE !== 'undefined' && STATE && STATE.quarter != null) q = STATE.quarter;
    ctx.fillStyle = 'rgba(255,255,255,0.98)';
    ctx.font = '600 28px system-ui, sans-serif';
    ctx.fillText('第 ' + (q != null ? q : '—') + ' 季 · 经营报表', 30, 48);
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(20, 62); ctx.lineTo(W - 20, 62); ctx.stroke();

    if (r){
      /* 左列 */
      let yL = 92;
      const colL = 30, colW = W / 2 - 50;
      /* 1) 现金截面 */
      yL = _drawSection(ctx, '现金截面', [
        ['期初现金',  _fmtSec(r.cash_before)],
        ['期末现金',  _fmtSec(r.cash_after)],
        ['本季增值',  _fmtSec(r.value_added)],
        ['营收',      _fmtSec(r.sales_revenue)],
      ], colL, yL, colW);

      /* 2) 队伍 */
      if (r.population || r.food){
        const pop = r.population || {};
        const food = r.food || {};
        const popTotal = pop.population != null ? pop.population : null;
        const popLvl   = (pop.population_level_avg != null) ? pop.population_level_avg
                        : (pop.avg_level != null) ? pop.avg_level : null;
        yL = _drawSection(ctx, '队伍', [
          ['人口',     popTotal != null ? Number(popTotal).toFixed(1) : null],
          ['劳均等级', popLvl   != null ? _fmtSec(popLvl) : null],
          ['幸福度',   food.happiness != null ? _fmtPct01(food.happiness) : null],
          ['饱食基准', food.baseline_food != null ? _fmtSec(food.baseline_food) : null],
        ], colL, yL, colW);
      }

      /* 3) 地块效率:最多 3 行 */
      if (Array.isArray(r.efficiency) && r.efficiency.length){
        const effRows = r.efficiency.slice(0, 3).map(e => {
          const lbl = (e && (e.plot_type || e.plot_id)) || '?';
          const eff_v = (e && e.efficiency != null) ? _fmtSec(e.efficiency) : '—';
          const prod = (e && e.product) ? (' → ' + e.product) : '';
          return [lbl, eff_v + prod];
        });
        if (r.efficiency.length > 3) effRows.push(['…', r.efficiency.length + ' 条']);
        yL = _drawSection(ctx, '地块效率', effRows, colL, yL, colW);
      }

      /* 右列 */
      let yR = 92;
      const colR = W / 2 + 20, colWR = W / 2 - 50;
      /* 4) 支出拆解 */
      yR = _drawSection(ctx, '支出拆解', [
        ['总支出',    _fmtSec(r.total_deduction)],
        ['工资',      _fmtSec(r.wages)],
        ['运营',      _fmtSec(r.operating)],
        ['建造',      _fmtSec(r.construction_cash)],
        ['采购',      _fmtSec(r.materials_cost)],
        ['退税',      _fmtSec(r.wage_tax_refund)],
        ['补贴',      _fmtSec(r.subsidy)],
      ], colR, yR, colWR);

      /* 5) 市场价 */
      if (r.prices){
        const pr = r.prices;
        const rows = ['H-A', 'H-F', 'H-G', 'H-J']
          .filter(k => pr[k] != null)
          .map(k => [k, _fmtSec(pr[k])]);
        yR = _drawSection(ctx, '市场价', rows, colR, yR, colWR);
      }

      /* 6) 交易 */
      if (r.bought != null || r.sold != null || r.bought_breakdown){
        const rows = [];
        if (r.bought != null) rows.push(['买入额', _fmtSec(r.bought)]);
        if (r.sold   != null) rows.push(['卖出额', _fmtSec(r.sold)]);
        if (r.bought_breakdown && typeof r.bought_breakdown === 'object'){
          const keys = Object.keys(r.bought_breakdown);
          if (keys.length) rows.push(['买入品类', keys.join(' · ')]);
        }
        yR = _drawSection(ctx, '交易', rows, colR, yR, colWR);
      }
    } else {
      /* fallback:STATE 公共字段 */
      if (typeof STATE !== 'undefined' && STATE){
        const rows = [];
        if (STATE.cash != null)         rows.push(['现金', _fmtSec(STATE.cash)]);
        if (STATE.population_count != null)
                                        rows.push(['人口 N', Number(STATE.population_count).toFixed(1)]);
        if (STATE.happiness != null)    rows.push(['幸福度', _fmtPct01(STATE.happiness)]);
        const plots = (STATE.plots || []);
        if (plots.length)               rows.push(['地块', plots.length + ' 块']);
        let y = _drawSection(ctx, '快照(实时 STATE)', rows, 30, 92, W - 60);
        ctx.fillStyle = 'rgba(255,255,255,0.5)';
        ctx.font = 'italic 12px ui-monospace, monospace';
        ctx.fillText('数据来自实时 STATE —— 完整报表需结算后由 /api/report 给出。', 30, y + 8);
      } else {
        ctx.fillStyle = 'rgba(255,255,255,0.5)';
        ctx.font = 'italic 14px ui-monospace, monospace';
        ctx.fillText('STATE 未就绪。', 30, 100);
      }
    }
  }

  /* 画 → 贴到 texture */
  function _repaint(){
    _paint();
    if (_tex) _tex.needsUpdate = true;
  }

  function _buildMesh(scene){
    if (typeof THREE === 'undefined') return null;
    /* offscreen canvas */
    _canvas = document.createElement('canvas');
    _canvas.width = CVS_W; _canvas.height = CVS_H;
    _ctx = _canvas.getContext('2d');
    _paint();   /* 先画一版 STATE fallback / 占位 */

    _tex = new THREE.CanvasTexture(_canvas);
    if ('colorSpace' in _tex && THREE.SRGBColorSpace){
      _tex.colorSpace = THREE.SRGBColorSpace;
    } else if ('encoding' in _tex && THREE.sRGBEncoding){
      _tex.encoding = THREE.sRGBEncoding;
    }
    _geo = new THREE.PlaneGeometry(SKY_W, SKY_H);
    _mat = new THREE.MeshBasicMaterial({
      map: _tex,
      transparent: true,
      side: THREE.DoubleSide,
      depthTest: false,         /* 永不被 3D 模型遮挡,视觉上"在那" */
      depthWrite: false,
    });
    const mesh = new THREE.Mesh(_geo, _mat);
    const { cx, cz } = _viewCenter();
    mesh.position.set(cx, SKY_Y, cz);
    /* 向下微倾 30°(用户原话"斜 30 度"),从斜下/侧面能看到正面主体 */
    mesh.rotation.x = -Math.PI / 2 * (30 / 90);  /* = -π/6 ≈ -30° */
    mesh.renderOrder = 1000;    /* depthTest=false 时压底渲染,确保最后画 */
    /* 不调 lookAt —— 静止的天上屏 */
    scene.add(mesh);
    return mesh;
  }

  function _open(){
    if (_opened) return;
    _opened = true;
    const scene = window.__cityScene;
    if (!scene){
      console.warn('[citySky] window.__cityScene 未就绪 — 取消挂屏');
      return;
    }
    if (!_mesh){
      _mesh = _buildMesh(scene);
      if (!_mesh){ _opened = false; return; }
    } else {
      /* 旧的还存在(理论上不应发生) → 复用,重定位 + 重画 */
      const { cx, cz } = _viewCenter();
      _mesh.position.set(cx, SKY_Y, cz);
      if (!_mesh.parent) scene.add(_mesh);
      _repaint();
    }
    APP.citySky._mesh = _mesh;
    /* fetch report,fallback → 完整重画 */
    if (_report == null){
      _fetchReport().then(r => {
        _report = r;
        if (_opened) _repaint();
      });
    }
  }

  function _close(){
    _opened = false;
    const scene = window.__cityScene;
    if (_mesh){
      if (scene) scene.remove(_mesh);
      else if (_mesh.parent) _mesh.parent.remove(_mesh);
      if (_geo){ _geo.dispose(); _geo = null; }
      if (_tex){ _tex.dispose(); _tex = null; }
      if (_mat){ _mat.dispose(); _mat = null; }
      _mesh = null;
    }
    if (APP.citySky) APP.citySky._mesh = null;
    _canvas = null; _ctx = null;
    /* 不重置 _report / _fetchPromise —— 下次打开直接复用缓存 */
  }

  function _refresh(){
    if (_opened && _mesh) _repaint();
  }

  APP.citySky = { open: _open, close: _close, refresh: _refresh, _mesh: null };
})(window.APP);
