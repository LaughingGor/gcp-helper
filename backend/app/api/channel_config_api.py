from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.db.session import get_db
from app.models.user import User
from app.schemas.channel_config import ChannelConfigResponse, UpdateChannelConfigRequest
from app.services.user_channel_config_service import UserChannelConfigService

router = APIRouter(prefix="/channel-config", tags=["channel-config"])


def config_response(config) -> ChannelConfigResponse:
    return ChannelConfigResponse(
        baseUrl=config.base_url,
        tokenConfigured=bool(config.token_encrypted),
        updatedAt=config.updated_at,
    )


@router.get("", response_model=ChannelConfigResponse | None)
def get_channel_config(
    user: User = Depends(require_permission("channel:config")),
    db: Session = Depends(get_db),
):
    config = UserChannelConfigService(db).get(user.id)
    return config_response(config) if config else None


@router.put("", response_model=ChannelConfigResponse)
def update_channel_config(
    req: UpdateChannelConfigRequest,
    user: User = Depends(require_permission("channel:config", csrf=True)),
    db: Session = Depends(get_db),
):
    raise HTTPException(410, "个人后台连接已停用，请管理员统一配置后台，用户仅设置默认后台偏好")
