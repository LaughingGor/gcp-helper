import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class PresetTaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class PresetTaskType(str, enum.Enum):
    BOOTSTRAP_PROJECT = "BOOTSTRAP_PROJECT"


def utcnow() -> datetime:
    return datetime.utcnow()


class GcpPresetTask(Base):
    __tablename__ = "gcp_preset_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    env_id: Mapped[str] = mapped_column(String(36), ForeignKey("gcp_environments.id"), nullable=False)
    task_type: Mapped[PresetTaskType] = mapped_column(Enum(PresetTaskType), nullable=False)
    status: Mapped[PresetTaskStatus] = mapped_column(Enum(PresetTaskStatus), nullable=False, default=PresetTaskStatus.PENDING)

    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    force_new_project: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    resource_plan_encrypted: Mapped[str | None] = mapped_column(Text)
    service_account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    key_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vm_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vm_zone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_vm_zone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credential_upload_id: Mapped[str | None] = mapped_column(ForeignKey("channel_uploads.id"))
    vm_external_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    firewall_rule_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    socks5_port: Mapped[str | None] = mapped_column(String(16), nullable=True)
    socks5_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    current_step: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    environment = relationship("GcpEnvironment", back_populates="preset_tasks")
