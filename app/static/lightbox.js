// Foto da conversa: clique abre em tela cheia. Clique na foto = zoom (tamanho real, rola pra ver
// os detalhes); no celular dá pra fazer pinça. Esc / ✕ / clique fora fecha.
(function () {
  var box, img, open, save;
  function build() {
    box = document.createElement("div");
    box.className = "lightbox";
    box.hidden = true;
    box.innerHTML =
      '<div class="lightbox-bar"><a target="_blank" rel="noopener" data-open>Abrir em nova aba</a>' +
      '<a download data-save>Baixar</a><button type="button" data-close>✕ Fechar</button></div>' +
      '<img alt=""><div class="lightbox-hint">Clique na foto pra dar zoom</div>';
    document.body.appendChild(box);
    img = box.querySelector("img");
    open = box.querySelector("[data-open]");
    save = box.querySelector("[data-save]");
    img.addEventListener("click", function (e) {
      e.stopPropagation();
      var zoom = !box.classList.contains("zoomed");
      // ponto clicado (em % da foto) pra rolar até ele depois do zoom
      var r = img.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
      box.classList.toggle("zoomed", zoom);
      if (zoom) {
        box.scrollLeft = img.offsetWidth * fx - box.clientWidth / 2;
        box.scrollTop = img.offsetHeight * fy - box.clientHeight / 2;
      }
    });
    box.addEventListener("click", function (e) { if (!e.target.closest(".lightbox-bar a")) close(); });
  }
  function close() { box.hidden = true; box.classList.remove("zoomed"); img.removeAttribute("src"); document.body.style.overflow = ""; }
  document.addEventListener("click", function (e) {
    var thumb = e.target.closest(".msg-img");
    if (!thumb) return;
    e.preventDefault();
    if (!box) build();
    img.src = thumb.currentSrc || thumb.src;  // mesma URL da miniatura: já está no cache, abre na hora
    open.href = save.href = thumb.src;
    box.hidden = false;
    document.body.style.overflow = "hidden";
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && box && !box.hidden) close(); });
})();
