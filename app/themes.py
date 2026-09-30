"""Identidade visual por cliente (tenant.theme).

O style.css só usa variáveis (--y, --panel, --text...); cada tema aqui define
os valores delas, a logo e as fontes. Pra criar o tema de um cliente novo:
copie um bloco abaixo, troque as cores/logo e depois mude o `theme` do tenant
(campo "theme" no /admin/bootstrap-tenant).
"""

from markupsafe import Markup

THEMES = {
    # Agência Junta: escuro, amarelo-limão, Stapel + Gilroy (manual da marca, p.17-20)
    "junta": {
        "fonts_href": "",
        "favicon": "/static/brand/favicon.svg",
        "theme_color": "#000000",
        "logo_topbar": Markup('<img class="logo-img" src="/static/brand/logo-horizontal-amarelo.svg" alt="Junta">'),
        "logo_login": Markup('<img class="logo-img" src="/static/brand/logo-horizontal-amarelo.svg" alt="Junta">'),
        "vars": {
            "--y": "#ecf23d",
            "--y-dark": "#c5cb12",
            "--y-hover": "#f3f76b",
            "--y-soft": "rgba(236, 242, 61, .10)",
            "--ink": "#0d0d0d",
            "--bg": "#0d0d0d",
            "--body-bg": "radial-gradient(900px 480px at 85% -10%, rgba(236, 242, 61, .10), transparent 60%) fixed, linear-gradient(#0d0d0d, #070707) fixed",
            "--panel": "#181818",
            "--panel-2": "#202020",
            "--panel-3": "#262626",
            "--col-bg": "#181818",
            "--card-bg": "#202020",
            "--input-bg": "#0b0b0b",
            "--line": "rgba(255, 255, 255, .10)",
            "--muted": "rgba(255, 255, 255, .60)",
            "--text": "#f5f5f5",
            "--link": "#ecf23d",
            "--link-decoration": "none",
            "--topbar-bg": "#000",
            "--topbar-border": "1px solid rgba(236, 242, 61, .22)",
            "--on-ink": "#ffffff",
            "--on-ink-muted": "rgba(255, 255, 255, .60)",
            "--accent-ring": "rgba(236, 242, 61, .45)",
            "--chat-bg": "transparent",
            "--chat-dot": "rgba(236, 242, 61, .035)",
            "--bubble-in": "#202020",
            "--danger": "#e21b3c",
            "--danger-bg": "rgba(226, 27, 60, .12)",
            "--success": "#1f9d45",
            "--success-bg": "rgba(31, 157, 69, .14)",
            "--info": "#5b9dff",
            "--info-bg": "rgba(19, 104, 206, .16)",
            "--radius": "14px",
            "--btn-radius": "10px",
            "--btn-shadow": "0 3px 0 #c5cb12",
            "--title": '"Stapel Semi Expanded", "Montserrat", -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
            "--font": '"Gilroy", "Montserrat", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif',
            "--title-weight": "600",
            "--title-transform": "none",
        },
    },
    # Nova Viseu Autopeças: claro/creme, amarelo + preto, Barlow (site nova-viseu-autopecas.lovable.app)
    "novaviseu": {
        "fonts_href": "https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700;800&family=Barlow+Condensed:ital,wght@0,700;0,800;1,800;1,900&display=swap",
        "favicon": "/static/brand/favicon-nv.svg",
        "theme_color": "#15130F",
        "logo_topbar": Markup('<span class="nv-logo" aria-label="Nova Viseu Autopeças"><b>NOVA VISEU</b><small>AUTOPEÇAS</small></span>'),
        "logo_login": Markup('<span class="nv-logo dark big" aria-label="Nova Viseu Autopeças"><b>NOVA VISEU</b><small>AUTOPEÇAS</small></span>'),
        "vars": {
            "--y": "#FDB813",
            "--y-dark": "#E9A400",
            "--y-hover": "#FFC73A",
            "--y-soft": "#FFF3CF",
            "--ink": "#15130F",
            "--bg": "#F6F4EF",
            "--body-bg": "#F6F4EF",
            "--panel": "#FFFFFF",
            "--panel-2": "#F6F4EF",
            "--panel-3": "#EFECE4",
            "--col-bg": "#EFECE4",
            "--card-bg": "#FFFFFF",
            "--input-bg": "#FFFFFF",
            "--line": "#DEDACF",
            "--muted": "#645E52",
            "--text": "#15130F",
            "--link": "#15130F",
            "--link-decoration": "underline",
            "--topbar-bg": "#15130F",
            "--topbar-border": "3px solid #FDB813",
            "--on-ink": "#F5F2E9",
            "--on-ink-muted": "#B3AC9C",
            "--accent-ring": "#15130F",
            "--chat-bg": "#F6F4EF",
            "--chat-dot": "rgba(21, 19, 15, .05)",
            "--bubble-in": "#FFFFFF",
            "--danger": "#C8102E",
            "--danger-bg": "#FDE8EB",
            "--success": "#1C9E4B",
            "--success-bg": "#E3F5E9",
            "--info": "#1B6BFF",
            "--info-bg": "#E6EEFF",
            "--radius": "10px",
            "--btn-radius": "999px",
            "--btn-shadow": "none",
            "--title": '"Barlow Condensed", "Arial Narrow", "Helvetica Neue", Arial, sans-serif',
            "--font": '"Barlow", "Helvetica Neue", Arial, sans-serif',
            "--title-weight": "800",
            "--title-transform": "uppercase",
        },
    },
}

DEFAULT_THEME = "junta"


def theme_for(tenant) -> dict:
    key = getattr(tenant, "theme", "") or DEFAULT_THEME
    return THEMES.get(key, THEMES[DEFAULT_THEME])


def theme_css(theme: dict) -> Markup:
    body = "; ".join(f"{name}: {value}" for name, value in theme["vars"].items())
    return Markup(f":root {{ {body} }}")
