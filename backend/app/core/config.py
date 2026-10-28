from functools import lru_cache
from pathlib import Path
from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr


class Settings(BaseSettings):
    app_secret_key: SecretStr = SecretStr("dev-only-change-me")
    app_name: str = "GCP Helper"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./data/gcp_helper.db"
    gcloud_base_dir: Path = Path("./data/gcloud-envs")
    login_timeout_seconds: int = 900
    max_concurrent_logins: int = 5
    max_concurrent_bootstrap_tasks: int = 2
    gcloud_console_max_timeout_seconds: int = 300
    gcloud_console_max_output_chars: int = 65536
    gcloud_console_max_arguments: int = 128
    auth_session_hours: int = 12
    auth_cookie_secure: bool = False
    auth_session_cookie_name: str = "gcph_session"
    auth_csrf_cookie_name: str = "gcph_csrf"
    gcloud_auth_browser_flag: str = "--no-launch-browser"
    allowed_cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    log_level: str = "INFO"
    log_file: Path = Path("./data/logs/app.log")
    socks5_startup_script_path: Path = Path("./script/socks5.sh")
    compute_vm_zone: str = "us-west2-a"
    compute_vm_machine_type: str = "e2-micro"
    compute_vm_image_family: str = "debian-12"
    compute_vm_image_project: str = "debian-cloud"
    compute_vm_boot_disk_size: str = "10GB"
    compute_vm_boot_disk_type: str = "pd-standard"
    compute_vm_network_name: str = "gcp-helper-network"
    compute_vm_network_tag: str = "socks5-server"
    compute_api_propagation_wait_seconds: int = 10
    service_account_key_policy_wait_seconds: int = 180
    org_policy_admin_role_wait_seconds: int = 5
    org_policy_api_propagation_wait_seconds: int = 10
    agent_platform_iam_propagation_wait_seconds: int = 10
    new_api_channel_template_dir: Path = Path("./data/channel-templates")
    new_api_channel_default_template: str = "default"
    new_api_channel_stats_group: str = "Gemini_Vertex_Pro"
    new_api_channel_budget_amount: Decimal = Decimal("300")
    new_api_quota_per_unit: Decimal = Decimal("500000")
    new_api_currency: str = "USD"
    new_api_channel_stats_cache_seconds: int = 30
    new_api_channel_stats_page_size: int = 100
    new_api_channel_stats_max_pages: int = 100
    # Optional shared destination. When unset uploads use the user's existing channel configuration.
    new_api_upload_base_url: str = ""
    new_api_upload_token: str = ""
    new_api_upload_models: str = "gemini-2.5-flash"
    new_api_upload_group: str = "default"
    new_api_upload_vertex_region: str = "global"
    new_api_upload_vertex_template: str = ""
    new_api_upload_gemini_template: str = ""
    channel_upload_credential_hours: int = 24  # Deprecated; accepted for old .env files. Credentials no longer expire.
    channel_upload_sync_seconds: int = 120
    channel_upload_snapshot_days: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.gcloud_base_dir.mkdir(parents=True, exist_ok=True)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    settings.new_api_channel_template_dir.mkdir(parents=True, exist_ok=True)
    return settings
