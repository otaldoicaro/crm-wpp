"""🗄️ Arquiva de uma vez os leads parados numa etapa há mais de N dias (padrão 7). Arquivar só tira do
Pipeline: nada é apagado, dá pra restaurar um a um, e se o cliente mandar mensagem o lead volta sozinho.
Sem --aplicar só mostra quantos são.

    docker exec -i crm-app python -m scripts.arquivar_etapa <identificador do cliente> "Lead sem conversa"
    docker exec -i crm-app python -m scripts.arquivar_etapa <identificador do cliente> "Lead sem conversa" --aplicar

Opcional: --dias 14 (em vez de 7). Conta a partir de quando o lead chegou (data de criação).
"""

import datetime
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, PipelineStage, Tenant


def _plain(text: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                            if unicodedata.category(c) != "Mn").split())


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--aplicar" in sys.argv
    days = 7
    if "--dias" in sys.argv:
        try:
            days = int(sys.argv[sys.argv.index("--dias") + 1])
        except (IndexError, ValueError):
            print("use --dias com um número, ex: --dias 14")
            return
        args = [a for a in args if a != str(days)]
    if len(args) < 2:
        print('use: python -m scripts.arquivar_etapa <identificador do cliente> "<nome da etapa>" [--dias 7] [--aplicar]')
        return
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.subdomain == args[0]).first()
        if tenant is None:
            print(f"cliente '{args[0]}' não encontrado")
            return
        stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).all()
        stage = next((s for s in stages if _plain(s.name) == _plain(args[1])), None)
        if stage is None:
            print(f"etapa '{args[1]}' não existe em {tenant.name}. As etapas são: " + ", ".join(s.name for s in stages))
            return
        limit = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.stage_id == stage.id, Lead.created_at < limit,
                                      Lead.archived_at.is_(None), Lead.deleted_at.is_(None))
        leads = query.all()
        total = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.stage_id == stage.id,
                                      Lead.archived_at.is_(None), Lead.deleted_at.is_(None)).count()
        print(f"{tenant.name} · etapa “{stage.name}”: {total} leads no Pipeline; {len(leads)} chegaram há mais de {days} dias")
        if not apply_changes:
            print("Nada foi mudado. Pra arquivar, rode de novo com --aplicar no final.")
            return
        now = datetime.datetime.utcnow()
        for lead in leads:
            lead.archived_at = now
        db.commit()
        print(f"✅ {len(leads)} leads arquivados (ficam {total - len(leads)} na etapa).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
