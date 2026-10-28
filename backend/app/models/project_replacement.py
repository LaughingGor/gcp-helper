"""Durable project rotation; snapshots remain encrypted and are never public."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ProjectReplacement(Base):
    __tablename__ = "project_replacements"
    __table_args__ = (UniqueConstraint("actor_user_id", "request_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Environment/task identifiers survive local environment cleanup for audit history.
    env_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str] = mapped_column(String(36))
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    executor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    deployment_id: Mapped[str] = mapped_column(ForeignKey("channel_uploads.id"))
    old_source_id: Mapped[str] = mapped_column(ForeignKey("channel_uploads.id"))
    new_source_id: Mapped[str | None] = mapped_column(ForeignKey("channel_uploads.id"))
    target_id: Mapped[str] = mapped_column(ForeignKey("new_api_targets.id"))
    remote_channel_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(36))
    old_project_id: Mapped[str | None] = mapped_column(String(255))
    billing_account_id: Mapped[str] = mapped_column(String(32))
    snapshot_encrypted: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    message: Mapped[str] = mapped_column(String(512), default="等待创建新项目")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
