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


def _agenda_active(lead):
    """📅 Visita em aberto deste lead (ou None)."""
    from sqlalchemy.orm import object_session

    from app.services import agenda

    session = object_session(lead) if lead is not None else None
    return agenda.active_for(session, lead.id) if session is not None else None


def _agenda_count(tenant, user) -> int:
    from app.db import SessionLocal
    from app.services import agenda

    db = SessionLocal()
    try:
        return agenda.pending_count(db, tenant, user)
    except Exception:
        return 0
    finally:
        db.close()


from app.services import agenda as _agenda  # noqa: E402

templates.env.globals["agenda_active"] = _agenda_active
templates.env.globals["agenda_count"] = _agenda_count  # número da aba 📅 Agendamentos
templates.env.globals["agenda_prefill_for"] = _agenda.prefill
templates.env.globals["agenda_texts_for"] = _agenda.texts
templates.env.globals["agenda_payments"] = _agenda.PAYMENTS
templates.env.globals["agenda_state"] = _agenda.state
templates.env.globals["agenda_when"] = _agenda.when_label
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


def _ig_info(tenant) -> dict:
    """📸 Card do Instagram em ⚙ Configurações → Integrações."""
    from app.services import instagram
    from app.tenancy import public_url

    account = next((n for n in tenant.whatsapp_numbers if n.provider == instagram.PROVIDER and n.is_active), None)
    return {"account": account, "callback": f"{public_url(tenant)}/webhooks/instagram", "verify": instagram.verify_token(),
            "lp": f"{public_url(tenant)}/webhooks/lp/{tenant.id}"}


templates.env.globals["ig_info"] = _ig_info


def _google_ads_info(tenant) -> dict:
    """📊 Card do Google Ads em ⚙ Configurações → Integrações: script pronto e última sincronização."""
    from app.db import SessionLocal
    from app.services import google_ads_spend

    with SessionLocal() as db:
        last_day, rows = google_ads_spend.last_sync(db, tenant.id)
    if last_day is not None and not isinstance(last_day, str):
        last_day = last_day.strftime("%d/%m/%Y")
    elif last_day:
        last_day = "/".join(reversed(str(last_day)[:10].split("-")))
    return {"script": google_ads_spend.script_for(tenant), "last_day": last_day or "—", "rows": rows}


templates.env.globals["google_ads_info"] = _google_ads_info
from app.services import lead_profile as _lead_profile  # noqa: E402

templates.env.globals["empresa_de"] = _lead_profile.company  # 🏢 empresa da ficha do lead (card, conversa)
# 📸 contato do Direct do Instagram ("ig:<id>"): aparece como "📸 Instagram" no lugar do telefone
templates.env.filters["contato"] = lambda phone: "📸 Instagram" if (phone or "").startswith("ig:") else (phone or "")


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


def _tasks_due(tenant, user) -> int:
    """✅ Tarefas de hoje e atrasadas desta pessoa (número no menu)."""
    from app import features
    from app.db import SessionLocal
    from app.services import tasks

    if not features.has(tenant, "tarefas"):
        return 0
    with SessionLocal() as db:
        try:
            return tasks.due_count(db, tenant, user)
        except Exception:
            return 0


def _lead_tasks(lead) -> list:
    from app.db import SessionLocal
    from app.services import tasks

    with SessionLocal() as db:
        rows = tasks.open_for_lead(db, lead)
        return [{"id": t.id, "title": t.title, "when": tasks.when_label(t), "late": t.due_date < tasks.today(),
                 "today": t.due_date == tasks.today(), "who": t.user.name if t.user else ""} for t in rows]


templates.env.globals["tasks_due"] = _tasks_due
templates.env.globals["lead_tasks"] = _lead_tasks
templates.env.globals["timedelta_days"] = lambda n: datetime.timedelta(days=n)


def _task_people(tenant, user) -> list:
    """Admin escolhe de quem é a tarefa; vendedor cria pra ele mesmo (lista vazia)."""
    if getattr(user, "role", "") != "admin":
        return []
    from app.db import SessionLocal
    from app.services.people import team_members

    with SessionLocal() as db:
        return [type("P", (), {"id": p.id, "name": p.name}) for p in team_members(db, tenant.id, only_active=True)]
templates.env.globals["task_people_for"] = _task_people
