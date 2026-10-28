import json
import shlex
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.gcp_command_log import GcpCommandLog
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
from app.schemas.gcloud_command import (
    ExecuteGcloudCommandRequest,
    GcloudCommandHistoryItem,
    GcloudCommandResponse,
)
from app.runners.gcloud_runner import GcloudRunner


class GcloudCommandPolicyError(ValueError):
    pass


class GcloudCommandConfirmationRequired(RuntimeError):
    pass


class GcloudCommandBusy(RuntimeError):
    pass


_console_locks_guard = threading.RLock()
_console_locks: dict[str, threading.Lock] = {}


class GcloudCommandService:
    forbidden_flag_prefixes = (
        "--configuration",
        "--account",
        "--impersonate-service-account",
        "--credential-file-override",
        "--access-token-file",
        "--flags-file",
    )
    forbidden_characters = (";", "|", "&", "`", "$", "\n", "\r")
    sensitive_prefixes = (
        ("auth", "print-access-token"),
        ("auth", "application-default", "print-access-token"),
    )
    read_only_verbs = {"list", "describe", "get", "show"}
    mutation_verbs = {
        "create", "delete", "update", "set", "enable", "disable", "add", "remove", "link",
        "attach", "detach", "activate", "undelete", "restore", "import", "deploy", "submit",
        "apply", "run", "stop", "start", "restart", "reset", "ssh", "scp", "copy", "move",
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.runner = GcloudRunner(db)

    def execute(self, env: GcpEnvironment, actor: str, request: ExecuteGcloudCommandRequest) -> GcloudCommandResponse:
        from app.services.project_replacement_service import ensure_no_replacement
        try:
            ensure_no_replacement(self.db, env.id)
        except RuntimeError as exc:
            raise GcloudCommandBusy(str(exc)) from None
        if env.status != GcpEnvironmentStatus.AUTHORIZED:
            raise GcloudCommandPolicyError("只有已授权的环境可以执行 gcloud 命令")
        if request.timeout_seconds > self.settings.gcloud_console_max_timeout_seconds:
            raise GcloudCommandPolicyError("命令超时设置超过系统允许的上限")
        if self._has_active_bootstrap_task(env.id):
            raise GcloudCommandBusy("环境正在执行预设任务，请等待任务完成后再运行命令")

        args = self.parse_command(request.command)
        if self._is_sensitive_command(args):
            raise GcloudCommandPolicyError("该命令可能输出凭据或 Access Token，禁止在控制台执行")
        if self._is_high_risk(args) and not request.confirm_high_risk:
            raise GcloudCommandConfirmationRequired("命令可能修改云资源，请确认后执行")

        lock = self._lock_for(env.id)
        if not lock.acquire(blocking=False):
            raise GcloudCommandBusy("该环境已有命令正在执行")
        started_at = datetime.utcnow()
        started_monotonic = time.monotonic()
        try:
            result = self.runner.run(
                env,
                args,
                timeout=request.timeout_seconds,
                command_type="MANUAL_GCLOUD",
                max_output_chars=self.settings.gcloud_console_max_output_chars,
                store_output_preview=False,
            )
        finally:
            lock.release()
        finished_at = datetime.utcnow()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        command = f"gcloud {shlex.join(args)}"
        self.db.add(AuditLog(
            actor=actor,
            action="GCLOUD_CONSOLE_EXECUTE",
            target_type="gcp_environment",
            target_id=env.id,
            detail_json=json.dumps({
                "command": command,
                "exitCode": result.exit_code,
                "durationMs": duration_ms,
                "outputTruncated": result.output_truncated,
            }, ensure_ascii=False),
        ))
        self.db.commit()
        return GcloudCommandResponse(
            command=command,
            exitCode=result.exit_code,
            success=result.ok,
            stdout=result.stdout,
            stderr=result.stderr,
            startedAt=started_at,
            finishedAt=finished_at,
            durationMs=duration_ms,
            outputTruncated=result.output_truncated,
        )

    def history(self, env_id: str, limit: int) -> list[GcloudCommandHistoryItem]:
        logs = self.db.scalars(
            select(GcpCommandLog)
            .where(GcpCommandLog.env_id == env_id, GcpCommandLog.command_type == "MANUAL_GCLOUD")
            .order_by(GcpCommandLog.started_at.desc())
            .limit(limit)
        ).all()
        return [
            GcloudCommandHistoryItem(
                id=item.id,
                command=self._display_command(item.command_args_json),
                exitCode=item.exit_code,
                status=item.status,
                startedAt=item.started_at,
                finishedAt=item.finished_at,
            )
            for item in logs
        ]

    def parse_command(self, command: str) -> list[str]:
        if any(character in command for character in self.forbidden_characters):
            raise GcloudCommandPolicyError("命令不能包含 Shell 运算符、换行或变量展开")
        try:
            args = shlex.split(command, posix=True)
        except ValueError as exc:
            raise GcloudCommandPolicyError(f"命令格式不正确: {exc}") from exc
        if not args:
            raise GcloudCommandPolicyError("gcloud 命令不能为空")
        if args[0] == "gcloud":
            raise GcloudCommandPolicyError("请输入 gcloud 后的参数，不要重复输入 gcloud")
        if len(args) > self.settings.gcloud_console_max_arguments:
            raise GcloudCommandPolicyError("命令参数过多")
        if any(len(arg) > 2048 for arg in args):
            raise GcloudCommandPolicyError("单个命令参数过长")
        for arg in args:
            lowered = arg.lower()
            if any(lowered == prefix or lowered.startswith(f"{prefix}=") for prefix in self.forbidden_flag_prefixes):
                raise GcloudCommandPolicyError(f"不允许覆盖当前环境的 gcloud 参数: {arg}")
        return args

    def _has_active_bootstrap_task(self, env_id: str) -> bool:
        task = self.db.scalars(
            select(GcpPresetTask)
            .where(
                GcpPresetTask.env_id == env_id,
                GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]),
            )
            .limit(1)
        ).first()
        return task is not None

    def _is_sensitive_command(self, args: list[str]) -> bool:
        lowered = tuple(arg.lower() for arg in args)
        return any(lowered[:len(prefix)] == prefix for prefix in self.sensitive_prefixes)

    def _is_high_risk(self, args: list[str]) -> bool:
        lowered = {arg.lower() for arg in args}
        if lowered & self.mutation_verbs:
            return True
        return not bool(lowered & self.read_only_verbs)

    @staticmethod
    def _lock_for(env_id: str) -> threading.Lock:
        with _console_locks_guard:
            return _console_locks.setdefault(env_id, threading.Lock())

    @staticmethod
    def _display_command(command_args_json: str | None) -> str:
        try:
            args = json.loads(command_args_json or "[]")
            if isinstance(args, list):
                values = [str(arg) for arg in args]
                if len(values) >= 4 and values[0] == "gcloud" and values[1] == "--quiet" and values[2] == "--configuration":
                    values = values[4:]
                return f"gcloud {shlex.join(values)}"
        except (TypeError, ValueError):
            pass
        return "gcloud 命令"
