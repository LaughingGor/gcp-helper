import re
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class ProxyConfigIn(BaseModel):
    enabled: bool = False
    type: str | None = Field(default=None, pattern="^(http|http_no_tunnel|socks4|socks5)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None

    @field_validator("host")
    @classmethod
    def strip_host(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class StartLoginRequest(BaseModel):
    google_account: str = Field(..., min_length=3, max_length=255, alias="googleAccount")
    proxy: ProxyConfigIn = Field(default_factory=ProxyConfigIn)
    vm_zone: str | None = Field(default=None, alias="vmZone", max_length=128)

    @field_validator("vm_zone")
    @classmethod
    def validate_vm_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from app.schemas.user_settings import UpdateUserSettingsRequest
        return UpdateUserSettingsRequest.validate_zone(value)

    model_config = {"populate_by_name": True}

    @field_validator("google_account")
    @classmethod
    def strip_google_account(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("Google 账号至少需要 3 个字符")
        return normalized


class SubmitCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=4096)


class LoginStartResponse(BaseModel):
    env_id: str = Field(alias="envId")
    login_session_id: str = Field(alias="loginSessionId")
    config_name: str = Field(alias="configName")
    status: str
    auth_url: str = Field(alias="authUrl")
    expires_at: datetime = Field(alias="expiresAt")

    model_config = {"populate_by_name": True}


class LoginSessionResponse(BaseModel):
    login_session_id: str = Field(alias="loginSessionId")
    env_id: str = Field(alias="envId")
    env_name: str | None = Field(default=None, alias="envName")
    google_account_hint: str | None = Field(default=None, alias="googleAccountHint")
    created_by: str | None = Field(default=None, alias="createdBy")
    status: str
    auth_url: str | None = Field(default=None, alias="authUrl")
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")
    expires_at: datetime = Field(alias="expiresAt")
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")

    model_config = {"populate_by_name": True}


class SubmitCodeResponse(BaseModel):
    status: str
    env_id: str = Field(alias="envId")
    authorized_account: str | None = Field(default=None, alias="authorizedAccount")
    config_name: str = Field(alias="configName")
    preset_task_id: str | None = Field(default=None, alias="presetTaskId")
    error_code: str | None = Field(default=None, alias="errorCode")
    message: str | None = None

    model_config = {"populate_by_name": True}


class DeleteEnvResponse(BaseModel):
    env_id: str = Field(alias="envId")
    deleted: bool

    model_config = {"populate_by_name": True}


class ChannelPushRequest(BaseModel):
    target_id: UUID | None = Field(default=None, alias="targetId")
    model_config = {"populate_by_name": True, "extra": "forbid"}


class ChannelPushResponse(BaseModel):
    success: bool
    message: str = ""
    env_id: str = Field(alias="envId")
    task_id: str = Field(alias="taskId")
    channel_name: str = Field(alias="channelName")
    template_name: str = Field(alias="templateName")
    deployment_id: str | None = Field(default=None, alias="deploymentId")
    target_id: str | None = Field(default=None, alias="targetId")
    status: str | None = None

    model_config = {"populate_by_name": True}


class PresetTaskResponse(BaseModel):
    task_id: str = Field(alias="taskId")
    env_id: str = Field(alias="envId")
    env_name: str | None = Field(default=None, alias="envName")
    google_account_hint: str | None = Field(default=None, alias="googleAccountHint")
    created_by: str | None = Field(default=None, alias="createdBy")
    task_type: str = Field(alias="taskType")
    status: str
    current_step: str | None = Field(default=None, alias="currentStep")
    project_id: str | None = Field(default=None, alias="projectId")
    service_account_email: str | None = Field(default=None, alias="serviceAccountEmail")
    key_file_name: str | None = Field(default=None, alias="keyFileName")
    key_file_exists: bool = Field(alias="keyFileExists")
    credential_stored: bool = Field(default=False, alias="credentialStored")
    vm_name: str | None = Field(default=None, alias="vmName")
    vm_zone: str | None = Field(default=None, alias="vmZone")
    requested_vm_zone: str | None = Field(default=None, alias="requestedVmZone")
    vm_external_ip: str | None = Field(default=None, alias="vmExternalIp")
    firewall_rule_name: str | None = Field(default=None, alias="firewallRuleName")
    socks5_port: str | None = Field(default=None, alias="socks5Port")
    socks5_url: str | None = Field(default=None, alias="socks5Url")
    error_code: str | None = Field(default=None, alias="errorCode")
    error_message: str | None = Field(default=None, alias="errorMessage")
    created_at: datetime = Field(alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    finished_at: datetime | None = Field(default=None, alias="finishedAt")

    model_config = {"populate_by_name": True}


class GcpEnvironmentResponse(BaseModel):
    env_id: str = Field(alias="envId")
    env_name: str = Field(alias="envName")
    google_account_hint: str = Field(alias="googleAccountHint")
    authorized_account: str | None = Field(default=None, alias="authorizedAccount")
    created_by: str | None = Field(default=None, alias="createdBy")
    status: str
    config_name: str = Field(alias="configName")
    proxy_enabled: bool = Field(alias="proxyEnabled")
    default_project_id: str | None = Field(default=None, alias="defaultProjectId")
    default_region: str | None = Field(default=None, alias="defaultRegion")
    default_zone: str | None = Field(default=None, alias="defaultZone")
    billing_account_id: str | None = Field(default=None, alias="billingAccountId")
    billing_account_display_name: str | None = Field(default=None, alias="billingAccountDisplayName")
    created_at: datetime = Field(alias="createdAt")
    channel_push_status: str | None = Field(default=None, alias="channelPushStatus")
    channel_push_action: str | None = Field(default=None, alias="channelPushAction")
    channel_pushed_at: datetime | None = Field(default=None, alias="channelPushedAt")
    channel_push_message: str | None = Field(default=None, alias="channelPushMessage")

    model_config = {"populate_by_name": True}


class BillingAccountResponse(BaseModel):
    billing_account_id: str = Field(alias="billingAccountId")
    display_name: str = Field(alias="displayName")
    open: bool

    model_config = {"populate_by_name": True}


class UpdateBillingAccountRequest(BaseModel):
    billing_account_id: str = Field(min_length=14, max_length=32, alias="billingAccountId")

    model_config = {"populate_by_name": True}

    @field_validator("billing_account_id")
    @classmethod
    def validate_billing_account_id(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}", normalized):
            raise ValueError("Billing Account ID 格式不正确")
        return normalized


class UpdateBillingAccountResponse(BaseModel):
    billing_account_id: str = Field(alias="billingAccountId")
    display_name: str = Field(alias="displayName")
    linked_project_id: str | None = Field(default=None, alias="linkedProjectId")

    model_config = {"populate_by_name": True}


class StartBootstrapRequest(BaseModel):
    vm_zone: str | None = Field(default=None, alias="vmZone", max_length=128)
    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("vm_zone")
    @classmethod
    def validate_zone(cls, value: str | None) -> str | None:
        return StartLoginRequest.validate_vm_zone(value)


class CreateProjectReplacement(StartBootstrapRequest):
    target_id: UUID = Field(alias="targetId")
    deployment_id: UUID = Field(alias="deploymentId")
    request_id: UUID = Field(alias="requestId")
