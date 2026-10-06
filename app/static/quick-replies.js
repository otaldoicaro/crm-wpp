/* Respostas rápidas: num campo com data-quick-replies, digitar "/" abre a lista das
   mensagens prontas (window.QUICK_REPLIES). Continuar digitando filtra; ↑ ↓ escolhem;
   Enter/Tab/clique inserem; Esc fecha. {nome} = 1º nome do cliente (data-lead-name),
   {vendedor} = quem está logado (window.QUICK_REPLIES_ME). */
(function () {
  var replies = window.QUICK_REPLIES || [];

  function firstName(full) { return ((full || "").trim().split(/\s+/)[0] || "").replace(/[^\p{L}\p{N}'-]/gu, ""); }
  function fill(text, field) {
    return text.replace(/\{nome\}/gi, firstName(field.dataset.leadName) || "").replace(/\{vendedor\}/gi, window.QUICK_REPLIES_ME || "");
  }

  function attach(field) {
    if (field.dataset.qrAttached) return;
    field.dataset.qrAttached = "1";
    var box = document.createElement("div");
    box.className = "qr-popup";
    box.hidden = true;
    field.closest("form").appendChild(box);
    var matches = [], active = 0;

    function close() { box.hidden = true; matches = []; }
    function render(term) {
      term = (term || "").toLowerCase();
      matches = replies.filter(function (r) {
        return !term || r.atalho.indexOf(term) !== -1 || r.texto.toLowerCase().indexOf(term) !== -1;
      }).slice(0, 8);
      box.innerHTML = "";
      if (!matches.length) {
        box.innerHTML = '<div class="qr-empty">' + (replies.length ? "Nenhuma resposta com “" + term.replace(/</g, "") + "”." : "Você ainda não tem respostas rápidas.") + "</div>";
      }
      matches.forEach(function (r, i) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "qr-item" + (i === active ? " on" : "");
        var head = document.createElement("b"); head.textContent = "/" + r.atalho + (r.equipe ? "  · equipe" : "");
        var body = document.createElement("span"); body.textContent = r.texto;
        item.appendChild(head); item.appendChild(body);
        item.addEventListener("mousedown", function (e) { e.preventDefault(); choose(i); });
        box.appendChild(item);
      });
      var manage = document.createElement("a");
      manage.href = "/respostas"; manage.className = "qr-manage"; manage.textContent = "⚙ Criar / editar respostas rápidas";
      box.appendChild(manage);
      box.hidden = false;
    }
    function choose(i) {
      var r = matches[i]; if (!r) return;
      field.value = fill(r.texto, field);
      close();
      field.focus();
      field.dispatchEvent(new Event("input"));
    }
    function check() {
      var m = /^\/(\S*)$/.exec(field.value);
      if (m) { active = 0; render(m[1]); } else close();
    }
    field.addEventListener("input", function (e) { if (e.isTrusted !== false) check(); });
    field.addEventListener("keydown", function (e) {
      if (box.hidden || !matches.length) { if (e.key === "Escape") close(); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
        box.querySelectorAll(".qr-item").forEach(function (b, i) { b.classList.toggle("on", i === active); });
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault(); e.stopImmediatePropagation(); choose(active);
      } else if (e.key === "Escape") { close(); }
    });
    field.addEventListener("blur", function () { setTimeout(close, 150); });
    field.qrOpen = function () { field.value = "/"; field.focus(); check(); };
    field.qrIsOpen = function () { return !box.hidden && matches.length > 0; };
  }

  window.attachQuickReplies = function (root) {
    (root || document).querySelectorAll("[data-quick-replies]").forEach(attach);
  };
  window.attachQuickReplies();
})();
