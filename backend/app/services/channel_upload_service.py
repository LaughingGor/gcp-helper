"""Durable upload workflow. Remote creation is never blindly retried after a timeout."""
import hashlib
import hmac
import json
import threading
import uuid
from urllib.parse import quote, urljoin
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.channel_upload import ChannelUpload, ChannelUsageSnapshot, NewApiTarget
from app.schemas.channel_upload import ChannelUploadInput, CreateChannelUpload
from app.services.channel_push_service import ChannelPushService, NewApiChannelConfig
from app.services.channel_statistics_service import ChannelStatisticsService
from app.services.secret_service import SecretService, get_app_secret_key

# The deployed application deliberately runs a single worker (like the login runner).
# One lock serializes upload retries and refreshes; database constraints also prevent duplicate submissions.
_workflow_lock = threading.Lock()
ACTIVE_STATES = {"PENDING", "CREATING", "TESTING"}


def validate_credential(req: ChannelUploadInput) -> tuple[str, str]:
    value = req.credential.get_secret_value().strip()
    if len(value.encode("utf-8")) > 65536:
        raise ValueError("凭据不能超过 64 KB")
    if req.kind == "gemini_key":
        if not 16 <= len(value) <= 4096 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("API Key 必须为 16～4096 位且不能包含空白字符")
        return value, "API Key ····" + value[-4:]
    try:
        data = json.loads(value)
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ValueError()
        for field in ("project_id", "client_email", "private_key_id", "private_key"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise ValueError()
        if len(data["client_email"]) > 254 or len(data["project_id"]) > 128 or "@" not in data["client_email"]:
            raise ValueError()
        key = load_pem_private_key(data["private_key"].encode(), password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError()
        # Do not forward arbitrary token endpoints supplied in a user-controlled JSON file.
        if data.get("token_uri") not in (None, "https://oauth2.googleapis.com/token"):
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        raise ValueError("请提供包含有效 RSA 私钥、项目 ID 和邮箱的 Service Account JSON") from None
    data.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    normalized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return normalized, f'{data["project_id"]} / {data["client_email"]}'


class UploadNewApiClient(ChannelPushService):
    """Never let upstream error bodies (which may echo credentials) reach logs or API errors."""

    def _request_new_api_json(self, *args, **kwargs):
        try:
            response = super()._request_new_api_json(*args, **kwargs)
            if not isinstance(response, dict):
                raise ValueError()
            if "message" in response:
                response["message"] = "远程后台操作未成功" if response.get("success") is False else ""
            return response
        except Exception:
            raise RuntimeError("远程后台请求失败，请检查连接配置和后台日志") from None

    def get_channel(self, config: NewApiChannelConfig, channel_id: str) -> dict:
        url = urljoin(config.base_url.rstrip("/") + "/", "api/channel/" + quote(channel_id, safe=""))
        response = self._request_new_api_json(config, url, method="GET")
        channel = response.get("data")
        if response.get("success") is not True or not isinstance(channel, dict):
            raise ValueError("无法读取原渠道详情，请确认后台支持渠道详情接口")
        if self._normalize_channel_id(channel.get("id")) != channel_id:
            raise ValueError("远端渠道 ID 不一致，已停止替换")
        return channel

    def update_channel(self, config: NewApiChannelConfig, payload: dict) -> None:
        # new-api's standard UpdateChannel endpoint takes a flat Channel object.
        url = urljoin(config.base_url.rstrip("/") + "/", "api/channel/")
        response = self._request_new_api_json(config, url, method="PUT",
            body=json.dumps({k: v for k, v in payload.items() if k != "status"}, separators=(",", ":")).encode())
        if response.get("success") is not True:
            raise RuntimeError("后台未确认渠道更新成功，请核对后台版本及更新接口")

    def set_channel_status(self, config: NewApiChannelConfig, channel_id: str, status: int) -> None:
        if status not in (1, 2):
            raise ValueError("仅支持启用或手动禁用渠道")
        url = urljoin(config.base_url.rstrip("/") + "/", "api/channel/" + quote(channel_id, safe="") + "/status")
        response = self._request_new_api_json(config, url, method="POST", body=json.dumps({"status": status}).encode())
        # data=false can mean the requested status was already set. Verify by ID.
        if response.get("success") is not True or self.get_channel(config, channel_id).get("status") != status:
            raise RuntimeError("后台未确认渠道启停状态，请核对")


class ChannelUploadService:
    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id
        self.settings = get_settings()
        self.remote = UploadNewApiClient(db, user_id=user_id)

    def config(self, row: ChannelUpload | None = None) -> NewApiChannelConfig:
        if row and row.target_id:
            target = self.db.get(NewApiTarget, row.target_id)
            if not target or not target.enabled or target.base_url != row.base_url:
                raise ValueError("目标后台已停用或地址不一致")
            token = SecretService().decrypt_optional(target.token_encrypted)
            if not token:
                raise ValueError("目标后台 Token 未配置")
            return NewApiChannelConfig(base_url=target.base_url, token=token)
        if row and row.config_source == "vault":
            raise ValueError("凭据已保存，等待管理员选择目标后台推送")
        source = row.config_source if row else ("shared" if self.settings.new_api_upload_base_url else "personal")
        if source == "shared":
            config = NewApiChannelConfig(
                base_url=self.settings.new_api_upload_base_url.rstrip("/"),
                token=self.settings.new_api_upload_token,
            )
            if not config.base_url.startswith(("https://", "http://")) or not config.token:
                raise ValueError("管理员尚未完整配置共享渠道后台")
        else:
            from app.services.user_channel_config_service import UserChannelConfigService
            legacy = UserChannelConfigService(self.db)
            stored = legacy.get(self.user_id)
            if not stored:
                raise ValueError("历史后台连接不存在，请管理员关联后台")
            config = NewApiChannelConfig(base_url=stored.base_url.rstrip("/"), token=legacy.decrypt_token(stored))
        if row and row.base_url != config.base_url:
            raise ValueError("渠道后台地址已变更，请恢复原地址后同步；历史记录不会迁移到新后台")
        return config

    def fingerprint(self, kind: str, credential: str) -> str:
        secret = get_app_secret_key(require_stable=True)
        # A service-account key remains the same credential even if optional JSON fields differ.
        if kind == "vertex_json":
            data = json.loads(credential)
            credential = load_pem_private_key(data["private_key"].encode(), None).public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo).hex()
        return hmac.new(secret.encode(), (kind + ":" + credential).encode(), hashlib.sha256).hexdigest()

    def payload(self, kind: str, name: str, target: NewApiTarget | None = None) -> dict:
        template = ((target.vertex_template if kind == "vertex_json" else target.gemini_template) if target else
                    (self.settings.new_api_upload_vertex_template if kind == "vertex_json"
                     else self.settings.new_api_upload_gemini_template))
        if template:
            payload = self.remote._load_template(template)
            self.remote._normalize_new_api_payload(payload)
        else:
            payload = {"mode": "single", "channel": {
                "type": 41 if kind == "vertex_json" else 24,
                "models": target.models if target else self.settings.new_api_upload_models,
                "group": target.group if target else self.settings.new_api_upload_group,
            }}
            if kind == "vertex_json":
                payload["channel"]["other"] = target.vertex_region if target else self.settings.new_api_upload_vertex_region
        channel = payload["channel"]
        channel.pop("id", None)
        channel.pop("key", None)
        channel.update(name=name, status=1)
        # A single submission must create exactly one channel, even with a custom template.
        payload["mode"] = "single"
        return payload

    def create(self, req: CreateChannelUpload) -> ChannelUpload:
        value, identity = validate_credential(req)
        fingerprint = self.fingerprint(req.kind, value)
        existing = self.db.scalar(select(ChannelUpload).where(
            ChannelUpload.user_id == self.user_id, ChannelUpload.request_id == str(req.request_id)))
        if existing:
            if existing.fingerprint != fingerprint or existing.kind != req.kind:
                raise ValueError("相同请求 ID 不能用于不同凭据")
            if not existing.credential_encrypted:
                if existing.identity != identity:
                    raise ValueError("凭据身份信息与已有记录不一致")
                existing.credential_encrypted = SecretService().encrypt_optional(value)
                self.db.add(AuditLog(actor=self.user_id, action="CHANNEL_CREDENTIAL_RESTORE", target_type="channel_upload", target_id=existing.id))
                self.db.commit()
            return existing
        existing = self.db.scalar(select(ChannelUpload).where(
            ChannelUpload.fingerprint == fingerprint, ChannelUpload.source_upload_id.is_(None))
            .order_by(ChannelUpload.created_at))
        if existing:
            if existing.user_id != self.user_id:
                raise ValueError("该凭据已上传")
            if existing.identity != identity:
                raise ValueError("凭据身份信息与已有记录不一致")
            # A re-upload can restore a key erased by the previous temporary-retention policy.
            existing.credential_encrypted = SecretService().encrypt_optional(value)
            self.db.add(AuditLog(actor=self.user_id, action="CHANNEL_CREDENTIAL_RESTORE", target_type="channel_upload", target_id=existing.id))
            self.db.commit()
            return existing
        if self.settings.new_api_quota_per_unit <= 0:
            raise ValueError("quota 换算比例必须大于零")
        identifier = str(uuid.uuid4())
        row = ChannelUpload(
            id=identifier, user_id=self.user_id, request_id=str(req.request_id),
            base_url="vault", config_source="vault", status="STORED", message="凭据已加密保存，等待管理员推送",
            name="gcph-upload-" + identifier, kind=req.kind, remark=req.remark.strip(), identity=identity,
            fingerprint=fingerprint, credential_encrypted=SecretService().encrypt_optional(value),
            payload_json="{}",
            quota_per_unit=self.settings.new_api_quota_per_unit, currency=self.settings.new_api_currency.upper(),
        )
        self.db.add(row)
        self.db.add(AuditLog(actor=self.user_id, action="CHANNEL_UPLOAD", target_type="channel_upload", target_id=identifier))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("该凭据已上传或请求正在处理，请刷新列表") from None
        return row

    def owned(self, identifier: str) -> ChannelUpload:
        row = self.db.get(ChannelUpload, identifier)
        if not row or row.user_id != self.user_id:
            raise KeyError("上传记录不存在")
        return row

    def execute(self, identifier: str) -> None:
        row = self.owned(identifier)
        if row.origin_env_id:
            from app.services.project_replacement_service import ensure_no_replacement
            ensure_no_replacement(self.db, row.origin_env_id)
        if row.status in {"ACTIVE", "STORED"}:
            return
        prior_status = row.status
        try:
            config = self.config(row)
            # Reconcile by the unguessable local name before any remote write.
            if not row.remote_channel_id:
                existing = self.remote._find_channel(config, row.name)
                if existing:
                    row.remote_channel_id = self.remote._normalize_channel_id(existing.get("id")) or None
                    if not row.remote_channel_id:
                        raise RuntimeError()
                    self.db.commit()
                elif prior_status in {"UNKNOWN", "CREATING", "TESTING"}:
                    row.status = "UNKNOWN"
                    row.message = "远程创建结果不明确，尚未找到渠道；请管理员核对，系统不会重复创建"
                    self.db.commit()
                    return
            if not row.remote_channel_id:
                source = self.db.get(ChannelUpload, row.source_upload_id) if row.source_upload_id else row
                if not source or not source.credential_encrypted:
                    row.status = "EXPIRED"
                    row.message = "历史凭据已清除，请原用户重新上传后由管理员推送"
                    self.db.commit()
                    return
                if source.origin_env_id:
                    from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
                    histories = self.db.scalars(select(ChannelPushRecord).where(
                        ChannelPushRecord.env_id == source.origin_env_id,
                        ChannelPushRecord.deployment_id.is_(None)))
                    checked = set()
                    for history in histories:
                        if history.channel_name in checked:
                            continue
                        checked.add(history.channel_name)
                        if self.remote._find_channel(config, history.channel_name):
                            row.status = "LEGACY_REVIEW"
                            row.message = "检测到历史同名渠道，请管理员在凭据管理中关联历史渠道后再重试"
                            self.db.commit()
                            return
                payload = json.loads(row.payload_json)
                payload["channel"]["key"] = SecretService().decrypt_optional(source.credential_encrypted)
                proxy_encrypted = row.proxy_encrypted if row.source_upload_id else source.proxy_encrypted
                if proxy_encrypted:
                    setting = payload["channel"].get("setting") or {}
                    if isinstance(setting, str):
                        setting = json.loads(setting)
                    setting["proxy"] = SecretService().decrypt_optional(proxy_encrypted)
                    payload["channel"]["setting"] = json.dumps(setting, separators=(",", ":"))
                row.status = "CREATING"
                row.message = "正在创建渠道"
                self.db.commit()
                response = self.remote._post_new_api_channel(config, payload)
                if response.get("success") is not True:
                    row.status = "REJECTED"
                    row.message = "后台拒绝创建，请检查渠道类型和管理员模板后重试"
                    self.db.commit()
                    return
                row.remote_channel_id = self.remote._resolve_created_channel_id(config, response, row.name)
                self.db.commit()
            row.status = "TESTING"
            row.message = "渠道已创建，正在测试"
            # Keep the source credential encrypted for administrator inspection and later deployments.
            self.db.commit()
            try:
                # Existing project behavior: test enabled channel; on failure disable it.
                self.remote._test_new_api_channel(config, row.remote_channel_id)
                self.remote._enable_new_api_channel(config, row.remote_channel_id)
            except Exception:
                row.status = "TEST_FAILED"
                row.message = "渠道测试或启用失败，可重试测试"
                try:
                    self.remote._disable_new_api_channel(config, row.remote_channel_id)
                    row.remote_status = "2"
                    row.message += "；已禁用渠道"
                except Exception:
                    row.message += "；自动禁用失败，请管理员立即核对远程状态"
                self.db.commit()
                return
            row.status = "ACTIVE"
            row.remote_status = "1"
            row.message = "渠道已创建、测试并启用"
            self.db.commit()
        except Exception:
            self.db.rollback()
            row = self.owned(identifier)
            row.status = "TEST_FAILED" if row.remote_channel_id else (
                "UNKNOWN" if row.status == "CREATING" or prior_status in {"UNKNOWN", "CREATING", "TESTING"} else "BLOCKED")
            row.message = "后台操作未完成，请核对连接配置并重试；远程创建结果不明确时不会重复创建"
            self.db.commit()

    def refresh(self, remote_pages: dict | None = None) -> None:
        rows = list(self.db.scalars(select(ChannelUpload).where(ChannelUpload.user_id == self.user_id,
                                                               ChannelUpload.remote_channel_id.is_not(None))))
        groups: dict[tuple[str, str, str | None], list[ChannelUpload]] = {}
        for row in rows:
            groups.setdefault((row.base_url, row.config_source, row.target_id), []).append(row)
        for items in groups.values():
            if all(r.last_attempt_at and r.last_attempt_at > datetime.utcnow() - timedelta(
                    seconds=max(10, self.settings.channel_upload_sync_seconds)) for r in items):
                continue
            now = datetime.utcnow()
            for row in items:
                row.last_attempt_at = now
            try:
                config = self.config(items[0])
                cache_key = (config.base_url, hashlib.sha256(config.token.encode()).hexdigest())
                cached = remote_pages.get(cache_key) if remote_pages is not None else None
                if cached is None:
                    statistics = ChannelStatisticsService(self.db, user_id=self.user_id)
                    statistics.channel_service = self.remote
                    channels, partial, _ = statistics._fetch_all_channels(config)
                    # A cycle may serve many owners sharing one management token. Retain no remote keys.
                    cached = ([{key: channel[key] for key in ("id", "status", "used_quota", "usedQuota") if key in channel}
                               for channel in channels], partial)
                    if remote_pages is not None:
                        remote_pages[cache_key] = cached
                channels, partial = cached
                by_id = {str(c.get("id")): c for c in channels}
                for row in items:
                    channel = by_id.get(row.remote_channel_id)
                    if channel is None:
                        row.sync_error = "后台返回不完整，保留历史值" if partial else "远程渠道已删除或不可见，保留历史值"
                        if not partial:
                            row.remote_status = "MISSING"
                        continue
                    quota, error = ChannelStatisticsService._parse_quota(channel.get("used_quota", channel.get("usedQuota")))
                    row.remote_status = str(channel.get("status", "UNKNOWN"))
                    if error or quota is None or (row.used_quota is not None and quota < row.used_quota):
                        row.sync_error = "消耗数据无效或计数器已重置，保留上次值，请管理员核对"
                        continue
                    row.used_quota = quota
                    row.synced_at = now
                    row.sync_error = "后台分页不完整，当前渠道已同步" if partial else None
                    self.db.add(ChannelUsageSnapshot(upload_id=row.id, used_quota=quota, sampled_at=now))
            except Exception:
                for row in items:
                    row.sync_error = "同步失败，请检查原后台地址和 Token；保留上次有效消耗"
            self.db.commit()

    @staticmethod
    def public(row: ChannelUpload) -> dict:
        amount = row.used_quota / row.quota_per_unit if row.used_quota is not None else None
        return {
            "id": row.id, "kind": row.kind, "name": row.name, "remark": row.remark,
            "identity": row.identity, "status": row.status, "message": row.message,
            "channelId": row.remote_channel_id, "remoteStatus": row.remote_status,
            "currency": row.currency, "usedAmount": str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if amount is not None else None,
            "syncedAt": row.synced_at, "syncError": row.sync_error, "createdAt": row.created_at,
            "sourceUploadId": row.source_upload_id, "targetId": row.target_id,
            "targetName": row.target.name if row.target else ("历史后台" if row.remote_channel_id else None),
            "credentialStored": bool(row.credential_encrypted) if not row.source_upload_id else False,
            "origin": row.origin, "originEnvId": row.origin_env_id, "originTaskId": row.origin_task_id,
        }

    def public_with_deployments(self, row: ChannelUpload) -> dict:
        return {**self.public(row), "deployments": [self.public(child) for child in row.deployments]}

    def summary(self) -> dict:
        rows = list(self.db.scalars(select(ChannelUpload).where(ChannelUpload.user_id == self.user_id)))
        amounts: dict[str, Decimal] = {}
        for row in rows:
            if row.used_quota is not None:
                amounts[row.currency] = amounts.get(row.currency, Decimal(0)) + row.used_quota / row.quota_per_unit
        return {"channelCount": sum(r.source_upload_id is None for r in rows), "deploymentCount": sum(r.target_id is not None or r.remote_channel_id is not None for r in rows),
                "activeCount": sum(r.remote_status == "1" for r in rows),
                "pendingCount": sum(r.status in ACTIVE_STATES for r in rows),
                "incompleteCount": sum(bool(r.sync_error) or r.synced_at is None for r in rows if r.remote_channel_id),
                "totals": {c: str(a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for c, a in amounts.items()}}


def run_channel_upload(identifier: str, user_id: str) -> None:
    with _workflow_lock, SessionLocal() as db:
        service = ChannelUploadService(db, user_id)
        service.execute(identifier)
        row = db.get(ChannelUpload, identifier)
        if row:
            from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus, ChannelPushAction
            for attempt in db.scalars(select(ChannelPushRecord).where(ChannelPushRecord.deployment_id == identifier,
                    ChannelPushRecord.status == ChannelPushStatus.PENDING)):
                attempt.channel_id = row.remote_channel_id
                attempt.status = ChannelPushStatus.SUCCESS if row.status == "ACTIVE" else ChannelPushStatus.FAILED
                attempt.action = ChannelPushAction.CREATED if row.status == "ACTIVE" else None
                attempt.message = row.message
                attempt.finished_at = datetime.utcnow()
            db.commit()


def refresh_channel_uploads(user_id: str, remote_pages: dict | None = None) -> None:
    with _workflow_lock, SessionLocal() as db:
        ChannelUploadService(db, user_id).refresh(remote_pages)


def recover_channel_uploads() -> None:
    with SessionLocal() as db:
        for row in db.scalars(select(ChannelUpload).where(ChannelUpload.status.in_(ACTIVE_STATES))):
            row.status = "TEST_FAILED" if row.remote_channel_id else ("PENDING" if row.status == "PENDING" else "UNKNOWN")
            row.message = "服务已重启，请重试；系统会先核对远程渠道，避免重复创建"
        from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
        for attempt in db.scalars(select(ChannelPushRecord).where(ChannelPushRecord.status == ChannelPushStatus.PENDING)):
            deployment = db.get(ChannelUpload, attempt.deployment_id) if attempt.deployment_id else None
            attempt.status = ChannelPushStatus.SUCCESS if deployment and deployment.status == "ACTIVE" else ChannelPushStatus.FAILED
            attempt.channel_id = deployment.remote_channel_id if deployment else None
            attempt.message = "渠道状态已恢复" if attempt.status == ChannelPushStatus.SUCCESS else "服务重启中断推送，请重试或核对远程结果"
            attempt.finished_at = datetime.utcnow()
        db.commit()


_stop_worker = threading.Event()
_worker: threading.Thread | None = None


def _usage_loop() -> None:
    while not _stop_worker.wait(max(10, get_settings().channel_upload_sync_seconds)):
        try:
            with SessionLocal() as db:
                owners = list(db.scalars(select(ChannelUpload.user_id).where(
                    ChannelUpload.remote_channel_id.is_not(None)).distinct()))
            remote_pages: dict = {}
            for owner_id in owners:
                if _stop_worker.is_set():
                    return
                refresh_channel_uploads(owner_id, remote_pages)
            with SessionLocal() as db:
                db.execute(delete(ChannelUsageSnapshot).where(ChannelUsageSnapshot.sampled_at <
                    datetime.utcnow() - timedelta(days=max(1, get_settings().channel_upload_snapshot_days))))
                db.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("channel upload usage worker could not complete this cycle")


def start_usage_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop_worker.clear()
    _worker = threading.Thread(target=_usage_loop, name="channel-upload-usage", daemon=True)
    _worker.start()


def stop_usage_worker() -> None:
    _stop_worker.set()
