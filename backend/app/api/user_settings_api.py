import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.core.config import get_settings
from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole
from app.schemas.user_settings import UpdateUserSettingsRequest, UserSettingsResponse

router = APIRouter(prefix="/user-settings", tags=["user-settings"])


@router.get("", response_model=UserSettingsResponse)
def get_user_settings(user: User = Depends(require_permission("settings:write"))):
    default_zone = get_settings().compute_vm_zone
    return UserSettingsResponse(
        computeVmZone=user.compute_vm_zone or default_zone,
        usingDefaultZone=not bool(user.compute_vm_zone),
        defaultTargetId=(user.default_target_id or user.bound_target_id) if user.role == UserRole.ADMIN else user.bound_target_id,
    )


@router.put("", response_model=UserSettingsResponse)
def update_user_settings(
    req: UpdateUserSettingsRequest,
    user: User = Depends(require_permission("settings:write", csrf=True)),
    db: Session = Depends(get_db),
):
    if user.role != UserRole.ADMIN and "default_target_id" in req.model_fields_set:
        raise HTTPException(403, "后台绑定由管理员在用户管理中设置，不能自行修改")
    if req.default_target_id:
        from app.services.new_api_target_service import resolve_target
        try:
            resolve_target(db, user.id, str(req.default_target_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
    user.compute_vm_zone = req.compute_vm_zone
    if "default_target_id" in req.model_fields_set:
        user.default_target_id = str(req.default_target_id) if req.default_target_id else None
    db.add(
        AuditLog(
            actor=user.username,
            action="USER_SETTINGS_UPDATE",
            target_type="user",
            target_id=user.id,
                detail_json=json.dumps({"computeVmZone": req.compute_vm_zone, "defaultTargetId": user.default_target_id}),
        )
    )
    db.commit()
    db.refresh(user)
    return UserSettingsResponse(computeVmZone=user.compute_vm_zone, usingDefaultZone=False,
        defaultTargetId=(user.default_target_id or user.bound_target_id) if user.role == UserRole.ADMIN else user.bound_target_id)
