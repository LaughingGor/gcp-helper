import re
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.models.gcp_environment import GcpEnvironment
from app.runners.gcloud_runner import GcloudRunner


class GcloudConfigService:
    def __init__(self, runner: GcloudRunner) -> None:
        self.runner = runner
        self.settings = get_settings()

    @staticmethod
    def make_config_name(env_name: str) -> str:
        slug = env_name.strip().lower()
        slug = re.sub(r"[^a-z0-9-]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not slug or not slug[0].isalpha():
            slug = f"env-{slug}"
        suffix = uuid.uuid4().hex[:8]
        return f"gcp-{slug}-{suffix}"[:63].rstrip("-")

    def make_config_dir(self, env_id: str) -> str:
        path = self.settings.gcloud_base_dir / env_id / "gcloud"
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return str(path)

    def create_configuration(self, env: GcpEnvironment) -> None:
        result = self.runner.run(
            env,
            ["config", "configurations", "create", env.config_name, "--no-activate"],
            command_type="CREATE_CONFIGURATION",
            use_configuration=False,
        )
        if not result.ok and "already exists" not in (result.stderr + result.stdout).lower():
            raise RuntimeError(f"创建 gcloud configuration 失败: {result.stderr or result.stdout}")

    def set_proxy(self, env: GcpEnvironment, proxy_password: str | None = None) -> None:
        if not env.proxy_enabled:
            return
        required = {
            "proxy/type": env.proxy_type,
            "proxy/address": env.proxy_host,
            "proxy/port": str(env.proxy_port) if env.proxy_port else None,
        }
        for key, value in required.items():
            if not value:
                raise ValueError(f"代理已启用，但缺少 {key}")
            self._set(env, key, value)
        if env.proxy_username:
            self._set(env, "proxy/username", env.proxy_username)
        if proxy_password:
            self._set(env, "proxy/password", proxy_password, sensitive=True)

    def _set(self, env: GcpEnvironment, key: str, value: str, *, sensitive: bool = False) -> None:
        result = self.runner.run(
            env,
            ["config", "set", key, value],
            command_type="CONFIG_SET",
            sensitive_values=[value] if sensitive else None,
        )
        if not result.ok:
            raise RuntimeError(f"gcloud config set {key} 失败: {result.stderr or result.stdout}")
