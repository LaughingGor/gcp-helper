from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.db.session import get_db
from app.models.channel_upload import ChannelUpload, NewApiTarget
from app.models.user import User, UserRole
from app.schemas.channel_upload import ChannelUploadInput, CreateChannelUpload, NewApiTargetInput, PushUploadInput, AdoptLegacyPushInput
from app.services.channel_vault_service import ChannelVaultService
from app.services.channel_upload_service import (
    ACTIVE_STATES, ChannelUploadService, refresh_channel_uploads, run_channel_upload, validate_credential,
)

class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Pydantic normally includes rejected input values in 422 responses.
                return JSONResponse(status_code=422, content={"detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]})
        return private_handler


router = APIRouter(tags=["channel-uploads"], route_class=PrivateValidationRoute)


@router.post("/admin/gcp-credentials/import")
def import_gcp_credentials(cursor: str = Query("", max_length=36), db: Session = Depends(get_db),
                           user: User = Depends(require_permission("user:manage", csrf=True))):
    from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
    from app.services.gcp_channel_service import register_gcp_credential
    tasks = list(db.scalars(select(GcpPresetTask).where(GcpPresetTask.status == PresetTaskStatus.SUCCESS,
        GcpPresetTask.id > cursor).order_by(GcpPresetTask.id).limit(51)))
    imported = 0
    failures = []
    for task in tasks[:50]:
        identifier = task.id
        try:
            register_gcp_credential(db, task)
            imported += 1
        except Exception:
            db.rollback()
            failures.append({"taskId": identifier, "message": "请检查环境归属、密钥文件及 APP_SECRET_KEY"})
    ChannelVaultService(db, user).audit("GCP_CREDENTIAL_ARCHIVE_BATCH", user.id, {"imported": imported, "failed": len(failures)})
    db.commit()
    return {"imported": imported, "failed": len(failures), "failures": failures,
            "nextCursor": tasks[49].id if len(tasks) > 50 else None}


@router.get("/new-api-targets")
def available_targets(db: Session = Depends(get_db), user: User = Depends(require_permission("channel:push"))):
    query = select(NewApiTarget).where(NewApiTarget.enabled.is_(True))
    if user.role != UserRole.ADMIN:
        query = query.where(NewApiTarget.id == user.bound_target_id) if user.bound_target_id else query.where(False)
    return [{"id": t.id, "name": t.name, "group": t.group, "models": t.models,
             "currency": t.currency, "quotaPerUnit": str(t.quota_per_unit), "vertexRegion": t.vertex_region}
            for t in db.scalars(query.order_by(NewApiTarget.name))]


@router.post("/channel-uploads/preview")
def preview(req: ChannelUploadInput, user: User = Depends(require_permission("channel:upload", csrf=True))):
    try:
        _, identity = validate_credential(req)
        return {"kind": req.kind, "identity": identity}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/channel-uploads", status_code=201)
def create(req: CreateChannelUpload, db: Session = Depends(get_db),
           user: User = Depends(require_permission("channel:upload", csrf=True))):
    service = ChannelUploadService(db, user.id)
    try:
        row = service.create(req)
    except (ValueError, RuntimeError, FileNotFoundError):
        raise HTTPException(400, "无法提交：请检查凭据、APP_SECRET_KEY 和渠道配置；凭据可能已上传") from None
    return service.public_with_deployments(row)


def owned(service: ChannelUploadService, identifier: str) -> ChannelUpload:
    try:
        return service.owned(identifier)
    except KeyError:
        raise HTTPException(404, "上传记录不存在") from None


@router.get("/channel-uploads/{identifier}")
def detail(identifier: str, db: Session = Depends(get_db),
           user: User = Depends(require_permission("channel:usage"))):
    service = ChannelUploadService(db, user.id)
    return service.public_with_deployments(owned(service, identifier))


@router.post("/channel-uploads/{identifier}/retry", status_code=202)
def retry(identifier: str, tasks: BackgroundTasks, db: Session = Depends(get_db),
          user: User = Depends(require_permission("user:manage", csrf=True))):
    row = db.get(ChannelUpload, identifier)
    if not row:
        raise HTTPException(404, "上传记录不存在")
    if row.origin_env_id:
        from app.services.project_replacement_service import ensure_no_replacement
        try:
            ensure_no_replacement(db, row.origin_env_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
    service = ChannelUploadService(db, row.user_id)
    if row.status == "STORED":
        raise HTTPException(409, "请在凭据管理中选择目标后台推送")
    if row.status in ACTIVE_STATES - {"PENDING"}:
        raise HTTPException(409, "任务正在执行，请稍后刷新")
    ChannelVaultService(db, user).audit("CHANNEL_ADMIN_RETRY", row.id)
    db.commit()
    tasks.add_task(run_channel_upload, row.id, row.user_id)
    return service.public(row)


@router.get("/my-channels")
def list_channels(page: int = Query(1, ge=1), page_size: int = Query(20, alias="pageSize", ge=1, le=100),
                  db: Session = Depends(get_db), user: User = Depends(require_permission("channel:usage"))):
    where = (ChannelUpload.user_id == user.id) & ChannelUpload.source_upload_id.is_(None)
    total = db.scalar(select(func.count()).select_from(ChannelUpload).where(where))
    rows = db.scalars(select(ChannelUpload).where(where).order_by(ChannelUpload.created_at.desc(), ChannelUpload.id)
                      .offset((page - 1) * page_size).limit(page_size))
    service = ChannelUploadService(db, user.id)
    return {"items": [service.public_with_deployments(row) for row in rows], "total": total}


@router.get("/my-channel-usage")
def usage(db: Session = Depends(get_db), user: User = Depends(require_permission("channel:usage"))):
    return ChannelUploadService(db, user.id).summary()


@router.post("/my-channel-usage/refresh", status_code=202)
def refresh(tasks: BackgroundTasks, user: User = Depends(require_permission("channel:usage", csrf=True))):
    tasks.add_task(refresh_channel_uploads, user.id)
    return {"message": "已请求同步；冷却期间使用上次有效数据"}


@router.get("/admin/channel-usage")
def admin_usage(db: Session = Depends(get_db), user: User = Depends(require_permission("user:manage"))):
    users = db.scalars(select(User).where(User.id.in_(select(ChannelUpload.user_id).distinct())).order_by(User.username))
    return [{"userId": owner.id, "username": owner.username, **ChannelUploadService(db, owner.id).summary()} for owner in users]


@router.get("/admin/channel-usage/{user_id}")
def admin_channels(user_id: str, page: int = Query(1, ge=1),
                   page_size: int = Query(20, alias="pageSize", ge=1, le=100),
                   db: Session = Depends(get_db), user: User = Depends(require_permission("user:manage"))):
    owner = db.get(User, user_id)
    if not owner:
        raise HTTPException(404, "用户不存在")
    return list_channels(page, page_size, db, owner)


@router.get("/admin/channel-uploads")
def vault_list(page: int = Query(1, ge=1), page_size: int = Query(20, alias="pageSize", ge=1, le=100),
               user_id: str | None = Query(None, alias="userId"), db: Session = Depends(get_db),
               user: User = Depends(require_permission("user:manage"))):
    where = ChannelUpload.source_upload_id.is_(None)
    if user_id:
        where = where & (ChannelUpload.user_id == user_id)
    total = db.scalar(select(func.count()).select_from(ChannelUpload).where(where))
    rows = db.execute(select(ChannelUpload, User.username).join(User, User.id == ChannelUpload.user_id)
                      .where(where).order_by(ChannelUpload.created_at.desc(), ChannelUpload.id)
                      .offset((page - 1) * page_size).limit(page_size))
    service = ChannelUploadService(db, user.id)
    return {"items": [{**service.public_with_deployments(row), "userId": row.user_id, "username": username}
                      for row, username in rows], "total": total}


def vault_error(exc: Exception) -> HTTPException:
    return HTTPException(404 if isinstance(exc, KeyError) else 400, str(exc).strip("'"))


@router.post("/admin/channel-uploads/{identifier}/credential")
def reveal_credential(identifier: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        row, value = ChannelVaultService(db, user).reveal(identifier)
    except (KeyError, ValueError) as exc:
        raise vault_error(exc) from None
    return JSONResponse({"kind": row.kind, "credential": value},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/admin/channel-uploads/{identifier}/download")
def download_credential(identifier: str, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        row, value = ChannelVaultService(db, user).reveal(identifier, download=True)
    except (KeyError, ValueError) as exc:
        raise vault_error(exc) from None
    suffix = "json" if row.kind == "vertex_json" else "txt"
    return Response(value, media_type="application/json" if suffix == "json" else "text/plain",
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache",
                             "Content-Disposition": f'attachment; filename="credential-{row.id}.{suffix}"'})


@router.get("/admin/new-api-targets")
def targets(db: Session = Depends(get_db), user: User = Depends(require_permission("user:manage"))):
    return [ChannelVaultService.public_target(t) for t in db.scalars(select(NewApiTarget).order_by(NewApiTarget.created_at))]


@router.post("/admin/new-api-targets", status_code=201)
def create_target(req: NewApiTargetInput, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        return ChannelVaultService.public_target(ChannelVaultService(db, user).save_target(req))
    except (KeyError, ValueError) as exc:
        raise vault_error(exc) from None


@router.put("/admin/new-api-targets/{identifier}")
def update_target(identifier: str, req: NewApiTargetInput, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        return ChannelVaultService.public_target(ChannelVaultService(db, user).save_target(req, identifier))
    except (KeyError, ValueError) as exc:
        raise vault_error(exc) from None


@router.delete("/admin/new-api-targets/{identifier}", status_code=204)
def delete_target(identifier: str, db: Session = Depends(get_db),
                  user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        ChannelVaultService(db, user).delete_target(identifier)
    except KeyError as exc:
        raise vault_error(exc) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(status_code=204)


@router.post("/admin/channel-uploads/{identifier}/push", status_code=202)
def push_to_targets(identifier: str, req: PushUploadInput, tasks: BackgroundTasks,
                    db: Session = Depends(get_db), user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        rows = ChannelVaultService(db, user).prepare_pushes(identifier, [str(t) for t in req.target_ids])
    except (KeyError, ValueError, FileNotFoundError) as exc:
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(400, "管理员渠道模板不存在") from None
        raise vault_error(exc) from None
    for row in rows:
        if row.status not in {"ACTIVE", "CREATING", "TESTING"}:
            tasks.add_task(run_channel_upload, row.id, row.user_id)
    return {"items": [ChannelUploadService.public(row) for row in rows]}


@router.get("/admin/channel-uploads/{identifier}/legacy-pushes")
def legacy_pushes(identifier: str, db: Session = Depends(get_db), user: User = Depends(require_permission("user:manage"))):
    from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
    try:
        source = ChannelVaultService(db, user).upload(identifier)
    except KeyError as exc:
        raise vault_error(exc) from None
    if not source.origin_env_id:
        return []
    return [{"id": r.id, "channelId": r.channel_id, "channelName": r.channel_name, "createdAt": r.created_at}
            for r in db.scalars(select(ChannelPushRecord).where(ChannelPushRecord.env_id == source.origin_env_id,
                ChannelPushRecord.deployment_id.is_(None)))]


@router.post("/admin/channel-uploads/{identifier}/adopt-legacy")
def adopt_legacy(identifier: str, req: AdoptLegacyPushInput, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("user:manage", csrf=True))):
    try:
        row = ChannelVaultService(db, user).adopt_legacy(identifier, str(req.record_id), str(req.target_id))
        return ChannelUploadService.public(row)
    except (ValueError, KeyError, RuntimeError) as exc:
        raise vault_error(exc) from None
