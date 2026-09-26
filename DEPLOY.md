# Publicar o CRM de teste (Render + subdomínio na Hostinger)

Mesmo esquema que vocês já usam no Quiz Junta: Render tem plano grátis que roda
um servidor de verdade (diferente do Netlify, que só serve arquivo estático).

**Aviso importante pro teste de agora:** no plano grátis do Render, o disco é
"ephemeral" — some quando o serviço reinicia/redeploy. Isso é totalmente OK
pra só validar o webhook do WhatsApp e a atribuição de UTM (que é o objetivo
agora), mas **não guarde dados reais de cliente nesse ambiente**. Quando
formos pra produção de verdade, trocamos `DATABASE_URL` por um Postgres
persistente (o próprio Render tem, ou pode ser o Postgres do Hostinger VPS).

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
