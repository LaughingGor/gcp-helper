"""Administrator controls for retained credentials and immutable new-api destinations."""
import json
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.channel_upload import ChannelUpload, NewApiTarget
from app.models.user import User
from app.models.user_channel_config import UserChannelConfig
from app.models.channel_push_record import ChannelPushRecord
from app.schemas.channel_upload import NewApiTargetInput
from app.services.channel_upload_service import ChannelUploadService
from app.services.channel_push_service import ChannelPushService, NewApiChannelConfig
from app.services.secret_service import SecretService, get_app_secret_key
from app.core.config import get_settings


class ChannelVaultService:
    def __init__(self, db: Session, actor: User):
        self.db = db
        self.actor = actor

    def audit(self, action: str, identifier: str, detail: dict | None = None) -> None:
        self.db.add(AuditLog(actor=self.actor.username, action=action, target_type="channel_vault",
                             target_id=identifier, detail_json=json.dumps(detail) if detail else None))

    def upload(self, identifier: str) -> ChannelUpload:
        row = self.db.get(ChannelUpload, identifier)
        if not row or row.source_upload_id:
            raise KeyError("上传凭据不存在")
        return row

    def reveal(self, identifier: str, *, download: bool = False) -> tuple[ChannelUpload, str]:
        row = self.upload(identifier)
        if not row.credential_encrypted:
            raise ValueError("历史凭据已被清除，无法恢复，请原用户重新上传")
        try:
            value = SecretService().decrypt_optional(row.credential_encrypted)
        except Exception:
            raise ValueError("凭据无法解密，请检查 APP_SECRET_KEY 和备份") from None
        if not value:
            raise ValueError("凭据内容为空")
        self.audit("CHANNEL_CREDENTIAL_DOWNLOAD" if download else "CHANNEL_CREDENTIAL_VIEW", row.id)
        self.db.commit()
        return row, value

    @staticmethod
    def public_target(target: NewApiTarget) -> dict:
        return {"id": target.id, "name": target.name, "baseUrl": target.base_url,
                "tokenConfigured": bool(target.token_encrypted), "enabled": target.enabled,
                "models": target.models, "group": target.group, "vertexRegion": target.vertex_region,
                "statsGroup": target.stats_group or target.group,
                "vertexTemplate": target.vertex_template, "geminiTemplate": target.gemini_template,
                "quotaPerUnit": str(target.quota_per_unit), "currency": target.currency}

    def save_target(self, req: NewApiTargetInput, identifier: str | None = None) -> NewApiTarget:
        get_app_secret_key(require_stable=True)
        base_url = req.normalize_url(req.base_url)
        target = self.db.get(NewApiTarget, identifier) if identifier else None
        if identifier and not target:
            raise KeyError("目标后台不存在")
        if target and target.base_url != base_url:
            raise ValueError("已有后台地址不可修改，请新增后台，避免历史渠道被错误关联")
        if target is None:
            if req.token is None:
                raise ValueError("新后台必须填写管理 Token")
            target = NewApiTarget(id=str(uuid.uuid4()), base_url=base_url)
        if req.token is not None:
            token = req.token.get_secret_value().strip()
            if not token:
                raise ValueError("管理 Token 不能为空")
        else:
            try:
                token = SecretService().decrypt_optional(target.token_encrypted)
            except Exception:
                raise ValueError("原 Access Token 无法解密，请检查 APP_SECRET_KEY 或重新填写 Token") from None
            if not token:
                raise ValueError("请填写后台管理 Access Token")
        settings = get_settings()
        vertex_template = settings.new_api_upload_vertex_template or settings.new_api_channel_default_template
        # Resolve the original template here so selectors and statistics use its
        # actual models/group/region, rather than separate form defaults.
        try:
            remote = ChannelUploadService(self.db, self.actor.id).remote
            payload = remote._load_template(vertex_template)
            if not isinstance(payload, dict):
                raise RuntimeError("模板必须为 JSON 对象")
            remote._normalize_new_api_payload(payload)
        except (FileNotFoundError, RuntimeError) as exc:
            raise ValueError("默认 Vertex 模板不可用，请检查模板配置文件") from exc
        channel = payload["channel"]
        if settings.new_api_quota_per_unit <= 0:
            raise ValueError("默认 quota 换算比例必须大于 0")
        # Validate the candidate before changing the persistent target, including
        # edits that reuse its stored token. Failed validation keeps it intact.
        try:
            ChannelPushService(self.db).validate_config(NewApiChannelConfig(base_url=base_url, token=token))
        except RuntimeError as exc:
            raise ValueError(str(exc)) from None
        if req.token is not None:
            target.token_encrypted = SecretService().encrypt_optional(token)
        target.stats_group = req.stats_group or target.stats_group or target.group or str(
            channel.get("group") or settings.new_api_channel_stats_group)
        target.name = base_url[:128]
        target.models = str(channel.get("models") or settings.new_api_upload_models)
        target.group = str(channel.get("group") or settings.new_api_channel_stats_group)
        target.vertex_region = str(channel.get("other") or settings.new_api_upload_vertex_region)
        target.vertex_template = vertex_template
        target.gemini_template = settings.new_api_upload_gemini_template
        target.enabled = True
        target.quota_per_unit = settings.new_api_quota_per_unit
        target.currency = settings.new_api_currency.upper()
        self.db.add(target)
        self.audit("NEW_API_TARGET_UPDATE" if identifier else "NEW_API_TARGET_CREATE", target.id)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("该后台地址已配置") from None
        return target

    def prepare_pushes(self, identifier: str, target_ids: list[str]) -> list[ChannelUpload]:
        upload = self.upload(identifier)
        if upload.origin_env_id:
            from app.services.project_replacement_service import ensure_no_replacement
            ensure_no_replacement(self.db, upload.origin_env_id)
        if not upload.credential_encrypted:
            raise ValueError("原始凭据已清除，请原用户重新上传后再推送")
        service = ChannelUploadService(self.db, upload.user_id)
        results = []
        try:
            if upload.status == "STORED":
                upload.message = "凭据已加密保存，可展开查看各后台推送状态"
            for target_id in dict.fromkeys(target_ids):
                target = self.db.get(NewApiTarget, target_id)
                if not target or not target.enabled:
                    raise ValueError("所选后台不存在或已停用")
                existing = self.db.scalar(select(ChannelUpload).where(
                    ChannelUpload.base_url == target.base_url, ChannelUpload.fingerprint == upload.fingerprint))
                if not existing and upload.base_url != "vault":
                    try:
                        same_destination = NewApiTargetInput.normalize_url(upload.base_url) == target.base_url
                    except ValueError:
                        same_destination = False
                    if same_destination:
                        existing = upload
                        existing.base_url = target.base_url
                if existing:
                    if existing.user_id != upload.user_id or (existing.id != upload.id and existing.source_upload_id != upload.id):
                        raise ValueError("该后台已有此凭据的其他归属记录，请核对历史数据")
                    # Adopt an original pre-vault push in place, preserving its channel ID and usage.
                    existing.target_id = target.id
                    existing.config_source = "managed"
                    if not existing.remote_channel_id and existing.status not in {"UNKNOWN", "CREATING", "TESTING"}:
                        existing.payload_json = json.dumps(service.payload(upload.kind, existing.name, target))
                    row = existing
                else:
                    push_id = str(uuid.uuid4())
                    row = ChannelUpload(
                        id=push_id, user_id=upload.user_id, request_id=str(uuid.uuid4()),
                        source_upload_id=upload.id, target_id=target.id, base_url=target.base_url,
                        origin=upload.origin, origin_env_id=upload.origin_env_id, origin_task_id=upload.origin_task_id,
                        proxy_encrypted=upload.proxy_encrypted,
                        config_source="managed", kind=upload.kind, name="gcph-upload-" + push_id,
                        remark=upload.remark, identity=upload.identity, fingerprint=upload.fingerprint,
                        credential_encrypted=None, payload_json=json.dumps(service.payload(upload.kind, "gcph-upload-" + push_id, target)),
                        status="PENDING", message="等待管理员推送任务执行", quota_per_unit=target.quota_per_unit,
                        currency=target.currency,
                    )
                    self.db.add(row)
                self.audit("CHANNEL_ADMIN_PUSH", row.id, {"uploadId": upload.id, "targetId": target.id})
                if upload.origin_env_id:
                    from app.models.gcp_environment import GcpEnvironment
                    from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus, ChannelPushAction
                    from datetime import datetime
                    if self.db.get(GcpEnvironment, upload.origin_env_id):
                        self.db.add(ChannelPushRecord(env_id=upload.origin_env_id, user_id=upload.user_id,
                            actor_user_id=self.actor.id, target_id=target.id, deployment_id=row.id,
                            task_id=upload.origin_task_id, channel_name=row.name,
                            template_name=target.vertex_template or "内置 Vertex 模板",
                            status=ChannelPushStatus.SUCCESS if row.status == "ACTIVE" else ChannelPushStatus.PENDING,
                            action=ChannelPushAction.ALREADY_EXISTS if row.status == "ACTIVE" else None,
                            channel_id=row.remote_channel_id, message=row.message,
                            finished_at=datetime.utcnow() if row.status == "ACTIVE" else None))
                results.append(row)
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("推送记录已存在，请刷新后重试") from None
        except Exception:
            self.db.rollback()
            raise
        return results

    def delete_target(self, identifier: str) -> None:
        target = self.db.get(NewApiTarget, identifier)
        if not target:
            raise KeyError("目标后台不存在")
        upload = self.db.scalar(select(ChannelUpload.id).where(or_(
            ChannelUpload.target_id == identifier, ChannelUpload.base_url == target.base_url,
        )).limit(1))
        history = self.db.scalar(select(ChannelPushRecord.id).where(
            ChannelPushRecord.target_id == identifier).limit(1))
        if upload or history:
            raise ValueError("该后台已关联推送或历史渠道记录，不能删除")
        # Legacy connections are imported on startup. Keep their destination
        # identity rather than deleting it only to recreate it on restart.
        for legacy_url in self.db.scalars(select(UserChannelConfig.base_url)):
            try:
                normalized_url = NewApiTargetInput.normalize_url(legacy_url)
            except ValueError:
                continue
            if normalized_url == target.base_url:
                raise ValueError("该后台仍关联旧版个人后台配置，不能删除")
        self.db.execute(update(User).where(User.default_target_id == identifier).values(default_target_id=None))
        self.db.execute(update(User).where(User.bound_target_id == identifier).values(bound_target_id=None))
        self.audit("NEW_API_TARGET_DELETE", identifier)
        self.db.delete(target)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("该后台仍有关联数据，不能删除，请刷新后重试") from None

    def adopt_legacy(self, identifier: str, record_id: str, target_id: str) -> ChannelUpload:
        from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
        from app.services.channel_statistics_service import ChannelStatisticsService
        from app.services.channel_upload_service import UploadNewApiClient
        from datetime import datetime
        source = self.upload(identifier)
        history = self.db.get(ChannelPushRecord, record_id)
        target = self.db.get(NewApiTarget, target_id)
        if not history or not source.origin_env_id or history.env_id != source.origin_env_id:
            raise ValueError("历史推送记录与该凭据来源不匹配")
        if history.deployment_id:
            raise ValueError("该历史记录已经关联，请刷新查看")
        if not target or not target.enabled:
            raise ValueError("请选择已启用的目标后台")
        from app.services.channel_push_service import NewApiChannelConfig
        client = UploadNewApiClient(self.db, user_id=source.user_id)
        config = NewApiChannelConfig(base_url=target.base_url,
            token=SecretService().decrypt_optional(target.token_encrypted) or "")
        channel = client._find_channel(config, history.channel_name)
        remote_id = client._normalize_channel_id(channel.get("id")) if channel else ""
        if not remote_id or (history.channel_id and remote_id != history.channel_id):
            raise ValueError("目标后台的渠道名称或 ID 与历史记录不一致，未关联")
        bound = self.db.scalar(select(ChannelUpload).where(ChannelUpload.base_url == target.base_url,
            ChannelUpload.remote_channel_id == remote_id))
        if bound and bound.id != source.id and bound.source_upload_id != source.id:
            raise ValueError("该远程渠道已关联其他凭据，不能重复归属或重复统计")
        deployment = self.prepare_pushes(source.id, [target_id])[0]
        if deployment.remote_channel_id and deployment.remote_channel_id != remote_id:
            raise ValueError("已有推送记录关联了其他渠道，不能覆盖")
        deployment.remote_channel_id = remote_id
        deployment.remote_status = str(channel.get("status", "UNKNOWN"))
        deployment.status = "ACTIVE" if deployment.remote_status == "1" else "TEST_FAILED"
        deployment.message = "管理员已核对并关联历史渠道"
        quota, error = ChannelStatisticsService._parse_quota(channel.get("used_quota", channel.get("usedQuota")))
        if quota is not None and not error and (deployment.used_quota is None or quota >= deployment.used_quota):
            deployment.used_quota = quota
            deployment.synced_at = datetime.utcnow()
        history.actor_user_id = history.actor_user_id or history.user_id
        history.channel_id = remote_id
        history.user_id = source.user_id
        history.target_id, history.deployment_id = target.id, deployment.id
        if history.status == ChannelPushStatus.PENDING:
            history.status = ChannelPushStatus.SUCCESS if deployment.status == "ACTIVE" else ChannelPushStatus.FAILED
            history.message, history.finished_at = deployment.message, datetime.utcnow()
        for attempt in self.db.scalars(select(ChannelPushRecord).where(ChannelPushRecord.deployment_id == deployment.id,
                ChannelPushRecord.status == ChannelPushStatus.PENDING)):
            attempt.status = ChannelPushStatus.SUCCESS if deployment.status == "ACTIVE" else ChannelPushStatus.FAILED
            attempt.channel_id, attempt.message, attempt.finished_at = remote_id, deployment.message, datetime.utcnow()
        self.audit("GCP_LEGACY_CHANNEL_ADOPT", deployment.id, {"recordId": history.id, "targetId": target.id})
        self.db.commit()
        return deployment
