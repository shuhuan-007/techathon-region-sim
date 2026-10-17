/* APP.citySky —— 3D 天上"静止"报表屏(2026-10-18 v5 拍板;v7 修 rotation.x)
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
     · rotation.x = +π/6 ≈ +30°(v7 拍板 B 修)
       v6 曾写 -π/6(向下微倾 30°),但 city-modal 相机 position (10.5,7.5,10.5)
       lookAt (5,0,5) → 视线俯角约 -35°(从上往下看);与 -30° 屏夹角 ≈ 60°,
       几乎平行,用户看到的是"边"——"斜 30 度并没有看到"。
       v7 改 +30°:屏正面朝相机那一侧往上倾 30°,两边总价 ≈ 30°,
       进入视锥,从斜下能正视屏面。
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
     _fetchPromise 去重,反复 open 不重复 fetch。refresh() 只重画纹理。
   ─────────────────────────────────────────────────────────────
   v9（2026-10-18 用户拍板,第二轮）
     用户原话:
       1.「现在的唯一问题就是出现在了地块中间。他应该出现在**地块远处**」
       2.「以及你应该保证任何情况下上面都有字。可以是一些公文什么的。
          参考**新闻的提示词**」
     落点 A · 屏往「地块远处」推:
       open() 与 refresh() 的 mesh.position.set(cx, SKY_Y, cz - 3)
       → cz - _skyDistance()。
       _skyDistance() = max(6, side + 3)，其中 side 是 STATE.plots
       占用的 bbox 的 max(行跨度, 列跨度)。相机在 +z 侧 (10.5,7.5,10.5)
       看向 -z,cz 减小 = 屏往「场景后面 / 远处」退,退出 bbox 之外。
       同时屏本身加大:SKY_W 8→10, SKY_H 4.5→5.5,
       SKY_Y = SKY_H/2 = 2.75 (下边缘仍贴地)。
     落点 B · 任何情况都有字（公文风）:
       canvas 底部 200 px 加一块"经营新闻与财报解读"公文区,
       数据源 fetch /api/narratives?game=<game>&player=p1。
       三档稳定显示:
         · ok      → 用 narratives.news.headline + .body 渲染正文
         · pending → "— 本季经营新闻与财报解读正在生成… —"
         · fail/未起 → "— 结算后此处将显示经营新闻与财报解读 —"
       fetch 由 open() 同时起;收到结果或失败都会触发 _repaint(),
       公文区立即被覆盖,不会出现"上空屏"。 */
window.APP = window.APP || {};
(function (APP) {

  /* ── v8 几何拍板（v9 微调记在下面）────────────────────────
     v8：下边缘贴地、远一点、大一点。
       下边缘贴地 → SKY_Y ≈ SKY_H/2
       大一点     → SKY_W 5→8, SKY_H 2.8→4.5, 中心 y=2.25
       远一点     → open()/refresh() 里把 cz 往 bbox 中心 -z 方向推
       相机 pos y=7.5 俯视 y=0/向 cz+ 方向 → 屏正面向相机 →
       rotation.x = +π/9（≈ +20°，屏面向下倾朝地面，用户 v10 拍板「差了 20°」的方向校正）

     v9：「地块远处」。
       屏宽 SKY_W 8→10，屏高 SKY_H 4.5→5.5，SKY_Y 随动 2.75。
       位置不再用常数 -3，而是 cz - _skyDistance()
       = cz - max(6, bbox_side + 3)。 */
  /* 屏幕物理尺寸(场景单位,1 = 1 cell)
     v10 拍板（2026-10-18 用户）：继续放大 + 倾斜方向倒过来。
     SKY_W 10→13、SKY_H 5.5→7。SKY_Y 自动 = 3.5（下边缘贴地规矩保持）。
     rotation.x 从 -π/9 → +π/9：反向 20°（v9 是上缘向你倾朝天，
     v10 是下缘向你倾朝地，"差了 20°" 的方向校正）。 */
  const SKY_W = 13.0;
  const SKY_H = 7.0;
  /* 屏幕中心高度：下边缘贴地 */
  const SKY_Y = SKY_H / 2;
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

  /* v9 落点 B · 公文区数据源 */
  let _narratives = null;     /* null = 未 fetch 或 fetch 失败 */
  let _narrState = 'idle';    /* idle / pending / ok / fail */
  let _narrPromise = null;    /* fetch 去重 */

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

  /* v9 拍板：fetch /api/narratives。与 _fetchReport 同样的去重思路。
     返回形状:{ok:true, narratives:{news:{...}|null, report:{...}|null}|null}
     ok 且 narratives 非空 → _narrState='ok';
     其它（null / 缺键 / 4xx5xx / 异常）→ _narrState='fail'，_narratives=null。
     每次终态都会 _repaint() —— 公文区的字立刻被覆盖。 */
  async function _fetchNarratives(){
    if (_narrPromise) return _narrPromise;
    _narrState = 'pending';
    const game = encodeURIComponent(_findGame());
    _narrPromise = fetch('/api/narratives?game=' + game + '&player=p1')
      .then(r => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(j => {
        if (j && j.narratives){
          _narratives = j.narratives;
          _narrState = 'ok';
        } else {
          _narratives = null;
          _narrState = 'fail';
        }
        if (_opened) _repaint();
      })
      .catch(() => {
        _narratives = null;
        _narrState = 'fail';
        if (_opened) _repaint();
      });
    return _narrPromise;
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

  /* v9 拍板 · 屏往「地块远处」推多远。
     数据:STATE.plots 的行/列占用 → side = max(行跨度, 列跨度);
     屏距 = max(6, side + 3)。
     · side + 3 保证屏退出视野 bbox 之外还留一点余量（不是贴着 bbox 边沿擦过去）;
     · 至少 6，保证哪怕只有 1 块地也不会还在「地块中间」。
     STATE 未就绪 / 解析失败 → 兜底 9（不挡视角，又远离 (5,5) 中心）。 */
  function _skyDistance(){
    if (typeof STATE === 'undefined' || !STATE) return 9;
    const plots = (STATE.plots || []);
    if (!plots.length) return 9;
    const rows = [];
    const cols = [];
    plots.forEach(p => {
      if (!p || !p.plot_id) return;
      const parts = String(p.plot_id).split(',');
      const r = parseInt(parts[0], 10);
      const c = parseInt(parts[1], 10);
      if (isNaN(r) || isNaN(c)) return;
      rows.push(r); cols.push(c);
    });
    if (!rows.length) return 9;
    const h = Math.max(...rows) - Math.min(...rows) + 1;
    const w = Math.max(...cols) - Math.min(...cols) + 1;
    const side = Math.max(h, w);
    return Math.max(6, side + 3);
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

  /* v9 落点 B · 公文风换行（按字符、按可见宽度 wrap;中文无空格也能切）*/
  function _wrapText(ctx, text, maxWidth){
    const paragraphs = String(text == null ? '' : text).split('\n');
    const out = [];
    paragraphs.forEach(p => {
      if (!p){ out.push(''); return; }
      let line = '';
      for (const ch of p){
        const trial = line + ch;
        if (ctx.measureText(trial).width > maxWidth && line){
          out.push(line);
          line = ch;
        } else {
          line = trial;
        }
      }
      if (line) out.push(line);
    });
    return out;
  }

  /* v9 拍板 · 公文区（任何情况都有内容）。
     位置:canvas 底部 180 px(顶 = H - 200,底 = H - 20);
     三档显示:
       ok      → 用 narratives.news.headline + 正文 wrap 逐行画
       pending → "— 本季经营新闻与财报解读正在生成… —"
       fail/未起 → "— 结算后此处将显示经营新闻与财报解读 —" */
  function _drawNewsRegion(ctx, W, H){
    const XR = 30;
    const XR2 = W - 30;
    const YT = H - 200;                 /* 区顶 */
    const YB = H - 20;                  /* 区底（canvas 底再留 20 px）*/

    /* 底框（比主面板略亮一点点）+ 更微的描边 */
    ctx.fillStyle = 'rgba(14, 20, 32, 0.6)';
    _roundRect(ctx, XR, YT, XR2 - XR, YB - YT, 8);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    _roundRect(ctx, XR, YT, XR2 - XR, YB - YT, 8);
    ctx.stroke();

    /* 公文标题 */
    ctx.fillStyle = 'rgba(255,255,255,0.75)';
    ctx.font = '600 15px system-ui, sans-serif';
    ctx.fillText('本季经营新闻与财报解读', XR + 14, YT + 22);

    /* 数据源三档 */
    let body;
    const n = _narratives;
    if (n && n.news && n.news.headline){
      const head = String(n.news.headline);
      const text = (n.news.body != null) ? String(n.news.body) : '';
      body = head + '\n\n' + text;
    } else if (n && n.news && typeof n.news === 'object' && n.news.headline == null && n.news.text){
      /* 防守：narratives.news 形状可能是 {text:...}（如 report 那一路）。
         新闻按理永远有 headline，但这里对"怪形状"也兜住。 */
      body = String(n.news.text);
    } else if (n && n.news && n.news.error){
      /* AI 生成器返回的 error 包装（§11.4 诚实未生成）→ 显示 error detail */
      body = '— ' + (n.news.detail || n.news.error || '生成失败') + ' —';
    } else if (_narrState === 'pending'){
      body = '— 本季经营新闻与财报解读正在生成… —';
    } else {
      body = '— 结算后此处将显示经营新闻与财报解读 —';
    }

    /* 多行 wrap on the fly；最多 8 行 */
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    ctx.font = '13px ui-monospace, monospace';
    const lines = _wrapText(ctx, body, XR2 - XR - 28);
    const maxLines = 8;
    let y = YT + 42;
    lines.slice(0, maxLines).forEach(ln => {
      ctx.fillText(ln, XR + 14, y);
      y += 18;
    });
    if (lines.length > maxLines){
      ctx.fillStyle = 'rgba(255,255,255,0.4)';
      ctx.fillText('…（' + (lines.length - maxLines) + ' 行省略）', XR + 14, y);
    }
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

    /* v9 拍板 · 公文区：无论上面走到哪个分支都执行，
       因此"任何情况屏上都有字"。 */
    _drawNewsRegion(ctx, W, H);
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
    _paint();   /* 先画一版 STATE fallback / 占位（含公文区 pending/缺档）*/

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
    /* v9 拍板：屏往「地块远处」推。
       相机在 +z 侧 (10.5,7.5,10.5) 看向 -z → cz 减小 = 屏退到场景后面。
       _skyDistance() = max(6, side + 3)，退出 bbox 之外。
       y 由 SKY_Y = SKY_H/2 = 2.75 决定（下边缘贴地）。 */
    mesh.position.set(cx, SKY_Y, cz - _skyDistance());
    /* rotation.x +π/9 ≈ +20°：屏面向下倾朝地面一侧（v10 拍板「差了 20°」方向校正）。
       v9 的 -π/9（上缘向你倾朝天）用户看到 "倾向天上"。
       +20° 反过来：下缘向你倾朝地 → 玩家看到的是"面向自己的招牌" 的场景感。 */
    mesh.rotation.x = Math.PI / 9;  /* = +π/9 ≈ +20° */
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
      _mesh.position.set(cx, SKY_Y, cz - _skyDistance());  /* v9：远处同步 */
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
    /* v9 拍板：同时起一发 fetch narratives。收到 ok/fail 都会 _repaint，
       公文区立刻被覆盖成对应档位（生成中/失败兜底文）。 */
    if (_narrPromise == null){
      _fetchNarratives();
    } else if (_narrState === 'pending' && _opened){
      /* 已在途 —— 当前 paint 里的 pending 兜底文案已经能显示，不重发 */
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
    /* 不重置 _report / _fetchPromise / _narratives / _narrPromise —— 下次打开直接复用缓存 */
  }

  function _refresh(){
    if (_opened && _mesh) _repaint();
  }

  APP.citySky = { open: _open, close: _close, refresh: _refresh, _mesh: null };
})(window.APP);
