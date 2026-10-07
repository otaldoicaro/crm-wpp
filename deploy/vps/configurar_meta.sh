#!/usr/bin/env bash
# Instala o token de leitura da conta de anúncios do Meta (ads_read) no CRM.
# Com ele o CRM mostra campanha / conjunto / anúncio dos leads de "clique pro WhatsApp".
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/configurar_meta.sh)
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

read -r -s -p "Cole o token do usuário do sistema do Meta (não aparece na tela): " TOKEN </dev/tty; echo
TOKEN=$(echo "$TOKEN" | tr -d '[:space:]')
[ -n "$TOKEN" ] || fail "token vazio"

grep -v '^META_ADS_ACCESS_TOKEN=' $ENV_FILE > $ENV_FILE.tmp || true
echo "META_ADS_ACCESS_TOKEN=$TOKEN" >> $ENV_FILE.tmp
mv $ENV_FILE.tmp $ENV_FILE && chmod 600 $ENV_FILE

say "Reiniciando o CRM com o token"
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)
for _ in $(seq 1 30); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 2
done

say "Testando o token e preenchendo as campanhas dos leads que já chegaram"
docker exec crm-app python -c "
from app.services.meta_ads_lookup import backfill_campaign_names, check_token
try:
    contas = check_token()
except Exception as exc:
    print('ERRO: o Meta recusou o token:', exc); raise SystemExit(1)
print('Contas de anúncio que o token enxerga:')
for nome, act in contas: print('  -', nome, '(act_' + act + ')')
if not contas: print('  (nenhuma! atribua a conta de anúncios ao usuário do sistema)')
ok, sem = backfill_campaign_names()
print(f'Leads atualizados com nome de campanha: {ok}' + (f' | sem acesso ao anúncio: {sem}' if sem else ''))
" || fail "token inválido ou sem permissão ads_read"

say "Buscando o investimento dos anúncios (últimos 90 dias; pode levar 1 minuto)"
docker exec crm-app python -c "
from app.services import meta_spend
for cliente, linhas in meta_spend.sync_all(days=meta_spend.FIRST_SYNC_DAYS).items():
    print('  -', cliente, '->', (str(linhas) + ' linhas de gasto') if isinstance(linhas, int) else linhas)
print('Depois disso o CRM atualiza sozinho a cada 3h.')
"
say "Pronto!"
