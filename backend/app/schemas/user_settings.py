import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class UpdateUserSettingsRequest(BaseModel):
    compute_vm_zone: str = Field(min_length=4, max_length=128, alias="computeVmZone")
    default_target_id: UUID | None = Field(default=None, alias="defaultTargetId")

    model_config = {"populate_by_name": True}

    @field_validator("compute_vm_zone")
    @classmethod
    def validate_zone(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]-[a-z]", normalized):
            raise ValueError("Zone 格式不正确，例如 us-west2-a")
        return normalized


class UserSettingsResponse(BaseModel):
    compute_vm_zone: str = Field(alias="computeVmZone")
    using_default_zone: bool = Field(alias="usingDefaultZone")
    default_target_id: str | None = Field(default=None, alias="defaultTargetId")

    model_config = {"populate_by_name": True}
