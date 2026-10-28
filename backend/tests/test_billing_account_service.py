import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from app.models.gcp_environment import GcpEnvironmentStatus
from app.runners.gcloud_runner import CommandResult
from app.services.billing_account_service import BillingAccountService
from app.services.preset_task_service import PresetTaskExecutor


def command_result(exit_code: int, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(args=[], exit_code=exit_code, stdout=stdout, stderr=stderr)


class BillingAccountServiceTest(TestCase):
    def setUp(self) -> None:
        self.db = Mock()
        self.db.scalar.return_value = None
        self.db.query.return_value.filter.return_value.first.return_value = None
        self.service = BillingAccountService(self.db)
        self.service.runner = Mock()
        self.env = SimpleNamespace(
            id="env-1",
            status=GcpEnvironmentStatus.AUTHORIZED,
            default_project_id="gcph-project-1",
            billing_account_id=None,
            billing_account_display_name=None,
            billing_account_selected_at=None,
            billing_account_selected_by=None,
        )

    def test_list_only_returns_open_accounts_in_stable_order(self) -> None:
        self.service.runner.run.return_value = command_result(
            0,
            json.dumps(
                [
                    {"name": "billingAccounts/BBBBBB-222222-333333", "displayName": "Zeta", "open": True},
                    {"name": "billingAccounts/AAAAAA-111111-222222", "displayName": "Alpha", "open": True},
                    {"name": "billingAccounts/CLOSED-111111-222222", "displayName": "Closed", "open": False},
                ]
            ),
        )

        accounts = self.service.list_available(self.env)

        self.assertEqual([account.billing_account_id for account in accounts], [
            "AAAAAA-111111-222222",
            "BBBBBB-222222-333333",
        ])
        self.assertEqual(
            self.service.runner.run.call_args.args[1],
            ["billing", "accounts", "list", "--filter=open=true", "--format=json(name,displayName,open)"],
        )

    def test_select_links_the_explicitly_selected_account_to_existing_project(self) -> None:
        self.service.runner.run.side_effect = [
            command_result(0, json.dumps([
                {"name": "billingAccounts/AAAAAA-111111-222222", "displayName": "Trial Credit", "open": True},
                {"name": "billingAccounts/BBBBBB-222222-333333", "displayName": "Other", "open": True},
            ])),
            command_result(0),
        ]

        account, linked_project_id = self.service.select(
            self.env,
            "AAAAAA-111111-222222",
            actor="operator",
        )

        self.assertEqual(account.display_name, "Trial Credit")
        self.assertEqual(linked_project_id, "gcph-project-1")
        self.assertEqual(self.env.billing_account_id, "AAAAAA-111111-222222")
        self.assertEqual(
            self.service.runner.run.call_args_list[1].args[1],
            [
                "billing", "projects", "link", "gcph-project-1",
                "--billing-account", "AAAAAA-111111-222222",
            ],
        )

    def test_bootstrap_never_falls_back_to_first_open_billing_account(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        with self.assertRaisesRegex(RuntimeError, "未选择 Billing Account"):
            executor._resolve_billing_account(SimpleNamespace(billing_account_id=None))
        self.assertEqual(
            executor._resolve_billing_account(SimpleNamespace(billing_account_id="AAAAAA-111111-222222")),
            "AAAAAA-111111-222222",
        )
