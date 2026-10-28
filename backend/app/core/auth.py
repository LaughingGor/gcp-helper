import hmac
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.config import get_settings
from app.models.gcp_environment import GcpEnvironment
from app.models.user import User, UserRole
from app.models.user_auth_session import UserAuthSession
from app.services.auth_service import AuthService


class AuthContext:
    def __init__(self, user: User, auth_session: UserAuthSession) -> None:
        self.user = user
        self.session = auth_session


def get_auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    settings = AuthService(db).settings
    raw_token = request.cookies.get(settings.auth_session_cookie_name, "")
    auth_session = AuthService(db).get_session(raw_token)
    if not auth_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return AuthContext(auth_session.user, auth_session)


def require_permission(permission: str, *, csrf: bool = False) -> Callable:
    def dependency(request: Request, context: AuthContext = Depends(get_auth_context)) -> User:
        if not AuthService.has_permission(context.user, permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        if csrf:
            _verify_csrf(request, context)
        return context.user

    return dependency


def require_authenticated(*, csrf: bool = False) -> Callable:
    def dependency(request: Request, context: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if csrf:
            _verify_csrf(request, context)
        return context

    return dependency


def _verify_csrf(request: Request, context: AuthContext) -> None:
    settings = get_settings()
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name, "")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    if not hmac.compare_digest(AuthService.hash_token(header_token), context.session.csrf_token_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")


def ensure_environment_access(user: User, env: GcpEnvironment) -> None:
    if user.role == UserRole.ADMIN:
        return
    if not env.owner_user_id or env.owner_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="环境不存在")
