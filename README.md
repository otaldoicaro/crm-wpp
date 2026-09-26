# CRM WhatsApp + Site (multi-tenant)

CRM próprio para distribuir leads de WhatsApp entre atendentes, receber leads de
formulário de site, conversar com os leads de WhatsApp por dentro da plataforma,
e devolver eventos de conversão pro Meta Ads e Google Ads (com a origem/UTM de
cada lead) para otimizar campanhas.

Multi-tenant: cada cliente tem seu próprio subdomínio (`clientea.seudominio.com.br`),
login isolado, e dados isolados no banco por `tenant_id`. Sem cobrança dentro do
sistema — isso fica fora (nota fiscal via boleto), como combinado.

## Stack e por quê

Python + FastAPI + SQLAlchemy + SQLite (dev) / PostgreSQL (produção), templates
Jinja2 (sem build de frontend). Essa escolha foi deliberada: bate com o padrão que
vocês já usam (o Quiz Junta também é um servidor Python simples publicado via
Docker + Render + subdomínio na Hostinger) e não depende de Node/Docker
instalados localmente para eu desenvolver e testar de verdade a cada passo.
Se preferir migrar para Next.js/Node no futuro, a lógica de negócio (distribuição,
atribuição, disparo de conversão) está isolada em `app/services/` e é portável.

## Rodando localmente

```bash
cd crm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python seed.py                # cria tenant "demo" + 2 usuários + pipeline
uvicorn app.main:app --reload --port 8000
```

Acesse `http://demo.localhost:8000/login` (o `*.localhost` já resolve pra
127.0.0.1 sozinho, sem mexer no `/etc/hosts`). Login: `admin@demo.com` / `demo1234`.

## O que já está implementado e testado

- **Multi-tenant real** por subdomínio, com login/senha isolado por cliente (`app/tenancy.py`, `app/deps.py`).
- **Pipeline de leads** (kanban simples) com etapas configuráveis por tenant (`app/routers/dashboard.py`).
- **Webhook de formulário de site** (`POST /webhooks/site-form/{tenant_id}`) que cria o lead e já captura UTMs/gclid/fbclid vindos da URL da página.
- **Webhook único do WhatsApp Cloud API** (`GET`/`POST /webhooks/whatsapp`) que recebe mensagens de QUALQUER número de QUALQUER tenant, identifica o número pelo `phone_number_id` do payload, cria o lead e a conversa, e guarda cada mensagem.
- **Distribuição automática de leads** entre atendentes por menor carga de leads em aberto (`app/services/distribution.py`) — fácil de trocar a regra depois (por horário, por número de origem, etc).
- **Captura de origem**:
  - Anúncio "Clique para WhatsApp" da Meta → objeto `referral` do próprio webhook (nativo, sem gambiarra).
  - Google Ads (URL final do anúncio vai direto pro WhatsApp, sem passar pelo site) → landing-ponte (`GET /go/{tenant_id}/{whatsapp_number_id}`) que guarda o `gclid`/UTMs e embute um código curto na mensagem pré-preenchida do `wa.me`, casado depois no webhook.
  - Botão de WhatsApp num site/landing page que já recebe tráfego pago (Google Ads, Facebook feed, etc.) → mesma landing-ponte, mas acionada pelo script `static-snippets/whatsapp-bridge.js` (ver seção abaixo), que repassa o `gclid`/UTMs já presentes na URL da página.
  - Formulário de site → UTMs/fbclid/gclid direto no POST.
- **Disparo de conversão** pra Meta Conversions API e Google Ads (click conversion upload) quando o lead entra numa etapa marcada com `conversion_event_name` (ex: "Qualified", "Purchase") — `app/services/conversions/`.
- **Inbox dentro do CRM**: o atendente responde o lead de WhatsApp sem sair da plataforma (`app/routers/dashboard.py::reply_lead`, via `app/services/whatsapp_client.py`).

Tudo isso já rodou de ponta a ponta neste ambiente (login, lead de site, lead de
WhatsApp com dado de anúncio, distribuição entre 2 atendentes, troca de etapa).
O que falta pra funcionar com dados reais são as credenciais abaixo — a lógica já
está pronta pra recebê-las.

## Botão de WhatsApp no site do cliente (rastreando UTM/gclid)

Quando o lead já cai numa landing page/site (ex: vindo do Google Ads, que
anexa `gclid` e UTMs na URL da página) e ali tem um botão "Fale no WhatsApp",
esse botão não deve apontar direto pra `wa.me` — ele precisa passar pela nossa
landing-ponte pra registrar a origem antes de redirecionar. Use o script
`static-snippets/whatsapp-bridge.js`:

```html
<a href="#" class="wa-bridge-btn"
   data-tenant="SEU_TENANT_ID"
   data-number="SEU_WHATSAPP_NUMBER_ID"
   data-text="Quero saber mais!">
  Fale no WhatsApp
</a>
<script src="https://SEU_DOMINIO_DO_CRM/static/whatsapp-bridge.js"></script>
```

O script lê `gclid`/`utm_*` da URL atual, guarda em `sessionStorage` (pra não
perder se a pessoa navegar pra outra página do site antes de clicar), e
reescreve o `href` do botão pra passar pela nossa ponte antes do WhatsApp.
Funciona pra Google Ads e também pra qualquer outro tráfego pago que chegue
no site com UTMs (ex: um anúncio do Facebook que leva pro site, não direto pro WhatsApp).

## O que falta você me passar (nenhuma delas eu posso gerar sozinho)

### 1. WhatsApp Cloud API (obrigatório pra ligar o WhatsApp de verdade)
Você **não precisa** começar com o WABA definitivo do cliente. Fluxo recomendado:
1. Crie um App em [developers.facebook.com](https://developers.facebook.com) (usando o Business Manager que vocês já têm) e adicione o produto "WhatsApp".
2. A Meta libera automaticamente um **número de teste gratuito** (até 5 destinatários, sem verificação de negócio) — dá pra testar TODO o fluxo (webhook, distribuição, resposta pelo CRM) com ele.
3. Quando o cliente aprovar, você adiciona o número real de produção dele na mesma WABA (ou pede pra portar o número atual do cliente pra Cloud API — só não pode estar ativo no app comum do WhatsApp/WhatsApp Business ao mesmo tempo). Não precisa reescrever nada, só troca o registro `WhatsAppNumber` no banco.
4. Me passa: `access token` (temporário pra teste, depois um token de sistema permanente), `phone_number_id`, e configura o webhook apontando pro nosso servidor com o `META_WEBHOOK_VERIFY_TOKEN` do `.env`.

### 2. Meta Conversions API (pra devolver conversão pro Meta Ads)
No Gerenciador de Eventos do Business Manager → Conversions API → gerar um
token de acesso do sistema. Preciso do `META_PIXEL_ID` e desse token.

### 2b. Nome de campanha em leads de anúncio "Clique para WhatsApp" (opcional)
O webhook só entrega o `ad_id`. Pra mostrar o NOME da campanha/conjunto/anúncio
(`utm_campaign`/`utm_content`/`utm_term`) em vez de só o ID, preciso de um
token separado com escopo `ads_read` sobre a conta de anúncios (Business
Settings → Usuários do sistema → gerar token com permissão `ads_read`) →
`META_ADS_ACCESS_TOKEN` no `.env`. Sem ele, a atribuição de CTWA continua
funcionando, só fica sem o nome legível da campanha.

### 3. Google Ads (pra devolver conversão pro Google Ads)
- Criar uma "ação de conversão" do tipo importação/clique no Google Ads (você tem acesso à conta, então dá pra fazer isso quando quiser).
- Um "developer token" da conta de gerenciador (MCC) — se a agência não tem MCC ainda, é o primeiro passo, e a aprovação do token pode levar alguns dias.
- Credenciais OAuth2 (client id/secret + refresh token) — gero um roteiro de como tirar isso quando chegarmos nessa etapa.

### 4. Hospedagem definitiva
Pra testar já, dá pra subir num serviço tipo Render/Railway (grátis, mesmo
padrão que vocês usam no Quiz Junta) e apontar um subdomínio de teste na
Hostinger. Pra produção com múltiplos clientes em `crm.nomedocliente.com.br`,
o requisito é: **um servidor que rode Docker/Python continuamente** (não é
hospedagem compartilhada de site comum) + um DNS coringa (`*.suaplataforma.com.br`
ou um CNAME por cliente) apontando pra ele. Quando você tiver decidido onde (Hostinger VPS, Railway, um droplet), eu ajusto o deploy.

## Roadmap (próximos passos, nessa ordem)

1. **Você cria o App de teste na Meta + número de teste** → eu conecto o webhook de verdade e testamos uma conversa real de ponta a ponta.
2. **Ajustar o pipeline/etapas por cliente** conforme o funil comercial real (hoje está genérico: Novo → Em atendimento → Qualificado → Ganho/Perdido).
3. **Tela de administração** pra cadastrar atendentes, números de WhatsApp e etapas pela interface (hoje isso só existe via `seed.py`/banco direto).
4. **Conectar Meta CAPI** com o pixel/token reais e validar no Gerenciador de Eventos que a conversão chega com "correspondência de eventos" boa.
5. **Conectar Google Ads** (developer token + ação de conversão) e validar upload de conversão.
6. **Deploy** com subdomínio de teste, depois multi-tenant real por cliente.

Qualquer um desses passos, me chama que eu sigo.
