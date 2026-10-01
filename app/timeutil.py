"""Horário de Brasília. O banco guarda tudo em UTC; a tela e os filtros de
data ("Hoje", "Este mês", datas personalizadas) usam o horário local.
O Brasil não tem horário de verão desde 2019, então é UTC-3 fixo."""

import datetime

LOCAL_OFFSET = datetime.timedelta(hours=-3)


def to_local(dt_utc: datetime.datetime) -> datetime.datetime:
    return dt_utc + LOCAL_OFFSET


def local_to_utc(dt_local: datetime.datetime) -> datetime.datetime:
    return dt_local - LOCAL_OFFSET


def fmt_local(dt_utc, fmt: str = "%d/%m %H:%M") -> str:
    """Filtro de template: {{ msg.created_at|hora }} ou {{ x|hora("%d/%m") }}"""
    return to_local(dt_utc).strftime(fmt) if dt_utc else ""
