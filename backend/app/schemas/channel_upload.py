from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator


class ChannelUploadInput(BaseModel):
    kind: Literal["vertex_json", "gemini_key"]
    credential: SecretStr = Field(min_length=1, max_length=65536)
    remark: str = Field(default="", max_length=255)

    model_config = {"extra": "forbid"}


class CreateChannelUpload(ChannelUploadInput):
    request_id: UUID = Field(alias="requestId")

    model_config = {"extra": "forbid", "populate_by_name": True}


class NewApiTargetInput(BaseModel):
    base_url: str = Field(alias="baseUrl", min_length=8, max_length=2048)
    token: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    stats_group: str | None = Field(default=None, alias="statsGroup", min_length=1, max_length=128)

    model_config = {"extra": "forbid", "populate_by_name": True}

    @field_validator("stats_group", mode="before")
    @classmethod
    def trim_stats_group(cls, value):
        return value.strip() if isinstance(value, str) else value

    @classmethod
    def normalize_url(cls, value: str) -> str:
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("后台地址必须为不含凭据、查询参数的 HTTP(S) 地址")
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and not (parsed.scheme == "http" and port == 80 or parsed.scheme == "https" and port == 443):
            host += f":{port}"
        return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


class PushUploadInput(BaseModel):
    target_ids: list[UUID] = Field(alias="targetIds", min_length=1, max_length=20)
    model_config = {"extra": "forbid", "populate_by_name": True}


class AdoptLegacyPushInput(BaseModel):
    target_id: UUID = Field(alias="targetId")
    record_id: UUID = Field(alias="recordId")
    model_config = {"extra": "forbid", "populate_by_name": True}
