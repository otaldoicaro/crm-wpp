from __future__ import annotations

import datetime
import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
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
    accepting_leads: Mapped[bool] = mapped_column(Boolean, default=True)  # pausa individual na fila
    max_open_leads: Mapped[int] = mapped_column(Integer, default=0)  # 0 = sem limite
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class WhatsAppNumber(Base):
    """Um número de WhatsApp (Cloud API) conectado ao tenant."""

    __tablename__ = "whatsapp_numbers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(String(30))  # E.164, ex: +5511999999999
    waba_phone_number_id: Mapped[str] = mapped_column(String(60))  # id do número na Cloud API
    waba_business_account_id: Mapped[str] = mapped_column(String(60), default="")
    access_token: Mapped[str] = mapped_column(Text, default="")  # token do sistema/app da Meta
    verify_token: Mapped[str] = mapped_column(String(120), default="")  # usado no handshake do webhook
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    tenant: Mapped["Tenant"] = relationship(back_populates="whatsapp_numbers")


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
    stage_id: Mapped[Optional[str]] = mapped_column(ForeignKey("pipeline_stages.id"), nullable=True)
    assigned_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    whatsapp_number_id: Mapped[Optional[str]] = mapped_column(ForeignKey("whatsapp_numbers.id"), nullable=True)
    deal_value: Mapped[Optional[float]] = mapped_column(nullable=True)  # valor fechado, preenchido na etapa "Ganho"
    loss_reason: Mapped[str] = mapped_column(String(255), default="")  # motivo, preenchido na etapa "Perdido"
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    tenant: Mapped["Tenant"] = relationship(back_populates="leads")
    stage: Mapped[Optional["PipelineStage"]] = relationship()
    assigned_user: Mapped[Optional["User"]] = relationship()
    attribution: Mapped[Optional["UtmAttribution"]] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


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
    utm_source: Mapped[str] = mapped_column(String(120), default="")
    utm_medium: Mapped[str] = mapped_column(String(120), default="")
    utm_campaign: Mapped[str] = mapped_column(String(160), default="")
    utm_content: Mapped[str] = mapped_column(String(160), default="")
    utm_term: Mapped[str] = mapped_column(String(160), default="")
    destination_phone: Mapped[str] = mapped_column(String(30), default="")
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
    last_message_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    lead: Mapped["Lead"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # in | out
    sender_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    wa_message_id: Mapped[str] = mapped_column(String(120), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str] = mapped_column(String(500), default="")
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
