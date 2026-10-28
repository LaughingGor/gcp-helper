import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class GcpEnvironmentStatus(str, enum.Enum):
    CREATED = "CREATED"
    CONFIG_INITIALIZING = "CONFIG_INITIALIZING"
    CONFIG_READY = "CONFIG_READY"
    AUTH_PROCESS_STARTING = "AUTH_PROCESS_STARTING"
    WAITING_USER_CODE = "WAITING_USER_CODE"
    AUTHORIZING = "AUTHORIZING"
    AUTHORIZED = "AUTHORIZED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


def utcnow() -> datetime:
    return datetime.utcnow()


class GcpEnvironment(Base):
    __tablename__ = "gcp_environments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    env_name: Mapped[str] = mapped_column(String(128), nullable=False)
    google_account_hint: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized_account: Mapped[str | None] = mapped_column(String(255), nullable=True)

    config_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    config_dir: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[GcpEnvironmentStatus] = mapped_column(
        Enum(GcpEnvironmentStatus), nullable=False, default=GcpEnvironmentStatus.CREATED
    )

    proxy_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proxy_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proxy_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proxy_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    proxy_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    default_project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    billing_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    billing_account_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    billing_account_selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_account_selected_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    owner_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    login_sessions = relationship("GcpLoginSession", back_populates="environment", cascade="all,delete-orphan")
    preset_tasks = relationship("GcpPresetTask", back_populates="environment", cascade="all,delete-orphan")
    owner = relationship("User", back_populates="environments")
    channel_push_records = relationship(
        "ChannelPushRecord",
        back_populates="environment",
        cascade="all,delete-orphan",
        order_by="ChannelPushRecord.created_at.desc()",
    )
