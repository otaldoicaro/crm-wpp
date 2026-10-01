#!/usr/bin/env bash
# Troca a apikey global do Evolution API (AUTHENTICATION_API_KEY) e atualiza o CRM.
# Usar quando a chave vazar (ex: apareceu em chat/print).
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/trocar_chave_evolution.sh)
#
# Faz backup do arquivo alterado (*.bak-AAAAMMDD-HHMMSS) e só mexe se achar a chave
# atual em exatamente um arquivo da pasta do Evolution. Recria o container do Evolution
# (os WhatsApps conectados continuam salvos no banco dele), reconecta à rede do CRM e
# reinicia o CRM com a chave nova.
set -euo pipefail

ENV_FILE=/opt/crm-config/crm.env
say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f $ENV_FILE ] || fail "CRM não instalado neste servidor ($ENV_FILE não existe)"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

EVO=$(docker ps --format '{{.Names}} {{.Image}}' | awk '/evolution-api/ && !/manager/ {print $1; exit}')
[ -n "$EVO" ] || fail "container do Evolution API não encontrado"
OLD_KEY=$(docker inspect "$EVO" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^AUTHENTICATION_API_KEY=//p')
[ -n "$OLD_KEY" ] || fail "não achei a chave atual no container $EVO"
WORKDIR=$(docker inspect "$EVO" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
[ -d "$WORKDIR" ] || fail "não achei a pasta do docker compose do Evolution"

say "Procurando onde a chave está configurada em $WORKDIR"
mapfile -t FILES < <(grep -rlF --exclude='*.bak-*' -- "$OLD_KEY" "$WORKDIR" 2>/dev/null || true)
[ "${#FILES[@]}" = 1 ] || fail "a chave aparece em ${#FILES[@]} arquivos (esperado 1): ${FILES[*]:-nenhum}. Fale com o Claude."
FILE=${FILES[0]}
echo "Arquivo: $FILE"

NEW_KEY=$(openssl rand -hex 24)
cp -p "$FILE" "$FILE.bak-$(date +%Y%m%d-%H%M%S)"
python3 - "$FILE" "$OLD_KEY" "$NEW_KEY" <<'PY'
import sys
path, old, new = sys.argv[1:]
text = open(path).read()
open(path, "w").write(text.replace(old, new))
PY

say "Recriando o Evolution com a chave nova"
(cd "$WORKDIR" && $DC up -d)
sleep 5
EVO=$(docker ps --format '{{.Names}} {{.Image}}' | awk '/evolution-api/ && !/manager/ {print $1; exit}')
docker network connect crm_default "$EVO" 2>/dev/null || true

say "Atualizando o CRM"
sed -i "s|^EVOLUTION_API_KEY=.*|EVOLUTION_API_KEY=$NEW_KEY|" $ENV_FILE
set -a; . $ENV_FILE; set +a
(cd /opt/crm/deploy/vps && $DC up -d app)

say "Conferindo"
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: $NEW_KEY" http://localhost:8080/instance/fetchInstances || true)
  [ "$code" = 200 ] && break
  sleep 2
done
[ "$code" = 200 ] || fail "o Evolution não aceitou a chave nova (HTTP $code). O backup do arquivo está ao lado dele."
old_code=$(curl -s -o /dev/null -w '%{http_code}' -H "apikey: $OLD_KEY" http://localhost:8080/instance/fetchInstances || true)
echo "Chave nova: OK (200) | chave antiga: recusada ($old_code)"
curl -s -H "apikey: $NEW_KEY" http://localhost:8080/instance/fetchInstances \
  | python3 -c "import sys,json; [print('  WhatsApp', i.get('name'), '->', i.get('connectionStatus')) for i in json.load(sys.stdin)]"
say "Pronto! A chave antiga não funciona mais."
