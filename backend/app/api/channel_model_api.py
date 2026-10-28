from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.db.session import get_db
from app.models.user import User
from app.schemas.channel_model import AddChannelModelRequest, ChannelModelApplyResponse, ChannelModelPreviewResponse
from app.services.channel_model_service import ChannelModelService

router = APIRouter(prefix="/channel-models", tags=["channel-models"])


@router.post("/preview", response_model=ChannelModelPreviewResponse)
def preview_add_model(
    req: AddChannelModelRequest,
    user: User = Depends(require_permission("channel:model:write", csrf=True)),
    db: Session = Depends(get_db),
):
    try:
        return ChannelModelService(db, user_id=user.id, actor_username=user.username,
            target_id=str(req.target_id) if req.target_id else None).preview(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/add", response_model=ChannelModelApplyResponse)
def add_model(
    req: AddChannelModelRequest,
    user: User = Depends(require_permission("channel:model:write", csrf=True)),
    db: Session = Depends(get_db),
):
    try:
        return ChannelModelService(db, user_id=user.id, actor_username=user.username,
            target_id=str(req.target_id) if req.target_id else None).apply(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
