"""Administrators manage shared destinations and bind users to one destination."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.channel_upload import NewApiTarget
from app.models.user import User, UserRole
from app.models.user_channel_config import UserChannelConfig
from app.schemas.channel_upload import NewApiTargetInput
from app.core.config import get_settings


class TargetAccessError(ValueError):
    pass


def resolve_target(db: Session, user_id: str | None, target_id: str | None = None) -> NewApiTarget:
    user = db.get(User, user_id) if user_id else None
    if user_id and not user:
        raise TargetAccessError("用户不存在")
    if user and user.role != UserRole.ADMIN:
        if not user.bound_target_id:
            raise TargetAccessError("尚未绑定 new-api 后台，请联系管理员")
        if target_id and target_id != user.bound_target_id:
            raise TargetAccessError("只能使用管理员绑定的 new-api 后台")
        target_id = user.bound_target_id
    identifier = target_id or ((user.default_target_id or user.bound_target_id) if user else None)
    target = db.get(NewApiTarget, identifier) if identifier else None
    if not target or not target.enabled:
        if user and user.role != UserRole.ADMIN:
            raise TargetAccessError("绑定的 new-api 后台不可用，请联系管理员")
        raise ValueError("请选择已启用的目标后台；后台地址和 Token 由管理员统一配置")
    return target


def migrate_legacy_targets(db: Session) -> None:
    """Copy old ciphertext without exposing private connection privileges automatically."""
    settings = get_settings()
    for old in db.scalars(select(UserChannelConfig).where(UserChannelConfig.token_encrypted != "")):
        if not old.token_encrypted:
            continue
        try:
            base_url = NewApiTargetInput.normalize_url(old.base_url)
        except ValueError:
            continue
        target = db.scalar(select(NewApiTarget).where(NewApiTarget.base_url == base_url))
        if not target:
            owner = db.get(User, old.user_id)
            target = NewApiTarget(name=f"旧配置 · {owner.username if owner else old.user_id}"[:128],
                base_url=base_url, token_encrypted=old.token_encrypted, enabled=False,
                models=settings.new_api_upload_models, group=settings.new_api_channel_stats_group,
                stats_group=settings.new_api_channel_stats_group,
                vertex_region=settings.new_api_upload_vertex_region,
                quota_per_unit=settings.new_api_quota_per_unit, currency=settings.new_api_currency.upper())
            db.add(target)
            db.flush()
            db.add(AuditLog(actor="system", action="LEGACY_TARGET_IMPORTED_DISABLED", target_type="new_api_target", target_id=target.id))
        owner = db.get(User, old.user_id)
        if owner and not owner.default_target_id:
            owner.default_target_id = target.id
    db.commit()
