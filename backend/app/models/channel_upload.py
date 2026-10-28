"""Uploads retain ownership independently of GCP environments and remote channel names."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ChannelUpload(Base):
    __tablename__ = "channel_uploads"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id"),
        UniqueConstraint("base_url", "fingerprint"),
        UniqueConstraint("base_url", "remote_channel_id"),
        Index("ux_channel_uploads_source_target", "source_upload_id", "target_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_upload_id: Mapped[str | None] = mapped_column(ForeignKey("channel_uploads.id"), index=True)
    target_id: Mapped[str | None] = mapped_column(ForeignKey("new_api_targets.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    base_url: Mapped[str] = mapped_column(String(2048))
    config_source: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32), default="UPLOAD", server_default="UPLOAD")
    origin_env_id: Mapped[str | None] = mapped_column(String(36), index=True)
    origin_task_id: Mapped[str | None] = mapped_column(String(36))
    proxy_encrypted: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    identity: Mapped[str] = mapped_column(String(512), default="")
    fingerprint: Mapped[str] = mapped_column(String(64))
    credential_encrypted: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    message: Mapped[str] = mapped_column(String(512), default="等待执行")
    remote_channel_id: Mapped[str | None] = mapped_column(String(128))
    remote_status: Mapped[str | None] = mapped_column(String(32))
    used_quota: Mapped[Decimal | None] = mapped_column(Numeric(30, 6))
    quota_per_unit: Mapped[Decimal] = mapped_column(Numeric(30, 6))
    currency: Mapped[str] = mapped_column(String(16))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    sync_error: Mapped[str | None] = mapped_column(String(512))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    target = relationship("NewApiTarget")
    source = relationship("ChannelUpload", remote_side=[id], back_populates="deployments")
    deployments = relationship("ChannelUpload", back_populates="source")


class NewApiTarget(Base):
    __tablename__ = "new_api_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str] = mapped_column(String(2048), unique=True)
    token_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    models: Mapped[str] = mapped_column(String(2048), default="gemini-2.5-flash")
    group: Mapped[str] = mapped_column(String(128), default="default")
    stats_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vertex_region: Mapped[str] = mapped_column(String(128), default="global")
    vertex_template: Mapped[str] = mapped_column(String(128), default="")
    gemini_template: Mapped[str] = mapped_column(String(128), default="")
    quota_per_unit: Mapped[Decimal] = mapped_column(Numeric(30, 6), default=Decimal("500000"))
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelUsageSnapshot(Base):
    __tablename__ = "channel_usage_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    upload_id: Mapped[str] = mapped_column(ForeignKey("channel_uploads.id"), index=True)
    used_quota: Mapped[Decimal] = mapped_column(Numeric(30, 6))
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
