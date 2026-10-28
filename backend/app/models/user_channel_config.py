import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class UserChannelConfig(Base):
    __tablename__ = "user_channel_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    login_path: Mapped[str] = mapped_column(String(512), nullable=False, default="/api/user/login?turnstile=")
    auth_header: Mapped[str] = mapped_column(String(128), nullable=False, default="Authorization")
    auth_scheme: Mapped[str] = mapped_column(String(64), nullable=False, default="Bearer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="channel_config")
