// Volta pro mesmo ponto quando a pessoa sai e volta de uma tela (abriu uma conversa, a ficha
// de um lead, arrastou um card...). Guarda a rolagem no próprio navegador (sessionStorage):
// nada vai pro servidor, então não pesa.
//   <div data-keep-scroll="nome">          rolagem desse elemento (vertical e horizontal)
//   data-keep-scope="x" (no elemento ou no <body>)  agrupa telas que dividem a mesma posição
//   <body data-keep-window>                 também guarda a rolagem da página inteira
(function () {
  var TTL = 30 * 60 * 1000;  // depois de 30 min sem voltar, começa do topo
  var scope = document.body.dataset.keepScope || (location.pathname + location.search);
  function key(name, el) { return "ks:" + ((el && el.dataset.keepScope) || scope) + ":" + name; }
  function read(k) {
    try {
      var v = JSON.parse(sessionStorage.getItem(k) || "null");
      return v && Date.now() - v.t < TTL ? v : null;
    } catch (e) { return null; }
  }
  function write(k, top, left) {
    try { sessionStorage.setItem(k, JSON.stringify({ top: top, left: left, t: Date.now() })); } catch (e) {}
  }
  function targets() {
    var list = Array.prototype.map.call(document.querySelectorAll("[data-keep-scroll]"), function (el) {
      return { el: el, k: key(el.dataset.keepScroll, el) };
    });
    // com âncora na URL (ex: #leads) quem manda é a âncora
    if (document.body.hasAttribute("data-keep-window") && !location.hash) list.push({ el: null, k: key("window") });
    return list;
  }
  var resetting = false;  // trocou de filtro: não grava de novo ao sair
  function save() {
    if (resetting) return;
    targets().forEach(function (t) {
      if (t.el) write(t.k, t.el.scrollTop, t.el.scrollLeft);
      else write(t.k, window.scrollY, window.scrollX);
    });
  }
  function restore() {
    targets().forEach(function (t) {
      var v = read(t.k); if (!v) return;
      if (t.el) { t.el.scrollTop = v.top; t.el.scrollLeft = v.left; }
      else window.scrollTo(v.left, v.top);
    });
  }
  // grava enquanto rola (no máximo 1x por quadro) e ao sair da página
  var pending = false;
  document.addEventListener("scroll", function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; save(); });
  }, true);
  window.addEventListener("pagehide", save);
  // link/botão com data-reset-scroll (ex: trocar de filtro): a próxima tela começa do topo
  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-reset-scroll]"); if (!a || a.tagName === "FORM") return;
    resetting = true;
    targets().forEach(function (t) { try { sessionStorage.removeItem(t.k); } catch (err) {} });
  }, true);
  document.addEventListener("submit", function (e) {
    if (!e.target.closest("[data-reset-scroll]")) return;
    resetting = true;
    targets().forEach(function (t) { try { sessionStorage.removeItem(t.k); } catch (err) {} });
  }, true);
  if ("scrollRestoration" in history && !location.hash) history.scrollRestoration = "manual";
  restore();
  window.addEventListener("load", restore);  // de novo depois das fotos (a altura muda)
  window.keepScroll = { save: save, restore: restore };
})();
