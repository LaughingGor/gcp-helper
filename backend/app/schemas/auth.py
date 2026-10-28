from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.user import UserRole, UserStatus


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    id: str
    username: str
    email: str | None = None
    role: UserRole
    status: UserStatus
    bound_target_id: str | None = Field(default=None, alias="boundTargetId")
    permissions: list[str]
    created_at: datetime = Field(alias="createdAt")
    last_login_at: datetime | None = Field(default=None, alias="lastLoginAt")

    model_config = {"populate_by_name": True}


class LoginResponse(BaseModel):
    user: UserResponse


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    password: str = Field(min_length=10, max_length=1024)
    email: str | None = Field(default=None, max_length=255)
    role: UserRole = UserRole.OPERATOR


class UpdateUserRequest(BaseModel):
    role: UserRole | None = None
    status: UserStatus | None = None
    password: str | None = Field(default=None, min_length=10, max_length=1024)
    model_config = {"extra": "forbid"}


class BindUserTargetRequest(BaseModel):
    bound_target_id: UUID | None = Field(alias="boundTargetId")
    model_config = {"populate_by_name": True, "extra": "forbid"}
