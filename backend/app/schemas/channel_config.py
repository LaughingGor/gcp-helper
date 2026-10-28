from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class UpdateChannelConfigRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=2048, alias="baseUrl")
    token: str | None = Field(default=None, min_length=1, max_length=8192)

    model_config = {"populate_by_name": True}

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("推送地址必须以 http:// 或 https:// 开头")
        if parsed.username or parsed.password:
            raise ValueError("推送地址不能包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("推送地址不能包含查询参数或锚点")
        return normalized

class ChannelConfigResponse(BaseModel):
    base_url: str = Field(alias="baseUrl")
    token_configured: bool = Field(alias="tokenConfigured")
    updated_at: datetime = Field(alias="updatedAt")

    model_config = {"populate_by_name": True}
