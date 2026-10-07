// Etapa + valor no topo da conversa aberta (visão da equipe e leads da campanha).
// As conversas são desenhadas de novo a cada atualização, então tudo é por delegação.
(function () {
  function money(raw) {
    var clean = raw.replace(/[^\d,]/g, ""), cut = clean.indexOf(",");
    var whole = (cut === -1 ? clean : clean.slice(0, cut)).replace(/^0+(?=\d)/, "");
    var cents = cut === -1 ? null : clean.slice(cut + 1).replace(/,/g, "").slice(0, 2);
    if (!whole && cents === null) return "";
    whole = (whole || "0").replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return cents === null ? whole : whole + "," + cents;
  }
  function changed(form) {
    form.querySelector("button").hidden = false;
    form.querySelector(".mini-stage-status").textContent = "";
    var opt = form.stage_id.options[form.stage_id.selectedIndex];
    var won = opt.dataset.won === "1";
    form.querySelector(".mini-loss").hidden = opt.dataset.lost !== "1";
    form.deal_value.classList.toggle("needed", won && !form.deal_value.value.trim());
  }
  document.addEventListener("input", function (e) {
    var form = e.target.closest(".mini-stage"); if (!form) return;
    if (e.target.name === "deal_value") e.target.value = money(e.target.value);
    changed(form);
  });
  document.addEventListener("change", function (e) { var form = e.target.closest(".mini-stage"); if (form) changed(form); });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest(".mini-stage"); if (!form) return;
    e.preventDefault();
    var status = form.querySelector(".mini-stage-status"), btn = form.querySelector("button");
    if (form.stage_id.options[form.stage_id.selectedIndex].dataset.lost === "1" && !form.loss_reason.value) {
      status.textContent = "Escolha o motivo da perda"; form.loss_reason.focus(); return;
    }
    btn.disabled = true; status.textContent = "Salvando…";
    fetch("/leads/" + form.dataset.lead + "/etapa", { method: "POST", body: new FormData(form) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        status.textContent = d.ok ? "✅ Salvo" : d.error;
        if (d.ok) { btn.hidden = true; document.dispatchEvent(new CustomEvent("lead-stage-saved", { detail: { lead: form.dataset.lead, stage: form.stage_id.value, name: form.stage_id.options[form.stage_id.selectedIndex].text } })); }
      })
      .catch(function () { status.textContent = "Não salvou (sem conexão)"; })
      .then(function () { btn.disabled = false; });
  });
})();

// 💸 comprovante na conversa aberta da visão da equipe / leads da campanha: confirma sem sair da tela
document.addEventListener("submit", function (e) {
  var form = e.target.closest("form[data-receipt]");
  if (!form || !form.closest(".team-thread-head, #campHead")) return;
  e.preventDefault();
  var data = new FormData(form);
  data.append("acao", e.submitter ? e.submitter.value : "confirmar");
  data.append("ajax", "1");
  fetch(form.action, { method: "POST", body: data })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      form.innerHTML = d && d.ok
        ? (data.get("acao") === "confirmar" ? "✅ Venda confirmada" : "Ok, não era venda.")
        : "Não deu certo. Tente pelo Inbox.";
    });
});

// 💡 sugestão de etapa na visão da equipe / leads da campanha: responde sem sair da tela
document.addEventListener("submit", function (e) {
  var form = e.target.closest("form[data-suggest]");
  if (!form || !form.closest(".team-thread-head, #campHead")) return;
  e.preventDefault();
  var data = new FormData(form);
  data.append("acao", e.submitter ? e.submitter.value : "aceitar");
  data.append("ajax", "1");
  fetch(form.action, { method: "POST", body: data })
    .then(function (r) { return r.json(); })
    .then(function (d) { form.innerHTML = d.ok ? (data.get("acao") === "aceitar" ? "✅ Feito" : "Ok, sem mudança.") : ("⚠️ " + d.error); });
});
