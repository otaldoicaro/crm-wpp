#!/usr/bin/env bash
# Configura a conta de e-mail que o CRM usa pra mandar o link de "Esqueceu a senha?".
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/configurar_email.sh)
#
# Google Workspace / Gmail: use smtp.gmail.com, porta 587, e uma "senha de app"
# (myaccount.google.com/apppasswords), não a senha normal da conta.
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }
ask() { local prompt=$1 default=${2:-} answer; read -r -p "$prompt${default:+ [$default]}: " answer </dev/tty; echo "${answer:-$default}"; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

say "Conta de e-mail que vai mandar os avisos do CRM"
HOST=$(ask "Servidor SMTP" "smtp.gmail.com")
PORT=$(ask "Porta" "587")
USER_=$(ask "E-mail da conta (login)")
FROM=$(ask "Endereço que aparece como remetente" "$USER_")
read -r -s -p "Senha de app (não aparece ao digitar/colar): " PASS </dev/tty; echo
[ -n "$USER_" ] && [ -n "$PASS" ] || fail "e-mail e senha são obrigatórios"

set_var() {  # troca ou adiciona VAR=valor no crm.env
  local key=$1 value=$2
  grep -v "^$key=" $ENV_FILE > $ENV_FILE.tmp || true
  printf '%s=%s\n' "$key" "$value" >> $ENV_FILE.tmp
  mv $ENV_FILE.tmp $ENV_FILE
}
set_var SMTP_HOST "$HOST"
set_var SMTP_PORT "$PORT"
set_var SMTP_USER "$USER_"
set_var SMTP_FROM "$FROM"
set_var SMTP_PASSWORD "${PASS// /}"  # senha de app do Google vem com espaços; eles não fazem parte da senha
chmod 600 $ENV_FILE

say "Reiniciando o CRM com a configuração nova"
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)
for _ in $(seq 1 30); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 2
done

say "Mandando um e-mail de teste pra $USER_"
docker exec crm-app python -c "
from app.services import mailer
ok = mailer.send_email('$USER_', 'Teste do CRM: e-mail configurado', 'Se você recebeu isto, o \"Esqueceu a senha?\" do CRM já funciona.')
print('ENVIADO' if ok else 'FALHOU')
" | grep -q ENVIADO || fail "o e-mail de teste não saiu. Confira o e-mail e a senha de app e rode de novo (detalhes: docker logs crm-app --tail 30)."
say "Pronto! Confira a caixa de entrada (e o spam) de $USER_."
