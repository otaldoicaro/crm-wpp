"""Antes mandava conversões com UM pixel/conta pro servidor inteiro (risco de misturar clientes).
Agora os eventos saem só pela fila por cliente (services/conversions/queue.py), registrados sozinhos
quando o lead muda de etapa (funnel.set_stage). Mantido pra quem ainda chama: não faz nada."""

from sqlalchemy.orm import Session

from app.models import ConversionEvent, Lead


def dispatch_stage_conversion(db: Session, lead: Lead, event_name: str) -> list[ConversionEvent]:
    return []
