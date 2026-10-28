from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


ChannelClassification = Literal[
    "DISABLED",
    "WITHIN_LIMIT",
    "EXHAUSTED",
    "OVER_LIMIT",
    "DATA_INVALID",
    "UNKNOWN_STATUS",
]


class DisabledChannelStatistics(BaseModel):
    channel_count: int = Field(alias="channelCount")
    valid_count: int = Field(alias="validCount")
    invalid_count: int = Field(alias="invalidCount")
    manual_disabled_count: int = Field(alias="manualDisabledCount")
    auto_disabled_count: int = Field(alias="autoDisabledCount")
    total_consumed_amount: Decimal = Field(alias="totalConsumedAmount")
    average_consumed_amount: Decimal | None = Field(alias="averageConsumedAmount")

    model_config = {"populate_by_name": True}


class EnabledChannelStatistics(BaseModel):
    channel_count: int = Field(alias="channelCount")
    total_consumed_amount: Decimal = Field(alias="totalConsumedAmount")
    within_limit_count: int = Field(alias="withinLimitCount")
    exhausted_count: int = Field(alias="exhaustedCount")
    over_limit_count: int = Field(alias="overLimitCount")
    invalid_count: int = Field(alias="invalidCount")
    total_remaining_amount: Decimal = Field(alias="totalRemainingAmount")
    average_remaining_amount: Decimal | None = Field(alias="averageRemainingAmount")
    total_over_amount: Decimal = Field(alias="totalOverAmount")

    model_config = {"populate_by_name": True}


class ChannelStatisticItem(BaseModel):
    channel_id: str = Field(alias="channelId")
    name: str
    channel_type: int | None = Field(default=None, alias="channelType")
    groups: list[str]
    status: int | None
    status_label: str = Field(alias="statusLabel")
    used_quota: Decimal | None = Field(default=None, alias="usedQuota")
    used_amount: Decimal | None = Field(default=None, alias="usedAmount")
    max_amount: Decimal = Field(alias="maxAmount")
    remaining_amount: Decimal | None = Field(default=None, alias="remainingAmount")
    over_amount: Decimal | None = Field(default=None, alias="overAmount")
    classification: ChannelClassification
    invalid_reason: str | None = Field(default=None, alias="invalidReason")

    model_config = {"populate_by_name": True}


class ChannelStatisticsResponse(BaseModel):
    group: str
    currency: str
    quota_per_unit: Decimal = Field(alias="quotaPerUnit")
    max_amount_per_channel: Decimal = Field(alias="maxAmountPerChannel")
    generated_at: datetime = Field(alias="generatedAt")
    partial: bool = False
    matched_channel_count: int = Field(alias="matchedChannelCount")
    unknown_status_count: int = Field(alias="unknownStatusCount")
    total_consumed_amount: Decimal = Field(alias="totalConsumedAmount")
    disabled: DisabledChannelStatistics
    enabled: EnabledChannelStatistics
    channels: list[ChannelStatisticItem]
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
