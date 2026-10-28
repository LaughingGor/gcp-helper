import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.auth import ensure_environment_access, require_permission
from app.core.date_filter import beijing_date_span_utc_range, beijing_day_utc_range
from app.db.session import get_db
from app.models.gcp_environment import GcpEnvironment
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
from app.models.user import User, UserRole
from app.schemas.gcp_env import PresetTaskResponse
from app.services.gcp_channel_service import task_credential_stored
from app.services.auth_service import AuthService
from app.services.preset_task_service import PresetTaskService, run_bootstrap_project_task

router = APIRouter(prefix="/preset-tasks", tags=["preset-tasks"])
logger = logging.getLogger(__name__)


def _key_file_exists(task) -> bool:
    if not task.key_file_path or not task.environment.config_dir:
        return False
    try:
        env_root = Path(task.environment.config_dir).parent.resolve()
        key_path = Path(task.key_file_path).resolve()
        return key_path.is_relative_to(env_root) and key_path.is_file()
    except Exception:
        return False


def _task_response(task, *, include_secrets: bool = True) -> PresetTaskResponse:
    env = task.environment
    return PresetTaskResponse(
        taskId=task.id,
        envId=task.env_id,
        envName=env.env_name if env else None,
        googleAccountHint=env.google_account_hint if env else None,
        createdBy=(env.created_by or (env.owner.username if env.owner else None)) if env else None,
        taskType=task.task_type.value,
        status=task.status.value,
        currentStep=task.current_step,
        projectId=task.project_id,
        serviceAccountEmail=task.service_account_email,
        keyFileName=task.key_file_name,
        keyFileExists=_key_file_exists(task),
        credentialStored=task_credential_stored(task),
        vmName=task.vm_name,
        vmZone=task.vm_zone,
        requestedVmZone=task.requested_vm_zone,
        vmExternalIp=task.vm_external_ip,
        firewallRuleName=task.firewall_rule_name,
        socks5Port=task.socks5_port,
        socks5Url=task.socks5_url if include_secrets else None,
        errorCode=task.error_code,
        errorMessage=task.error_message,
        createdAt=task.created_at,
        startedAt=task.started_at,
        finishedAt=task.finished_at,
    )


@router.get("", response_model=list[PresetTaskResponse])
def list_preset_tasks(
    status: PresetTaskStatus | None = Query(default=None),
    env_id: str | None = Query(default=None, alias="envId"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    created_date: date | None = Query(default=None, alias="createdDate"),
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:read")),
):
    logger.info("api list preset tasks status=%s env_id=%s limit=%s offset=%s", status, env_id, limit, offset)
    service = PresetTaskService(db)
    try:
        if env_id:
            env = db.get(GcpEnvironment, env_id)
            if not env:
                raise KeyError("环境不存在")
            ensure_environment_access(user, env)
        if created_date:
            created_from, created_to = beijing_day_utc_range(created_date)
        elif start_date or end_date:
            if not start_date or not end_date:
                raise ValueError("开始日期和结束日期必须同时提供")
            created_from, created_to = beijing_date_span_utc_range(start_date, end_date)
        else:
            created_from, created_to = None, None
        tasks = service.list_tasks(
            status=status,
            env_id=env_id,
            limit=limit,
            offset=offset,
            owner_user_id=user.id if user.role != UserRole.ADMIN else None,
            created_from=created_from,
            created_to=created_to,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    include_secrets = AuthService.has_permission(user, "secret:read")
    return [_task_response(task, include_secrets=include_secrets) for task in tasks]


@router.post("/{task_id}/retry", response_model=PresetTaskResponse)
def retry_preset_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:run", csrf=True)),
):
    logger.info("api retry preset task task_id=%s", task_id)
    service = PresetTaskService(db)
    try:
        existing_task = db.get(GcpPresetTask, task_id)
        if not existing_task:
            raise KeyError("预设任务不存在")
        ensure_environment_access(user, existing_task.environment)
        task, should_start = service.retry_failed_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if should_start:
        background_tasks.add_task(run_bootstrap_project_task, task.id)
    return _task_response(task, include_secrets=AuthService.has_permission(user, "secret:read"))
