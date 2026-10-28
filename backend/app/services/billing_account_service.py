import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
from app.runners.gcloud_runner import GcloudRunner


@dataclass(frozen=True)
class AvailableBillingAccount:
    billing_account_id: str
    display_name: str
    open: bool


class BillingAccountService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.runner = GcloudRunner(db)

    def list_available(self, env: GcpEnvironment) -> list[AvailableBillingAccount]:
        self._require_authorized(env)
        result = self.runner.run(
            env,
            [
                "billing",
                "accounts",
                "list",
                "--filter=open=true",
                "--format=json(name,displayName,open)",
            ],
            command_type="LIST_BILLING_ACCOUNTS_FOR_SELECTION",
            timeout=120,
        )
        if not result.ok:
            raise RuntimeError(f"查询可用 Billing Account 失败: {result.stderr or result.stdout}")
        try:
            records = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Billing Account 列表返回了无效 JSON") from exc
        if not isinstance(records, list):
            raise RuntimeError("Billing Account 列表返回格式不正确")

        accounts: list[AvailableBillingAccount] = []
        for record in records:
            if not isinstance(record, dict) or record.get("open") is not True:
                continue
            account_id = str(record.get("name") or "").removeprefix("billingAccounts/").strip().upper()
            if not account_id:
                continue
            display_name = str(record.get("displayName") or account_id).strip() or account_id
            accounts.append(AvailableBillingAccount(account_id, display_name, True))
        return sorted(accounts, key=lambda account: (account.display_name.lower(), account.billing_account_id))

    def select(
        self,
        env: GcpEnvironment,
        billing_account_id: str,
        *,
        actor: str,
    ) -> tuple[AvailableBillingAccount, str | None]:
        self._require_authorized(env)
        self._ensure_no_active_task(env.id)
        selected = next(
            (account for account in self.list_available(env) if account.billing_account_id == billing_account_id),
            None,
        )
        if not selected:
            raise ValueError("所选 Billing Account 不可用、已关闭或当前授权账号无权访问")

        project_id = env.default_project_id
        if project_id:
            result = self.runner.run(
                env,
                ["billing", "projects", "link", project_id, "--billing-account", selected.billing_account_id],
                command_type="UPDATE_PROJECT_BILLING_ACCOUNT",
                timeout=180,
            )
            if not result.ok:
                raise RuntimeError(f"链接 Billing Account 失败: {result.stderr or result.stdout}")

        env.billing_account_id = selected.billing_account_id
        env.billing_account_display_name = selected.display_name
        env.billing_account_selected_at = datetime.utcnow()
        env.billing_account_selected_by = actor
        self.db.add(
            AuditLog(
                actor=actor,
                action="BILLING_ACCOUNT_SELECT",
                target_type="gcp_environment",
                target_id=env.id,
                detail_json=json.dumps(
                    {
                        "billingAccountId": selected.billing_account_id,
                        "displayName": selected.display_name,
                        "linkedProjectId": project_id,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self.db.commit()
        self.db.refresh(env)
        return selected, project_id

    @staticmethod
    def _require_authorized(env: GcpEnvironment) -> None:
        if env.status != GcpEnvironmentStatus.AUTHORIZED:
            raise RuntimeError("只有已授权的环境可以选择 Billing Account")

    def _ensure_no_active_task(self, env_id: str) -> None:
        from app.services.project_replacement_service import ensure_no_replacement
        ensure_no_replacement(self.db, env_id)
        active_task = self.db.query(GcpPresetTask.id).filter(
            GcpPresetTask.env_id == env_id,
            GcpPresetTask.status.in_([PresetTaskStatus.PENDING, PresetTaskStatus.RUNNING]),
        ).first()
        if active_task:
            raise RuntimeError("预设任务正在运行，不能更换 Billing Account")
