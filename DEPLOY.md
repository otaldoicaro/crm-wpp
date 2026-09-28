# Publicar o CRM de teste (Render + subdomínio na Hostinger)

Mesmo esquema que vocês já usam no Quiz Junta: Render tem plano grátis que roda
um servidor de verdade (diferente do Netlify, que só serve arquivo estático).

**Aviso importante:** no plano grátis do Render, o disco do servidor é
"ephemeral" — some não só em deploy, mas toda vez que o serviço "dorme" por
inatividade e acorda de novo (o que acontece sozinho, sem avisar). Por isso
os dados de teste (SQLite) ficam sumindo. A seção "3b" abaixo resolve isso
ligando um banco Postgres separado (também grátis), que não some. **Ainda
não guarde dados reais de cliente nesse ambiente** — quando formos pra
produção de verdade, o Postgres vira pago (ou migra pra um Postgres do
Hostinger VPS), junto com o upgrade do servidor pra não dormir mais.

## 1. Código no GitHub (privado)
1. github.com → **New repository** → `crm-junta` → **Private** → Create.
2. No terminal, dentro da pasta `crm/`:
```bash
cd crm
git init
git add .
git commit -m "CRM multi-tenant WhatsApp + site"
git branch -M main
git remote add origin https://github.com/SEU-USUARIO/crm-junta.git
git push -u origin main
```

## 2. Render (grátis)
1. render.com → entre com o GitHub → **New +** → **Blueprint** → escolha `crm-junta` → **Apply**.
   (O `render.yaml` já configura Docker + plano free + healthcheck.)
2. Nas variáveis de ambiente pedidas pelo Render, preencha AGORA só o essencial pro teste:
   - `BASE_DOMAIN`: o domínio que o Render vai te dar antes de configurar subdomínio próprio (pode deixar em branco por enquanto, ou já colocar `crm-junta-xxxx.onrender.com` depois que souber a URL).
   - `DATABASE_URL`: pode deixar em branco por enquanto (usa SQLite, ok pra teste).
   - `META_WEBHOOK_VERIFY_TOKEN`: invente uma string qualquer (ex: `junta-verify-2024`) — vamos usar o MESMO valor na configuração do webhook lá na Meta.
   - Todo o resto (Google Ads, Meta CAPI, etc.) pode ficar em branco por enquanto — só precisamos deles mais pra frente.
3. Espere ficar **Live** e copie a URL (`https://crm-junta-xxxx.onrender.com`).

## 3b. Ligar um banco Postgres grátis (pra parar de perder os dados de teste)
1. render.com → **New +** → **PostgreSQL** → nome `crm-junta-db` → plano **Free** → **Create Database**.
2. Espere ficar disponível, entre na página do banco, copie o campo **"Internal Database URL"**.
3. Vai na página do serviço `crm-junta` → **Environment** → edita a variável `DATABASE_URL` → cola essa URL → **Save Changes**.
4. O serviço reinicia sozinho. A partir daqui os dados sobrevivem a "sono"/restart do servidor (só não sobrevivem se você apagar o banco).

## 3. Registrar o número de teste no CRM
Depois que o serviço estiver no ar, me avisa a URL que o Render gerou — eu te
passo o comando (via `python -c` no shell do próprio Render, ou eu ajusto o
`seed.py`) pra registrar o número de teste da Meta (token + phone_number_id +
waba_id que você já me passou) como um `WhatsAppNumber` de um tenant de teste.

## 4. Configurar o Webhook na Meta
No app da Meta → dentro de "Conectar no WhatsApp" → **Configuração da API**
(mesma tela de antes) → seção **"Etapa 3: Configure webhooks para receber
mensagens"**:
- **Callback URL**: `https://crm-junta-xxxx.onrender.com/webhooks/whatsapp`
- **Verify token**: o mesmo valor que você colocou em `META_WEBHOOK_VERIFY_TOKEN` no Render
- Marcar o campo **"messages"** na lista de campos do webhook

## 5. Testar de verdade
Manda uma mensagem do seu WhatsApp pro número de teste. Ela deve aparecer
no pipeline do CRM em segundos. Esse é o teste que valida o fluxo inteiro.

## Depois do teste
Pra não deixar rodando à toa: Render → serviço → **Settings → Delete Web
Service** (ou deixa no ar mesmo, sem custo, se quiser continuar testando).
