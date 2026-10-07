from __future__ import annotations

import datetime
import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


class Tenant(Base):
    """Um cliente da agência. Cada tenant tem seus próprios usuários, leads e números."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    subdomain: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    # domínio próprio do cliente (ex: crm.novaviseu.com.br), apontado pro nosso servidor; vazio = só subdomínio
    custom_domain: Mapped[str] = mapped_column(String(253), default="", index=True)
    # identidade visual do painel (chave de app/themes.py, ex: "junta", "novaviseu")
    theme: Mapped[str] = mapped_column(String(40), default="junta")
    # segredo do link de convite (/convite/{token}) pra vendedor criar o próprio login; vazio = sem link
    invite_token: Mapped[str] = mapped_column(String(40), default="", index=True)
    # contas de anúncio do Meta deste cliente ("123,456", sem o act_): descobertas sozinhas pelos
    # anúncios dos leads, ou preenchidas no Dashboard. Os gastos são buscados dessas contas.
    meta_ad_accounts: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    leads: Mapped[list["Lead"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    stages: Mapped[list["PipelineStage"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    whatsapp_numbers: Mapped[list["WhatsAppNumber"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    """Atendente ou admin dentro de um tenant."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="agent")  # admin | agent
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # pediu acesso pela tela de login e ainda espera um admin aprovar (fica is_active=False até lá)
    pending_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    # a partir de quando contam as "não lidas" desta pessoa (1ª vez que abriu o Inbox com essa função)
    inbox_seen_from: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # removido da equipe (saiu da empresa): some da tela Equipe, não entra, sai do rodízio.
    # O cadastro fica (histórico de quem atendeu/enviou), e dá pra restaurar em "Removidos".
    removed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    accepting_leads: Mapped[bool] = mapped_column(Boolean, default=True)  # pausa individual na fila
    max_open_leads: Mapped[int] = mapped_column(Integer, default=0)  # 0 = sem limite
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class WhatsAppNumber(Base):
    """Um número de WhatsApp conectado ao tenant. Dois tipos (`provider`):
    - "cloud_api": API oficial da Meta (usa waba_phone_number_id + access_token)
    - "evolution": Evolution API não-oficial, pareada por QR code (usa evolution_instance);
      o número continua funcionando no app nativo do celular ao mesmo tempo."""

    __tablename__ = "whatsapp_numbers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(String(30))  # E.164, ex: +5511999999999
    waba_phone_number_id: Mapped[str] = mapped_column(String(60), default="")  # id do número na Cloud API
    waba_business_account_id: Mapped[str] = mapped_column(String(60), default="")
    access_token: Mapped[str] = mapped_column(Text, default="")  # token do sistema/app da Meta
    verify_token: Mapped[str] = mapped_column(String(120), default="")  # usado no handshake do webhook
    provider: Mapped[str] = mapped_column(String(20), default="cloud_api")  # cloud_api | evolution
    evolution_instance: Mapped[str] = mapped_column(String(80), default="", index=True)
    # último estado conhecido da conexão (Evolution): open | connecting | close
    connection_state: Mapped[str] = mapped_column(String(20), default="")
    # vendedor dono do número (o WhatsApp do celular dele). Lead que chega nesse
    # número vai direto pra ele; vazio = número compartilhado, entra no rodízio.
    owner_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    # último clique do link rotativo (/go/{tenant}) mandado pra este número
    last_routed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    tenant: Mapped["Tenant"] = relationship(back_populates="whatsapp_numbers")
    owner: Mapped[Optional["User"]] = relationship()


class PipelineStage(Base):
    """Etapa do funil comercial (ex: Novo, Em atendimento, Qualificado, Ganho, Perdido)."""

    __tablename__ = "pipeline_stages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    order: Mapped[int] = mapped_column(Integer, default=0)
    is_won: Mapped[bool] = mapped_column(Boolean, default=False)  # etapa terminal de ganho
    is_lost: Mapped[bool] = mapped_column(Boolean, default=False)  # etapa terminal de perda
    # nome do evento a disparar pro Meta/Google quando um lead ENTRA nesta etapa
    # (ex: "Lead", "Qualified", "Purchase"). Vazio = não dispara nada nesta etapa.
    conversion_event_name: Mapped[str] = mapped_column(String(60), default="")

    tenant: Mapped["Tenant"] = relationship(back_populates="stages")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(30), default="", index=True)
    email: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(30), default="site_form")  # whatsapp | site_form
    stage_id: Mapped[Optional[str]] = mapped_column(ForeignKey("pipeline_stages.id"), nullable=True, index=True)
    assigned_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    whatsapp_number_id: Mapped[Optional[str]] = mapped_column(ForeignKey("whatsapp_numbers.id"), nullable=True)
    deal_value: Mapped[Optional[float]] = mapped_column(nullable=True)  # valor fechado, preenchido na etapa "Ganho"
    loss_reason: Mapped[str] = mapped_column(String(255), default="")  # motivo, preenchido na etapa "Perdido"
    # etiqueta do contato: "" = lead normal | "cliente" = já é cliente | "outro" = não é venda
    # (fornecedor, conhecido...): "outro" sai do Pipeline e das métricas, a conversa continua no Inbox
    tag: Mapped[str] = mapped_column(String(20), default="", index=True)
    # grupo de WhatsApp (phone = "<id>@g.us"): só aparece no Inbox — não é lead, não entra
    # no Pipeline, Dashboard, rodízio nem em "Não respondidas"
    is_group: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # tempos de atendimento (preenchidos a cada mensagem; leads antigos calculados no start):
    # 1º atendimento = first_response_at - created_at; "sem interação" = agora - last_outbound_at
    first_response_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_inbound_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_outbound_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, index=True)
    # "✓ Encerrar atendimento" (ou o vendedor reagiu à mensagem do cliente pelo celular): o que o
    # cliente mandou até aqui não precisa de resposta, então não conta como "aguardando"
    settled_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    settled_by_user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # última vez que o vendedor do lead viu a conversa (abriu no CRM ou leu no celular)
    seen_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    # o que a pessoa preencheu nos formulários (LP/site), um bloco por envio, mais recente primeiro
    form_details: Mapped[str] = mapped_column(Text, default="")
    # foto de perfil do WhatsApp (URL temporária do WhatsApp; renovada a cada 24h em /leads/{id}/avatar)
    avatar_url: Mapped[str] = mapped_column(String(1000), default="")
    avatar_checked_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    # arquivado = some do Pipeline (continua no banco, no Dashboard e em /arquivados).
    # Automático (app/services/archiver.py) ou manual; volta sozinho se o cliente mandar mensagem.
    archived_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, index=True)
    # excluído = vai pra Lixeira (some de tudo, inclusive Dashboard). Nada é apagado de verdade:
    # a Lixeira mostra quem excluiu e quando, e o admin pode restaurar.
    deleted_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="leads")
    stage: Mapped[Optional["PipelineStage"]] = relationship()
    assigned_user: Mapped[Optional["User"]] = relationship(foreign_keys=[assigned_user_id])
    deleted_by: Mapped[Optional["User"]] = relationship(foreign_keys=[deleted_by_user_id])
    attribution: Mapped[Optional["UtmAttribution"]] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    # uma conversa por número de WhatsApp com que o lead falou; ordenadas da
    # menos pra mais recente (a última é a "ativa", por onde a resposta sai)
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Conversation.last_message_at"
    )

    @property
    def active_conversation(self) -> Optional["Conversation"]:
        return self.conversations[-1] if self.conversations else None

    @property
    def all_messages(self) -> list["Message"]:
        """Histórico completo, juntando todos os números por onde a conversa passou."""
        return sorted((m for c in self.conversations for m in c.messages), key=lambda m: m.created_at)


class UtmAttribution(Base):
    """Origem do lead: UTMs de site, ou dados de anúncio (Meta CTWA / ponte de Google Ads)."""

    __tablename__ = "utm_attributions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), unique=True)

    utm_source: Mapped[str] = mapped_column(String(120), default="")
    utm_medium: Mapped[str] = mapped_column(String(120), default="")
    utm_campaign: Mapped[str] = mapped_column(String(160), default="")
    utm_content: Mapped[str] = mapped_column(String(160), default="")
    utm_term: Mapped[str] = mapped_column(String(160), default="")

    # Meta: clique em anúncio "Clique para WhatsApp" (CTWA) — vem no webhook da Cloud API
    ctwa_clid: Mapped[str] = mapped_column(String(255), default="")
    ad_id: Mapped[str] = mapped_column(String(60), default="")
    ad_source_url: Mapped[str] = mapped_column(String(500), default="")
    ad_headline: Mapped[str] = mapped_column(String(255), default="")

    # Meta: leads de formulário do site / navegação
    fbclid: Mapped[str] = mapped_column(String(255), default="")
    fbc: Mapped[str] = mapped_column(String(255), default="")
    fbp: Mapped[str] = mapped_column(String(255), default="")

    # Google Ads
    gclid: Mapped[str] = mapped_column(String(255), default="")

    # nosso código curto embutido na mensagem pré-preenchida do wa.me (ponte Google Ads -> WhatsApp)
    tracking_code: Mapped[str] = mapped_column(String(20), default="", index=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    lead: Mapped["Lead"] = relationship(back_populates="attribution")


class ClickBridge(Base):
    """Registro temporário criado quando alguém clica num anúncio do Google Ads e é
    redirecionado para o WhatsApp. Guardamos o gclid + UTMs aqui, associados a um
    tracking_code curto embutido na mensagem pré-preenchida do wa.me. Quando a
    mensagem chega no webhook do WhatsApp, casamos pelo tracking_code."""

    __tablename__ = "click_bridges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    tracking_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    gclid: Mapped[str] = mapped_column(String(255), default="")
    fbclid: Mapped[str] = mapped_column(String(255), default="")
    utm_source: Mapped[str] = mapped_column(String(120), default="")
    utm_medium: Mapped[str] = mapped_column(String(120), default="")
    utm_campaign: Mapped[str] = mapped_column(String(160), default="")
    utm_content: Mapped[str] = mapped_column(String(160), default="")
    utm_term: Mapped[str] = mapped_column(String(160), default="")
    destination_phone: Mapped[str] = mapped_column(String(30), default="")
    # telefone que a pessoa digitou no formulário da LP (só dígitos, com 55). Plano B pra
    # casar a origem se ela apagar o código de rastreio da mensagem antes de enviar.
    lead_phone: Mapped[str] = mapped_column(String(30), default="", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    consumed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    whatsapp_number_id: Mapped[str] = mapped_column(ForeignKey("whatsapp_numbers.id"))
    assigned_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | closed
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, index=True)
    # texto da última mensagem, pra lista do Inbox não precisar abrir todas as mensagens
    last_preview: Mapped[str] = mapped_column(String(200), default="")

    lead: Mapped["Lead"] = relationship(back_populates="conversations")
    whatsapp_number: Mapped["WhatsAppNumber"] = relationship()
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # in | out
    sender_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    wa_message_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str] = mapped_column(String(500), default="")
    # id de mídia da Cloud API (áudio/imagem/documento) — a URL da Meta expira em minutos,
    # então guardamos o id e buscamos os bytes sob demanda via /media/{message_id}
    media_id: Mapped[str] = mapped_column(String(120), default="")
    media_type: Mapped[str] = mapped_column(String(30), default="")  # audio | image | video | document | sticker
    # chave da cópia própria da mídia (app/services/media_store.py); vazio = não copiada
    media_stored_key: Mapped[str] = mapped_column(String(160), default="")
    # messageSecret do WhatsApp (base64): necessário pra abrir uma edição futura desta mensagem
    secret: Mapped[str] = mapped_column(String(100), default="")
    # em grupo: quem mandou (nome no WhatsApp)
    sender_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class ConversionEvent(Base):
    """Evento de conversão enviado (ou pendente de envio) para Meta CAPI / Google Ads."""

    __tablename__ = "conversion_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))  # meta | google
    event_name: Mapped[str] = mapped_column(String(60))  # Lead | Qualified | Purchase
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed | skipped
    payload_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    sent_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)


class CampaignSpend(Base):
    """Gasto de anúncio por dia, importado de uma fonte externa (Meta Ads,
    Google Ads, Windsor.ai, planilha manual, etc). Casamos com os leads pelo
    nome de campanha/conjunto/anúncio (utm_campaign/utm_term/utm_content) —
    não é um join perfeito por ID, mas é o suficiente pra calcular CPL, custo
    por qualificado, CAC e ticket médio sem depender de nenhuma integração
    específica. Ainda não tem nenhuma fonte plugada — a estrutura só está
    pronta pra receber os dados assim que decidirmos por onde importar
    (Windsor.ai é uma opção já disponível neste ambiente, mas precisa
    conectar as contas de anúncio reais dos clientes primeiro)."""

    __tablename__ = "campaign_spend"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))  # google | meta | bing | tiktok
    campaign: Mapped[str] = mapped_column(String(160), default="")
    adset: Mapped[str] = mapped_column(String(160), default="")  # conjunto (Meta) / grupo de anúncios (Google)
    ad: Mapped[str] = mapped_column(String(160), default="")  # anúncio/criativo
    date: Mapped[datetime.date] = mapped_column(Date)
    spend: Mapped[float] = mapped_column(default=0.0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(40), default="manual")  # manual | windsor_ai | meta_api | google_ads_api
    # ids do Meta (vazios em importação manual): o lead de anúncio traz o ad_id, então o gasto
    # casa com o lead pelo id mesmo se a campanha for renomeada
    campaign_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    adset_id: Mapped[str] = mapped_column(String(40), default="")
    ad_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class InboxRead(Base):
    """Até quando cada pessoa já leu cada conversa (não lidas são por pessoa)."""

    __tablename__ = "inbox_reads"
    __table_args__ = (UniqueConstraint("user_id", "lead_id", name="uq_inbox_read_user_lead"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), index=True)
    last_read_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    manual_unread: Mapped[bool] = mapped_column(Boolean, default=False)  # "Marcar como não lida"
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)  # ⭐ favoritas (de cada pessoa)


class QuickReply(Base):
    """Resposta rápida: mensagem pronta que aparece ao digitar "/" na conversa.
    shared=True vale pra equipe toda; senão só pra quem criou."""

    __tablename__ = "quick_replies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    shortcut: Mapped[str] = mapped_column(String(40), default="")  # o que se digita depois da "/" (ex: pix)
    body: Mapped[str] = mapped_column(Text, default="")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    created_by: Mapped[Optional["User"]] = relationship()
