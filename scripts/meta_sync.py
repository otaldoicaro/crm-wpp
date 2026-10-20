"""Busca de novo o investimento e as métricas do Meta (alcance, cliques no link, visualizações
da página, resultados) e mostra o resultado ou o erro de cada cliente.

Rodar no terminal do VPS:
    docker exec -it crm-app python -m scripts.meta_sync        # últimos 90 dias
    docker exec -it crm-app python -m scripts.meta_sync 30     # últimos 30 dias
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.config import META_ADS_ACCESS_TOKEN
from app.db import SessionLocal
from app.models import CampaignSpend
from sqlalchemy import func


def main() -> None:
    from app.services import meta_spend

    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else meta_spend.FIRST_SYNC_DAYS
    if not META_ADS_ACCESS_TOKEN:
        print("❌ Falta o token do Meta (rode deploy/vps/configurar_meta.sh).")
        return
    print(f"\nBuscando os últimos {days} dias no Meta (de {meta_spend.CHUNK_DAYS} em {meta_spend.CHUNK_DAYS} dias)...\n")
    for name, result in meta_spend.sync_all(days=days).items():
        print(f"  {name}: {result if isinstance(result, str) else str(result) + ' linhas (anúncio x dia)'}")
    db = SessionLocal()
    try:
        row = db.query(func.sum(CampaignSpend.spend), func.sum(CampaignSpend.impressions), func.sum(CampaignSpend.reach),
                       func.sum(CampaignSpend.link_clicks), func.sum(CampaignSpend.landing_page_views),
                       func.sum(CampaignSpend.meta_results)).filter(CampaignSpend.source == "meta_api").one()
        print("\nTotais gravados: investido R$ %.2f · impressões %s · alcance %s · cliques no link %s · "
              "visualizações da página %s · resultados %s" % tuple(v or 0 for v in row))
    finally:
        db.close()


if __name__ == "__main__":
    main()
