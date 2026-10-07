#!/usr/bin/env bash
# Instala (ou atualiza) o CRM no VPS da Hostinger, ao lado do Evolution API.
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/instalar.sh)
#
# O que faz:
#   1. baixa/atualiza o código em /opt/crm
#   2. cria a configuração em /opt/crm-config/crm.env (só na 1ª vez; senhas geradas aqui,
#      apikey do Evolution e senha das mídias lidas dos containers que já rodam no VPS)
#   3. sobe Postgres + CRM + Caddy (HTTPS automático) via Docker
#   4. (1ª vez) copia os dados do banco antigo do Render, se você colar o endereço dele
#   5. liga o domínio próprio ao cliente e aponta os WhatsApps pro novo endereço
#   6. agenda backup diário do banco (/root/crm-backups, guarda 14 dias)
# Pode rodar de novo quando quiser atualizar: não apaga dados nem troca senhas.
set -euo pipefail

REPO="https://github.com/otaldoicaro/crm-wpp.git"
CODE_DIR=/opt/crm
CONFIG_DIR=/opt/crm-config
ENV_FILE=$CONFIG_DIR/crm.env
COMPOSE_DIR=$CODE_DIR/deploy/vps

say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }
ask() { local prompt=$1 default=${2:-} answer; read -r -p "$prompt${default:+ [$default]}: " answer </dev/tty; echo "${answer:-$default}"; }

[ "$(id -u)" = 0 ] || fail "rode como root"
command -v docker >/dev/null || fail "docker não encontrado"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; elif command -v docker-compose >/dev/null; then DC="docker-compose"; else fail "docker compose não encontrado"; fi
command -v git >/dev/null || { say "Instalando git"; apt-get update -qq && apt-get install -y -qq git; }

# ---------- portas 80/443 (HTTPS) ----------
for port in 80 443; do
  holder=$(docker ps --filter "publish=$port" --format '{{.Names}}' | grep -v '^crm-caddy$' || true)
  if [ -n "$holder" ]; then
    if echo "$holder" | grep -q "evolution-manager"; then
      say "Parando o painel visual quebrado do Evolution ($holder), que ocupava a porta $port"
      docker update --restart=no "$holder" >/dev/null; docker stop "$holder" >/dev/null
    else
      fail "a porta $port está em uso pelo container '$holder'. Fale com o Claude antes de continuar."
    fi
  elif ss -ltn "( sport = :$port )" | grep -q LISTEN && ! docker ps --format '{{.Names}}' | grep -q '^crm-caddy$'; then
    fail "a porta $port está em uso por um programa fora do Docker. Fale com o Claude antes de continuar."
  fi
done

# ---------- código ----------
say "Baixando o código do CRM"
if [ -d $CODE_DIR/.git ]; then git -C $CODE_DIR pull -q --ff-only; else git clone -q $REPO $CODE_DIR; fi

EVO=$(docker ps --format '{{.Names}} {{.Image}}' | awk '/evolution-api/ && !/manager/ {print $1; exit}')
[ -n "$EVO" ] || fail "container do Evolution API não encontrado"

# ---------- configuração (só na 1ª vez) ----------
FIRST_RUN=0
if [ ! -f $ENV_FILE ]; then
  FIRST_RUN=1
  say "Configuração inicial"
  DOMAIN=$(ask "Endereço do CRM deste cliente" "crm.novaviseu.com.br")
  TENANT=$(ask "Identificador do cliente no CRM" "novaviseu")

  EVO_KEY=$(docker inspect "$EVO" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^AUTHENTICATION_API_KEY=//p')
  [ -n "$EVO_KEY" ] || fail "não achei a apikey do Evolution no container $EVO"
  MEDIA_SECRET=$(docker inspect crm-midias --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' || true)
  [ -n "$MEDIA_SECRET" ] || echo "Aviso: armazenamento de mídias (crm-midias) não encontrado; cópia de mídias fica desligada."

  IP=$(curl -fsS -4 https://api.ipify.org || hostname -I | awk '{print $1}')
  mkdir -p $CONFIG_DIR /opt/crm-data && chmod 700 $CONFIG_DIR
  cat > $ENV_FILE <<CFG
# gerado por instalar.sh em $(date -Is) — guarde em segredo
DEBUG=0
SECRET_KEY=$(openssl rand -hex 32)
BASE_DOMAIN=${IP//./-}.sslip.io
PUBLIC_BASE_URL=https://$DOMAIN
CRM_DOMAIN=$DOMAIN
CRM_TENANT=$TENANT
POSTGRES_PASSWORD=$(openssl rand -hex 24)
ADMIN_SETUP_TOKEN=$(openssl rand -hex 24)
EVOLUTION_API_URL=http://host.docker.internal:8080
EVOLUTION_API_KEY=$EVO_KEY
EVOLUTION_WEBHOOK_TOKEN=$(openssl rand -hex 24)
MEDIA_S3_ENDPOINT=${MEDIA_SECRET:+http://host.docker.internal:8333}
MEDIA_S3_ACCESS_KEY=${MEDIA_SECRET:+crm}
MEDIA_S3_SECRET_KEY=$MEDIA_SECRET
MEDIA_RETENTION_DAYS=90
CFG
  # DATABASE_URL depende da senha gerada acima
  PGPASS=$(sed -n 's/^POSTGRES_PASSWORD=//p' $ENV_FILE)
  echo "DATABASE_URL=postgresql://crm:$PGPASS@db:5432/crm" >> $ENV_FILE
  chmod 600 $ENV_FILE
fi
# instalações antigas: o Evolution entrega as mensagens pela rede interna do Docker
grep -q '^WEBHOOK_BASE_URL=' $ENV_FILE || echo "WEBHOOK_BASE_URL=http://crm-app:8000" >> $ENV_FILE
set -a; . $ENV_FILE; set +a
cd $COMPOSE_DIR

# ---------- banco ----------
say "Subindo o banco de dados"
$DC up -d db
for _ in $(seq 1 60); do docker exec crm-db pg_isready -U crm -d crm >/dev/null 2>&1 && break; sleep 2; done

TABLES=$(docker exec crm-db psql -U crm -d crm -tAc "select count(*) from information_schema.tables where table_schema='public'")
if [ "$TABLES" = "0" ]; then
  echo
  echo "Banco novo e vazio. Pra trazer os dados do Render, cole o 'External Database URL'"
  echo "(Render > crm-junta-db > Connect > External). Não aparece na tela. Enter = começar vazio."
  read -r -s -p "External Database URL: " RENDER_URL </dev/tty; echo
  if [ -n "$RENDER_URL" ]; then
    # colaram o endereço INTERNO (host "dpg-xxxx-a", só funciona dentro do Render)? vira o externo
    RENDER_HOST=$(echo "$RENDER_URL" | sed -E 's#^[a-z]+://[^@]*@([^:/]+).*#\1#')
    if ! echo "$RENDER_HOST" | grep -q '\.'; then
      RENDER_URL=$(echo "$RENDER_URL" | sed -E "s#@$RENDER_HOST([:/])#@$RENDER_HOST.oregon-postgres.render.com\1#")
      echo "(endereço interno do Render detectado; usando o externo: $RENDER_HOST.oregon-postgres.render.com)"
    fi
    say "Copiando os dados do Render (pode levar 1-2 minutos)"
    if ! docker run --rm postgres:18 pg_dump --no-owner --no-acl "$RENDER_URL" > /tmp/crm-render.sql; then
      rm -f /tmp/crm-render.sql
      fail "não consegui ler o banco do Render (endereço errado?). Nada foi alterado: rode o instalador de novo e cole o 'External Database URL'."
    fi
    docker exec -i crm-db psql -q -U crm -d crm -v ON_ERROR_STOP=1 --single-transaction < /tmp/crm-render.sql >/dev/null  # tudo ou nada
    rm -f /tmp/crm-render.sql
    echo "Dados copiados: $(docker exec crm-db psql -U crm -d crm -tAc 'select count(*) from leads') leads, $(docker exec crm-db psql -U crm -d crm -tAc 'select count(*) from users') logins."
  fi
fi

# ---------- CRM + HTTPS ----------
say "Subindo o CRM e o HTTPS (a primeira vez demora uns minutos)"
$DC up -d --build app caddy
# coloca o Evolution na mesma rede interna do CRM (webhook direto em http://crm-app:8000)
docker network connect crm_default "$EVO" 2>/dev/null || true
for _ in $(seq 1 60); do
  docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 && break
  sleep 3
done
docker exec crm-app python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" >/dev/null 2>&1 || { $DC logs --tail 40 app; fail "o CRM não respondeu"; }

say "Ligando $CRM_DOMAIN ao cliente '$CRM_TENANT' e atualizando os WhatsApps"
docker exec -i crm-app python - <<PY
from app.db import SessionLocal
from app.models import Tenant, WhatsAppNumber
from app.services import evolution_client
db = SessionLocal()
tenant = db.query(Tenant).filter(Tenant.subdomain == "$CRM_TENANT").first()
if tenant:
    tenant.custom_domain = "$CRM_DOMAIN"
    db.commit()
    print("Cliente encontrado:", tenant.name)
else:
    print("ATENÇÃO: cliente '$CRM_TENANT' não existe neste banco (crie com scripts/novo_cliente.py).")
# só os números ativos (os removidos na tela WhatsApp já foram apagados no Evolution)
for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == "evolution", WhatsAppNumber.is_active.is_(True)).all():
    try:
        evolution_client.set_webhook(n.evolution_instance)
        print("WhatsApp", n.label, "-> webhook atualizado")
    except Exception as exc:
        if "does not exist" in str(exc):
            print("WhatsApp", n.label, "-> ainda não conectado (o QR code da tela WhatsApp resolve)")
        else:
            print("WhatsApp", n.label, "-> falhou:", exc)
PY

# ---------- backup diário ----------
mkdir -p /root/crm-backups
cat > /etc/cron.d/crm-backup <<'CRON'
# backup diário do banco do CRM às 3h; guarda 14 dias
0 3 * * * root docker exec crm-db pg_dump -U crm crm | gzip > /root/crm-backups/crm-$(date +\%F).sql.gz && find /root/crm-backups -name 'crm-*.sql.gz' -mtime +14 -delete
CRON

IP_DASH=${BASE_DOMAIN%.sslip.io}
say "Pronto!"
echo "  CRM:      https://$CRM_DOMAIN/login   (precisa do registro DNS 'crm' apontando pra este servidor)"
echo "  Reserva:  https://$CRM_TENANT.$BASE_DOMAIN/login   (funciona sem mexer em DNS)"
echo "  Backups:  /root/crm-backups (todo dia às 3h)"
echo "  Config:   $ENV_FILE (senhas — não compartilhe)"
