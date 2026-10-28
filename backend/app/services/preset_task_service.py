import logging
import json
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus, PresetTaskType
from app.runners.gcloud_runner import CommandResult, GcloudRunner

logger = logging.getLogger(__name__)
_bootstrap_semaphore = threading.BoundedSemaphore(
    max(1, get_settings().max_concurrent_bootstrap_tasks)
)
_task_creation_lock = threading.RLock()
_running_tasks_lock = threading.Lock()
_running_tasks: set[str] = set()


class OrgPolicyPermissionError(RuntimeError):
    pass


class OrgPolicyApiDisabledError(RuntimeError):
    pass


class PresetTaskService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def start_bootstrap_project(self, env_id: str, *, vm_zone: str | None = None) -> GcpPresetTask:
        with _task_creation_lock:
            return self._create_bootstrap_task(env_id, vm_zone=vm_zone)

    def _create_bootstrap_task(self, env_id: str, *, vm_zone: str | None = None) -> GcpPresetTask:
        from app.services.project_replacement_service import ensure_no_replacement
        ensure_no_replacement(self.db, env_id)
        env = self.db.get(GcpEnvironment, env_id)
        if not env:
            raise KeyError("环境不存在")
        if env.status != GcpEnvironmentStatus.AUTHORIZED:
            raise RuntimeError("只有已授权的环境可以执行预设任务")
        if not env.billing_account_id:
            raise RuntimeError("请先在环境详情中选择 Billing Account，再启动预设任务")

        active = self.db.scalars(select(GcpPresetTask).where(GcpPresetTask.env_id == env_id,
            GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]))).first()
        if active:
            return active
        from app.schemas.user_settings import UpdateUserSettingsRequest
        resolved_zone = vm_zone or PresetTaskExecutor._resolve_compute_vm_zone(env, self.settings.compute_vm_zone)
        resolved_zone = UpdateUserSettingsRequest.validate_zone(resolved_zone)

        task = GcpPresetTask(
            env_id=env.id,
            task_type=PresetTaskType.BOOTSTRAP_PROJECT,
            status=PresetTaskStatus.PENDING,
            current_step="PENDING",
            requested_vm_zone=resolved_zone,
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        logger.info("preset task created env_id=%s task_id=%s task_type=%s", env.id, task.id, task.task_type.value)
        return task

    def retry_failed_task(self, task_id: str) -> tuple[GcpPresetTask, bool]:
        failed_task = self.db.get(GcpPresetTask, task_id)
        if not failed_task:
            raise KeyError("预设任务不存在")
        if failed_task.status != PresetTaskStatus.FAILED:
            raise RuntimeError("只有失败的预设任务可以重试")
        if failed_task.force_new_project:
            raise RuntimeError("请通过新建项目替换操作重试，避免遗漏渠道替换阶段")

        active_task = self.db.scalars(
            select(GcpPresetTask)
            .where(
                GcpPresetTask.env_id == failed_task.env_id,
                GcpPresetTask.task_type == failed_task.task_type,
                GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]),
            )
            .order_by(GcpPresetTask.created_at.desc())
            .limit(1)
        ).first()
        if active_task:
            logger.info(
                "reuse active preset task on retry failed_task_id=%s active_task_id=%s env_id=%s",
                failed_task.id,
                active_task.id,
                active_task.env_id,
            )
            return active_task, False

        retry_task = self.start_bootstrap_project(failed_task.env_id,
            vm_zone=failed_task.requested_vm_zone or failed_task.vm_zone)
        logger.info(
            "preset task retry created failed_task_id=%s retry_task_id=%s env_id=%s",
            failed_task.id,
            retry_task.id,
            retry_task.env_id,
        )
        return retry_task, True

    def get_task(self, env_id: str, task_id: str) -> GcpPresetTask:
        task = self.db.get(GcpPresetTask, task_id)
        if not task or task.env_id != env_id:
            raise KeyError("预设任务不存在")
        return task

    def get_latest_task(self, env_id: str) -> GcpPresetTask | None:
        return self.db.scalars(
            select(GcpPresetTask)
            .where(
                GcpPresetTask.env_id == env_id,
                GcpPresetTask.task_type == PresetTaskType.BOOTSTRAP_PROJECT,
            )
            .order_by(GcpPresetTask.created_at.desc())
            .limit(1)
        ).first()

    def list_tasks(
        self,
        *,
        status: PresetTaskStatus | None = None,
        env_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        owner_user_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> list[GcpPresetTask]:
        query = select(GcpPresetTask)
        if status:
            query = query.where(GcpPresetTask.status == status)
        if env_id:
            query = query.where(GcpPresetTask.env_id == env_id)
        if owner_user_id:
            query = query.join(GcpPresetTask.environment).where(GcpEnvironment.owner_user_id == owner_user_id)
        if created_from:
            query = query.where(GcpPresetTask.created_at >= created_from)
        if created_to:
            query = query.where(GcpPresetTask.created_at < created_to)
        query = query.order_by(GcpPresetTask.created_at.desc(), GcpPresetTask.id.desc()).offset(offset).limit(limit)
        return list(self.db.scalars(query).all())

    def get_key_file_path(self, env_id: str, task_id: str) -> Path:
        task = self.get_task(env_id, task_id)
        if task.status != PresetTaskStatus.SUCCESS:
            raise RuntimeError("预设任务尚未成功，不能下载 JSON key")
        if not task.key_file_path:
            raise FileNotFoundError("JSON key 文件不存在")

        env = task.environment
        env_root = Path(env.config_dir).parent.resolve()
        key_path = Path(task.key_file_path).resolve()
        if not key_path.is_relative_to(env_root):
            raise RuntimeError(f"拒绝下载不在环境目录下的文件: {key_path}")
        if not key_path.exists():
            raise FileNotFoundError("JSON key 文件不存在")
        return key_path


class PresetTaskExecutor:
    service_account_key_creation_constraint = "constraints/iam.disableServiceAccountKeyCreation"
    service_account_key_creation_constraints = (
        "constraints/iam.disableServiceAccountKeyCreation",
        "constraints/iam.managed.disableServiceAccountKeyCreation",
    )
    service_names = [
        "compute.googleapis.com",
        "aiplatform.googleapis.com",
        "iam.googleapis.com",
        "orgpolicy.googleapis.com",
    ]

    def __init__(self, db: Session, task_id: str) -> None:
        self.db = db
        self.task_id = task_id
        self.settings = get_settings()
        self.compute_vm_zone = self.settings.compute_vm_zone
        self.runner = GcloudRunner(db)

    def run(self) -> None:
        task = self.db.get(GcpPresetTask, self.task_id)
        if not task:
            logger.warning("preset task executor skipped missing task_id=%s", self.task_id)
            return
        env = task.environment
        if not task.requested_vm_zone:
            task.requested_vm_zone = task.vm_zone or self._resolve_compute_vm_zone(env, self.settings.compute_vm_zone)
            self.db.commit()
        self.compute_vm_zone = task.requested_vm_zone
        logger.info("preset task start env_id=%s task_id=%s", env.id, task.id)

        try:
            self._mark(task, PresetTaskStatus.RUNNING, "CHECK_AUTH", started_at=datetime.utcnow())
            self._require_ok(
                self.runner.run(
                    env,
                    ["auth", "print-access-token"],
                    command_type="CHECK_AUTH",
                    timeout=60,
                    redact_output=True,
                ),
                "当前环境授权已失效，请重新授权",
            )

            if getattr(task, "force_new_project", False):
                project_id = self._ensure_replacement_project(env, task)
                self._ensure_project_billing(env, project_id)
            else:
                self._clear_unmanaged_current_project(env, task)
                self._mark(task, PresetTaskStatus.RUNNING, "RESOLVE_MANAGED_PROJECT")
                project_id = self._resolve_managed_project_from_remote_list(env)
                if project_id:
                    self._mark(task, PresetTaskStatus.RUNNING, "USE_REMOTE_PROJECT")
                else:
                    self._mark(task, PresetTaskStatus.RUNNING, "CREATE_PROJECT")
                    project_id = self._create_project(env)
            task.project_id = project_id
            if not task.force_new_project:
                env.default_project_id = project_id
            self.db.commit()

            self._mark(task, PresetTaskStatus.RUNNING, "SET_CURRENT_PROJECT")
            self._require_ok(
                self.runner.run(env, ["config", "set", "project", project_id], command_type="SET_PROJECT", timeout=60),
                "设置当前 project 失败",
            )

            has_existing_key = self._has_existing_key(env, task)
            has_socks5_server = self._has_socks5_server(task)
            enabled_apis_in_this_run = False
            if has_existing_key and has_socks5_server:
                service_account = self._resolve_compute_default_service_account(env, task, project_id)
                self._ensure_agent_platform_user_role(env, task, project_id, service_account)
                self._mark(task, PresetTaskStatus.SUCCESS, "SUCCESS", finished_at=datetime.utcnow())
                logger.info("preset task no-op existing resources env_id=%s task_id=%s project_id=%s", env.id, task.id, project_id)
                return

            if has_existing_key:
                logger.info("skip enable apis because json key exists env_id=%s task_id=%s project_id=%s", env.id, task.id, project_id)
            else:
                self._mark(task, PresetTaskStatus.RUNNING, "ENABLE_APIS")
                try:
                    self._enable_apis(env, task, project_id)
                except Exception as exc:
                    if getattr(task, "force_new_project", False):
                        raise
                    project_id = self._recreate_project_after_api_enable_failure(env, task, project_id, exc)
                    self._mark(task, PresetTaskStatus.RUNNING, "SET_CURRENT_PROJECT")
                    self._require_ok(
                        self.runner.run(env, ["config", "set", "project", project_id], command_type="SET_REPLACEMENT_PROJECT", timeout=60),
                        "设置新 project 失败",
                    )
                    self._mark(task, PresetTaskStatus.RUNNING, "ENABLE_APIS")
                    self._enable_apis(env, task, project_id)
                enabled_apis_in_this_run = True

            if enabled_apis_in_this_run:
                self._mark(task, PresetTaskStatus.RUNNING, "WAIT_API_PROPAGATION")
                self._wait_for_compute_api_propagation(env, task)

            self._mark(task, PresetTaskStatus.RUNNING, "ENSURE_COMPUTE_NETWORK")
            network_name, subnet_self_link = self._ensure_compute_network(env, project_id)

            if self._has_socks5_server(task):
                vm_name = task.vm_name or ""
                socks5_port = task.socks5_port or ""
                logger.info("reuse socks5 vm env_id=%s task_id=%s vm_name=%s port=%s", env.id, task.id, vm_name, socks5_port)
            else:
                self._mark(task, PresetTaskStatus.RUNNING, "CREATE_COMPUTE_VM")
                vm_name, socks5_port, socks5_url, external_ip = self._create_compute_vm(
                    env,
                    task,
                    project_id,
                    network_name,
                    subnet_self_link,
                )
                task.vm_name = vm_name
                task.vm_zone = self.compute_vm_zone
                task.socks5_url = socks5_url
                task.socks5_port = socks5_port
                task.vm_external_ip = external_ip
                self.db.commit()

            if task.firewall_rule_name:
                logger.info("reuse firewall rule env_id=%s task_id=%s rule=%s", env.id, task.id, task.firewall_rule_name)
            else:
                self._mark(task, PresetTaskStatus.RUNNING, "CREATE_SOCKS5_FIREWALL")
                firewall_rule = self._create_firewall_rule(
                    env,
                    task,
                    project_id,
                    socks5_port,
                    network_name,
                )
                task.firewall_rule_name = firewall_rule
                self.db.commit()

            if self._has_existing_key(env, task):
                service_account = self._resolve_compute_default_service_account(env, task, project_id)
                self._ensure_agent_platform_user_role(env, task, project_id, service_account)
                logger.info("reuse service account json key env_id=%s task_id=%s key_file=%s", env.id, task.id, task.key_file_path)
            else:
                service_account = self._resolve_compute_default_service_account(env, task, project_id)

                self._mark(task, PresetTaskStatus.RUNNING, "CREATE_SERVICE_ACCOUNT_KEY")
                key_path = self._create_key(env, task, project_id, service_account)
                task.key_file_path = str(key_path)
                task.key_file_name = key_path.name
            self._mark(task, PresetTaskStatus.SUCCESS, "SUCCESS", finished_at=datetime.utcnow())
            logger.info("preset task success env_id=%s task_id=%s project_id=%s key_file=%s", env.id, task.id, project_id, task.key_file_path)
        except Exception as exc:
            task.error_code = "BOOTSTRAP_PROJECT_FAILED"
            task.error_message = str(exc)
            self._mark(task, PresetTaskStatus.FAILED, task.current_step or "FAILED", finished_at=datetime.utcnow())
            logger.exception("preset task failed env_id=%s task_id=%s step=%s", env.id, task.id, task.current_step)

    def _has_socks5_server(self, task: GcpPresetTask) -> bool:
        return bool(task.vm_name and task.vm_zone and task.socks5_port and task.socks5_url)

    def _has_existing_key(self, env: GcpEnvironment, task: GcpPresetTask) -> bool:
        if not task.key_file_path:
            return False
        env_root = Path(env.config_dir).parent.resolve()
        key_path = Path(task.key_file_path).resolve()
        if not key_path.is_relative_to(env_root):
            logger.warning("ignore key outside env dir env_id=%s task_id=%s key_path=%s", env.id, task.id, key_path)
            return False
        if not key_path.exists():
            logger.warning("ignore missing service account key env_id=%s task_id=%s key_path=%s", env.id, task.id, key_path)
            return False
        if getattr(task, "force_new_project", False):
            # A file left by an interrupted gcloud write must not count as a usable key.
            try:
                from app.schemas.channel_upload import ChannelUploadInput
                from app.services.channel_upload_service import validate_credential
                normalized, _ = validate_credential(ChannelUploadInput(kind="vertex_json", credential=key_path.read_text()))
                if json.loads(normalized)["project_id"] != task.project_id:
                    raise ValueError()
            except Exception:
                raise RuntimeError("新项目 Key 文件不完整或项目不符，请核对文件及远端 Key 后重试") from None
        return True

    def _clear_unmanaged_current_project(self, env: GcpEnvironment, task: GcpPresetTask) -> bool:
        result = self.runner.run(
            env,
            ["config", "get-value", "project"],
            command_type="GET_CURRENT_PROJECT",
            timeout=60,
        )
        self._require_ok(result, "读取当前 project 配置失败")
        configured_project = self._first_value(result.stdout)
        if configured_project == "(unset)":
            configured_project = ""

        known_projects = {
            project_id
            for project_id in (configured_project, task.project_id, env.default_project_id)
            if project_id
        }
        unmanaged_projects = sorted(
            project_id for project_id in known_projects if not self._is_managed_project_id(project_id)
        )
        if not unmanaged_projects:
            return False

        logger.warning(
            "clearing unmanaged local project env_id=%s task_id=%s projects=%s",
            env.id,
            task.id,
            ",".join(unmanaged_projects),
        )
        self._mark(task, PresetTaskStatus.RUNNING, "CLEAR_UNMANAGED_PROJECT")
        if configured_project and not self._is_managed_project_id(configured_project):
            self._require_ok(
                self.runner.run(
                    env,
                    ["config", "unset", "project"],
                    command_type="UNSET_UNMANAGED_PROJECT",
                    timeout=60,
                ),
                "清除非本系统 project 配置失败",
            )

        task.project_id = None
        env.default_project_id = None
        task.service_account_email = None
        task.key_file_path = None
        task.key_file_name = None
        task.vm_name = None
        task.vm_zone = None
        task.vm_external_ip = None
        task.firewall_rule_name = None
        task.socks5_port = None
        task.socks5_url = None
        self.db.commit()
        return True

    @staticmethod
    def _is_managed_project_id(project_id: str) -> bool:
        return project_id.strip().lower().startswith("gcph-")

    def _resolve_managed_project_from_remote_list(self, env: GcpEnvironment) -> str | None:
        result = self.runner.run(
            env,
            [
                "projects",
                "list",
                "--filter=lifecycleState=ACTIVE",
                "--sort-by=~createTime",
                "--format=value(projectId)",
            ],
            command_type="LIST_MANAGED_PROJECTS",
            timeout=120,
        )
        if not result.ok:
            logger.warning(
                "list remote projects failed env_id=%s detail=%s",
                env.id,
                result.stderr or result.stdout,
            )
            return None

        project_id = next(
            (
                value
                for line in result.stdout.splitlines()
                if (value := line.strip()) and self._is_managed_project_id(value)
            ),
            None,
        )
        if project_id:
            logger.info("using remote managed project env_id=%s project_id=%s", env.id, project_id)
        return project_id

    @staticmethod
    def _clear_project_related_data(task: GcpPresetTask, env: GcpEnvironment, project_id: str) -> None:
        if task.project_id == project_id:
            task.project_id = None
        if env.default_project_id == project_id:
            env.default_project_id = None
        task.service_account_email = None
        task.key_file_path = None
        task.key_file_name = None
        task.vm_name = None
        task.vm_zone = None
        task.vm_external_ip = None
        task.firewall_rule_name = None
        task.socks5_port = None
        task.socks5_url = None

    def _create_project(self, env: GcpEnvironment) -> str:
        created_at = datetime.utcnow()
        project_id = self._generate_project_id(created_at)
        project_name = f"gcp-helper-{created_at:%Y%m%d%H%M%S}"
        logger.info("creating no-organization project env_id=%s project_id=%s", env.id, project_id)
        # Omitting both --organization and --folder makes gcloud create the
        # project under "No organization".
        result = self.runner.run(
            env,
            ["projects", "create", project_id, "--name", project_name],
            command_type="CREATE_PROJECT",
            timeout=300,
        )
        self._require_ok(result, "创建 project 失败")
        logger.info("project created env_id=%s project_id=%s", env.id, project_id)
        return project_id

    @staticmethod
    def _is_not_found(result: CommandResult) -> bool:
        detail = result.stderr.lower()
        return not result.ok and ("not_found" in detail or "was not found" in detail)

    def _ensure_replacement_project(self, env: GcpEnvironment, task: GcpPresetTask) -> str:
        # Persist identity BEFORE the remote write, including across timeouts/restarts.
        if not task.project_id:
            task.project_id = self._generate_project_id()
            self.db.commit()
        self._mark(task, PresetTaskStatus.RUNNING, "CREATE_NEW_PROJECT")
        from app.services.secret_service import SecretService
        secrets_service = SecretService()
        plan = json.loads(secrets_service.decrypt_optional(getattr(task, "resource_plan_encrypted", None)) or "{}")
        if not plan.get("project_create_attempted"):
            # A brand-new project ID may return PERMISSION_DENIED on describe. The
            # first attempt can create directly; later attempts reconcile the same ID.
            plan["project_create_attempted"] = True
            task.resource_plan_encrypted = secrets_service.encrypt_optional(json.dumps(plan))
            self.db.commit()
            self._require_ok(self.runner.run(env, ["projects", "create", task.project_id,
                "--name", f"gcp-helper-{task.id[:8]}"], command_type="CREATE_PROJECT", timeout=300),
                "创建新 project 失败；重试将核对同一项目")
            return task.project_id
        result = self.runner.run(env, ["projects", "describe", task.project_id,
            "--format=value(lifecycleState)"], command_type="CHECK_NEW_PROJECT", timeout=60)
        if result.ok:
            if result.stdout.strip() != "ACTIVE":
                raise RuntimeError("新项目不处于 ACTIVE 状态，请核对后重试")
        elif self._is_not_found(result):
            self._require_ok(self.runner.run(env, ["projects", "create", task.project_id,
                "--name", f"gcp-helper-{task.id[:8]}"], command_type="CREATE_PROJECT", timeout=300),
                "创建新 project 失败；重试将核对同一项目")
        else:
            raise RuntimeError("无法核对新项目是否存在，请确认权限后重试；不会创建其他项目")
        return task.project_id

    def _recreate_project_after_api_enable_failure(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        cause: Exception,
    ) -> str:
        logger.warning(
            "clear local project data and recreate project after api enable failure env_id=%s task_id=%s project_id=%s error=%s",
            env.id,
            task.id,
            project_id,
            cause,
        )
        self._mark(task, PresetTaskStatus.RUNNING, "CLEAR_STALE_PROJECT")
        self._clear_project_related_data(task, env, project_id)
        self.db.commit()

        self._mark(task, PresetTaskStatus.RUNNING, "CREATE_REPLACEMENT_PROJECT")
        replacement_project_id = self._create_project(env)
        task.project_id = replacement_project_id
        env.default_project_id = replacement_project_id
        self.db.commit()
        logger.info(
            "replacement project resolved env_id=%s task_id=%s old_project_id=%s replacement_project_id=%s",
            env.id,
            task.id,
            project_id,
            replacement_project_id,
        )
        return replacement_project_id

    @staticmethod
    def _generate_project_id(created_at: datetime | None = None) -> str:
        timestamp = (created_at or datetime.utcnow()).strftime("%y%m%d%H%M")
        project_id = f"gcph-{timestamp}-{secrets.token_hex(3)}"
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
            raise RuntimeError(f"生成的 project id 不符合 GCP 规则: {project_id}")
        return project_id

    def _enable_apis(self, env: GcpEnvironment, task: GcpPresetTask, project_id: str) -> None:
        last_result: CommandResult | None = None
        for attempt in range(1, 5):
            command_type = "ENABLE_APIS" if attempt == 1 else f"ENABLE_APIS_RETRY_{attempt}"
            result = self.runner.run(
                env,
                ["services", "enable", *self.service_names, "--project", project_id],
                command_type=command_type,
                timeout=600,
            )
            last_result = result
            if result.ok:
                return

            if self._is_billing_not_enabled_error(result):
                logger.info("api enable blocked by missing billing env_id=%s task_id=%s project_id=%s", env.id, task.id, project_id)
                self._mark(task, PresetTaskStatus.RUNNING, "LINK_BILLING")
                self._ensure_project_billing(env, project_id)
                self._mark(task, PresetTaskStatus.RUNNING, "ENABLE_APIS")
                time.sleep(10)
                continue

            if self._is_retryable_service_usage_error(result):
                if self._all_services_enabled(env, project_id):
                    return
                if attempt < 4:
                    delay = attempt * 15
                    logger.warning(
                        "retry service enable after transient serviceusage error env_id=%s task_id=%s project_id=%s attempt=%s delay=%ss",
                        env.id,
                        task.id,
                        project_id,
                        attempt,
                        delay,
                    )
                    time.sleep(delay)
                    continue

            self._require_ok(result, "启用 GCP API 失败")

        if self._all_services_enabled(env, project_id):
            return
        if last_result:
            self._require_ok(last_result, "启用 GCP API 失败")

    def _ensure_project_billing(self, env: GcpEnvironment, project_id: str) -> None:
        describe = self.runner.run(
            env,
            ["billing", "projects", "describe", project_id, "--format=value(billingEnabled)"],
            command_type="DESCRIBE_PROJECT_BILLING",
            timeout=120,
        )
        if describe.ok and self._first_value(describe.stdout).lower() == "true":
            logger.info("project billing already enabled env_id=%s project_id=%s", env.id, project_id)
            return

        account = self._resolve_billing_account(env)
        logger.info("linking billing account env_id=%s project_id=%s billing_account=%s", env.id, project_id, account)
        result = self.runner.run(
            env,
            ["billing", "projects", "link", project_id, "--billing-account", account],
            command_type="LINK_PROJECT_BILLING",
            timeout=180,
        )
        self._require_ok(result, "绑定 Billing Account 失败")
        logger.info("billing account linked env_id=%s project_id=%s billing_account=%s", env.id, project_id, account)

    def _resolve_billing_account(self, env: GcpEnvironment) -> str:
        account = (env.billing_account_id or "").strip()
        if not account:
            raise RuntimeError("未选择 Billing Account，无法为新 project 启用计费")
        return account

    def _all_services_enabled(self, env: GcpEnvironment, project_id: str) -> bool:
        result = self.runner.run(
            env,
            ["services", "list", "--enabled", "--project", project_id, "--format=value(config.name)"],
            command_type="LIST_ENABLED_APIS",
            timeout=120,
        )
        if not result.ok:
            logger.warning("list enabled apis failed env_id=%s project_id=%s detail=%s", env.id, project_id, result.stderr or result.stdout)
            return False

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        missing = [service for service in self.service_names if not any(service in line for line in lines)]
        if missing:
            logger.info("apis not fully enabled env_id=%s project_id=%s missing=%s", env.id, project_id, ",".join(missing))
            return False
        logger.info("all required apis enabled env_id=%s project_id=%s", env.id, project_id)
        return True

    def _ensure_compute_network(self, env: GcpEnvironment, project_id: str) -> tuple[str, str]:
        network_name = self.settings.compute_vm_network_name.strip()
        if not network_name or not re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", network_name):
            raise RuntimeError(f"Compute 网络名称不合法: {network_name or '(empty)'}")

        describe = self.runner.run(
            env,
            [
                "compute",
                "networks",
                "describe",
                network_name,
                "--project",
                project_id,
                "--format=value(autoCreateSubnetworks)",
            ],
            command_type="DESCRIBE_COMPUTE_NETWORK",
            timeout=120,
        )
        if describe.ok:
            auto_subnets = self._first_value(describe.stdout).lower()
            if auto_subnets != "true":
                raise RuntimeError(
                    f"Compute 网络 {network_name} 已存在但不是自动子网模式，"
                    "请删除同名网络或修改 COMPUTE_VM_NETWORK_NAME"
                )
        else:
            logger.info(
                "creating compute network env_id=%s project_id=%s network=%s",
                env.id,
                project_id,
                network_name,
            )
            created = self.runner.run(
                env,
                [
                    "compute",
                    "networks",
                    "create",
                    network_name,
                    "--subnet-mode=auto",
                    "--bgp-routing-mode=regional",
                    "--project",
                    project_id,
                ],
                command_type="CREATE_COMPUTE_NETWORK",
                timeout=300,
            )
            self._require_ok(created, "创建 Compute VPC 网络失败")

        region = self._zone_to_region(self.compute_vm_zone)
        last_output = ""
        for attempt in range(12):
            subnet = self.runner.run(
                env,
                [
                    "compute",
                    "networks",
                    "subnets",
                    "list",
                    "--network",
                    network_name,
                    "--regions",
                    region,
                    "--project",
                    project_id,
                    "--format=value(selfLink)",
                ],
                command_type="CHECK_COMPUTE_SUBNET",
                timeout=120,
            )
            subnet_self_link = self._first_value(subnet.stdout) if subnet.ok else ""
            if subnet_self_link:
                logger.info(
                    "compute network ready env_id=%s project_id=%s network=%s region=%s",
                    env.id,
                    project_id,
                    network_name,
                    region,
                )
                return network_name, subnet_self_link
            last_output = subnet.stderr or subnet.stdout
            if attempt < 11:
                time.sleep(5)

        raise RuntimeError(
            f"Compute 网络 {network_name} 在 Region {region} 中没有可用子网: {last_output or '等待自动子网创建超时'}"
        )

    @staticmethod
    def _zone_to_region(zone: str) -> str:
        normalized = zone.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]-[a-z]", normalized):
            raise RuntimeError(f"无法从 Zone 解析 Region: {zone}")
        return normalized.rsplit("-", 1)[0]

    def _create_compute_vm(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        network_name: str,
        subnet_self_link: str,
    ) -> tuple[str, str, str, str]:
        startup_script = self.settings.socks5_startup_script_path
        if not startup_script.exists():
            raise FileNotFoundError(f"startup script 不存在: {startup_script}")

        vm_name = f"gcp-helper-socks5-{datetime.utcnow():%Y%m%d%H%M%S}-{secrets.token_hex(2)}"
        socks5_port, socks5_username, socks5_password = self._generate_socks5_credentials()
        if getattr(task, "force_new_project", False):
            from app.services.secret_service import SecretService
            secrets_service = SecretService()
            plan = json.loads(secrets_service.decrypt_optional(task.resource_plan_encrypted) or "{}")
            if not plan.get("vm_name"):
                plan.update({
                    "vm_name": f"gcp-helper-socks5-{task.id}", "port": socks5_port,
                    "username": socks5_username, "password": socks5_password,
                })
                task.resource_plan_encrypted = secrets_service.encrypt_optional(json.dumps(plan))
                self.db.commit()
            vm_name, socks5_port = plan["vm_name"], plan["port"]
            socks5_username, socks5_password = plan["username"], plan["password"]
            existing = self.runner.run(env, ["compute", "instances", "describe", vm_name,
                "--zone", self.compute_vm_zone, "--project", project_id, "--format=value(name)"],
                command_type="CHECK_REPLACEMENT_VM", timeout=60, redact_output=True)
            if existing.ok:
                external_ip = self._get_vm_external_ip(env, project_id, vm_name)
                if not external_ip:
                    raise RuntimeError("已有 VM 尚未分配外网 IP，请重试")
                return vm_name, socks5_port, f"socks5://{socks5_username}:{socks5_password}@{external_ip}:{socks5_port}", external_ip
            if not self._is_not_found(existing):
                raise RuntimeError("无法核对新项目 VM，不会重复创建，请确认权限后重试")
        logger.info(
            "creating compute vm env_id=%s task_id=%s project_id=%s vm_name=%s socks5_port=%s",
            env.id,
            task.id,
            project_id,
            vm_name,
            socks5_port,
        )
        result = self.runner.run(
            env,
            [
                "compute",
                "instances",
                "create",
                vm_name,
                "--zone",
                self.compute_vm_zone,
                "--machine-type",
                self.settings.compute_vm_machine_type,
                "--image-family",
                self.settings.compute_vm_image_family,
                "--image-project",
                self.settings.compute_vm_image_project,
                "--boot-disk-size",
                self.settings.compute_vm_boot_disk_size,
                "--boot-disk-type",
                self.settings.compute_vm_boot_disk_type,
                "--network",
                network_name,
                "--subnet",
                subnet_self_link,
                "--tags",
                self.settings.compute_vm_network_tag,
                "--metadata",
                f"socks5-port={socks5_port},socks5-username={socks5_username},socks5-password={socks5_password}",
                "--metadata-from-file",
                f"startup-script={startup_script}",
                "--project",
                project_id,
            ],
            command_type="CREATE_COMPUTE_VM",
            timeout=600,
            sensitive_values=[socks5_password],
            redact_output=True,
        )
        if not result.ok:
            detail = (result.stderr or result.stdout).replace(socks5_password, "***REDACTED***")
            raise RuntimeError(f"创建 Compute VM 失败: {detail}")
        external_ip = self._get_vm_external_ip(env, project_id, vm_name)
        if not external_ip:
            raise RuntimeError("创建 VM 成功，但未获取到外网 IP")
        socks5_url = f"socks5://{socks5_username}:{socks5_password}@{external_ip}:{socks5_port}"
        return vm_name, socks5_port, socks5_url, external_ip

    def _wait_for_compute_api_propagation(self, env: GcpEnvironment, task: GcpPresetTask) -> None:
        delay = max(0, self.settings.compute_api_propagation_wait_seconds)
        if not delay:
            return
        logger.info(
            "waiting for compute api propagation env_id=%s task_id=%s delay=%ss",
            env.id,
            task.id,
            delay,
        )
        time.sleep(delay)

    @staticmethod
    def _generate_socks5_credentials() -> tuple[str, str, str]:
        port = str(20000 + secrets.randbelow(40000))
        username = f"u{secrets.token_hex(4)}"
        password = secrets.token_hex(12)
        return port, username, password

    @staticmethod
    def _resolve_compute_vm_zone(env: GcpEnvironment, default_zone: str) -> str:
        return (getattr(env, "default_zone", None)
                or (env.owner.compute_vm_zone if env.owner and env.owner.compute_vm_zone else default_zone))

    def _get_vm_external_ip(self, env: GcpEnvironment, project_id: str, vm_name: str) -> str:
        result = self.runner.run(
            env,
            [
                "compute",
                "instances",
                "describe",
                vm_name,
                "--zone",
                self.compute_vm_zone,
                "--project",
                project_id,
                "--format=value(networkInterfaces[0].accessConfigs[0].natIP)",
            ],
            command_type="GET_VM_EXTERNAL_IP",
            timeout=60,
        )
        if result.ok:
            return self._first_value(result.stdout)
        return ""

    def _create_firewall_rule(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        socks5_port: str,
        network_name: str,
    ) -> str:
        rule_name = f"gcp-helper-socks5-{task.id[:8]}"
        if getattr(task, "force_new_project", False):
            existing = self.runner.run(env, ["compute", "firewall-rules", "describe", rule_name,
                "--project", project_id, "--format=value(name)"], command_type="CHECK_REPLACEMENT_FIREWALL", timeout=60)
            if existing.ok:
                return rule_name
            if not self._is_not_found(existing):
                raise RuntimeError("无法核对防火墙规则，请确认权限后重试")
        logger.info("creating socks5 firewall env_id=%s task_id=%s rule=%s port=%s", env.id, task.id, rule_name, socks5_port)
        result = self.runner.run(
            env,
            [
                "compute",
                "firewall-rules",
                "create",
                rule_name,
                "--direction",
                "INGRESS",
                "--priority",
                "1000",
                "--network",
                network_name,
                "--action",
                "ALLOW",
                "--rules",
                f"tcp:{socks5_port}",
                "--source-ranges",
                "0.0.0.0/0",
                "--target-tags",
                self.settings.compute_vm_network_tag,
                "--project",
                project_id,
            ],
            command_type="CREATE_SOCKS5_FIREWALL",
            timeout=120,
        )
        self._require_ok(result, "创建 socks5 防火墙规则失败")
        return rule_name

    def _get_project_number(self, env: GcpEnvironment, project_id: str) -> str:
        result = self.runner.run(
            env,
            ["projects", "describe", project_id, "--format=value(projectNumber)"],
            command_type="GET_PROJECT_NUMBER",
            timeout=120,
        )
        self._require_ok(result, "获取 project number 失败")
        project_number = self._first_value(result.stdout)
        if not project_number:
            raise RuntimeError("获取 project number 失败: 返回为空")
        logger.info("resolved project number env_id=%s project_id=%s project_number=%s", env.id, project_id, project_number)
        return project_number

    def _wait_for_service_account(self, env: GcpEnvironment, project_id: str, service_account: str) -> None:
        last_output = ""
        for _ in range(12):
            result = self.runner.run(
                env,
                ["iam", "service-accounts", "describe", service_account, "--project", project_id],
                command_type="DESCRIBE_COMPUTE_DEFAULT_SA",
                timeout=60,
            )
            if result.ok:
                logger.info("compute default service account ready env_id=%s service_account=%s", env.id, service_account)
                return
            last_output = result.stderr or result.stdout
            time.sleep(5)
        raise RuntimeError(f"Compute Engine 默认 service account 尚未可用: {last_output}")

    def _resolve_compute_default_service_account(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
    ) -> str:
        service_account = (task.service_account_email or "").strip()
        if not service_account:
            self._mark(task, PresetTaskStatus.RUNNING, "GET_PROJECT_NUMBER")
            project_number = self._get_project_number(env, project_id)
            service_account = f"{project_number}-compute@developer.gserviceaccount.com"
            task.service_account_email = service_account
            self.db.commit()

        self._mark(task, PresetTaskStatus.RUNNING, "WAIT_COMPUTE_DEFAULT_SERVICE_ACCOUNT")
        self._wait_for_service_account(env, project_id, service_account)
        return service_account

    def _ensure_agent_platform_user_role(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        service_account: str,
    ) -> None:
        self._mark(task, PresetTaskStatus.RUNNING, "GRANT_AGENT_PLATFORM_USER_ROLE")
        last_detail = ""
        for attempt in range(1, 4):
            result = self.runner.run(
                env,
                [
                    "projects",
                    "add-iam-policy-binding",
                    project_id,
                    "--member",
                    f"serviceAccount:{service_account}",
                    "--role",
                    "roles/aiplatform.user",
                    "--condition=None",
                ],
                command_type="GRANT_AGENT_PLATFORM_USER_ROLE",
                timeout=180,
            )
            if result.ok:
                delay = max(0, self.settings.agent_platform_iam_propagation_wait_seconds)
                if delay:
                    self._mark(task, PresetTaskStatus.RUNNING, "WAIT_AGENT_PLATFORM_IAM_PROPAGATION")
                    logger.info(
                        "waiting for Agent Platform IAM propagation env_id=%s task_id=%s project_id=%s delay=%ss",
                        env.id,
                        task.id,
                        project_id,
                        delay,
                    )
                    time.sleep(delay)
                return

            last_detail = result.stderr or result.stdout
            if self._is_iam_policy_conflict(last_detail) and attempt < 3:
                delay = attempt * 2
                logger.warning(
                    "retry agent platform role binding after IAM policy conflict env_id=%s task_id=%s attempt=%s delay=%ss",
                    env.id,
                    task.id,
                    attempt,
                    delay,
                )
                time.sleep(delay)
                continue
            break

        raise RuntimeError(
            "无法为 Compute 默认 Service Account 授予 roles/aiplatform.user；"
            "当前授权账号需要项目 IAM 修改权限（resourcemanager.projects.setIamPolicy）: "
            + last_detail
        )

    def _create_key(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        service_account: str,
    ) -> Path:
        env_root = Path(env.config_dir).parent
        key_dir = env_root / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        key_dir.chmod(0o700)
        key_path = key_dir / f"{task.id}.json"
        if getattr(task, "force_new_project", False):
            task.key_file_path, task.key_file_name = str(key_path), key_path.name
            self.db.commit()
            from app.services.secret_service import SecretService
            secrets_service = SecretService()
            plan = json.loads(secrets_service.decrypt_optional(task.resource_plan_encrypted) or "{}")
            keys = self.runner.run(env, ["iam", "service-accounts", "keys", "list", "--iam-account", service_account,
                "--managed-by=user", "--project", project_id, "--format=value(name)"],
                command_type="CHECK_REPLACEMENT_KEYS", timeout=60, redact_output=True)
            self._require_ok(keys, "无法核对 Service Account Key，请确认权限后重试")
            current_keys = sorted(line.strip() for line in keys.stdout.splitlines() if line.strip())
            if "keys_before_creation" in plan and set(current_keys) - set(plan["keys_before_creation"]):
                raise RuntimeError("发现上次尝试后新增的远端 Key，但本地 JSON 不可用；请核对并清理无用 Key 后重试，系统不会盲目重复创建")
            plan["keys_before_creation"] = current_keys
            task.resource_plan_encrypted = secrets_service.encrypt_optional(json.dumps(plan))
            self.db.commit()
        logger.info("creating service account key env_id=%s task_id=%s service_account=%s key_path=%s", env.id, task.id, service_account, key_path)

        self._ensure_agent_platform_user_role(env, task, project_id, service_account)

        result = self._run_create_service_account_key(
            env,
            key_path,
            project_id,
            service_account,
            command_type="CREATE_SERVICE_ACCOUNT_KEY",
        )
        if not result.ok:
            if not self._is_service_account_key_creation_disabled(result):
                self._require_ok(result, "创建 service account JSON key 失败")

            constraints = self._key_creation_constraints_to_disable(result)
            self._allow_service_account_key_creation(env, task, project_id, constraints)

            delay = max(0, self.settings.service_account_key_policy_wait_seconds)
            if delay:
                self._mark(task, PresetTaskStatus.RUNNING, "WAIT_KEY_POLICY_PROPAGATION")
                logger.info(
                    "waiting for service account key policy propagation env_id=%s task_id=%s project_id=%s delay=%ss",
                    env.id,
                    task.id,
                    project_id,
                    delay,
                )
                time.sleep(delay)

            self._mark(task, PresetTaskStatus.RUNNING, "CREATE_SERVICE_ACCOUNT_KEY_RETRY")
            result = self._run_create_service_account_key(
                env,
                key_path,
                project_id,
                service_account,
                command_type="CREATE_SERVICE_ACCOUNT_KEY_RETRY",
            )
            self._require_ok(result, "放开项目级 Key 创建策略后仍无法创建 service account JSON key")

        key_path.chmod(0o600)
        logger.info("service account key created env_id=%s task_id=%s key_path=%s", env.id, task.id, key_path)
        return key_path

    def _allow_service_account_key_creation(
        self,
        env: GcpEnvironment,
        task: GcpPresetTask,
        project_id: str,
        constraints: list[str],
    ) -> None:
        api_enable_attempted = False
        role_grant_attempted = False
        while True:
            self._mark(task, PresetTaskStatus.RUNNING, "ALLOW_SERVICE_ACCOUNT_KEY_CREATION")
            try:
                for constraint in constraints:
                    self._disable_key_creation_policy_enforcement(env, project_id, constraint)
                return
            except OrgPolicyApiDisabledError:
                if api_enable_attempted:
                    raise
                api_enable_attempted = True
                self._mark(task, PresetTaskStatus.RUNNING, "ENABLE_ORG_POLICY_API")
                self._enable_org_policy_api(env, project_id)
                delay = max(0, self.settings.org_policy_api_propagation_wait_seconds)
                if delay:
                    self._mark(task, PresetTaskStatus.RUNNING, "WAIT_ORG_POLICY_API_PROPAGATION")
                    logger.info(
                        "waiting for Organization Policy API propagation env_id=%s task_id=%s project_id=%s delay=%ss",
                        env.id,
                        task.id,
                        project_id,
                        delay,
                    )
                    time.sleep(delay)
            except OrgPolicyPermissionError:
                if role_grant_attempted:
                    raise
                role_grant_attempted = True
                self._mark(task, PresetTaskStatus.RUNNING, "GRANT_ORG_POLICY_ADMIN_ROLE")
                self._grant_org_policy_admin_role(env, project_id)

                role_delay = max(0, self.settings.org_policy_admin_role_wait_seconds)
                if role_delay:
                    logger.info(
                        "waiting for org policy admin role propagation env_id=%s task_id=%s project_id=%s delay=%ss",
                        env.id,
                        task.id,
                        project_id,
                        role_delay,
                    )
                    time.sleep(role_delay)

    def _enable_org_policy_api(self, env: GcpEnvironment, project_id: str) -> None:
        result = self.runner.run(
            env,
            ["services", "enable", "orgpolicy.googleapis.com", "--project", project_id],
            command_type="ENABLE_ORG_POLICY_API",
            timeout=600,
        )
        self._require_ok(result, "启用 Organization Policy API 失败")

    def _run_create_service_account_key(
        self,
        env: GcpEnvironment,
        key_path: Path,
        project_id: str,
        service_account: str,
        *,
        command_type: str,
    ) -> CommandResult:
        return self.runner.run(
            env,
            [
                "iam",
                "service-accounts",
                "keys",
                "create",
                str(key_path),
                "--iam-account",
                service_account,
                "--project",
                project_id,
                "--key-file-type",
                "json",
            ],
            command_type=command_type,
            timeout=120,
        )

    def _grant_org_policy_admin_role(self, env: GcpEnvironment, project_id: str) -> None:
        account = (env.authorized_account or env.google_account_hint or "").strip()
        if not account:
            raise RuntimeError("无法确定需要授予 Org Policy Admin 的 Google 账号")

        organization_id = self._resolve_project_organization(env, project_id)

        logger.warning(
            "granting organization policy admin role env_id=%s project_id=%s organization_id=%s account=%s",
            env.id,
            project_id,
            organization_id,
            account,
        )
        result = self.runner.run(
            env,
            [
                "organizations",
                "add-iam-policy-binding",
                organization_id,
                "--member",
                f"user:{account}",
                "--role",
                "roles/orgpolicy.policyAdmin",
                "--condition=None",
            ],
            command_type="GRANT_ORG_POLICY_ADMIN_ROLE",
            timeout=180,
        )
        if not result.ok:
            detail = result.stderr or result.stdout
            raise RuntimeError(
                "无法在 Organization 层给当前 Google 账号授予 roles/orgpolicy.policyAdmin；"
                "该角色不支持 Project 级绑定，必须由组织管理员授予。执行账号需要 "
                "resourcemanager.organizations.setIamPolicy（通常来自 roles/resourcemanager.organizationAdmin）: "
                + detail
            )

    def _resolve_project_organization(self, env: GcpEnvironment, project_id: str) -> str:
        result = self.runner.run(
            env,
            [
                "projects",
                "get-ancestors",
                project_id,
                "--format=json",
            ],
            command_type="RESOLVE_PROJECT_ORGANIZATION",
            timeout=120,
        )
        if not result.ok:
            raise RuntimeError(f"查询 Project 所属 Organization 失败: {result.stderr or result.stdout}")
        try:
            ancestors = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("查询 Project 所属 Organization 返回了无效 JSON") from exc
        organization_id = next(
            (
                str(item.get("id") or "").strip()
                for item in ancestors
                if isinstance(item, dict) and str(item.get("type") or "").lower() == "organization"
            ),
            "",
        )
        if not organization_id:
            raise RuntimeError(
                "当前 Project 不属于可识别的 Organization，无法授予 roles/orgpolicy.policyAdmin"
            )
        return organization_id

    def _disable_key_creation_policy_enforcement(
        self,
        env: GcpEnvironment,
        project_id: str,
        constraint: str,
    ) -> None:
        logger.warning(
            "service account key creation blocked by org policy; creating project override env_id=%s project_id=%s constraint=%s",
            env.id,
            project_id,
            constraint,
        )
        if constraint.startswith("constraints/iam.managed."):
            result = self._set_managed_key_creation_policy_override(env, project_id, constraint)
        else:
            result = self.runner.run(
                env,
                [
                    "resource-manager",
                    "org-policies",
                    "disable-enforce",
                    constraint,
                    "--project",
                    project_id,
                ],
                command_type="ALLOW_SERVICE_ACCOUNT_KEY_CREATION",
                timeout=180,
            )
        if not result.ok:
            detail = result.stderr or result.stdout
            normalized_detail = detail.lower()
            if self._is_org_policy_api_disabled(normalized_detail):
                raise OrgPolicyApiDisabledError(
                    "Organization Policy API 未启用，正在尝试启用后重试: " + detail
                )
            if (
                "permission_denied" in normalized_detail
                or "permission denied" in normalized_detail
                or "does not have permission" in normalized_detail
                or "required permission" in normalized_detail
            ):
                raise OrgPolicyPermissionError(
                    "当前账号缺少修改 Organization Policy 的权限: " + detail
                )
            raise RuntimeError(f"为当前 Project 修改 Key 创建组织策略失败: {detail}")

    @staticmethod
    def _is_org_policy_api_disabled(detail: str) -> bool:
        return (
            "service_disabled" in detail
            or (
                "orgpolicy.googleapis.com" in detail
                and "has not been used in project" in detail
            )
        )

    def _set_managed_key_creation_policy_override(
        self,
        env: GcpEnvironment,
        project_id: str,
        constraint: str,
    ) -> CommandResult:
        policy_dir = Path(env.config_dir).parent / "org-policies"
        policy_dir.mkdir(parents=True, exist_ok=True)
        policy_dir.chmod(0o700)
        policy_path = policy_dir / f"allow-key-creation-{secrets.token_hex(4)}.json"
        policy_path.write_text(
            json.dumps(
                self._managed_key_policy_payload(project_id, constraint),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        policy_path.chmod(0o600)
        try:
            return self.runner.run(
                env,
                ["org-policies", "set-policy", str(policy_path)],
                command_type="ALLOW_MANAGED_SERVICE_ACCOUNT_KEY_CREATION",
                timeout=180,
            )
        finally:
            policy_path.unlink(missing_ok=True)

    @staticmethod
    def _managed_key_policy_payload(project_id: str, constraint: str) -> dict:
        constraint_name = constraint.removeprefix("constraints/")
        return {
            "name": f"projects/{project_id}/policies/{constraint_name}",
            "spec": {"rules": [{"enforce": False}]},
        }

    @classmethod
    def _is_service_account_key_creation_disabled(cls, result: CommandResult) -> bool:
        detail = f"{result.stderr}\n{result.stdout}".lower()
        return (
            "disableserviceaccountkeycreation" in detail
            or "service account key creation is disabled" in detail
            or "key creation is not allowed" in detail
            or "key_creation_disabled" in detail
        )

    @staticmethod
    def _is_iam_policy_conflict(detail: str) -> bool:
        normalized = detail.lower()
        return "aborted" in normalized or "etag" in normalized or "concurrent" in normalized

    @classmethod
    def _extract_key_creation_constraint(cls, result: CommandResult) -> str:
        detail = f"{result.stderr}\n{result.stdout}"
        matches = re.findall(r"constraints/[A-Za-z0-9_.]+", detail)
        return next(
            (constraint for constraint in matches if "disableServiceAccountKeyCreation" in constraint),
            cls.service_account_key_creation_constraint,
        )

    @classmethod
    def _key_creation_constraints_to_disable(cls, result: CommandResult) -> list[str]:
        detected = cls._extract_key_creation_constraint(result)
        return [
            detected,
            *[
                constraint
                for constraint in cls.service_account_key_creation_constraints
                if constraint != detected
            ],
        ]

    def _mark(
        self,
        task: GcpPresetTask,
        status: PresetTaskStatus,
        step: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        task.status = status
        task.current_step = step
        if started_at:
            task.started_at = started_at
        if finished_at:
            task.finished_at = finished_at
        self.db.commit()
        logger.info("preset task step env_id=%s task_id=%s status=%s step=%s", task.env_id, task.id, status.value, step)

    @staticmethod
    def _first_value(value: str) -> str:
        return value.strip().splitlines()[0].strip() if value and value.strip() else ""

    @staticmethod
    def _require_ok(result: CommandResult, message: str) -> None:
        if not result.ok:
            detail = result.stderr or result.stdout
            raise RuntimeError(f"{message}: {detail}")

    @staticmethod
    def _is_billing_not_enabled_error(result: CommandResult) -> bool:
        detail = f"{result.stderr}\n{result.stdout}"
        return "UREQ_PROJECT_BILLING_NOT_FOUND" in detail or "billing-enabled" in detail

    @staticmethod
    def _is_retryable_service_usage_error(result: CommandResult) -> bool:
        detail = f"{result.stderr}\n{result.stdout}"
        return (
            "OPERATIONS_RESOURCE_NOT_FOUND" in detail
            or "Operations instance" in detail
            or "Could not find container for operation" in detail
        )


def run_bootstrap_project_task(task_id: str) -> None:
    with _running_tasks_lock:
        if task_id in _running_tasks:
            return
        _running_tasks.add(task_id)
    try:
        _run_bootstrap_project_task(task_id)
    finally:
        with _running_tasks_lock:
            _running_tasks.discard(task_id)


def _run_bootstrap_project_task(task_id: str) -> None:
    logger.info("preset task waiting for execution slot task_id=%s", task_id)
    with _bootstrap_semaphore:
        db = SessionLocal()
        try:
            task = db.get(GcpPresetTask, task_id)
            if not task or task.status != PresetTaskStatus.PENDING:
                return
            PresetTaskExecutor(db, task_id).run()
            task = db.get(GcpPresetTask, task_id)
            if task and task.status == PresetTaskStatus.SUCCESS:
                try:
                    from app.services.gcp_channel_service import register_gcp_credential
                    register_gcp_credential(db, task)
                except Exception:
                    db.rollback()
                    logger.warning("GCP key archive pending env_id=%s task_id=%s; retry from push endpoint", task.env_id, task.id)
        finally:
            db.close()


def recover_interrupted_preset_tasks() -> None:
    with SessionLocal() as db:
        for task in db.scalars(select(GcpPresetTask).where(
                GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]))):
            task.status = PresetTaskStatus.FAILED
            task.error_code = "BACKEND_RESTARTED"
            task.error_message = "服务重启中断任务，请重试；重试保留原计划 Zone"
            task.finished_at = datetime.utcnow()
        db.commit()
