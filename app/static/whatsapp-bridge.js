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
 *       tenant: "SEU_TENANT_ID"
 *       // number: "ID_DE_UM_NUMERO"  -> opcional: sem ele, cada clique vai
 *       // pro WhatsApp de um vendedor diferente (rodízio, pula número caído)
 *     };
 *   </script>
 *   <script src="https://SEU_DOMINIO_DO_CRM/static/whatsapp-bridge.js"></script>
 *
 * O script pega o gclid/gbraid/fbclid/UTMs da URL atual (ex: quem clicou num
 * anúncio e caiu aqui) e o cookie _fbc do pixel, e repassa pra nossa
 * landing-ponte, que registra a origem e redireciona pro WhatsApp já com o
 * texto pré-preenchido original. Guarda a origem por 7 dias (não perde se a
 * pessoa navegar pra outra página ou voltar outro dia), também completa o
 * link de rodízio do CRM colado direto no site (/go/...), e observa mudanças
 * no DOM (sites React/Next.js redesenham o botão depois do carregamento).
 */
(function () {
  var config = window.CRM_JUNTA_WA_BRIDGE || {};
  var BRIDGE_BASE = (config.base || "https://SEU_DOMINIO_DO_CRM").replace(/\/+$/, "");
  var TENANT_ID = config.tenant || "SEU_TENANT_ID";
  var WHATSAPP_NUMBER_ID = config.number || "";
  // links do WhatsApp e também o link de rodízio do CRM colado direto no site (/go/...)
  var LINK_SELECTOR = 'a[href*="wa.me/"], a[href*="api.whatsapp.com/send"], a[href*="whatsapp://send"], a[href*="/go/"]';

  var TRACKED_PARAMS = ["gclid", "gbraid", "wbraid", "fbclid", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
  var STORAGE_KEY = "wa_bridge_attribution";
  var KEEP_MS = 7 * 24 * 60 * 60 * 1000;  // a origem vale 7 dias (a pessoa volta outro dia e clica)

  function readCurrentParams() {
    var params = new URLSearchParams(window.location.search);
    var found = {};
    TRACKED_PARAMS.forEach(function (key) {
      var value = params.get(key);
      if (value) found[key] = value;
    });
    return found;
  }

  function cookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function loadStored() {
    var raw = null;
    try { raw = localStorage.getItem(STORAGE_KEY) || sessionStorage.getItem(STORAGE_KEY); } catch (e) { /* bloqueado */ }
    try {
      var data = JSON.parse(raw || "{}");
      if (data && data._at && Date.now() - data._at > KEEP_MS) return {};
      return data || {};
    } catch (e) {
      return {};
    }
  }

  function persist(data) {
    data._at = Date.now();
    var raw = JSON.stringify(data);
    try { localStorage.setItem(STORAGE_KEY, raw); } catch (e) { /* modo privado etc */ }
    try { sessionStorage.setItem(STORAGE_KEY, raw); } catch (e) { /* idem */ }
  }

  // chegou agora por um anúncio/link com UTM: vale essa origem (não mistura com a de uma visita antiga);
  // sem nada na URL: usa a guardada (outra página do site ou outro dia, até 7 dias)
  var current = readCurrentParams();
  var origin = Object.keys(current).length ? current : loadStored();
  if (!origin.utm_source && !origin.gclid && !origin.gbraid && !origin.wbraid && !origin.fbclid && document.referrer) {
    var ref = "";
    try { ref = new URL(document.referrer).hostname; } catch (e) { /* referrer inválido */ }
    var engine = /(^|\.)google\./.test(ref) ? "google" : /(^|\.)bing\.com$/.test(ref) ? "bing" : "";
    if (engine && ref !== window.location.hostname) origin = { utm_source: engine, utm_medium: "organic" };  // busca orgânica
  }
  if (Object.keys(current).length || origin.utm_source) persist(origin);
  var fbc = cookie("_fbc");  // pixel do Meta guarda o clique do anúncio aqui (fb.1.<hora>.<fbclid>)

  function extractPrefilledText(href) {
    try {
      var url = new URL(href, window.location.href);
      return url.searchParams.get("text") || url.searchParams.get("texto") || "";
    } catch (e) {
      return "";
    }
  }

  function bridged(href) {
    return href.indexOf(BRIDGE_BASE + "/go/") === 0 && href.indexOf("_b=1") !== -1;
  }

  function rewriteLinks() {
    var links = document.querySelectorAll(LINK_SELECTOR);
    links.forEach(function (link) {
      var href = link.getAttribute("href") || "";
      // confere pelo próprio link (sites em React redesenham o botão e voltam o href original)
      if (!href || bridged(href)) return;
      var isGo = href.indexOf(BRIDGE_BASE + "/go/") === 0 || href.indexOf("/go/" + TENANT_ID) !== -1;
      if (href.indexOf("/go/") !== -1 && !isGo) return;  // outro "/go/" que não é do CRM

      var url = isGo ? new URL(href, window.location.href)
        : new URL(BRIDGE_BASE + "/go/" + TENANT_ID + (WHATSAPP_NUMBER_ID ? "/" + WHATSAPP_NUMBER_ID : ""));
      Object.keys(origin).forEach(function (key) {
        if (key.charAt(0) !== "_" && !url.searchParams.get(key)) url.searchParams.set(key, origin[key]);
      });
      if (fbc && !url.searchParams.get("fbc")) url.searchParams.set("fbc", fbc);
      if (!isGo) {
        var text = extractPrefilledText(href);
        if (text) url.searchParams.set("texto", text);
      }
      url.searchParams.set("_b", "1");

      link.setAttribute("href", url.toString());
    });
  }

  rewriteLinks();

  // botões renderizados depois do load inicial (comum em SPA/React) também
  // precisam ser pegos — observa mudanças no DOM e reaplica
  if (window.MutationObserver) {
    var observer = new MutationObserver(rewriteLinks);
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["href"] });
  }
})();
