from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


ChannelModelStatusFilter = Literal["enabled", "disabled", "all"]
ChannelModelUpdateStatus = Literal["SUCCESS", "SKIPPED", "FAILED"]


class AddChannelModelRequest(BaseModel):
    target_id: UUID | None = Field(default=None, alias="targetId")
    model_config = {"populate_by_name": True}
    group: str = Field(min_length=1, max_length=128)
    status: ChannelModelStatusFilter = "enabled"
    model: str = Field(min_length=1, max_length=256)

    @field_validator("group", "model")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("不能为空")
        return normalized

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if "," in value or any(character.isspace() for character in value):
            raise ValueError("模型名称不能包含逗号或空白字符")
        return value


class ChannelModelCandidate(BaseModel):
    channel_id: str = Field(alias="channelId")
    name: str
    status: int | None = None
    current_models: str = Field(alias="currentModels")
    updated_models: str = Field(alias="updatedModels")

    model_config = {"populate_by_name": True}


class ChannelModelPreviewResponse(BaseModel):
    group: str
    status: ChannelModelStatusFilter
    model: str
    total_channels: int = Field(alias="totalChannels")
    already_present_count: int = Field(alias="alreadyPresentCount")
    candidate_count: int = Field(alias="candidateCount")
    candidates: list[ChannelModelCandidate]

    model_config = {"populate_by_name": True}


class ChannelModelUpdateResult(BaseModel):
    channel_id: str = Field(alias="channelId")
    name: str
    status: ChannelModelUpdateStatus
    message: str

    model_config = {"populate_by_name": True}


class ChannelModelApplyResponse(BaseModel):
    group: str
    status: ChannelModelStatusFilter
    model: str
    requested_count: int = Field(alias="requestedCount")
    success_count: int = Field(alias="successCount")
    skipped_count: int = Field(alias="skippedCount")
    failed_count: int = Field(alias="failedCount")
    results: list[ChannelModelUpdateResult]

    model_config = {"populate_by_name": True}
