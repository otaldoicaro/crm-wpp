// ↕ Toda tabela com cabeçalho: clicar no título de uma coluna ordena (números do maior pro menor;
// clicar de novo inverte). Entende R$, %, "12 min", "2h 05min", "3 dias". A linha de total
// (.team-total-row ou data-pin) fica sempre no fim. Tabela com regra própria (.traffic-table) ou
// marcada .no-sort fica como está.
(function () {
  function num(text) {
    var t = (text || "").trim().toLowerCase();
    if (!t || t === "—" || t === "-") return null;
    if (t.indexOf("menos de 1 min") === 0) return 0.5;
    var d = t.match(/^(\d+)\s*dias?\b/); if (d) return +d[1] * 1440;
    var hm = t.match(/^(\d+)h(?:\s*(\d+)\s*min)?/); if (hm) return +hm[1] * 60 + (+hm[2] || 0);
    var m = t.match(/^(\d+)\s*min\b/); if (m) return +m[1];
    var n = t.replace(/r\$\s*/, "").match(/^-?[\d.]*\d(?:,\d+)?/);
    if (!n) return undefined;
    return parseFloat(n[0].replace(/\./g, "").replace(",", "."));
  }
  function valueOf(cell) {
    if (!cell) return null;
    if (cell.dataset.v !== undefined && cell.dataset.v !== "") {
      var v = parseFloat(cell.dataset.v);
      return isNaN(v) ? cell.dataset.v.toLowerCase() : v;
    }
    var text = cell.textContent.replace(/\s+/g, " ").trim();
    var n = num(text);
    return n === undefined ? text.toLowerCase() : n;
  }
  function setup(table) {
    if (table.classList.contains("traffic-table") || table.classList.contains("no-sort") || table.dataset.sortReady) return;
    var head = table.tHead, body = table.tBodies[0];
    if (!head || !body || head.rows.length !== 1 || body.rows.length < 2) return;
    var ths = head.rows[0].cells;
    for (var i = 0; i < body.rows.length; i++) {  // linha com colspan/rowspan (agrupada): não mexe
      var r = body.rows[i];
      if (r.cells.length !== ths.length) { if (!r.matches(".team-total-row, [data-pin]")) return; }
      for (var c = 0; c < r.cells.length; c++) if (r.cells[c].colSpan > 1 || r.cells[c].rowSpan > 1) return;
    }
    table.dataset.sortReady = "1";
    Array.prototype.forEach.call(ths, function (th, col) {
      if (th.colSpan > 1 || th.hasAttribute("data-nosort")) return;
      th.classList.add("js-sortable");
      th.title = (th.title ? th.title + " — " : "") + "clique pra ordenar (de novo: inverte)";
      th.addEventListener("click", function () {
        var was = th.classList.contains("sorted-desc") ? "desc" : (th.classList.contains("sorted-asc") ? "asc" : "");
        Array.prototype.forEach.call(ths, function (h) { h.classList.remove("sorted-desc", "sorted-asc"); });
        var rows = Array.prototype.slice.call(body.rows);
        var pinned = rows.filter(function (r) { return r.matches(".team-total-row, [data-pin]"); });
        rows = rows.filter(function (r) { return pinned.indexOf(r) < 0; });
        var vals = rows.map(function (r) { return valueOf(r.cells[col]); });
        var numeric = vals.some(function (v) { return typeof v === "number"; });
        var desc = was ? was === "asc" : numeric;  // 1º clique: número do maior pro menor, texto de A a Z
        th.classList.add(desc ? "sorted-desc" : "sorted-asc");
        var order = rows.map(function (r, i) { return i; });
        order.sort(function (a, b) {
          var x = vals[a], y = vals[b];
          if (x === null && y === null) return a - b;
          if (x === null) return 1;  // vazio sempre no fim
          if (y === null) return -1;
          var res = (typeof x === "number" && typeof y === "number") ? x - y : String(x).localeCompare(String(y), "pt");
          return (desc ? -res : res) || a - b;
        });
        order.forEach(function (i) { body.appendChild(rows[i]); });
        pinned.forEach(function (r) { body.appendChild(r); });
      });
    });
  }
  function run() { document.querySelectorAll("table").forEach(setup); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
  window.tableSortSetup = run;  // telas que montam tabela depois (ajax) podem chamar de novo
})();
