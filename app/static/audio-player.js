// Áudios e vídeos das conversas, igual ao WhatsApp:
// - um de cada vez: dar play num pausa o que estiver tocando (nesta aba);
// - áudios seguidos da mesma pessoa tocam em sequência;
// - a atualização automática da conversa (mensagem nova, ✓✓ azul...) não interrompe o que está
//   tocando: o player em uso é reaproveitado no lugar do novo (window.swapKeepingMedia).
(function () {
  function isMedia(el) { return el && (el.tagName === "AUDIO" || el.tagName === "VIDEO"); }

  document.addEventListener("play", function (e) {
    if (!isMedia(e.target)) return;
    document.querySelectorAll("audio, video").forEach(function (m) { if (m !== e.target && !m.paused) m.pause(); });
  }, true);

  // terminou um áudio: se a próxima mensagem (da mesma pessoa) também é áudio, toca ela
  document.addEventListener("ended", function (e) {
    if (!e.target || e.target.tagName !== "AUDIO") return;
    var bubble = e.target.closest(".msg-bubble"); if (!bubble) return;
    var side = bubble.classList.contains("out") ? "out" : "in";
    var next = bubble.nextElementSibling;
    while (next && !next.classList.contains("msg-bubble")) {
      if (next.classList.contains("msg-note")) return;  // uma nota no meio quebra a sequência
      next = next.nextElementSibling;
    }
    if (!next || !next.classList.contains(side)) return;
    var audio = next.querySelector("audio");
    if (audio) { audio.currentTime = 0; audio.play().catch(function () {}); }
  }, true);

  // troca o HTML da conversa mantendo os players que estão tocando ou já começados
  window.swapKeepingMedia = function (container, html) {
    var keep = {};
    container.querySelectorAll(".msg-bubble[id] audio, .msg-bubble[id] video").forEach(function (m) {
      if (!m.paused || m.currentTime > 0) keep[m.closest(".msg-bubble").id] = m;
    });
    container.innerHTML = html;
    Object.keys(keep).forEach(function (id) {
      var bubble = document.getElementById(id);
      var fresh = bubble && bubble.querySelector(keep[id].tagName.toLowerCase());
      if (fresh) fresh.replaceWith(keep[id]);
    });
  };
})();
