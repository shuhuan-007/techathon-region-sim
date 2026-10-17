/* ══════════════════════════════════════════════════════════════
   main.js —— 入口。
     · state.js 已在 prior <script> 里建好 window.APP.store(本文件确认一下即可)
     · 事件绑定分散在 panel.js / map.js(保持原单文件的行为一行不改)
     · 下面这段启动代码在原 index.html 末尾(1652-1660),原样搬来:
         - 调 refreshGames() 拉存档下拉
         - 如果 URL 带 ?game=gid 则立即读档 + 调 refresh() 完成首次拉状态
   ══════════════════════════════════════════════════════════════ */

/* 确认 store 就绪(state.js 必须先于本文件加载;若不就绪说明加载顺序被破坏)。 */
if (!window.APP || !window.APP.store){
  throw new Error("[main.js] window.APP.store 未初始化 —— <script src> 顺序应为 state.js → api.js → map.js → panel.js → main.js");
}
/* 启动 */
refreshGames();
if (new URLSearchParams(location.search).get("game")){
  GAME = new URLSearchParams(location.search).get("game");
  $("in-game").value = GAME;
  $("main").classList.remove("hidden");
  aiGreet();
  refresh();
}
