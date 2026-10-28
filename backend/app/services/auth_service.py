import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.gcp_environment import GcpEnvironment
from app.models.user import User, UserRole, UserStatus
from app.models.user_auth_session import UserAuthSession


ROLE_PERMISSIONS: dict[UserRole, frozenset[str]] = {
    UserRole.ADMIN: frozenset(
        {
            "env:read",
            "env:create",
            "env:authorize",
            "env:delete",
            "task:read",
            "task:run",
            "secret:read",
            "channel:push",
            "channel:config",
            "channel:stats",
            "channel:upload",
            "channel:usage",
            "channel:model:write",
            "gcloud:execute",
            "settings:write",
            "user:manage",
        }
    ),
    UserRole.OPERATOR: frozenset(
        {
            "env:read",
            "env:create",
            "env:authorize",
            "env:delete",
            "task:read",
            "task:run",
            "secret:read",
            "channel:push",
            "channel:config",
            "channel:stats",
            "channel:upload",
            "channel:usage",
            "settings:write",
        }
    ),
    UserRole.CHANNEL_USER: frozenset({"channel:upload", "channel:usage"}),
}


@dataclass
class CreatedAuthSession:
    session: UserAuthSession
    raw_token: str
    raw_csrf_token: str


class PasswordHasher:
    algorithm = "scrypt"
    n = 2**14
    r = 8
    p = 1
    key_length = 32

    @classmethod
    def hash(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=cls.n, r=cls.r, p=cls.p, dklen=cls.key_length
        )
        return "$".join(
            [
                cls.algorithm,
                str(cls.n),
                str(cls.r),
                str(cls.p),
                base64.urlsafe_b64encode(salt).decode("ascii"),
                base64.urlsafe_b64encode(derived).decode("ascii"),
            ]
        )

    @classmethod
    def verify(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt_value, expected_value = encoded.split("$", 5)
            if algorithm != cls.algorithm:
                return False
            salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
            expected = base64.urlsafe_b64decode(expected_value.encode("ascii"))
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(expected),
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False


class AuthService:
    username_pattern = re.compile(r"^[a-zA-Z0-9_.@-]+$")

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def permissions_for(user: User) -> list[str]:
        return sorted(ROLE_PERMISSIONS.get(user.role, frozenset()))

    @classmethod
    def has_permission(cls, user: User, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(user.role, frozenset())

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: UserRole = UserRole.OPERATOR,
        email: str | None = None,
        actor: User | None = None,
    ) -> User:
        normalized_username = username.strip().lower()
        if len(normalized_username) < 3 or not self.username_pattern.fullmatch(normalized_username):
            raise ValueError("用户名至少 3 位，且只能包含字母、数字、点、下划线、@ 或连字符")
        if len(password) < 10:
            raise ValueError("密码至少需要 10 个字符")
        existing = self.db.scalar(
            select(User).where(func.lower(User.username) == normalized_username.lower())
        )
        if existing:
            raise ValueError("用户名已存在")

        is_first_user = (self.db.scalar(select(func.count(User.id))) or 0) == 0
        if is_first_user and role != UserRole.ADMIN:
            raise ValueError("首个系统用户必须是管理员")

        user = User(
            username=normalized_username,
            email=email.strip() if email else None,
            password_hash=PasswordHasher.hash(password),
            role=role,
            status=UserStatus.ACTIVE,
        )
        self.db.add(user)
        self.db.flush()
        if is_first_user:
            self.db.execute(
                update(GcpEnvironment)
                .where(GcpEnvironment.owner_user_id.is_(None))
                .values(owner_user_id=user.id, created_by=user.username)
            )
        self._audit((actor or user).username, "USER_CREATE", "user", user.id, {"role": role.value})
        self.db.commit()
        self.db.refresh(user)
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        normalized_username = username.strip()
        user = self.db.scalar(
            select(User).where(func.lower(User.username) == normalized_username.lower())
        )
        if not user or user.status != UserStatus.ACTIVE or not PasswordHasher.verify(password, user.password_hash):
            self._audit(normalized_username or None, "LOGIN_FAILED", "user", user.id if user else None)
            self.db.commit()
            return None
        return user

    def create_session(self, user: User, *, ip_address: str | None, user_agent: str | None) -> CreatedAuthSession:
        now = datetime.utcnow()
        raw_token = secrets.token_urlsafe(48)
        raw_csrf_token = secrets.token_urlsafe(32)
        auth_session = UserAuthSession(
            user_id=user.id,
            token_hash=self.hash_token(raw_token),
            csrf_token_hash=self.hash_token(raw_csrf_token),
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            created_at=now,
            last_active_at=now,
            expires_at=now + timedelta(hours=self.settings.auth_session_hours),
        )
        user.last_login_at = now
        self.db.add(auth_session)
        self._audit(user.username, "LOGIN_SUCCESS", "user", user.id)
        self.db.commit()
        self.db.refresh(auth_session)
        return CreatedAuthSession(auth_session, raw_token, raw_csrf_token)

    def get_session(self, raw_token: str) -> UserAuthSession | None:
        if not raw_token:
            return None
        auth_session = self.db.scalar(
            select(UserAuthSession)
            .options(joinedload(UserAuthSession.user))
            .where(UserAuthSession.token_hash == self.hash_token(raw_token))
        )
        now = datetime.utcnow()
        if (
            not auth_session
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= now
            or auth_session.user.status != UserStatus.ACTIVE
        ):
            return None
        if auth_session.last_active_at < now - timedelta(minutes=5):
            auth_session.last_active_at = now
            self.db.commit()
        return auth_session

    def revoke_session(self, auth_session: UserAuthSession) -> None:
        if auth_session.revoked_at is None:
            auth_session.revoked_at = datetime.utcnow()
            self._audit(auth_session.user.username, "LOGOUT", "auth_session", auth_session.id)
            self.db.commit()

    def list_users(self) -> list[User]:
        return list(self.db.scalars(select(User).order_by(User.created_at.asc())).all())

    def update_user(
        self,
        user: User,
        *,
        role: UserRole | None = None,
        status: UserStatus | None = None,
        password: str | None = None,
        actor: User,
    ) -> User:
        if role is not None:
            if user.id == actor.id and role != UserRole.ADMIN:
                raise ValueError("不能移除当前登录用户的管理员角色")
            user.role = role
        if status is not None:
            if user.id == actor.id and status == UserStatus.DISABLED:
                raise ValueError("不能禁用当前登录用户")
            user.status = status
            if status == UserStatus.DISABLED:
                now = datetime.utcnow()
                for auth_session in user.sessions:
                    auth_session.revoked_at = now
        if password is not None:
            if len(password) < 10:
                raise ValueError("密码至少需要 10 个字符")
            user.password_hash = PasswordHasher.hash(password)
            now = datetime.utcnow()
            for auth_session in user.sessions:
                auth_session.revoked_at = now
        self._audit(actor.username, "USER_UPDATE", "user", user.id, {"role": user.role.value, "status": user.status.value})
        self.db.commit()
        self.db.refresh(user)
        return user

    def bind_user_target(self, user: User, target_id: str | None, *, actor: User) -> User:
        from app.models.channel_upload import NewApiTarget
        if actor.role != UserRole.ADMIN:
            raise ValueError("只有管理员可以设置后台绑定")
        target = self.db.get(NewApiTarget, target_id) if target_id else None
        if target_id and (not target or not target.enabled):
            raise ValueError("请选择已启用的 new-api 后台")
        previous = user.bound_target_id
        user.bound_target_id = target_id
        self._audit(actor.username, "USER_TARGET_BIND", "user", user.id,
                    {"previousTargetId": previous, "boundTargetId": target_id})
        self.db.commit()
        self.db.refresh(user)
        return user

    def _audit(
        self,
        actor: str | None,
        action: str,
        target_type: str | None,
        target_id: str | None,
        detail: dict | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
            )
        )
