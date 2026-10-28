import base64
import hashlib
import os

from cryptography.fernet import Fernet
from app.core.config import get_settings


def get_app_secret_key(*, require_stable: bool = False) -> str:
    # Preserve exact key bytes to keep existing ciphertext and fingerprints valid.
    raw_key = os.getenv("APP_SECRET_KEY")
    if raw_key is None:
        raw_key = get_settings().app_secret_key.get_secret_value()
    if require_stable and raw_key.strip() in {
        "", "dev-only-change-me", "please-change-this-dev-secret", "replace-with-a-long-random-secret",
    }:
        raise ValueError("请先在后端配置 APP_SECRET_KEY（环境变量或 backend/.env），使用稳定的非示例密钥并重启后端")
    return raw_key


class SecretService:
    """MVP secret helper.

    Production 建议换成 Google Secret Manager / Vault / KMS envelope encryption。
    当前实现用 APP_SECRET_KEY 派生 Fernet key，便于本地先跑起来。
    """

    def __init__(self) -> None:
        raw_key = get_app_secret_key()
        digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt_optional(self, value: str | None) -> str | None:
        if not value:
            return None
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt_optional(self, token: str | None) -> str | None:
        if not token:
            return None
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
