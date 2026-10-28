import logging
import subprocess
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.auth import ensure_environment_access, require_permission
from app.core.date_filter import beijing_date_span_utc_range, beijing_day_utc_range
from app.db.session import get_db
from app.models.gcp_environment import GcpEnvironment
from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
from app.models.gcp_login_session import GcpLoginSession
from app.models.user import User
from app.schemas.gcloud_command import ExecuteGcloudCommandRequest, GcloudCommandHistoryItem, GcloudCommandResponse
from app.schemas.gcp_env import (
    ChannelPushRequest,
    ChannelPushResponse,
    BillingAccountResponse,
    DeleteEnvResponse,
    GcpEnvironmentResponse,
    LoginSessionResponse,
    LoginStartResponse,
    PresetTaskResponse,
    StartLoginRequest,
    StartBootstrapRequest,
    SubmitCodeRequest,
    SubmitCodeResponse,
    UpdateBillingAccountRequest,
    UpdateBillingAccountResponse,
)
from app.services.gcp_channel_service import task_credential_stored
from app.services.billing_account_service import BillingAccountService
from app.services.channel_push_service import ChannelPushService
from app.services.auth_service import AuthService
from app.services.gcloud_command_service import (
    GcloudCommandBusy,
    GcloudCommandConfirmationRequired,
    GcloudCommandPolicyError,
    GcloudCommandService,
)
from app.services.gcp_env_service import (
    DuplicateGoogleAccountError,
    GcpEnvService,
    prepare_login_session,
    submit_login_code_and_bootstrap,
)
from app.services.preset_task_service import PresetTaskService, run_bootstrap_project_task

router = APIRouter(prefix="/gcp-envs", tags=["gcp-envs"])
logger = logging.getLogger(__name__)


def _env_response(env) -> GcpEnvironmentResponse:
    latest_push = env.channel_push_records[0] if env.channel_push_records else None
    return GcpEnvironmentResponse(
        envId=env.id,
        envName=env.env_name,
        googleAccountHint=env.google_account_hint,
        authorizedAccount=env.authorized_account,
        createdBy=env.created_by or (env.owner.username if env.owner else None),
        status=env.status.value,
        configName=env.config_name,
        proxyEnabled=env.proxy_enabled,
        defaultProjectId=env.default_project_id,
        defaultRegion=env.default_region,
        defaultZone=env.default_zone,
        billingAccountId=env.billing_account_id,
        billingAccountDisplayName=env.billing_account_display_name,
        createdAt=env.created_at,
        channelPushStatus=latest_push.status.value if latest_push else None,
        channelPushAction=latest_push.action.value if latest_push and latest_push.action else None,
        channelPushedAt=latest_push.finished_at if latest_push else None,
        channelPushMessage=(latest_push.message or latest_push.error_message) if latest_push else None,
    )


def _session_response(session) -> LoginSessionResponse:
    env = session.environment
    return LoginSessionResponse(
        loginSessionId=session.id,
        envId=session.env_id,
        envName=env.env_name if env else None,
        googleAccountHint=env.google_account_hint if env else None,
        createdBy=(env.created_by or (env.owner.username if env.owner else None)) if env else None,
        status=session.status.value,
        authUrl=session.auth_url,
        errorCode=session.error_code,
        errorMessage=session.error_message,
        expiresAt=session.expires_at,
        startedAt=session.started_at,
        finishedAt=session.finished_at,
    )


def _task_response(task, *, include_secrets: bool = True) -> PresetTaskResponse:
    return PresetTaskResponse(
        taskId=task.id,
        envId=task.env_id,
        taskType=task.task_type.value,
        createdBy=task.environment.created_by or (task.environment.owner.username if task.environment.owner else None),
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


def _authorized_env(db: Session, env_id: str, user: User) -> GcpEnvironment:
    env = db.get(GcpEnvironment, env_id)
    if not env:
        raise HTTPException(status_code=404, detail="环境不存在")
    ensure_environment_access(user, env)
    return env


def _authorized_login_session(db: Session, login_session_id: str, user: User) -> GcpLoginSession:
    session = db.get(GcpLoginSession, login_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    ensure_environment_access(user, session.environment)
    return session


def _key_file_exists(task) -> bool:
    if not task.key_file_path or not task.environment.config_dir:
        return False
    try:
        env_root = Path(task.environment.config_dir).parent.resolve()
        key_path = Path(task.key_file_path).resolve()
        return key_path.is_relative_to(env_root) and key_path.is_file()
    except Exception:
        return False


@router.post("/login/start", response_model=LoginStartResponse, status_code=status.HTTP_202_ACCEPTED)
def start_login(
    req: StartLoginRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:create", csrf=True)),
):
    logger.info("api start login account=%s", req.google_account)
    service = GcpEnvService(db)
    try:
        env, session = service.start_login(req, owner=user)
    except DuplicateGoogleAccountError as exc:
        existing_env = exc.environment
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GOOGLE_ACCOUNT_ALREADY_EXISTS",
                "message": str(exc),
                "envId": existing_env.id,
                "envName": existing_env.env_name,
                "envStatus": existing_env.status.value,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(prepare_login_session, session.id)
    return LoginStartResponse(
        envId=env.id,
        loginSessionId=session.id,
        configName=env.config_name,
        status=session.status.value,
        authUrl=session.auth_url or "",
        expiresAt=session.expires_at,
    )


@router.get("/login-sessions", response_model=list[LoginSessionResponse])
def list_login_sessions(
    active_only: bool = Query(default=False, alias="activeOnly"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:authorize")),
):
    sessions = GcpEnvService(db).list_login_sessions(active_only=active_only, limit=limit, user=user)
    return [_session_response(session) for session in sessions]


@router.get("/login-sessions/{login_session_id}", response_model=LoginSessionResponse)
def get_login_session(
    login_session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:authorize")),
):
    _authorized_login_session(db, login_session_id, user)
    service = GcpEnvService(db)
    try:
        session = service.get_login_session(login_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _session_response(session)


@router.post("/login-sessions/{login_session_id}/code", response_model=SubmitCodeResponse)
def submit_code(
    login_session_id: str,
    req: SubmitCodeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:authorize", csrf=True)),
):
    logger.info("api submit login code login_session_id=%s", login_session_id)
    service = GcpEnvService(db)
    try:
        _authorized_login_session(db, login_session_id, user)
        env, session = service.begin_submit_code(login_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background_tasks.add_task(submit_login_code_and_bootstrap, login_session_id, req.code)
    return SubmitCodeResponse(
        status=session.status.value,
        envId=env.id,
        authorizedAccount=env.authorized_account,
        configName=env.config_name,
        errorCode=session.error_code,
        message=session.error_message,
    )


@router.post("/login-sessions/{login_session_id}/cancel", response_model=LoginSessionResponse)
def cancel_login(
    login_session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:authorize", csrf=True)),
):
    logger.info("api cancel login login_session_id=%s", login_session_id)
    service = GcpEnvService(db)
    try:
        _authorized_login_session(db, login_session_id, user)
        session = service.cancel_login(login_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _session_response(session)


@router.post(
    "/login-sessions/{login_session_id}/retry",
    response_model=LoginSessionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_login(
    login_session_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:authorize", csrf=True)),
):
    logger.info("api retry login login_session_id=%s", login_session_id)
    service = GcpEnvService(db)
    try:
        _authorized_login_session(db, login_session_id, user)
        _, session = service.retry_login(login_session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(prepare_login_session, session.id)
    return _session_response(session)


@router.get("", response_model=list[GcpEnvironmentResponse])
def list_envs(
    created_date: date | None = Query(default=None, alias="createdDate"),
    start_date: date | None = Query(default=None, alias="startDate"),
    end_date: date | None = Query(default=None, alias="endDate"),
    google_account: str | None = Query(default=None, alias="googleAccount", max_length=255),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:read")),
):
    service = GcpEnvService(db)
    try:
        if created_date:
            created_from, created_to = beijing_day_utc_range(created_date)
        elif start_date or end_date:
            if not start_date or not end_date:
                raise ValueError("开始日期和结束日期必须同时提供")
            created_from, created_to = beijing_date_span_utc_range(start_date, end_date)
        else:
            created_from, created_to = None, None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account_query = google_account.strip() if google_account else None
    return [
        _env_response(env)
        for env in service.list_envs(
            user=user,
            created_from=created_from,
            created_to=created_to,
            google_account=account_query,
        )
    ]


@router.get("/{env_id}", response_model=GcpEnvironmentResponse)
def get_env(
    env_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:read")),
):
    return _env_response(_authorized_env(db, env_id, user))


@router.get("/{env_id}/billing-accounts", response_model=list[BillingAccountResponse])
def list_billing_accounts(
    env_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:run")),
):
    env = _authorized_env(db, env_id, user)
    try:
        accounts = BillingAccountService(db).list_available(env)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        BillingAccountResponse(
            billingAccountId=account.billing_account_id,
            displayName=account.display_name,
            open=account.open,
        )
        for account in accounts
    ]


@router.put("/{env_id}/billing-account", response_model=UpdateBillingAccountResponse)
def select_billing_account(
    env_id: str,
    req: UpdateBillingAccountRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:run", csrf=True)),
):
    env = _authorized_env(db, env_id, user)
    try:
        account, linked_project_id = BillingAccountService(db).select(
            env,
            req.billing_account_id,
            actor=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UpdateBillingAccountResponse(
        billingAccountId=account.billing_account_id,
        displayName=account.display_name,
        linkedProjectId=linked_project_id,
    )


@router.post("/{env_id}/preset-tasks/bootstrap", response_model=PresetTaskResponse)
def start_bootstrap_task(
    env_id: str,
    background_tasks: BackgroundTasks,
    req: StartBootstrapRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:run", csrf=True)),
):
    logger.info("api start bootstrap task env_id=%s", env_id)
    service = PresetTaskService(db)
    try:
        _authorized_env(db, env_id, user)
        task = service.start_bootstrap_project(env_id, vm_zone=req.vm_zone if req else None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(run_bootstrap_project_task, task.id)
    return _task_response(task, include_secrets=AuthService.has_permission(user, "secret:read"))


@router.get("/{env_id}/preset-tasks/latest", response_model=PresetTaskResponse | None)
def get_latest_bootstrap_task(
    env_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:read")),
):
    _authorized_env(db, env_id, user)
    service = PresetTaskService(db)
    task = service.get_latest_task(env_id)
    return _task_response(task, include_secrets=AuthService.has_permission(user, "secret:read")) if task else None


@router.get("/{env_id}/preset-tasks/{task_id}", response_model=PresetTaskResponse)
def get_preset_task(
    env_id: str,
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("task:read")),
):
    _authorized_env(db, env_id, user)
    service = PresetTaskService(db)
    try:
        task = service.get_task(env_id, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _task_response(task, include_secrets=AuthService.has_permission(user, "secret:read"))


@router.get("/{env_id}/preset-tasks/{task_id}/service-account-key")
def download_service_account_key(
    env_id: str,
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("secret:read")),
):
    logger.info("api download service account key env_id=%s task_id=%s", env_id, task_id)
    service = PresetTaskService(db)
    try:
        _authorized_env(db, env_id, user)
        task = service.get_task(env_id, task_id)
        key_path = service.get_key_file_path(env_id, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    project_id = task.project_id or "project"
    filename = task.key_file_name or f"gcp-helper-{project_id}-compute-default-key.json"
    return FileResponse(
        path=key_path,
        media_type="application/json",
        filename=filename,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/{env_id}/channel-push", response_model=ChannelPushResponse, status_code=202)
def push_channel(
    env_id: str,
    req: ChannelPushRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("channel:push", csrf=True)),
):
    from app.services.gcp_channel_service import register_gcp_credential
    from app.services.channel_vault_service import ChannelVaultService
    from app.services.channel_upload_service import run_channel_upload
    from app.services.new_api_target_service import resolve_target, TargetAccessError
    env = _authorized_env(db, env_id, user)
    try:
        target = resolve_target(db, user.id, str(req.target_id) if req.target_id else None)
    except TargetAccessError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    task = ChannelPushService(db, user_id=env.owner_user_id)._get_latest_success_task(env_id)
    if not task:
        raise HTTPException(400, "没有可推送的成功预设任务")
    try:
        upload = register_gcp_credential(db, task)
        deployment = ChannelVaultService(db, user).prepare_pushes(upload.id, [target.id])[0]
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    if deployment.status not in {"ACTIVE", "CREATING", "TESTING"}:
        background_tasks.add_task(run_channel_upload, deployment.id, env.owner_user_id)
    return ChannelPushResponse(success=True, message="已提交到目标后台，可在环境列表查看推送状态",
        envId=env.id, taskId=task.id, channelName=deployment.name,
        templateName=deployment.target.vertex_template or "内置 Vertex 模板", deploymentId=deployment.id,
        targetId=target.id, status=deployment.status)


@router.post("/{env_id}/commands", response_model=GcloudCommandResponse)
def execute_gcloud_command(
    env_id: str,
    req: ExecuteGcloudCommandRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("gcloud:execute", csrf=True)),
):
    logger.info("api execute manual gcloud command env_id=%s user=%s", env_id, user.username)
    try:
        env = _authorized_env(db, env_id, user)
        return GcloudCommandService(db).execute(env, user.username, req)
    except GcloudCommandConfirmationRequired as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "COMMAND_CONFIRMATION_REQUIRED", "message": str(exc)},
        ) from exc
    except GcloudCommandBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except GcloudCommandPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="gcloud 命令执行超时") from exc
    except Exception as exc:
        logger.exception("manual gcloud command failed env_id=%s", env_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/{env_id}/commands", response_model=list[GcloudCommandHistoryItem])
def list_gcloud_command_history(
    env_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("gcloud:execute")),
):
    _authorized_env(db, env_id, user)
    return GcloudCommandService(db).history(env_id, limit)


@router.delete("/{env_id}", response_model=DeleteEnvResponse)
def delete_env(
    env_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("env:delete", csrf=True)),
):
    logger.info("api delete env env_id=%s", env_id)
    service = GcpEnvService(db)
    try:
        _authorized_env(db, env_id, user)
        service.delete_env(env_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DeleteEnvResponse(envId=env_id, deleted=True)
