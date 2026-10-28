import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pexpect

from app.core.config import get_settings
from app.models.gcp_environment import GcpEnvironment

logger = logging.getLogger(__name__)

AUTH_URL_RE = re.compile(r"https://[^\s'\"]+")


@dataclass
class ManagedLoginProcess:
    child: pexpect.spawn
    env_id: str
    login_session_id: str
    started_at: datetime
    auth_url: str | None = None
    output_buffer: str = ""
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class LoginProcessManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, ManagedLoginProcess] = {}

    def add(self, session_id: str, process: ManagedLoginProcess, *, max_processes: int) -> None:
        with self._lock:
            if len(self._processes) >= max_processes:
                raise RuntimeError(f"同时等待授权的环境已达到上限（{max_processes}）")
            self._processes[session_id] = process
        logger.info("login process registered env_id=%s login_session_id=%s", process.env_id, session_id)

    def get(self, session_id: str) -> ManagedLoginProcess | None:
        with self._lock:
            return self._processes.get(session_id)

    def pop(self, session_id: str) -> ManagedLoginProcess | None:
        with self._lock:
            return self._processes.pop(session_id, None)

    def cancel(self, session_id: str) -> bool:
        process = self.pop(session_id)
        if not process:
            logger.warning("login process cancel skipped missing login_session_id=%s", session_id)
            return False
        try:
            process.child.close(force=True)
        except Exception:
            logger.exception("login process cancel failed env_id=%s login_session_id=%s", process.env_id, session_id)
        logger.info("login process cancelled env_id=%s login_session_id=%s", process.env_id, session_id)
        return True

    def contains(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._processes


login_process_manager = LoginProcessManager()


class GcloudLoginRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def start_login(
        self,
        env: GcpEnvironment,
        *,
        login_session_id: str,
        google_account: str,
    ) -> tuple[str, int | None]:
        Path(env.config_dir).mkdir(parents=True, exist_ok=True)
        process_env = os.environ.copy()
        process_env["CLOUDSDK_CONFIG"] = env.config_dir

        logger.info("gcloud login start env_id=%s login_session_id=%s account=%s", env.id, login_session_id, google_account)
        child = pexpect.spawn(
            "gcloud",
            [
                "--configuration",
                env.config_name,
                "auth",
                "login",
                google_account,
                self.settings.gcloud_auth_browser_flag,
            ],
            env=process_env,
            encoding="utf-8",
            timeout=self.settings.login_timeout_seconds,
        )
        managed = ManagedLoginProcess(
            child=child,
            env_id=env.id,
            login_session_id=login_session_id,
            started_at=datetime.utcnow(),
        )
        try:
            login_process_manager.add(
                login_session_id,
                managed,
                max_processes=self.settings.max_concurrent_logins,
            )
            auth_url = self._wait_for_auth_url(managed)
            managed.auth_url = auth_url
            logger.info("gcloud login auth url ready env_id=%s login_session_id=%s pid=%s", env.id, login_session_id, child.pid)
            return auth_url, child.pid
        except Exception:
            login_process_manager.pop(login_session_id)
            try:
                if child.isalive():
                    child.close(force=True)
            except Exception:
                logger.exception("gcloud login startup cleanup failed env_id=%s login_session_id=%s", env.id, login_session_id)
            raise

    def submit_code_and_wait(self, login_session_id: str, code: str) -> tuple[int | None, str]:
        managed = login_process_manager.get(login_session_id)
        if not managed:
            logger.warning("gcloud login submit failed missing process login_session_id=%s", login_session_id)
            raise RuntimeError("登录进程不存在，可能后端已重启或会话已过期，请重新授权")

        try:
            managed.child.setecho(False)
            logger.info("gcloud login code submitted env_id=%s login_session_id=%s", managed.env_id, login_session_id)
            managed.child.sendline(code)
            pieces: list[str] = []
            deadline = time.time() + self.settings.login_timeout_seconds
            while time.time() < deadline:
                index = managed.child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=5)
                pieces.append(managed.child.before or "")
                if index == 0:
                    break
                if not managed.child.isalive():
                    break
            else:
                raise TimeoutError("等待 gcloud 完成授权超时")
            output = "".join(pieces)
            logger.info(
                "gcloud login process finished env_id=%s login_session_id=%s exit_code=%s",
                managed.env_id,
                login_session_id,
                managed.child.exitstatus,
            )
            return managed.child.exitstatus, output
        finally:
            login_process_manager.pop(login_session_id)
            try:
                if managed.child.isalive():
                    managed.child.close(force=True)
            except Exception:
                logger.exception("gcloud login process close failed env_id=%s login_session_id=%s", managed.env_id, login_session_id)

    def cancel(self, login_session_id: str) -> bool:
        return login_process_manager.cancel(login_session_id)

    def has_process(self, login_session_id: str) -> bool:
        return login_process_manager.contains(login_session_id)

    def _wait_for_auth_url(self, managed: ManagedLoginProcess) -> str:
        deadline = time.time() + min(self.settings.login_timeout_seconds, 120)
        patterns = [AUTH_URL_RE, pexpect.EOF, pexpect.TIMEOUT]

        while time.time() < deadline:
            index = managed.child.expect(patterns, timeout=5)
            managed.output_buffer += managed.child.before or ""
            if index == 0:
                url = managed.child.match.group(0)
                managed.output_buffer += url
                return url
            if index == 1:
                logger.error("gcloud login exited before auth url env_id=%s login_session_id=%s", managed.env_id, managed.login_session_id)
                raise RuntimeError(f"gcloud auth login 过早退出: {managed.output_buffer}")
            # TIMEOUT: continue waiting

        logger.error("gcloud login auth url timeout env_id=%s login_session_id=%s", managed.env_id, managed.login_session_id)
        raise TimeoutError("等待 gcloud 授权链接超时")
