from fastapi import APIRouter, Depends, HTTPException, Query
from uuid import UUID
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.db.session import get_db
from app.models.user import User
from app.schemas.channel_statistics import ChannelStatisticsResponse
from app.services.channel_statistics_service import ChannelStatisticsService
from app.services.new_api_target_service import TargetAccessError

router = APIRouter(prefix="/channel-statistics", tags=["channel-statistics"])


@router.get("", response_model=ChannelStatisticsResponse)
def get_channel_statistics(
    target_id: UUID | None = Query(default=None, alias="targetId"),
    refresh: bool = Query(default=False),
    user: User = Depends(require_permission("channel:stats")),
    db: Session = Depends(get_db),
):
    try:
        return ChannelStatisticsService(db, user_id=user.id, target_id=str(target_id) if target_id else None).get_statistics(refresh=refresh)
    except TargetAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
