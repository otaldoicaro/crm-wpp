"""Instância única do Jinja2Templates, compartilhada entre routers.

Também expõe `asset_version` (baseado na data de modificação do CSS) pra
fazer cache-busting dos arquivos estáticos — sem isso, navegadores (e às
vezes o Render/CDN) seguram uma versão antiga do style.css depois de um
deploy, e a interface fica com o visual desatualizado até o usuário limpar
o cache manualmente.
"""

import os

from fastapi.templating import Jinja2Templates

from app.themes import theme_css, theme_for

templates = Jinja2Templates(directory="app/templates")


def _asset_version() -> str:
    try:
        return str(int(os.path.getmtime("app/static/style.css")))
    except OSError:
        return "1"


templates.env.globals["asset_version"] = _asset_version()
templates.env.globals["theme_for"] = theme_for
templates.env.globals["theme_css"] = theme_css
