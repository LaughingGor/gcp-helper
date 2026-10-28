import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class LoginSessionStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESS_STARTED = "PROCESS_STARTED"
    AUTH_URL_READY = "AUTH_URL_READY"
    WAITING_CODE = "WAITING_CODE"
    CODE_SUBMITTED = "CODE_SUBMITTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


def utcnow() -> datetime:
    return datetime.utcnow()


class GcpLoginSession(Base):
    __tablename__ = "gcp_login_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    env_id: Mapped[str] = mapped_column(String(36), ForeignKey("gcp_environments.id"), nullable=False)
    status: Mapped[LoginSessionStatus] = mapped_column(Enum(LoginSessionStatus), nullable=False)

    auth_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    process_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    environment = relationship("GcpEnvironment", back_populates="login_sessions")
