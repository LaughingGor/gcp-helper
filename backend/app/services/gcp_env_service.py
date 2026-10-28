import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.config import get_settings
from app.core.date_filter import beijing_today
from app.db.session import SessionLocal
from app.models.gcp_command_log import GcpCommandLog
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_login_session import GcpLoginSession, LoginSessionStatus
from app.models.user import User, UserRole
from app.runners.gcloud_login_runner import GcloudLoginRunner
from app.runners.gcloud_runner import GcloudRunner
from app.schemas.gcp_env import StartLoginRequest
from app.services.gcloud_config_service import GcloudConfigService
from app.services.secret_service import SecretService

logger = logging.getLogger(__name__)
_env_name_lock = threading.RLock()


class DuplicateGoogleAccountError(RuntimeError):
    def __init__(self, environment: GcpEnvironment) -> None:
        self.environment = environment
        super().__init__(
            f"目标账号 {environment.google_account_hint} 已存在于环境“{environment.env_name}”中，"
            "请使用已有环境进行再次授权，或清理旧环境后再创建。"
        )


class GcpEnvService:
    active_login_statuses = (
        LoginSessionStatus.PENDING,
        LoginSessionStatus.PROCESS_STARTED,
        LoginSessionStatus.AUTH_URL_READY,
        LoginSessionStatus.WAITING_CODE,
        LoginSessionStatus.CODE_SUBMITTED,
    )

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.secret_service = SecretService()
        self.runner = GcloudRunner(db)
        self.config_service = GcloudConfigService(self.runner)
        self.login_runner = GcloudLoginRunner()

    def start_login(self, req: StartLoginRequest, *, owner: User | None = None) -> tuple[GcpEnvironment, GcpLoginSession]:
        google_account = req.google_account.strip()
        logger.info("start login requested account=%s proxy_enabled=%s", google_account, req.proxy.enabled)
        with _env_name_lock:
            existing_env = self._find_existing_account_environment(google_account, owner=owner)
            if existing_env:
                logger.warning(
                    "duplicate google account rejected account=%s owner_user_id=%s existing_env_id=%s",
                    google_account,
                    owner.id if owner else None,
                    existing_env.id,
                )
                raise DuplicateGoogleAccountError(existing_env)

            proxy_password_encrypted = self.secret_service.encrypt_optional(req.proxy.password)
            env_name = self._next_environment_name(owner.username if owner else "system")
            config_name = self.config_service.make_config_name(env_name)
            env = GcpEnvironment(
                env_name=env_name,
                google_account_hint=google_account,
                config_name=config_name,
                config_dir="",  # id 生成后再填
                status=GcpEnvironmentStatus.CREATED,
                proxy_enabled=req.proxy.enabled,
                proxy_type=req.proxy.type,
                proxy_host=req.proxy.host,
                proxy_port=req.proxy.port,
                proxy_username=req.proxy.username,
                proxy_password_encrypted=proxy_password_encrypted,
                owner_user_id=owner.id if owner else None,
                created_by=owner.username if owner else None,
                default_zone=req.vm_zone,
            )
            self.db.add(env)
            self.db.flush()
            env.config_dir = self.config_service.make_config_dir(env.id)
            session = GcpLoginSession(
                env_id=env.id,
                status=LoginSessionStatus.PENDING,
                expires_at=datetime.utcnow() + timedelta(seconds=self.settings.login_timeout_seconds),
            )
            self.db.add(session)
            self.db.commit()
        self.db.refresh(env)
        self.db.refresh(session)
        logger.info(
            "environment and pending login session created env_id=%s login_session_id=%s config_name=%s",
            env.id,
            session.id,
            env.config_name,
        )
        return env, session

    def _find_existing_account_environment(
        self,
        google_account: str,
        *,
        owner: User | None,
    ) -> GcpEnvironment | None:
        query = select(GcpEnvironment).where(
            func.lower(GcpEnvironment.google_account_hint) == google_account.strip().lower()
        )
        if owner:
            query = query.where(GcpEnvironment.owner_user_id == owner.id)
        else:
            query = query.where(GcpEnvironment.owner_user_id.is_(None))
        return self.db.scalars(
            query.order_by(GcpEnvironment.created_at.desc(), GcpEnvironment.id.desc()).limit(1)
        ).first()

    def _next_environment_name(self, owner_username: str) -> str:
        date_prefix = beijing_today().strftime("%Y-%m-%d")
        username_prefix = owner_username.strip().lower()[:100] or "system"
        name_prefix = f"{username_prefix}-{date_prefix}"
        names = self.db.scalars(
            select(GcpEnvironment.env_name).where(GcpEnvironment.env_name.startswith(f"{name_prefix}-", autoescape=True))
        ).all()
        sequences: list[int] = []
        for name in names:
            suffix = name.removeprefix(f"{name_prefix}-")
            if suffix.isdigit():
                sequences.append(int(suffix))
        return f"{name_prefix}-{max(sequences, default=0) + 1:03d}"

    def prepare_login(self, login_session_id: str) -> tuple[GcpEnvironment, GcpLoginSession]:
        session = self.get_login_session(login_session_id)
        env = session.environment
        if session.status != LoginSessionStatus.PENDING:
            logger.info(
                "prepare login skipped login_session_id=%s status=%s",
                session.id,
                session.status.value,
            )
            return env, session

        try:
            env.status = GcpEnvironmentStatus.CONFIG_INITIALIZING
            self.db.commit()
            proxy_password = self.secret_service.decrypt_optional(env.proxy_password_encrypted)
            self.config_service.create_configuration(env)
            self.config_service.set_proxy(env, proxy_password=proxy_password)

            self.db.refresh(session)
            self.db.refresh(env)
            if session.status == LoginSessionStatus.CANCELLED:
                return env, session
            if session.expires_at < datetime.utcnow():
                session.status = LoginSessionStatus.TIMEOUT
                session.error_code = "LOGIN_TIMEOUT"
                session.error_message = "登录会话已过期，请点击再次授权重新发起登录"
                session.finished_at = datetime.utcnow()
                env.status = GcpEnvironmentStatus.EXPIRED
                self.db.commit()
                return env, session

            env.status = GcpEnvironmentStatus.AUTH_PROCESS_STARTING
            session.status = LoginSessionStatus.PROCESS_STARTED
            self.db.commit()

            auth_url, pid = self.login_runner.start_login(
                env,
                login_session_id=session.id,
                google_account=env.google_account_hint,
            )
            self.db.refresh(session)
            self.db.refresh(env)
            if session.status == LoginSessionStatus.CANCELLED:
                self.login_runner.cancel(session.id)
                return env, session
            session.auth_url = auth_url
            session.process_pid = pid
            session.status = LoginSessionStatus.WAITING_CODE
            env.status = GcpEnvironmentStatus.WAITING_USER_CODE
            self.db.commit()
            self.db.refresh(session)
            self.db.refresh(env)
            logger.info("login waiting user code env_id=%s login_session_id=%s pid=%s", env.id, session.id, pid)
            return env, session
        except Exception as exc:
            self.login_runner.cancel(login_session_id)
            self.db.rollback()
            session = self.get_login_session(login_session_id)
            env = session.environment
            if session.status == LoginSessionStatus.CANCELLED:
                return env, session
            session.status = LoginSessionStatus.FAILED
            session.error_code = "START_LOGIN_FAILED"
            session.error_message = self._trim_error(str(exc))
            session.finished_at = datetime.utcnow()
            env.status = GcpEnvironmentStatus.FAILED
            self.db.commit()
            logger.exception("start login failed env_id=%s", env.id)
            return env, session

    def begin_submit_code(self, login_session_id: str) -> tuple[GcpEnvironment, GcpLoginSession]:
        session = self.get_login_session(login_session_id)
        env = session.environment
        logger.info("submit login code env_id=%s login_session_id=%s", env.id, login_session_id)
        now = datetime.utcnow()
        if session.expires_at < now:
            session.status = LoginSessionStatus.TIMEOUT
            session.error_code = "LOGIN_TIMEOUT"
            session.error_message = "登录会话已过期，请点击再次授权重新发起登录"
            session.finished_at = datetime.utcnow()
            env.status = GcpEnvironmentStatus.EXPIRED
            self.db.commit()
            logger.warning("login session expired env_id=%s login_session_id=%s", env.id, login_session_id)
            raise RuntimeError("登录会话已过期，请重新授权")

        if session.status != LoginSessionStatus.WAITING_CODE:
            raise RuntimeError(f"当前登录会话状态不允许提交授权码: {session.status.value}")
        if not self.login_runner.has_process(login_session_id):
            raise RuntimeError("登录进程不存在，可能后端已重启，请重新授权")

        session.status = LoginSessionStatus.CODE_SUBMITTED
        env.status = GcpEnvironmentStatus.AUTHORIZING
        self.db.commit()
        self.db.refresh(session)
        self.db.refresh(env)
        return env, session

    def retry_login(self, login_session_id: str) -> tuple[GcpEnvironment, GcpLoginSession]:
        source_session = self.get_login_session(login_session_id)
        env = source_session.environment
        if source_session.status not in (
            LoginSessionStatus.FAILED,
            LoginSessionStatus.TIMEOUT,
            LoginSessionStatus.CANCELLED,
        ):
            raise RuntimeError("只有失败、已过期或已取消的登录会话可以再次授权")

        latest_session = self.db.scalars(
            select(GcpLoginSession)
            .where(GcpLoginSession.env_id == env.id)
            .order_by(GcpLoginSession.started_at.desc(), GcpLoginSession.id.desc())
            .limit(1)
        ).first()
        if not latest_session or latest_session.id != source_session.id:
            raise RuntimeError("该会话不是当前环境的最新授权记录")

        active_session = self.db.scalars(
            select(GcpLoginSession)
            .where(
                GcpLoginSession.env_id == env.id,
                GcpLoginSession.status.in_(self.active_login_statuses),
            )
            .limit(1)
        ).first()
        if active_session:
            raise RuntimeError("当前环境已有进行中的授权会话")

        self.login_runner.cancel(source_session.id)
        retry_session = GcpLoginSession(
            env_id=env.id,
            status=LoginSessionStatus.PENDING,
            expires_at=datetime.utcnow() + timedelta(seconds=self.settings.login_timeout_seconds),
        )
        env.status = GcpEnvironmentStatus.CREATED
        env.authorized_account = None
        self.db.add(retry_session)
        self.db.commit()
        self.db.refresh(env)
        self.db.refresh(retry_session)
        logger.info(
            "login retry created env_id=%s source_session_id=%s login_session_id=%s",
            env.id,
            source_session.id,
            retry_session.id,
        )
        return env, retry_session

    def complete_submit_code(self, login_session_id: str, code: str) -> tuple[GcpEnvironment, GcpLoginSession]:
        session = self.get_login_session(login_session_id)
        env = session.environment
        if session.status != LoginSessionStatus.CODE_SUBMITTED:
            return env, session
        try:
            exit_code, output = self.login_runner.submit_code_and_wait(login_session_id, code)
            if exit_code not in (0, None):
                session.status = LoginSessionStatus.FAILED
                session.error_code = "GCLOUD_AUTH_FAILED"
                session.error_message = self._trim_error(output)
                session.finished_at = datetime.utcnow()
                env.status = GcpEnvironmentStatus.FAILED
                self.db.commit()
                logger.error("gcloud auth failed env_id=%s login_session_id=%s", env.id, login_session_id)
                raise RuntimeError(session.error_message or "gcloud 授权失败")

            account = self.get_active_account(env)
            if account.lower() != env.google_account_hint.lower():
                session.status = LoginSessionStatus.FAILED
                session.error_code = "ACCOUNT_MISMATCH"
                session.error_message = f"实际授权账号 {account} 与填写账号 {env.google_account_hint} 不一致"
                session.finished_at = datetime.utcnow()
                env.status = GcpEnvironmentStatus.FAILED
                self.db.commit()
                logger.warning("authorized account mismatch env_id=%s login_session_id=%s actual=%s expected=%s", env.id, login_session_id, account, env.google_account_hint)
                return env, session

            env.authorized_account = account
            env.status = GcpEnvironmentStatus.AUTHORIZED
            session.status = LoginSessionStatus.SUCCESS
            session.finished_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(env)
            self.db.refresh(session)
            logger.info("login success env_id=%s login_session_id=%s account=%s", env.id, login_session_id, account)
            return env, session
        except Exception as exc:
            if session.status not in (LoginSessionStatus.FAILED, LoginSessionStatus.SUCCESS):
                session.status = LoginSessionStatus.FAILED
                session.error_code = "SUBMIT_CODE_FAILED"
                session.error_message = str(exc)
                session.finished_at = datetime.utcnow()
                env.status = GcpEnvironmentStatus.FAILED
                self.db.commit()
                logger.exception("submit login code failed env_id=%s login_session_id=%s", env.id, login_session_id)
            raise

    def get_active_account(self, env: GcpEnvironment) -> str:
        result = self.runner.run(
            env,
            ["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            command_type="AUTH_LIST",
        )
        if not result.ok:
            raise RuntimeError(f"获取 active account 失败: {result.stderr or result.stdout}")
        account = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not account:
            raise RuntimeError("授权完成但没有找到 active account")
        return account

    def ensure_billing_for_auto_bootstrap(self, env: GcpEnvironment) -> bool:
        """Select the only available billing account; never guess when there are multiple."""
        if env.billing_account_id:
            return True

        from app.services.billing_account_service import BillingAccountService

        billing_service = BillingAccountService(self.db)
        accounts = billing_service.list_available(env)
        if len(accounts) != 1:
            logger.info(
                "auto bootstrap requires billing selection env_id=%s available_billing_accounts=%s",
                env.id,
                len(accounts),
            )
            return False

        account = accounts[0]
        billing_service.select(
            env,
            account.billing_account_id,
            actor="SYSTEM_AUTO_BILLING_SELECTION",
        )
        logger.info(
            "auto selected sole billing account env_id=%s billing_account=%s",
            env.id,
            account.billing_account_id,
        )
        return True

    def get_login_session(self, login_session_id: str) -> GcpLoginSession:
        session = self.db.get(GcpLoginSession, login_session_id)
        if not session:
            raise KeyError("登录会话不存在")
        self._expire_login_session_if_needed(session)
        return session

    def list_login_sessions(
        self,
        *,
        active_only: bool = False,
        limit: int = 100,
        user: User | None = None,
    ) -> list[GcpLoginSession]:
        query = (
            select(GcpLoginSession)
            .options(joinedload(GcpLoginSession.environment))
            .order_by(GcpLoginSession.started_at.desc())
            .limit(limit)
        )
        if active_only:
            query = query.where(GcpLoginSession.status.in_(self.active_login_statuses))
        if user and user.role != UserRole.ADMIN:
            query = query.join(GcpLoginSession.environment).where(GcpEnvironment.owner_user_id == user.id)
        sessions = list(self.db.scalars(query).all())
        for session in sessions:
            self._expire_login_session_if_needed(session)
        if active_only:
            sessions = [session for session in sessions if session.status in self.active_login_statuses]
        return sessions

    def cancel_login(self, login_session_id: str) -> GcpLoginSession:
        session = self.get_login_session(login_session_id)
        logger.info("cancel login requested env_id=%s login_session_id=%s", session.env_id, login_session_id)
        if session.status == LoginSessionStatus.CODE_SUBMITTED:
            raise RuntimeError("授权码已提交，正在验证，不能取消")
        if session.status not in self.active_login_statuses:
            return session
        self.login_runner.cancel(login_session_id)
        session.status = LoginSessionStatus.CANCELLED
        session.finished_at = datetime.utcnow()
        session.environment.status = GcpEnvironmentStatus.CANCELLED
        self.db.commit()
        self.db.refresh(session)
        return session

    def _expire_login_session_if_needed(self, session: GcpLoginSession) -> None:
        if session.status not in self.active_login_statuses or session.expires_at >= datetime.utcnow():
            return
        self.login_runner.cancel(session.id)
        session.status = LoginSessionStatus.TIMEOUT
        session.error_code = "LOGIN_TIMEOUT"
        session.error_message = "登录会话已过期，请点击再次授权重新发起登录"
        session.finished_at = datetime.utcnow()
        session.environment.status = GcpEnvironmentStatus.EXPIRED
        self.db.commit()

    def list_envs(
        self,
        *,
        user: User | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        google_account: str | None = None,
    ) -> list[GcpEnvironment]:
        query = select(GcpEnvironment).options(selectinload(GcpEnvironment.channel_push_records))
        if user and user.role != UserRole.ADMIN:
            query = query.where(GcpEnvironment.owner_user_id == user.id)
        if google_account:
            query = query.where(func.lower(GcpEnvironment.google_account_hint).contains(google_account.lower(), autoescape=True))
        if created_from:
            query = query.where(GcpEnvironment.created_at >= created_from)
        if created_to:
            query = query.where(GcpEnvironment.created_at < created_to)
        query = query.order_by(GcpEnvironment.created_at.desc(), GcpEnvironment.id.desc())
        return list(self.db.scalars(query).all())

    def delete_env(self, env_id: str) -> None:
        from app.services.project_replacement_service import ensure_no_replacement
        ensure_no_replacement(self.db, env_id)
        env = self.db.get(GcpEnvironment, env_id)
        if not env:
            raise KeyError("环境不存在")
        logger.info("delete env requested env_id=%s config_dir=%s", env.id, env.config_dir)

        for session in env.login_sessions:
            if session.status not in (
                LoginSessionStatus.SUCCESS,
                LoginSessionStatus.FAILED,
                LoginSessionStatus.TIMEOUT,
                LoginSessionStatus.CANCELLED,
            ):
                self.login_runner.cancel(session.id)

        self._remove_config_dir(env)
        self.db.execute(delete(GcpCommandLog).where(GcpCommandLog.env_id == env.id))
        self.db.delete(env)
        self.db.commit()
        logger.info("delete env complete env_id=%s", env_id)

    def _remove_config_dir(self, env: GcpEnvironment) -> None:
        if not env.config_dir:
            return

        base_dir = self.settings.gcloud_base_dir.resolve()
        config_dir = Path(env.config_dir)
        target_dir = config_dir.parent if config_dir.name == "gcloud" else config_dir
        target_dir = target_dir.resolve()

        if target_dir == base_dir or not target_dir.is_relative_to(base_dir):
            raise RuntimeError(f"拒绝清理不在 gcloud 环境目录下的路径: {target_dir}")
        if target_dir.exists():
            shutil.rmtree(target_dir)
            logger.info("removed env config dir env_id=%s path=%s", env.id, target_dir)

    @staticmethod
    def _trim_error(value: str, limit: int = 1000) -> str:
        return value[-limit:] if value and len(value) > limit else value


def prepare_login_session(login_session_id: str) -> None:
    db = SessionLocal()
    try:
        GcpEnvService(db).prepare_login(login_session_id)
    except Exception:
        logger.exception("background login preparation crashed login_session_id=%s", login_session_id)
    finally:
        db.close()


def submit_login_code_and_bootstrap(login_session_id: str, code: str) -> None:
    db = SessionLocal()
    task_id: str | None = None
    try:
        env, session = GcpEnvService(db).complete_submit_code(login_session_id, code)
        if session.status == LoginSessionStatus.SUCCESS:
            try:
                can_auto_bootstrap = GcpEnvService(db).ensure_billing_for_auto_bootstrap(env)
            except Exception:
                logger.exception("auto billing account lookup failed env_id=%s", env.id)
                can_auto_bootstrap = False

            if can_auto_bootstrap:
                try:
                    from app.services.preset_task_service import PresetTaskService

                    task = PresetTaskService(db).start_bootstrap_project(env.id)
                    task_id = task.id
                    logger.info("auto bootstrap task created env_id=%s task_id=%s", env.id, task.id)
                except Exception:
                    logger.exception("auto bootstrap task creation failed env_id=%s", env.id)
            else:
                logger.info("auto bootstrap skipped until billing account selected env_id=%s", env.id)
    except Exception:
        logger.exception("background login completion failed login_session_id=%s", login_session_id)
    finally:
        code = ""
        db.close()

    if task_id:
        from app.services.preset_task_service import run_bootstrap_project_task

        run_bootstrap_project_task(task_id)


def recover_interrupted_login_sessions() -> int:
    """Mark sessions left active by a previous backend process as failed."""
    db = SessionLocal()
    try:
        sessions = list(
            db.scalars(
                select(GcpLoginSession).where(
                    GcpLoginSession.status.in_(GcpEnvService.active_login_statuses)
                )
            ).all()
        )
        if not sessions:
            return 0
        finished_at = datetime.utcnow()
        for session in sessions:
            session.status = LoginSessionStatus.FAILED
            session.error_code = "BACKEND_RESTARTED"
            session.error_message = "后端服务已重启，原登录进程无法恢复，请重新创建环境或发起授权"
            session.finished_at = finished_at
            session.environment.status = GcpEnvironmentStatus.FAILED
        db.commit()
        logger.warning("recovered interrupted login sessions count=%s", len(sessions))
        return len(sessions)
    finally:
        db.close()
