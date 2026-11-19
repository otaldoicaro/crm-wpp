#!/usr/bin/env bash
# 📱 Liga a conta da UAZAPI no CRM (o WhatsApp que substitui o Evolution, número por número).
# Depois, em cada cliente que for usar: ⚙ Configurações → Recursos → "📱 WhatsApp pela UAZAPI" → Salvar,
# e na tela WhatsApp: 🔁 Passar pro UAZapi em cada número (o vendedor lê o QR code de novo).
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/configurar_uazapi.sh)
#
# Pede o endereço do servidor da UAZAPI (ex: https://suaempresa.uazapi.com) e o "admin token" do painel
# deles (digitado escondido; nunca no chat nem no repositório). Pra desligar: rode de novo e cole "desligar".
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

read -r -p "Endereço do servidor da UAZAPI (ex: https://suaempresa.uazapi.com) ou \"desligar\": " URL </dev/tty
URL=$(echo "$URL" | tr -d '[:space:]')
URL=${URL%/}
[ -n "$URL" ] || fail "endereço vazio"
TOKEN=""
if [ "$URL" = "desligar" ]; then
  URL=""
else
  case "$URL" in https://*) ;; *) fail "o endereço tem que começar com https://" ;; esac
  read -r -s -p "Cole o admin token da UAZAPI (não aparece na tela): " TOKEN </dev/tty; echo
  TOKEN=$(echo "$TOKEN" | tr -d '[:space:]')
  [ -n "$TOKEN" ] || fail "token vazio"
  say "Testando a conta"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "admintoken: $TOKEN" "$URL/instance/all" || echo 000)
  case "$CODE" in
    200) echo "Conta OK." ;;
    401|403) fail "a UAZAPI recusou o admin token (confira se copiou inteiro). Nada foi mudado." ;;
    000) fail "não consegui falar com $URL (confira o endereço). Nada foi mudado." ;;
    *) fail "a UAZAPI respondeu $CODE. Nada foi mudado." ;;
  esac
fi

grep -v -E '^(UAZAPI_URL|UAZAPI_ADMIN_TOKEN)=' $ENV_FILE > $ENV_FILE.tmp || true
if [ -n "$URL" ]; then
  echo "UAZAPI_URL=$URL" >> $ENV_FILE.tmp
  echo "UAZAPI_ADMIN_TOKEN=$TOKEN" >> $ENV_FILE.tmp
fi
mv $ENV_FILE.tmp $ENV_FILE && chmod 600 $ENV_FILE

say "Reiniciando o CRM (uns 30 segundos)"
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)
for _ in $(seq 1 30); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 2
done
[ -z "$URL" ] && { say "UAZAPI desligada. Os números que estavam nela param de funcionar até voltar pro Evolution."; exit 0; }

say "Conferindo se a UAZAPI consegue entregar as mensagens no CRM"
docker exec crm-app python -c "
import requests
from app.services import uazapi_client
url = uazapi_client.webhook_url()
if not url.startswith('https://'):
    print('ERRO: o endereço público do CRM não é https:', url.split('/webhooks')[0]); raise SystemExit(1)
r = requests.post(url, json={'EventType': 'teste'}, timeout=20)
if r.status_code != 200:
    print('ERRO: o CRM não respondeu pelo endereço público:', r.status_code); raise SystemExit(1)
print('Webhook OK:', url.split('/webhooks')[0] + '/webhooks/uazapi/…')
" || fail "a UAZAPI não vai conseguir entregar as mensagens (fale com o Claude)"

say "Pronto! Agora, no CRM do cliente: ⚙ Configurações → Recursos → 📱 WhatsApp pela UAZAPI → Salvar."
echo "Depois, na tela WhatsApp, em cada número: 🔁 Passar pro UAZapi (com o celular do vendedor na mão pro QR code)."
