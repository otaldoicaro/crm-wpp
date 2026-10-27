"""Instância única do Jinja2Templates, compartilhada entre routers.

Também expõe `asset_version` (baseado na data de modificação do CSS) pra
fazer cache-busting dos arquivos estáticos — sem isso, navegadores (e às
vezes o Render/CDN) seguram uma versão antiga do style.css depois de um
deploy, e a interface fica com o visual desatualizado até o usuário limpar
o cache manualmente.
"""

import datetime
import re

from markupsafe import Markup, escape
import os

from fastapi.templating import Jinja2Templates

from app import features, segments
from app.themes import theme_css, theme_for
from app.services import buying, car_deal, followup, funnel, response_times, suggestions
from app.timeutil import fmt_local

templates = Jinja2Templates(directory="app/templates")


def _asset_version() -> str:
    try:
        return str(int(os.path.getmtime("app/static/style.css")))
    except OSError:
        return "1"


templates.env.globals["asset_version"] = _asset_version()
templates.env.globals["theme_for"] = theme_for
templates.env.globals["theme_css"] = theme_css
templates.env.filters["hora"] = fmt_local
templates.env.filters["duracao"] = response_times.human
templates.env.filters["ha"] = response_times.ago
templates.env.globals["rt"] = response_times
from app.services import traffic as _traffic  # noqa: E402

templates.env.globals["METRICS"] = _traffic.METRICS
templates.env.globals["METRIC_GROUPS"] = _traffic.METRIC_GROUPS
templates.env.globals["GOAL_METRICS"] = _traffic.GOAL_METRICS
templates.env.globals["goal_status"] = _traffic.goal_status
templates.env.globals["fmt_metric"] = _traffic.fmt
templates.env.globals["auto_stage"] = funnel.is_automatic  # etapas que só o CRM preenche
templates.env.globals["loss_reasons"] = segments.loss_reasons  # motivos de perda do ramo do cliente
templates.env.globals["segment_for"] = segments.segment_for
templates.env.globals["has_feature"] = features.has  # recursos ligados só pra este cliente
from app.services import calls as _calls  # noqa: E402

templates.env.globals["call_duration"] = _calls.duration  # 📞 "2:10", "1h05"
templates.env.globals["pay_terms"] = car_deal.describe  # "Entrada R$ ... + 60x R$ ..."
templates.env.filters["brl"] = lambda v: f"{float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
templates.env.globals["sug_current"] = suggestions.current  # 💡 sugestão de etapa pendente
templates.env.globals["SUG_LABEL"] = suggestions.KIND_LABEL
templates.env.globals["buy_pending"] = buying.pending  # 🔥 quis comprar depois do preço
templates.env.globals["fu_state"] = followup.state  # selos ⏰ parado / 📞 follow-up / 📅 próximo contato
templates.env.globals["agora"] = datetime.datetime.utcnow
_URL = re.compile(r"(?<![\w@/])((?:https?://|www\.)[^\s<]+[^\s<.,;:!?)\]'\"])", re.IGNORECASE)


def _quebras(text) -> Markup:
    """Texto da mensagem: quebra de linha vira <br> e link vira clicável (abre em outra aba,
    ou no app certo no celular: Instagram, Maps, YouTube...)."""
    text, out, last = text or "", [], 0
    for m in _URL.finditer(text):
        url = m.group(1)
        href = url if url.lower().startswith("http") else "https://" + url
        out.append(str(escape(text[last:m.start(1)])))
        out.append(f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer" class="msg-link">{escape(url)}</a>')
        last = m.end(1)
    out.append(str(escape(text[last:])))
    return Markup("".join(out).replace("\n", "<br>"))


MEDIA_PLACEHOLDERS = ("📷 Imagem", "🎥 Vídeo", "🎤 Mensagem de áudio", "🎤 Áudio", "📄 Documento", "🖼️ Figurinha", "🎞️ GIF")


def _legenda(text) -> str:
    """Legenda de foto/vídeo/áudio: o CRM guarda "📷 Imagem — legenda"; mostra só a legenda."""
    text = text or ""
    for p in MEDIA_PLACEHOLDERS:
        if text.startswith(p):
            rest = text[len(p):]
            return rest[3:] if rest.startswith(" — ") else ""
    return text if not text.startswith(("📷", "🎥", "🎤")) else ""


templates.env.filters["quebras"] = _quebras
templates.env.filters["legenda"] = _legenda
# "Carlos  Vendedor" e "carlos vendedor" contam como o mesmo nome
templates.env.filters["chave_nome"] = lambda s: " ".join((s or "").lower().split())


def _fone(phone: str) -> str:
    """+5521999043392 -> +55 21 99904-3392 (o jeito que o WhatsApp mostra)."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("55") and len(d) in (12, 13):
        local = d[4:]
        return f"+55 {d[2:4]} {local[:-4]}-{local[-4:]}"
    return phone or ""


templates.env.filters["fone"] = _fone


def _recados_pending(user) -> int:
    """💬 Recados esperando resposta desta pessoa (número vermelho na barra de cima)."""
    from app.db import SessionLocal
    from app.services import comments

    db = SessionLocal()
    try:
        return comments.pending_count(db, user)
    except Exception:
        return 0
    finally:
        db.close()


templates.env.globals["recados_pending"] = _recados_pending
