import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class GcpCommandLog(Base):
    __tablename__ = "gcp_command_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    env_id: Mapped[str] = mapped_column(String(36), ForeignKey("gcp_environments.id"), nullable=False)
    login_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    command_args_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
