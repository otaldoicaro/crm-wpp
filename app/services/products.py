"""🔧🚗 O que mais vende: peça (tipo) e carro das vendas (Ganho) do período, pro Dashboard.

De onde vem:
- formulário da LP: "Peças" (o nome da peça, sem SKU/preço/código) e "Marca"/"Modelo";
  seminovos: "Carros de interesse";
- quem veio direto pelo WhatsApp não preenche nada: lê as mensagens do próprio cliente e pega o
  primeiro tipo de peça (farol, lanterna, motor...) e o primeiro modelo de carro que ele citou.
A peça é agrupada pelo tipo (as 1ª palavras: "Farol", "Lanterna Traseira", "Bomba Combustível"),
senão cada anúncio vira uma linha só."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from app.models import Conversation, Message

PART_WORDS = (
    "farol", "lanterna", "retrovisor", "parachoque", "para-choque", "para choque", "motor", "cambio", "câmbio",
    "porta", "capo", "capô", "paralama", "radiador", "pastilha", "amortecedor", "bomba", "embreagem", "vidro",
    "roda", "pneu", "banco", "painel", "grade", "spoiler", "tampa", "cabecote", "cabeçote", "modulo", "módulo",
    "alternador", "arranque", "compressor", "condensador", "eixo", "virabrequim", "turbina", "junta", "chicote",
    "volante", "coluna", "caixa", "semi eixo", "semieixo", "bandeja", "pivo", "pivô", "cubo", "disco", "pinça",
    "pinca", "macaneta", "maçaneta", "fechadura", "moldura", "friso", "emblema", "sensor", "injetor", "bico",
)
# palavras que não dizem o tipo da peça (o tipo é o que vem antes delas)
STOP = {"do", "da", "de", "dos", "das", "para", "pra", "com", "sem", "lado", "original", "usado", "usada",
        "esquerdo", "esquerda", "direito", "direita", "dianteiro", "dianteira", "traseiro", "traseira",
        "fiat", "volkswagen", "vw", "chevrolet", "gm", "ford", "renault", "toyota", "honda", "hyundai", "nissan",
        "peugeot", "citroen", "jeep", "kia", "mitsubishi", "audi", "bmw", "mercedes", "volvo", "jac", "caoa", "chery"}


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


_PART_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted({re.escape(_plain(w)) for w in PART_WORDS}, key=len, reverse=True))
                      + r")(?![a-z])")


def part_kind(name: str) -> str:
    """"Lanterna Mala Traseira Direita Virtus 2018/22" -> "Lanterna Mala"; "Farol Gol G5" -> "Farol Gol"…
    fica com as 2 primeiras palavras que dizem o que é a peça (sem lado, posição, carro, ano)."""
    from app.services.suggestions import _MODEL

    words = []
    for word in re.split(r"\s+", (name or "").strip()):
        plain = _plain(word).strip(".,-/()")
        if not plain or any(ch.isdigit() for ch in plain) or plain in STOP or _MODEL.fullmatch(plain):
            if words:
                break
            continue
        words.append(word.strip(".,-/()").capitalize())
        if len(words) == 2:
            break
    return " ".join(words)


def _from_messages(texts: list) -> tuple:
    """(tipo de peça, carro) citados primeiro pelo cliente nas mensagens dele."""
    from app.services.suggestions import _MODEL

    part, car = "", ""
    for text in texts:
        plain = _plain(text)
        if not part:
            hit = _PART_RE.search(plain)
            if hit:
                part = hit.group(1).replace("para choque", "parachoque").capitalize()
        if not car:
            m = _MODEL.search(plain)
            if m:
                car = m.group(1).upper() if len(m.group(1)) <= 4 else m.group(1).capitalize()
        if part and car:
            break
    return part, car


def top_sold(db, won_leads: list, limit: int = 10) -> dict:
    """{"parts": [(tipo, vendas, receita)], "cars": [...], "unknown": vendas sem peça nem carro}."""
    from app.services.first_contact import car_names, form_fields, part_names

    need_msgs = []
    found = {}
    for lead in won_leads:
        fields = form_fields(lead.form_details)
        part = part_kind(part_names(fields.get("peca", "")).split(" e ")[0])
        car = (" ".join(x for x in (fields.get("marca", ""), fields.get("modelo", "")) if x).strip()
               or car_names(fields.get("carro", "")).split(" e pelo ")[0])
        found[lead.id] = [part, car]
        if not part or not car:
            need_msgs.append(lead.id)
    if need_msgs:  # quem veio pelo WhatsApp: o que o próprio cliente escreveu (as 1ªs mensagens dele)
        texts = defaultdict(list)
        rows = (db.query(Conversation.lead_id, Message.body)
                .join(Message, Message.conversation_id == Conversation.id)
                .filter(Conversation.lead_id.in_(need_msgs), Message.direction == "in", Message.body != "")
                .order_by(Message.created_at).all())
        for lead_id, body in rows:
            if len(texts[lead_id]) < 15:
                texts[lead_id].append(body)
        for lead_id in need_msgs:
            part, car = _from_messages(texts.get(lead_id, []))
            found[lead_id][0] = found[lead_id][0] or part
            found[lead_id][1] = found[lead_id][1] or car
    parts, cars, unknown = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0]), 0
    for lead in won_leads:
        part, car = found[lead.id]
        if not part and not car:
            unknown += 1
        if part:
            parts[part][0] += 1
            parts[part][1] += lead.deal_value or 0
        if car:
            key = car.strip().title() if not car.isupper() else car
            cars[key][0] += 1
            cars[key][1] += lead.deal_value or 0

    def ranked(d):
        return [(k, n, v) for k, (n, v) in sorted(d.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)[:limit]]

    return {"parts": ranked(parts), "cars": ranked(cars), "unknown": unknown, "total": len(won_leads)}
