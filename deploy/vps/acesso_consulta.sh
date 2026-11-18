#!/usr/bin/env bash
# 🔎 Acesso SÓ DE LEITURA ao banco do CRM pra uma pessoa de confiança do time (ou o Claude dela).
#
# Cria no VPS:
#   - usuário Linux "consulta" que entra só por chave SSH e só consegue rodar consultas no banco
#     (sem terminal, sem docker, sem mexer em nada do servidor);
#   - usuário "consulta" no banco do CRM, só leitura: SELECT, sem senhas/tokens (as colunas secretas
#     ficam escondidas), consulta que passa de 60 s é cancelada.
#
# Rodar no terminal do VPS (como root):
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/acesso_consulta.sh)
#   (pede a chave PÚBLICA da pessoa, a que termina em .pub; nunca a privada)
# Tirar o acesso:
#   bash <(curl -fsSL https://raw.githubusercontent.com/otaldoicaro/crm-wpp/main/deploy/vps/acesso_consulta.sh) --remover
#
# Do lado da pessoa (depois de liberado):
#   ssh -i ~/.ssh/crm_consulta consulta@<IP do VPS> "select count(*) from leads"
set -euo pipefail

say() { printf "\n\033[1;33m==> %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31mERRO: %s\033[0m\n" "$*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
docker ps --format '{{.Names}}' | grep -qx crm-db || fail "banco do CRM (crm-db) não está rodando"

LINUX_USER=consulta
DB_ROLE=consulta
WRAPPER=/usr/local/bin/crm-consulta          # o que roda quando a pessoa entra por SSH
RUNNER=/usr/local/sbin/crm-consulta-sql      # roda a consulta no banco (único comando liberado no sudo)
SUDOERS=/etc/sudoers.d/crm-consulta
PSQL="docker exec -i crm-db psql -U crm -d crm -v ON_ERROR_STOP=1 -q"

if [ "${1:-}" = "--remover" ]; then
  say "Tirando o acesso de consulta"
  pkill -u "$LINUX_USER" 2>/dev/null || true
  id "$LINUX_USER" >/dev/null 2>&1 && userdel -r "$LINUX_USER" 2>/dev/null || true
  rm -f "$WRAPPER" "$RUNNER" "$SUDOERS"
  $PSQL <<'SQL' || true
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'consulta';
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'consulta') THEN
    EXECUTE 'DROP OWNED BY consulta';
    EXECUTE 'DROP ROLE consulta';
  END IF;
END $$;
SQL
  echo "Pronto: o acesso de consulta foi removido (usuário, chave e permissão no banco)."
  exit 0
fi

say "Chave PÚBLICA da pessoa"
echo "Cole a linha inteira do arquivo .pub (começa com ssh-ed25519) e aperte Enter."
echo "Nunca cole a chave privada (a que não termina em .pub)."
read -r PUBKEY
case "$PUBKEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*\ *) ;;
  *) fail "isso não parece uma chave pública (tem que começar com ssh-ed25519). Nada foi feito." ;;
esac
echo "$PUBKEY" | grep -q "PRIVATE" && fail "isso é uma chave PRIVADA. Nada foi feito; peça pra pessoa gerar outra."

say "Usuário 'consulta' no banco do CRM (só leitura, sem senhas e tokens)"
$PSQL <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'consulta') THEN
    CREATE ROLE consulta LOGIN CONNECTION LIMIT 3;
  END IF;
END $$;
ALTER ROLE consulta NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 3;
ALTER ROLE consulta SET default_transaction_read_only = on;
ALTER ROLE consulta SET statement_timeout = '60s';
GRANT CONNECT ON DATABASE crm TO consulta;
GRANT USAGE ON SCHEMA public TO consulta;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO consulta;
ALTER DEFAULT PRIVILEGES FOR ROLE crm IN SCHEMA public GRANT SELECT ON TABLES TO consulta;
-- tabelas com senha/token: a pessoa lê todas as colunas MENOS essas (permissão por coluna, que não
-- atrapalha as atualizações do CRM). Nessas tabelas "select *" não funciona: liste as colunas.
DO $$
DECLARE
  secret text[] := ARRAY['tenants.invite_token', 'tenants.meta_capi_token', 'tenants.meta_wa_token',
                         'users.password_hash', 'whatsapp_numbers.access_token', 'whatsapp_numbers.verify_token',
                         'messages.secret'];
  t text;
  cols text;
BEGIN
  FOR t IN SELECT DISTINCT split_part(s, '.', 1) FROM unnest(secret) AS s LOOP
    IF to_regclass('public.' || t) IS NULL THEN CONTINUE; END IF;
    EXECUTE format('REVOKE ALL ON public.%I FROM consulta', t);
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = t AND NOT ((t || '.' || column_name) = ANY (secret));
    EXECUTE format('GRANT SELECT (%s) ON public.%I TO consulta', cols, t);
  END LOOP;
END $$;
SQL

say "Comandos de consulta"
cat > "$RUNNER" <<'SH'
#!/usr/bin/env bash
# roda UMA consulta (recebida pela entrada) no banco do CRM como o usuário "consulta" (só leitura)
set -euo pipefail
SQL=$(head -c 20001)
[ -n "${SQL//[[:space:]]/}" ] || { echo "mande a consulta SQL. Ex: select count(*) from leads"; exit 1; }
[ "${#SQL}" -le 20000 ] || { echo "consulta grande demais (máx. 20 mil caracteres)"; exit 1; }
case "$SQL" in *\\*) echo "comandos com barra invertida (\\) não são permitidos: use só SQL"; exit 1 ;; esac
FORMAT=()
case "$SQL" in --csv*) FORMAT=(--csv); SQL=${SQL#--csv} ;; esac
exec docker exec crm-db psql -U consulta -d crm -X -q -P pager=off -v ON_ERROR_STOP=1 "${FORMAT[@]}" -c "$SQL"
SH
chmod 755 "$RUNNER"
chown root:root "$RUNNER"

cat > "$WRAPPER" <<SH
#!/usr/bin/env bash
# o que roda quando "consulta" entra por SSH: a consulta vem no próprio comando do ssh ou pela entrada
if [ -n "\${SSH_ORIGINAL_COMMAND:-}" ]; then
  printf '%s' "\$SSH_ORIGINAL_COMMAND" | sudo -n $RUNNER
else
  sudo -n $RUNNER
fi
SH
chmod 755 "$WRAPPER"
chown root:root "$WRAPPER"

echo "$LINUX_USER ALL=(root) NOPASSWD: $RUNNER" > "$SUDOERS.tmp"
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || { rm -f "$SUDOERS.tmp"; fail "regra do sudo inválida"; }
mv "$SUDOERS.tmp" "$SUDOERS"

say "Usuário 'consulta' no servidor (entra só por chave, sem terminal)"
id "$LINUX_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$LINUX_USER"
passwd -l "$LINUX_USER" >/dev/null
HOME_DIR=$(getent passwd "$LINUX_USER" | cut -d: -f6)
install -d -m 700 -o "$LINUX_USER" -g "$LINUX_USER" "$HOME_DIR/.ssh"
echo "command=\"$WRAPPER\",restrict $PUBKEY" > "$HOME_DIR/.ssh/authorized_keys"
chown "$LINUX_USER:$LINUX_USER" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"
if sshd -T 2>/dev/null | grep -qiE "^(allowusers|allowgroups) "; then
  echo "Aviso: o SSH deste servidor só aceita alguns usuários (AllowUsers/AllowGroups). Fale com o Claude."
fi

say "Conferindo"
echo "select 'ok' as acesso, count(*) as leads from leads" | sudo -u "$LINUX_USER" sudo -n "$RUNNER"
if echo "select password_hash from users limit 1" | sudo -u "$LINUX_USER" sudo -n "$RUNNER" >/dev/null 2>&1; then
  fail "a coluna de senha ficou visível; o acesso NÃO está seguro. Rode de novo com --remover e fale com o Claude."
fi
if echo "create table teste_consulta (x int)" | sudo -u "$LINUX_USER" sudo -n "$RUNNER" >/dev/null 2>&1; then
  fail "o usuário conseguiu criar tabela; o acesso NÃO é só leitura. Rode de novo com --remover e fale com o Claude."
fi
IP=$(curl -fsS -4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Pronto: acesso só de leitura liberado (senhas e tokens escondidos, nada pode ser alterado)."
echo "A pessoa testa no computador dela com:"
echo "   ssh -i ~/.ssh/crm_consulta consulta@$IP \"select count(*) from leads\""
