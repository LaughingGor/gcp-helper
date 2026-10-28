import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.gcp_command_log import GcpCommandLog
from app.models.gcp_environment import GcpEnvironment

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    args: list[str]
    exit_code: int
    stdout: str
    stderr: str
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _preview(value: str, limit: int = 4000) -> str:
    return value[-limit:] if value and len(value) > limit else value


def _redact_command(command: list[str], sensitive_values: list[str] | None = None) -> list[str]:
    if not sensitive_values:
        return command

    redacted = []
    values = [value for value in sensitive_values if value]
    for item in command:
        masked = item
        for value in values:
            masked = masked.replace(value, "***REDACTED***")
        redacted.append(masked)
    return redacted


def _limit_output(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    suffix = "\n...[输出已截断]"
    return value[: max(0, limit - len(suffix))] + suffix, True


def _redact_preview(value: str, *, redact_output: bool = False) -> str:
    if not value:
        return value
    if redact_output:
        return "***REDACTED***"
    return _preview(value)


class GcloudRunner:
    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def build_env(self, env: GcpEnvironment, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        process_env = os.environ.copy()
        process_env["CLOUDSDK_CONFIG"] = env.config_dir
        if extra_env:
            process_env.update({k: v for k, v in extra_env.items() if v is not None})
        return process_env

    def run(
        self,
        env: GcpEnvironment,
        args: list[str],
        *,
        timeout: int = 300,
        command_type: str = "GCLOUD",
        extra_env: dict[str, str] | None = None,
        use_configuration: bool = True,
        sensitive_values: list[str] | None = None,
        redact_output: bool = False,
        log_command: bool = True,
        max_output_chars: int | None = None,
        store_output_preview: bool = True,
    ) -> CommandResult:
        Path(env.config_dir).mkdir(parents=True, exist_ok=True)
        command = ["gcloud", "--quiet", "--configuration", env.config_name, *args] if use_configuration else ["gcloud", "--quiet", *args]
        started_at = datetime.utcnow()
        stdout = ""
        stderr = ""
        exit_code = -1
        status = "FAILED"
        logged_command = _redact_command(command, sensitive_values)
        if log_command:
            logger.info(
                "gcloud command start env_id=%s command_type=%s command=%s",
                env.id,
                command_type,
                json.dumps(logged_command, ensure_ascii=False),
            )
        try:
            completed = subprocess.run(
                command,
                env=self.build_env(env, extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
            status = "SUCCESS" if completed.returncode == 0 else "FAILED"
            output_truncated = False
            if max_output_chars is not None:
                stdout, stdout_truncated = _limit_output(stdout, max_output_chars)
                stderr, stderr_truncated = _limit_output(stderr, max_output_chars)
                output_truncated = stdout_truncated or stderr_truncated
            return CommandResult(
                args=command,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                output_truncated=output_truncated,
            )
        except subprocess.TimeoutExpired:
            logger.exception("gcloud command timeout env_id=%s command_type=%s timeout=%s", env.id, command_type, timeout)
            raise
        except Exception:
            logger.exception("gcloud command crashed env_id=%s command_type=%s", env.id, command_type)
            raise
        finally:
            duration = (datetime.utcnow() - started_at).total_seconds()
            if log_command:
                logger.info(
                    "gcloud command finish env_id=%s command_type=%s status=%s exit_code=%s duration=%.2fs",
                    env.id,
                    command_type,
                    status,
                    exit_code,
                    duration,
                )
            if self.db:
                self.db.add(
                    GcpCommandLog(
                        env_id=env.id,
                        command_type=command_type,
                        command_args_json=json.dumps(logged_command, ensure_ascii=False),
                        stdout_preview=_redact_preview(stdout, redact_output=redact_output) if store_output_preview else None,
                        stderr_preview=_redact_preview(stderr, redact_output=redact_output) if store_output_preview else None,
                        exit_code=exit_code,
                        status=status,
                        started_at=started_at,
                        finished_at=datetime.utcnow(),
                    )
                )
                self.db.commit()
