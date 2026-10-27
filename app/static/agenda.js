// 📅 janela "Agendar visita": botões [data-open="<id do dialog>"] abrem, [data-close] fecha
document.addEventListener("click", function (e) {
  var open = e.target.closest("[data-open]");
  if (open) { var d = document.getElementById(open.dataset.open); if (d && d.showModal) { e.preventDefault(); d.showModal(); } return; }
  var close = e.target.closest("dialog.agenda-dialog [data-close]");
  if (close) close.closest("dialog").close();
});
