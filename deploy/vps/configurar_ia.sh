#!/usr/bin/env bash
# Instala a chave da API do Claude (Anthropic) no CRM: liga a leitura automática de
# comprovantes de pagamento (o CRM lê a foto/PDF e pergunta ao vendedor se confirma a venda).
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/configurar_ia.sh)
#
# A chave se cria em console.anthropic.com > API Keys. Pra desligar: rode de novo e cole "desligar".
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

read -r -s -p "Cole a chave da API do Claude (começa com sk-ant-; não aparece na tela): " KEY </dev/tty; echo
KEY=$(echo "$KEY" | tr -d '[:space:]')
[ -n "$KEY" ] || fail "chave vazia"
[ "$KEY" = "desligar" ] && KEY=""

grep -v '^ANTHROPIC_API_KEY=' $ENV_FILE > $ENV_FILE.tmp || true
[ -n "$KEY" ] && echo "ANTHROPIC_API_KEY=$KEY" >> $ENV_FILE.tmp
mv $ENV_FILE.tmp $ENV_FILE && chmod 600 $ENV_FILE

say "Reiniciando o CRM"
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)
for _ in $(seq 1 30); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 2
done
[ -z "$KEY" ] && { say "Leitura de comprovantes desligada."; exit 0; }

say "Testando a chave"
docker exec crm-app python -c "
import anthropic
from app.config import ANTHROPIC_API_KEY
try:
    anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).models.retrieve('claude-opus-5-5')
except anthropic.AuthenticationError:
    print('ERRO: a Anthropic recusou a chave.'); raise SystemExit(1)
except anthropic.APIError as exc:
    print('ERRO:', exc); raise SystemExit(1)
print('Chave OK: leitura de comprovantes ligada.')
" || fail "a chave não funcionou (confira se copiou inteira e se a conta tem créditos)"
say "Pronto!"
