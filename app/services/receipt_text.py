"""Leitura GRÁTIS de comprovante, no próprio servidor (sem API paga):
- PDF (o app do banco gera): o texto já vem no arquivo -> pypdf.
- Imagem (print do app do banco): OCR com o Tesseract (programa livre instalado no container).
Depois, regras simples em cima do texto: é comprovante? qual valor? qual data/forma?

Um OCR por vez (semáforo): print de banco leva ~1-3s de CPU; mesmo 300 por dia somam poucos
minutos de processamento espalhados no dia."""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import threading
import unicodedata
from typing import Optional

logger = logging.getLogger("receipt_text")

_OCR_SLOT = threading.Semaphore(1)
OCR_TIMEOUT = 40  # segundos
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# palavras de comprovante (sem acento, minúsculas)
STRONG = ("comprovante", "pix enviado", "pix realizado", "transferencia realizada", "pagamento realizado",
          "pagamento efetuado", "pagamento aprovado", "transacao realizada", "transferencia enviada")
SUPPORT = ("pix", "transferencia", "ted", "pagador", "recebedor", "favorecido", "destino", "origem",
           "chave", "autenticacao", "id da transacao", "e2e", "codigo da transacao", "instituicao",
           "agencia", "conta", "cpf", "cnpj", "data", "valor", "pago", "debitado")
NOT_RECEIPT = ("orcamento", "pedido de compra", "nota fiscal", "danfe", "vencimento", "linha digitavel",
               "pagar ate", "pague ate", "aguardando pagamento")

# "R$ 1.300" (sem centavos) também: o milhar com ponto vem antes do número solto
_MONEY = re.compile(r"R\$\s*(\d{1,3}(?:[.\s]\d{3})+(?:,\d{2})?(?!\d)|\d+(?:,\d{2})?)")
_VALUE_LINE = re.compile(r"valor[^\n\dR]{0,25}(?:R\$\s*)?(\d{1,3}(?:[.\s]\d{3})*,\d{2}|\d+,\d{2})", re.IGNORECASE)
_DATE = re.compile(
    r"(\d{2}/\d{2}/\d{2,4}|\d{1,2}(?: de |/| )(?:jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-zç]*\.?(?: de |/| )\d{4})"
    r"(?:\D{0,8}(\d{2}:\d{2}))?",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _money(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(" ", "").replace(".", "").replace(",", "."))
    except ValueError:
        return None


def pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:3])
    except Exception:
        logger.info("receipt_text: PDF sem texto legível")
        return ""


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def image_text(content: bytes) -> str:
    """OCR em português. Vazio se o Tesseract não está instalado ou deu erro."""
    if not ocr_available():
        return ""
    with _OCR_SLOT:
        try:
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "por", "--psm", "4"],
                input=content, capture_output=True, timeout=OCR_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("receipt_text: OCR demorou demais")
            return ""
    return result.stdout.decode("utf-8", "ignore")


def extract_text(content: bytes, mime: str) -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime == "application/pdf":
        return pdf_text(content)
    if mime in IMAGE_TYPES:
        return image_text(content)
    return ""


def classify(text: str) -> dict:
    """Mesmo formato da leitura por IA: is_payment_receipt, amount, paid_at, method, payer,
    payee, confidence (alta | media | baixa)."""
    plain = _plain(text)
    strong = sum(1 for k in STRONG if k in plain)
    support = sum(1 for k in SUPPORT if k in plain)
    negative = any(k in plain for k in NOT_RECEIPT)

    amount = None
    for m in _VALUE_LINE.finditer(text):  # "Valor: R$ 1.250,00" vale mais que qualquer R$ solto
        amount = _money(m.group(1))
        if amount:
            break
    if not amount:
        values = [v for v in (_money(m.group(1)) for m in _MONEY.finditer(text)) if v]
        amount = max(values) if values else None

    method = "PIX" if "pix" in plain else ("TED" if " ted" in plain else ("Transferência" if "transferencia" in plain else
             ("Boleto" if "boleto" in plain else ("Cartão" if "cartao" in plain else ""))))
    date = _DATE.search(text)
    paid_at = " ".join(g for g in date.groups() if g) if date else ""

    is_receipt = bool(amount) and not negative and (strong >= 1 and support >= 2 or support >= 5)
    confidence = "alta" if is_receipt and strong and support >= 3 else ("media" if is_receipt else "baixa")
    return {
        "is_payment_receipt": is_receipt, "amount": amount or 0, "paid_at": paid_at, "method": method,
        "payer": "", "payee": "", "confidence": confidence,
    }
