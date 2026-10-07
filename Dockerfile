FROM python:3.11-slim

WORKDIR /app

# Tesseract (OCR, livre e gratuito): lê o texto dos prints de comprovante, sem sair do servidor.
# SECURITY_UPDATES muda toda semana (instalar.sh): refaz esta etapa e puxa as correções de segurança.
ARG SECURITY_UPDATES=0
RUN echo "atualizações: $SECURITY_UPDATES" && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
