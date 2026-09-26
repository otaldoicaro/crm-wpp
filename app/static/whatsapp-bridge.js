/**
 * Cole este script no site/landing page do cliente (antes do </body>), e
 * marque os botões de WhatsApp com a classe "wa-bridge-btn" + atributos
 * data-tenant e data-number. Exemplo:
 *
 *   <a href="#" class="wa-bridge-btn"
 *      data-tenant="SEU_TENANT_ID"
 *      data-number="SEU_WHATSAPP_NUMBER_ID"
 *      data-text="Quero saber mais!">
 *     Fale no WhatsApp
 *   </a>
 *
 * O script pega o gclid/UTMs da URL atual da página (ex: quem clicou num
 * anúncio do Google Ads e caiu aqui) e os repassa pra nossa landing-ponte,
 * que registra a origem e redireciona pro WhatsApp já com o texto
 * pré-preenchido. Também guarda os parâmetros em sessionStorage, pra não
 * perder a origem se a pessoa navegar pra outra página do site antes de
 * clicar no botão.
 */
(function () {
  var BRIDGE_BASE = "https://SEU_DOMINIO_DO_CRM"; // troque pelo domínio onde o CRM está publicado
  var TRACKED_PARAMS = [
    "gclid",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
  ];
  var STORAGE_KEY = "wa_bridge_attribution";

  function readCurrentParams() {
    var params = new URLSearchParams(window.location.search);
    var found = {};
    TRACKED_PARAMS.forEach(function (key) {
      var value = params.get(key);
      if (value) found[key] = value;
    });
    return found;
  }

  function loadStored() {
    try {
      return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "{}");
    } catch (e) {
      return {};
    }
  }

  function persist(merged) {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
    } catch (e) {
      /* sessionStorage indisponível (modo privado etc); segue sem persistir */
    }
  }

  // valores da URL atual têm prioridade; o que não vier agora usa o que já
  // tinha sido salvo numa página anterior da mesma visita
  var merged = Object.assign({}, loadStored(), readCurrentParams());
  persist(merged);

  document.querySelectorAll(".wa-bridge-btn").forEach(function (btn) {
    var tenant = btn.getAttribute("data-tenant");
    var number = btn.getAttribute("data-number");
    if (!tenant || !number) return;

    var url = new URL(BRIDGE_BASE + "/go/" + tenant + "/" + number);
    Object.keys(merged).forEach(function (key) {
      url.searchParams.set(key, merged[key]);
    });
    var text = btn.getAttribute("data-text");
    if (text) url.searchParams.set("texto", text);

    btn.setAttribute("href", url.toString());
  });
})();
