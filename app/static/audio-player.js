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

  // ---------- player próprio (.ap): ▶/⏸, barra, tempo e velocidade ----------
  // A velocidade escolhida (1x, 1.5x, 2x) fica lembrada neste navegador e vale pra todos os áudios.
  var RATES = [1, 1.5, 2];
  function savedRate() { try { var r = parseFloat(localStorage.getItem("ap-rate")); return RATES.indexOf(r) >= 0 ? r : 1; } catch (e) { return 1; } }
  function saveRate(r) { try { localStorage.setItem("ap-rate", String(r)); } catch (e) {} }
  function fmt(s) { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }
  function total(audio) {
    var d = audio.duration;
    if (isFinite(d) && d > 0) return d;
    var box = audio.closest(".ap");
    return box ? parseFloat(box.dataset.sec) || 0 : 0;
  }
  function render(audio) {
    var box = audio && audio.closest(".ap"); if (!box) return;
    var dur = total(audio), cur = audio.currentTime || 0, started = cur > 0 || !audio.paused;
    var pct = dur ? Math.min(100, cur / dur * 100) : 0;
    box.querySelector(".ap-fill").style.width = pct + "%";
    var seek = box.querySelector(".ap-seek"); if (document.activeElement !== seek) seek.value = Math.round(pct * 10);
    box.querySelector(".ap-time").textContent = started ? fmt(cur) + (dur ? " / " + fmt(dur) : "") : (dur ? fmt(dur) : "0:00");
    var play = box.querySelector(".ap-play");
    play.textContent = audio.paused ? "▶" : "⏸";
    play.setAttribute("aria-label", audio.paused ? "Tocar áudio" : "Pausar áudio");
    var rate = audio.playbackRate || 1, speed = box.querySelector(".ap-speed");
    speed.textContent = (rate === 1.5 ? "1.5" : rate) + "x";
    speed.classList.toggle("fast", rate !== 1);
  }
  function setRate(a, r) { a.defaultPlaybackRate = r; a.playbackRate = r; }  // default: vale também depois de recarregar o áudio
  window.apRefresh = function (container) {
    (container || document).querySelectorAll(".ap audio").forEach(function (a) { setRate(a, savedRate()); render(a); });
  };
  ["timeupdate", "play", "pause", "ended", "loadedmetadata", "durationchange", "ratechange", "seeked"].forEach(function (type) {
    document.addEventListener(type, function (e) { if (e.target && e.target.tagName === "AUDIO") render(e.target); }, true);
  });
  document.addEventListener("play", function (e) {
    if (e.target && e.target.tagName === "AUDIO" && e.target.closest(".ap")) setRate(e.target, savedRate());
  }, true);
  // terminou: volta a barra pro começo, como no WhatsApp
  document.addEventListener("ended", function (e) {
    var a = e.target;
    if (a && a.tagName === "AUDIO" && a.closest(".ap")) { a.currentTime = 0; render(a); }
  }, true);
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".ap-play, .ap-speed"); if (!btn) return;
    var audio = btn.closest(".ap").querySelector("audio");
    if (btn.classList.contains("ap-play")) {
      if (audio.paused) audio.play().catch(function () {}); else audio.pause();
    } else {
      var next = RATES[(RATES.indexOf(audio.playbackRate) + 1) % RATES.length] || 1;
      saveRate(next);
      document.querySelectorAll(".ap audio").forEach(function (a) { setRate(a, next); render(a); });
    }
  });
  document.addEventListener("input", function (e) {
    if (!e.target.classList || !e.target.classList.contains("ap-seek")) return;
    var audio = e.target.closest(".ap").querySelector("audio"), dur = total(audio);
    if (!dur) return;
    audio.currentTime = dur * e.target.value / 1000;
    render(audio);
  });
  document.addEventListener("DOMContentLoaded", function () { window.apRefresh(); });

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
    window.apRefresh(container);
  };
})();
