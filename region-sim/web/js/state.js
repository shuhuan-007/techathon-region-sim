/* 极简 pub/sub store：零依赖、零魔法 */
window.APP = window.APP || {};
(function (APP) {
  const listeners = {};
  APP.store = {
    data: {},   // game / player / map / state / buildOptions / chat 等
    on(key, fn) { (listeners[key] = listeners[key] || []).push(fn); },
    emit(key) { (listeners[key] || []).forEach(f => f(APP.store.data[key])); },
    set(key, val) { APP.store.data[key] = val; APP.store.emit(key); },
    get(key) { return APP.store.data[key]; },
  };
})(window.APP);