"""🏢 Empresas no Pipeline (recurso "empresas").

Quem chamou passou o número de outra pessoa da mesma empresa: no status do lead, o vendedor junta esse
contato ao card da empresa. O contato juntado guarda o id do lead principal em Lead.company_lead_id:
- sai das colunas do Pipeline e das contagens do Dashboard (a empresa conta como um lead só);
- continua no Inbox normalmente (cada número tem a sua conversa);
- aparece dentro do card principal, com nome e telefone.
Empresa com uma pessoa só fica como sempre foi.

A busca (Pipeline, Inbox, Arquivados, Lixeira) acha pelo nome do contato, telefone ou nome da empresa
(Lead.company, que acompanha a empresa da ficha sozinho: models._sync_company)."""

from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import features
from app.models import Lead

FEATURE = "empresas"


def enabled(tenant) -> bool:
    return features.has(tenant, FEATURE)


def not_joined(tenant):
    """Filtro: tira os contatos juntados (só com o recurso ligado; desligado, cada um volta a ser um card)."""
    from sqlalchemy import true

    return Lead.company_lead_id == "" if enabled(tenant) else true()


def main_of(db: Session, lead: Lead):
    """O lead principal do card da empresa (None se este contato não foi juntado a ninguém)."""
    if not lead.company_lead_id:
        return None
    main = db.get(Lead, lead.company_lead_id)
    if main is None or main.deleted_at is not None or main.tenant_id != lead.tenant_id:
        return None
    return main


def contacts_of(db: Session, main: Lead) -> list:
    return (db.query(Lead).filter(Lead.tenant_id == main.tenant_id, Lead.company_lead_id == main.id,
                                  Lead.deleted_at.is_(None)).order_by(Lead.created_at).all())


def contacts_by_main(db: Session, tenant_id: str, ids: list) -> dict:
    """{id do lead principal: [contatos juntados]} pros cards na tela (uma consulta só)."""
    if not ids:
        return {}
    out: dict = {}
    for c in (db.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.company_lead_id.in_(ids), Lead.deleted_at.is_(None))
              .order_by(Lead.created_at)):
        out.setdefault(c.company_lead_id, []).append(c)
    return out


def choices(db: Session, tenant_id: str, exclude_id: str = "") -> list:
    """Cards de empresa pra escolher ao juntar: leads principais com empresa na ficha, por nome da empresa."""
    rows = (db.query(Lead).filter(Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None), Lead.company != "",
                                  Lead.company_lead_id == "", Lead.is_group.is_(False), Lead.id != exclude_id)
            .order_by(Lead.company, Lead.created_at).limit(800).all())
    return rows


def join(db: Session, lead: Lead, main: Lead) -> bool:
    """Junta `lead` (e quem já estava junto dele) ao card de `main`. A empresa do card vai pra ficha
    do contato se ela estiver vazia."""
    root = main_of(db, main) or main
    if root.id == lead.id or root.tenant_id != lead.tenant_id or lead.is_group or root.is_group:
        return False
    for other in contacts_of(db, lead):
        other.company_lead_id = root.id
    lead.company_lead_id = root.id
    if root.company and not lead.company:
        try:
            profile = json.loads(lead.profile or "{}")
        except ValueError:
            profile = {}
        if isinstance(profile, dict):
            profile["empresa"] = root.company
            lead.profile = json.dumps(profile, ensure_ascii=False)
    return True


def split(lead: Lead) -> None:
    """Separa o contato: volta a ser um card próprio no Pipeline."""
    lead.company_lead_id = ""


def search_condition(text: str):
    """Nome do contato, telefone (4+ dígitos) ou nome da empresa; acha também o card principal
    quando quem bate é um contato juntado a ele."""
    text = (text or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    own = [Lead.name.ilike(f"%{text}%"), Lead.company.ilike(f"%{text}%")]
    if len(digits) >= 4:
        own.append(Lead.phone.like(f"%{digits}%"))
    joined = select(Lead.company_lead_id).where(Lead.company_lead_id != "", or_(*own))
    return or_(*own, Lead.id.in_(joined))


def backfill(db: Session) -> int:
    """Preenche Lead.company dos leads antigos que já têm empresa na ficha (roda no start; rápido)."""
    from app.models import _company_of

    count = 0
    for lead in db.query(Lead).filter(Lead.company == "", Lead.profile.like('%"empresa"%')):
        name = _company_of(lead.profile)
        if name:
            lead.company = name
            count += 1
    if count:
        db.commit()
    return count
