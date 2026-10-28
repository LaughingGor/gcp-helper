from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ExecuteGcloudCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=60, ge=5, le=300, alias="timeoutSeconds")
    confirm_high_risk: bool = Field(default=False, alias="confirmHighRisk")

    model_config = {"populate_by_name": True}

    @field_validator("command")
    @classmethod
    def normalize_command(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("gcloud 命令不能为空")
        return normalized


class GcloudCommandResponse(BaseModel):
    command: str
    exit_code: int = Field(alias="exitCode")
    success: bool
    stdout: str
    stderr: str
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    duration_ms: int = Field(alias="durationMs")
    output_truncated: bool = Field(alias="outputTruncated")

    model_config = {"populate_by_name": True}


class GcloudCommandHistoryItem(BaseModel):
    id: str
    command: str
    exit_code: int | None = Field(default=None, alias="exitCode")
    status: str
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")

    model_config = {"populate_by_name": True}
