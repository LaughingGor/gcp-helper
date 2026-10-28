"""Bridge generated GCP keys into the same credential/deployment workflow as direct uploads."""
import uuid

from sqlalchemy.orm import Session

from app.models.channel_upload import ChannelUpload
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
from app.schemas.channel_upload import CreateChannelUpload
from app.services.channel_upload_service import ChannelUploadService
from app.services.preset_task_service import PresetTaskService
from app.services.secret_service import SecretService


def register_gcp_credential(db: Session, task: GcpPresetTask) -> ChannelUpload:
    env = task.environment
    if task.status != PresetTaskStatus.SUCCESS or not env.owner_user_id:
        raise ValueError("请先完成预设任务并确认环境归属")
    if task.credential_upload_id:
        archived = db.get(ChannelUpload, task.credential_upload_id)
        if archived and archived.user_id == env.owner_user_id and archived.credential_encrypted:
            return archived
    key_path = PresetTaskService(db).get_key_file_path(env.id, task.id)
    if key_path.stat().st_size > 65536:
        raise ValueError("服务账号 JSON 超过 64 KB")
    service = ChannelUploadService(db, env.owner_user_id)
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"gcp-helper-key:{env.id}:{task.id}")
    row = service.create(CreateChannelUpload(kind="vertex_json", credential=key_path.read_text(encoding="utf-8"),
        requestId=request_id, remark=env.env_name))
    # Keep an existing direct upload's provenance; generated credentials are otherwise marked GCP.
    if row.request_id == str(request_id):
        row.origin = "GCP"
        if row.status == "STORED":
            row.message = "GCP JSON 已加密归档，可选择后台推送"
    previous = db.get(GcpPresetTask, row.origin_task_id) if row.origin_task_id else None
    task.credential_upload_id = row.id
    if previous and previous.created_at > task.created_at:
        db.commit()
        return row
    row.origin_env_id = env.id
    row.origin_task_id = task.id
    row.proxy_encrypted = SecretService().encrypt_optional(task.socks5_url if task.socks5_url != "-" else None)
    db.commit()
    return row


def task_credential_stored(task: GcpPresetTask) -> bool:
    from sqlalchemy.orm import object_session
    db = object_session(task)
    row = db.get(ChannelUpload, task.credential_upload_id) if db and task.credential_upload_id else None
    return bool(row and row.credential_encrypted)
