import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ChannelPushStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ChannelPushAction(str, enum.Enum):
    CREATED = "CREATED"
    REACTIVATED = "REACTIVATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"


def utcnow() -> datetime:
    return datetime.utcnow()


class ChannelPushRecord(Base):
    __tablename__ = "channel_push_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    env_id: Mapped[str] = mapped_column(String(36), ForeignKey("gcp_environments.id"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    target_id: Mapped[str | None] = mapped_column(ForeignKey("new_api_targets.id"))
    deployment_id: Mapped[str | None] = mapped_column(ForeignKey("channel_uploads.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[ChannelPushStatus] = mapped_column(Enum(ChannelPushStatus), nullable=False)
    action: Mapped[ChannelPushAction | None] = mapped_column(Enum(ChannelPushAction), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    environment = relationship("GcpEnvironment", back_populates="channel_push_records")
