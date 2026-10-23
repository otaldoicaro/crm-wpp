#!/usr/bin/env bash
# Instala a chave do Groq no CRM: liga a transcrição dos áudios do WhatsApp (o texto aparece embaixo
# do áudio). Depois, em cada cliente que quiser: ⚙ Configurações → Prazos e automações → Recursos → "🎙️ Transcrever os áudios".
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/configurar_groq.sh)
#
# A chave se cria em console.groq.com > API Keys (começa com gsk_). Pra desligar: rode de novo e cole "desligar".
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

read -r -s -p "Cole a chave do Groq (começa com gsk_; não aparece na tela): " KEY </dev/tty; echo
KEY=$(echo "$KEY" | tr -d '[:space:]')
[ -n "$KEY" ] || fail "chave vazia"
[ "$KEY" = "desligar" ] && KEY=""

grep -v '^GROQ_API_KEY=' $ENV_FILE > $ENV_FILE.tmp || true
[ -n "$KEY" ] && echo "GROQ_API_KEY=$KEY" >> $ENV_FILE.tmp
mv $ENV_FILE.tmp $ENV_FILE && chmod 600 $ENV_FILE

say "Reiniciando o CRM (uns 30 segundos)"
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)
for _ in $(seq 1 30); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 2
done
[ -z "$KEY" ] && { say "Transcrição de áudios desligada."; exit 0; }

say "Testando a chave"
docker exec crm-app python -c "
import requests
from app.config import GROQ_API_KEY
r = requests.get('https://api.groq.com/openai/v1/models', headers={'Authorization': 'Bearer ' + GROQ_API_KEY}, timeout=20)
if r.status_code == 401:
    print('ERRO: o Groq recusou a chave.'); raise SystemExit(1)
if not r.ok:
    print('ERRO:', r.status_code, r.text[:200]); raise SystemExit(1)
print('Chave OK.')
" || fail "a chave não funcionou (confira se copiou inteira)"
say "Pronto! Agora ligue nos clientes: ⚙ Configurações → Prazos e automações → Recursos → 🎙️ Transcrever os áudios → Salvar."
