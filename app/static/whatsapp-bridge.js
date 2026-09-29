/**
 * Faz QUALQUER link de WhatsApp que já exista na página (botão flutuante de
 * plugin, componente React/Next.js, link manual, o que for) passar pela
 * nossa landing-ponte antes de abrir o WhatsApp — sem precisar editar o
 * HTML/código do site. Acha os links sozinho por padrão de URL
 * (wa.me / api.whatsapp.com), então funciona também com botões que já
 * existem hoje, plugin ou não.
 *
 * Uso mais comum: colar via Google Tag Manager (tag "HTML personalizado",
 * disparo "Todas as páginas"), sem precisar mexer no código do site:
 *
 *   <script>
 *     window.CRM_JUNTA_WA_BRIDGE = {
 *       base: "https://SEU_DOMINIO_DO_CRM",
 *       tenant: "SEU_TENANT_ID",
 *       number: "SEU_WHATSAPP_NUMBER_ID"
 *     };
 *   </script>
 *   <script src="https://SEU_DOMINIO_DO_CRM/static/whatsapp-bridge.js"></script>
 *
 * O script pega o gclid/UTMs da URL atual (ex: quem clicou num anúncio do
 * Google Ads e caiu aqui) e repassa pra nossa landing-ponte, que registra a
 * origem e redireciona pro WhatsApp já com o texto pré-preenchido original
 * preservado. Também guarda os parâmetros em sessionStorage (não perde a
 * origem se a pessoa navegar pra outra página antes de clicar) e observa
 * mudanças no DOM (o botão pode renderizar depois do carregamento inicial,
 * comum em sites React/Next.js).
 */
(function () {
  var config = window.CRM_JUNTA_WA_BRIDGE || {};
  var BRIDGE_BASE = config.base || "https://SEU_DOMINIO_DO_CRM";
  var TENANT_ID = config.tenant || "SEU_TENANT_ID";
  var WHATSAPP_NUMBER_ID = config.number || "SEU_WHATSAPP_NUMBER_ID";
  var LINK_SELECTOR = 'a[href*="wa.me/"], a[href*="api.whatsapp.com/send"]';

  var TRACKED_PARAMS = ["gclid", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
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

  function extractPrefilledText(href) {
    try {
      var url = new URL(href, window.location.href);
      return url.searchParams.get("text") || "";
    } catch (e) {
      return "";
    }
  }

  function rewriteLinks() {
    var links = document.querySelectorAll(LINK_SELECTOR);
    links.forEach(function (link) {
      if (link.dataset.crmJuntaBridged === "1") return; // não reprocessa o mesmo link

      var text = extractPrefilledText(link.href);
      var url = new URL(BRIDGE_BASE + "/go/" + TENANT_ID + "/" + WHATSAPP_NUMBER_ID);
      Object.keys(merged).forEach(function (key) {
        url.searchParams.set(key, merged[key]);
      });
      if (text) url.searchParams.set("texto", text);

      link.setAttribute("href", url.toString());
      link.dataset.crmJuntaBridged = "1";
    });
  }

  rewriteLinks();

  // botões renderizados depois do load inicial (comum em SPA/React) também
  // precisam ser pegos — observa mudanças no DOM e reaplica
  if (window.MutationObserver) {
    var observer = new MutationObserver(rewriteLinks);
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
