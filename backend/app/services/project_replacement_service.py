"""Prepare a fresh project, then rotate one explicitly linked remote channel in place."""
import json
import threading
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import ensure_environment_access
from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.channel_upload import ChannelUpload
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus, PresetTaskType
from app.models.project_replacement import ProjectReplacement
from app.models.user import User, UserStatus
from app.schemas.user_settings import UpdateUserSettingsRequest
from app.schemas.gcp_env import CreateProjectReplacement
from app.services.auth_service import AuthService
from app.services.channel_upload_service import ChannelUploadService, _workflow_lock
from app.services.gcp_channel_service import register_gcp_credential
from app.services.new_api_target_service import resolve_target
from app.services.preset_task_service import PresetTaskExecutor, _bootstrap_semaphore, _task_creation_lock
from app.services.secret_service import SecretService, get_app_secret_key

BUSY = {"PENDING", "PREPARING", "READY", "UPDATING", "TESTING", "ROLLING_BACK", "UNKNOWN"}
_running: set[str] = set()
_running_lock = threading.Lock()
# Preserve channel configuration; never send read-only counters/timestamps back to new-api.
CONFIG_FIELDS = {"id", "type", "name", "status", "weight", "priority", "models", "group", "base_url",
    "other", "setting", "model_mapping", "status_code_mapping", "auto_ban", "test_model", "tag",
    "param_override", "header_override", "settings", "remark", "openai_organization"}


def ensure_no_replacement(db: Session, env_id: str, *, except_id: str | None = None) -> None:
    query = select(ProjectReplacement.id).where(ProjectReplacement.env_id == env_id,
        ProjectReplacement.status.in_(BUSY))
    if except_id:
        query = query.where(ProjectReplacement.id != except_id)
    if db.scalar(query):
        raise RuntimeError("环境存在进行中或结果待核对的项目替换，请先完成该操作")


class ProjectReplacementService:
    def __init__(self, db: Session, actor: User):
        self.db, self.actor = db, actor

    def environment(self, env_id: str) -> GcpEnvironment:
        env = self.db.get(GcpEnvironment, env_id)
        if not env:
            raise KeyError("环境不存在")
        ensure_environment_access(self.actor, env)
        return env

    def deployment(self, env: GcpEnvironment, target_id: str | None) -> ChannelUpload:
        target = resolve_target(self.db, self.actor.id, target_id)
        rows = list(self.db.scalars(select(ChannelUpload).where(
            ChannelUpload.origin_env_id == env.id, ChannelUpload.user_id == env.owner_user_id,
            ChannelUpload.target_id == target.id, ChannelUpload.remote_channel_id.is_not(None))))
        if not rows:
            raise ValueError("当前后台没有已关联的渠道，请先推送渠道；历史渠道须先由管理员关联")
        if len(rows) != 1:
            raise ValueError("当前后台存在多条历史渠道，请先由管理员核对，系统不会猜测待替换渠道")
        row = rows[0]
        if row.status not in {"ACTIVE", "TEST_FAILED"} or not row.source_upload_id or row.kind != "vertex_json":
            raise ValueError("渠道尚未完成关联或状态不明确，请先核对原推送记录")
        source = self.db.get(ChannelUpload, row.source_upload_id)
        if not source or source.user_id != env.owner_user_id or not source.credential_encrypted:
            raise ValueError("原凭据不可用，无法提供失败恢复，请先补录凭据")
        return row

    def preview(self, env_id: str, target_id: str | None) -> dict:
        env = self.environment(env_id)
        row = self.deployment(env, target_id)
        task = self.db.get(GcpPresetTask, row.origin_task_id) if row.origin_task_id else None
        return {"deploymentId": row.id, "targetId": row.target_id, "targetName": row.target.name,
            "channelId": row.remote_channel_id, "channelName": row.name,
            "projectId": task.project_id if task else env.default_project_id,
            "billingAccountId": env.billing_account_id,
            "vmZone": (task.requested_vm_zone or task.vm_zone) if task else env.default_zone}

    def create(self, env_id: str, req: CreateProjectReplacement) -> ProjectReplacement:
        with _task_creation_lock:
            env = self.environment(env_id)
            resolve_target(self.db, self.actor.id, str(req.target_id))
            existing = self.db.scalar(select(ProjectReplacement).where(
                ProjectReplacement.actor_user_id == self.actor.id, ProjectReplacement.request_id == str(req.request_id)))
            if existing:
                if existing.env_id != env_id or existing.deployment_id != str(req.deployment_id) or existing.target_id != str(req.target_id):
                    raise ValueError("请求 ID 已用于其他操作")
                return existing
            row = self.deployment(env, str(req.target_id))
            if row.id != str(req.deployment_id):
                raise ValueError("待替换渠道已变化，请重新打开弹窗")
            self._check_ready(env)
            ensure_no_replacement(self.db, env.id)
            get_app_secret_key(require_stable=True)
            zone = UpdateUserSettingsRequest.validate_zone(req.vm_zone or self.preview(env_id, row.target_id)["vmZone"]
                or PresetTaskExecutor._resolve_compute_vm_zone(env, ChannelUploadService(self.db, row.user_id).settings.compute_vm_zone))
            task = GcpPresetTask(env_id=env.id, task_type=PresetTaskType.BOOTSTRAP_PROJECT,
                status=PresetTaskStatus.PENDING, current_step="PENDING", requested_vm_zone=zone,
                force_new_project=True, project_id=PresetTaskExecutor._generate_project_id())
            self.db.add(task)
            self.db.flush()
            op = ProjectReplacement(env_id=env.id, task_id=task.id, actor_user_id=self.actor.id, executor_user_id=self.actor.id,
                deployment_id=row.id, old_source_id=row.source_upload_id, target_id=row.target_id,
                remote_channel_id=row.remote_channel_id, request_id=str(req.request_id),
                old_project_id=self.preview(env_id, row.target_id)["projectId"], billing_account_id=env.billing_account_id)
            self.db.add(op)
            self.db.flush()
            self.audit(op, "PROJECT_REPLACEMENT_CREATED")
            self.db.commit()
            return op

    def _check_ready(self, env: GcpEnvironment, task_id: str | None = None) -> None:
        if env.status != GcpEnvironmentStatus.AUTHORIZED or not env.billing_account_id:
            raise ValueError("请先授权环境并选择 Billing Account")
        query = select(GcpPresetTask.id).where(GcpPresetTask.env_id == env.id,
            GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]))
        if task_id:
            query = query.where(GcpPresetTask.id != task_id)
        if self.db.scalar(query):
            raise ValueError("预设任务正在运行，请等待完成")

    def get(self, env_id: str, identifier: str) -> ProjectReplacement:
        self.environment(env_id)
        op = self.db.get(ProjectReplacement, identifier)
        if not op or op.env_id != env_id:
            raise KeyError("项目替换记录不存在")
        resolve_target(self.db, self.actor.id, op.target_id)
        return op

    def retry(self, env_id: str, identifier: str) -> ProjectReplacement:
        with _task_creation_lock:
            op = self.get(env_id, identifier)
            if op.status not in {"FAILED", "ROLLED_BACK", "UNKNOWN"}:
                raise ValueError("只有失败、已恢复或待核对的操作可以重试")
            self.validate(op)
            self._check_ready(self.environment(env_id), op.task_id)
            ensure_no_replacement(self.db, env_id, except_id=op.id)
            # UNKNOWN must restore the saved original before attempting another switch.
            op.status = "ROLLING_BACK" if op.status == "UNKNOWN" else "PENDING"
            op.message = "等待恢复原配置" if op.status == "ROLLING_BACK" else "等待继续替换；复用已创建资源"
            op.executor_user_id = self.actor.id
            self.audit(op, "PROJECT_REPLACEMENT_RETRY")
            self.db.commit()
            return op

    def validate(self, op: ProjectReplacement) -> tuple[GcpEnvironment, ChannelUpload]:
        if self.actor.status != UserStatus.ACTIVE or not all(AuthService.has_permission(self.actor, p)
                for p in ("task:run", "channel:push")):
            raise ValueError("操作用户已停用或权限已变更")
        env = self.environment(op.env_id)
        if env.status != GcpEnvironmentStatus.AUTHORIZED:
            raise ValueError("环境已失去授权，请重新授权后重试")
        resolve_target(self.db, self.actor.id, op.target_id)
        row = self.db.get(ChannelUpload, op.deployment_id)
        if not row or row.origin_env_id != env.id or row.user_id != env.owner_user_id or row.target_id != op.target_id \
                or row.remote_channel_id != op.remote_channel_id or row.source_upload_id != op.old_source_id:
            raise ValueError("原渠道归属或凭据关联已变化，已停止替换")
        if env.billing_account_id != op.billing_account_id:
            raise ValueError("Billing Account 已变化，不能继续本次替换")
        return env, row

    def audit(self, op: ProjectReplacement, action: str) -> None:
        self.db.add(AuditLog(actor=self.actor.username, action=action,
            target_type="project_replacement", target_id=op.id))

    def public(self, op: ProjectReplacement) -> dict:
        task = self.db.get(GcpPresetTask, op.task_id)
        return {"id": op.id, "envId": op.env_id, "taskId": op.task_id, "targetId": op.target_id,
            "deploymentId": op.deployment_id, "channelId": op.remote_channel_id,
            "oldProjectId": op.old_project_id, "newProjectId": task.project_id if task else None,
            "vmZone": task.requested_vm_zone if task else None, "status": op.status, "message": op.message,
            "currentStep": task.current_step if op.status == "PREPARING" and task else op.status,
            "createdAt": op.created_at, "updatedAt": op.updated_at}

    def mark(self, op: ProjectReplacement, status: str, message: str) -> None:
        op.status, op.message = status, message
        self.db.commit()

    def snapshot(self, op: ProjectReplacement, row: ChannelUpload, remote: dict) -> dict:
        if remote.get("name") != row.name or remote.get("type") != 41 or remote.get("status") not in (1, 2, 3):
            raise ValueError("远端渠道名称、类型或状态不一致，请先核对")
        channel_info = remote.get("channel_info") or {}
        if not isinstance(channel_info, dict) or channel_info.get("is_multi_key"):
            raise ValueError("仅支持单 Key 渠道替换，请先由管理员核对多 Key 配置")
        source = self.db.get(ChannelUpload, op.old_source_id)
        credential = SecretService().decrypt_optional(source.credential_encrypted) if source else None
        if not credential:
            raise ValueError("原凭据不可用")
        original = {k: v for k, v in remote.items() if k in CONFIG_FIELDS}
        original["key"] = credential
        return {"channel": original, "localStatus": row.status, "remoteStatus": str(remote["status"])}

    def restore(self, op: ProjectReplacement, row: ChannelUpload, service: ChannelUploadService) -> None:
        if not op.snapshot_encrypted:
            raise ValueError("没有可用的恢复快照，请管理员核对")
        saved = json.loads(SecretService().decrypt_optional(op.snapshot_encrypted))
        # Verify the remote identity before restoring; never overwrite another channel by name.
        remote = service.remote.get_channel(service.config(row), op.remote_channel_id)
        if remote.get("name") != row.name or remote.get("type") != 41:
            raise ValueError("远端渠道身份已变化，不能自动恢复")
        self.mark(op, "ROLLING_BACK", "正在恢复原渠道配置")
        service.remote.update_channel(service.config(row), saved["channel"])
        restored_status = 1 if saved["channel"]["status"] == 1 else 2
        service.remote.set_channel_status(service.config(row), op.remote_channel_id, restored_status)
        row.status, row.remote_status = saved["localStatus"], str(restored_status)
        row.message = "项目替换失败，已恢复原配置"
        self.audit(op, "PROJECT_REPLACEMENT_ROLLED_BACK")
        self.mark(op, "ROLLED_BACK", "已恢复原渠道配置；可重试替换，复用已创建的新项目")

    def execute(self, op: ProjectReplacement) -> None:
        if op.status not in {"PENDING", "ROLLING_BACK"}:
            return
        try:
            env, row = self.validate(op)
            service = ChannelUploadService(self.db, row.user_id)
            if op.status == "ROLLING_BACK":
                with _workflow_lock:
                    self.restore(op, row, service)
                return
            if op.status != "PENDING":
                return
            # Preflight read before spending cloud resources. Masked remote keys are never used for rollback.
            remote = service.remote.get_channel(service.config(row), op.remote_channel_id)
            self.snapshot(op, row, remote)
            task = self.db.get(GcpPresetTask, op.task_id)
            if not task:
                raise ValueError("新项目任务不存在")
            self.mark(op, "PREPARING", "正在准备新项目、SOCKS5 和 JSON；原渠道继续使用原配置")
            if task.status != PresetTaskStatus.SUCCESS:
                task.status, task.error_code, task.error_message, task.finished_at = PresetTaskStatus.PENDING, None, None, None
                self.db.commit()
                with _bootstrap_semaphore:
                    PresetTaskExecutor(self.db, task.id).run()
                if task.status != PresetTaskStatus.SUCCESS:
                    raise ValueError("新项目准备失败，请查看预设任务；重试将复用同一项目")
            upload = register_gcp_credential(self.db, task)
            op.new_source_id = upload.id
            self.mark(op, "READY", "新凭据已加密归档，准备替换原渠道")
            with _workflow_lock:
                self.db.refresh(row)
                self.validate(op)
                remote = service.remote.get_channel(service.config(row), op.remote_channel_id)
                snapshot = self.snapshot(op, row, remote)
                op.snapshot_encrypted = SecretService().encrypt_optional(json.dumps(snapshot))
                new_channel = dict(snapshot["channel"])
                new_channel["key"] = SecretService().decrypt_optional(upload.credential_encrypted)
                setting = new_channel.get("setting") or {}
                if isinstance(setting, str):
                    setting = json.loads(setting)
                if not isinstance(setting, dict):
                    raise ValueError("原渠道 setting 格式不受支持")
                setting = {**setting, "proxy": SecretService().decrypt_optional(upload.proxy_encrypted)}
                new_channel["setting"] = json.dumps(setting, separators=(",", ":"))
                new_channel["status"] = 1
                if not setting["proxy"]:
                    raise ValueError("新项目 SOCKS5 代理尚未归档")
                conflict = self.db.scalar(select(ChannelUpload.id).where(ChannelUpload.base_url == row.base_url,
                    ChannelUpload.fingerprint == upload.fingerprint, ChannelUpload.id != row.id))
                if conflict:
                    raise ValueError("新凭据已在当前后台关联其他渠道，请管理员核对")
                self.mark(op, "UPDATING", "正在更新原渠道的 JSON 和 SOCKS5 代理")
                service.remote.update_channel(service.config(row), new_channel)
                self.mark(op, "TESTING", "正在测试替换后的原渠道")
                service.remote.set_channel_status(service.config(row), op.remote_channel_id, 1)
                service.remote._test_new_api_channel(service.config(row), op.remote_channel_id)
                service.remote.set_channel_status(service.config(row), op.remote_channel_id, 1)
                row.source_upload_id, row.fingerprint, row.identity = upload.id, upload.fingerprint, upload.identity
                row.origin_task_id, row.proxy_encrypted = task.id, upload.proxy_encrypted
                row.status, row.remote_status, row.message = "ACTIVE", "1", "已使用新项目替换原渠道，渠道 ID 与累计消耗保留"
                env.default_project_id = task.project_id
                self.audit(op, "PROJECT_REPLACEMENT_SUCCEEDED")
                self.mark(op, "SUCCESS", row.message)
        except Exception:
            self.db.rollback()
            self.db.refresh(op)
            if op.status in {"UPDATING", "TESTING", "ROLLING_BACK"}:
                try:
                    # Authorization is checked again: revoked access must not trigger more remote writes.
                    _, row = self.validate(op)
                    with _workflow_lock:
                        self.restore(op, row, ChannelUploadService(self.db, row.user_id))
                except Exception:
                    self.db.rollback()
                    self.db.refresh(op)
                    row = self.db.get(ChannelUpload, op.deployment_id)
                    if row:
                        row.status = "UNKNOWN"
                        row.message = "项目替换结果待核对，请先通过替换操作恢复原配置"
                    self.mark(op, "UNKNOWN", "渠道更新或恢复结果待核对；重试会先恢复原配置，请勿手动重复推送")
            else:
                task = self.db.get(GcpPresetTask, op.task_id)
                if task and task.status in {PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING}:
                    task.status = PresetTaskStatus.FAILED
                    task.error_code = "PROJECT_REPLACEMENT_PREPARATION_FAILED"
                    task.error_message = "项目替换准备或校验失败，请通过替换操作重试"
                    task.finished_at = datetime.utcnow()
                self.mark(op, "FAILED", "新项目准备或替换前校验失败，原渠道配置未修改；请核对预设任务、后台和归属后重试")


def run_project_replacement(identifier: str) -> None:
    with _running_lock:
        if identifier in _running:
            return
        _running.add(identifier)
    try:
        with SessionLocal() as db:
            op = db.get(ProjectReplacement, identifier)
            actor = db.get(User, op.executor_user_id) if op else None
            if op and actor:
                ProjectReplacementService(db, actor).execute(op)
    finally:
        with _running_lock:
            _running.discard(identifier)


def recover_project_replacements() -> None:
    with SessionLocal() as db:
        for op in db.scalars(select(ProjectReplacement).where(ProjectReplacement.status.in_(BUSY))):
            uncertain = op.status in {"UPDATING", "TESTING", "ROLLING_BACK", "UNKNOWN"}
            op.status = "UNKNOWN" if uncertain else "FAILED"
            op.message = "服务重启，渠道结果待核对；重试先恢复原配置" if uncertain else "服务重启中断任务；重试复用已创建的新项目"
            if uncertain:
                row = db.get(ChannelUpload, op.deployment_id)
                if row:
                    row.status, row.message = "UNKNOWN", op.message
        db.commit()
