import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.core.auth import AuthContext, require_authenticated, require_permission
from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import BindUserTargetRequest, CreateUserRequest, LoginRequest, LoginResponse, UpdateUserRequest, UserResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
admin_router = APIRouter(prefix="/admin/users", tags=["users"])


class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后重试")

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._attempts[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


login_rate_limiter = LoginRateLimiter()


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        status=user.status,
        boundTargetId=user.bound_target_id,
        permissions=AuthService.permissions_for(user),
        createdAt=user.created_at,
        lastLoginAt=user.last_login_at,
    )


def _set_auth_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    settings = get_settings()
    max_age = settings.auth_session_hours * 3600
    response.set_cookie(
        settings.auth_session_cookie_name,
        session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.auth_session_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    rate_limit_key = f"{client_ip}:{req.username.strip().lower()}"
    login_rate_limiter.check(rate_limit_key)
    service = AuthService(db)
    user = service.authenticate(req.username, req.password)
    if not user:
        login_rate_limiter.record_failure(rate_limit_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    login_rate_limiter.clear(rate_limit_key)
    created = service.create_session(
        user,
        ip_address=client_ip,
        user_agent=request.headers.get("User-Agent"),
    )
    _set_auth_cookies(response, created.raw_token, created.raw_csrf_token)
    return LoginResponse(user=user_response(user))


@router.get("/me", response_model=UserResponse)
def me(context: AuthContext = Depends(require_authenticated())):
    return user_response(context.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    context: AuthContext = Depends(require_authenticated(csrf=True)),
    db: Session = Depends(get_db),
):
    AuthService(db).revoke_session(context.session)
    _clear_auth_cookies(response)


@admin_router.get("", response_model=list[UserResponse])
def list_users(
    _: User = Depends(require_permission("user:manage")),
    db: Session = Depends(get_db),
):
    return [user_response(user) for user in AuthService(db).list_users()]


@admin_router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    req: CreateUserRequest,
    actor: User = Depends(require_permission("user:manage", csrf=True)),
    db: Session = Depends(get_db),
):
    try:
        user = AuthService(db).create_user(
            username=req.username,
            password=req.password,
            email=req.email,
            role=req.role,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user_response(user)


@admin_router.put("/{user_id}/target", response_model=UserResponse)
def bind_user_target(user_id: str, req: BindUserTargetRequest,
                     actor: User = Depends(require_permission("user:manage", csrf=True)),
                     db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    target_id = str(req.bound_target_id) if req.bound_target_id else None
    try:
        return user_response(AuthService(db).bind_user_target(user, target_id, actor=actor))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@admin_router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    req: UpdateUserRequest,
    actor: User = Depends(require_permission("user:manage", csrf=True)),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        user = AuthService(db).update_user(
            user,
            role=req.role,
            status=req.status,
            password=req.password,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user_response(user)
