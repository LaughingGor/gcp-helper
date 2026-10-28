import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_channel_config import UserChannelConfig
from app.schemas.channel_config import UpdateChannelConfigRequest
from app.services.secret_service import SecretService


class UserChannelConfigService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.secrets = SecretService()

    def get(self, user_id: str) -> UserChannelConfig | None:
        return self.db.scalar(
            select(UserChannelConfig).where(UserChannelConfig.user_id == user_id)
        )

    def upsert(self, user: User, req: UpdateChannelConfigRequest) -> UserChannelConfig:
        config = self.get(user.id)
        if not config:
            if not req.token:
                raise ValueError("首次保存渠道配置时必须填写 Token")
            config = UserChannelConfig(
                user_id=user.id,
                username="",
                password_encrypted="",
                token_encrypted="",
            )
            self.db.add(config)

        if req.token:
            config.token_encrypted = self.secrets.encrypt_optional(req.token) or ""
        if not config.token_encrypted:
            raise ValueError("渠道 Access Token 不能为空")

        config.base_url = req.base_url
        config.username = ""
        config.password_encrypted = ""
        config.login_path = "/api/user/login?turnstile="
        config.auth_header = "Authorization"
        config.auth_scheme = "Bearer"
        self.db.add(
            AuditLog(
                actor=user.username,
                action="CHANNEL_CONFIG_UPDATE",
                target_type="user_channel_config",
                target_id=user.id,
                detail_json=json.dumps(
                    {"baseUrl": config.base_url},
                    ensure_ascii=False,
                ),
            )
        )
        self.db.commit()
        self.db.refresh(config)
        return config

    def decrypt_token(self, config: UserChannelConfig) -> str:
        try:
            token = self.secrets.decrypt_optional(config.token_encrypted)
        except Exception as exc:
            raise RuntimeError("渠道 Token 解密失败，请重新保存渠道配置") from exc
        if not token:
            raise RuntimeError("渠道 Access Token 未配置")
        return token
