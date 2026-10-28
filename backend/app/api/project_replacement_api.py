from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_permission
from app.db.session import get_db
from app.models.project_replacement import ProjectReplacement
from app.models.user import User
from app.schemas.gcp_env import CreateProjectReplacement
from app.services.auth_service import AuthService
from app.services.new_api_target_service import TargetAccessError
from app.services.project_replacement_service import ProjectReplacementService, run_project_replacement

router = APIRouter(prefix="/gcp-envs/{env_id}/project-replacements", tags=["project-replacements"])


def service(db: Session, user: User) -> ProjectReplacementService:
    if not AuthService.has_permission(user, "channel:push"):
        raise HTTPException(403, "权限不足")
    return ProjectReplacementService(db, user)


def error(exc: Exception) -> HTTPException:
    if isinstance(exc, TargetAccessError):
        return HTTPException(403, str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@router.get("/preview")
def preview(env_id: str, target_id: UUID = Query(alias="targetId"), db: Session = Depends(get_db),
            user: User = Depends(require_permission("task:run"))):
    try:
        return service(db, user).preview(env_id, str(target_id))
    except (KeyError, ValueError, RuntimeError) as exc:
        raise error(exc) from None


@router.get("")
def list_replacements(env_id: str, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("task:run"))):
    workflow = service(db, user)
    try:
        workflow.environment(env_id)
        from app.services.new_api_target_service import resolve_target
        query = select(ProjectReplacement).where(ProjectReplacement.env_id == env_id)
        from app.models.user import UserRole
        if user.role != UserRole.ADMIN:
            target = resolve_target(db, user.id)
            query = query.where(ProjectReplacement.target_id == target.id)
        rows = db.scalars(query.order_by(ProjectReplacement.created_at.desc()).limit(20))
        return [workflow.public(row) for row in rows]
    except (KeyError, ValueError, RuntimeError) as exc:
        raise error(exc) from None


@router.post("", status_code=202)
def create_replacement(env_id: str, req: CreateProjectReplacement, background_tasks: BackgroundTasks,
                       db: Session = Depends(get_db), user: User = Depends(require_permission("task:run", csrf=True))):
    try:
        workflow = service(db, user)
        op = workflow.create(env_id, req)
        if op.status == "PENDING":
            background_tasks.add_task(run_project_replacement, op.id)
        return workflow.public(op)
    except (KeyError, ValueError, RuntimeError) as exc:
        db.rollback()
        raise error(exc) from None


@router.get("/{identifier}")
def get_replacement(env_id: str, identifier: str, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("task:run"))):
    try:
        workflow = service(db, user)
        return workflow.public(workflow.get(env_id, identifier))
    except (KeyError, ValueError, RuntimeError) as exc:
        raise error(exc) from None


@router.post("/{identifier}/retry", status_code=202)
def retry_replacement(env_id: str, identifier: str, background_tasks: BackgroundTasks,
                      db: Session = Depends(get_db), user: User = Depends(require_permission("task:run", csrf=True))):
    try:
        workflow = service(db, user)
        op = workflow.retry(env_id, identifier)
        background_tasks.add_task(run_project_replacement, op.id)
        return workflow.public(op)
    except (KeyError, ValueError, RuntimeError) as exc:
        db.rollback()
        raise error(exc) from None
