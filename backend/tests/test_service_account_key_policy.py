from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.runners.gcloud_runner import CommandResult
from app.services.preset_task_service import OrgPolicyApiDisabledError, PresetTaskExecutor


def result(exit_code: int, stderr: str = "") -> CommandResult:
    return CommandResult(args=[], exit_code=exit_code, stdout="", stderr=stderr)


class ServiceAccountKeyPolicyTest(TestCase):
    def test_org_policy_api_is_enabled_with_required_services(self) -> None:
        self.assertIn("orgpolicy.googleapis.com", PresetTaskExecutor.service_names)

    def test_service_disabled_is_not_misclassified_as_permission_denied(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.runner = Mock()
        executor.runner.run.return_value = result(
            1,
            "PERMISSION_DENIED: Organization Policy API has not been used in project project-1 "
            "before or it is disabled. reason: SERVICE_DISABLED service: orgpolicy.googleapis.com",
        )

        with self.assertRaises(OrgPolicyApiDisabledError):
            executor._disable_key_creation_policy_enforcement(
                SimpleNamespace(id="env-1"),
                "project-1",
                "constraints/iam.disableServiceAccountKeyCreation",
            )

    def test_detects_and_extracts_key_creation_constraint(self) -> None:
        failed = result(
            1,
            "Key creation is not allowed because constraints/iam.disableServiceAccountKeyCreation is enforced",
        )

        self.assertTrue(PresetTaskExecutor._is_service_account_key_creation_disabled(failed))
        self.assertEqual(
            PresetTaskExecutor._extract_key_creation_constraint(failed),
            "constraints/iam.disableServiceAccountKeyCreation",
        )
        self.assertEqual(
            PresetTaskExecutor._key_creation_constraints_to_disable(failed),
            [
                "constraints/iam.disableServiceAccountKeyCreation",
                "constraints/iam.managed.disableServiceAccountKeyCreation",
            ],
        )

    def test_resolves_organization_from_ancestor_json(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.runner = Mock()
        executor.runner.run.return_value = CommandResult(
            args=[],
            exit_code=0,
            stdout='[{"id":"project-1","type":"project"},{"id":"123456789","type":"organization"}]',
            stderr="",
        )

        organization_id = executor._resolve_project_organization(
            SimpleNamespace(id="env-1"),
            "project-1",
        )

        self.assertEqual(organization_id, "123456789")
        self.assertEqual(
            executor.runner.run.call_args.args[1],
            ["projects", "get-ancestors", "project-1", "--format=json"],
        )

    def test_managed_constraint_uses_v2_policy_payload(self) -> None:
        self.assertEqual(
            PresetTaskExecutor._managed_key_policy_payload(
                "project-1",
                "constraints/iam.managed.disableServiceAccountKeyCreation",
            ),
            {
                "name": "projects/project-1/policies/iam.managed.disableServiceAccountKeyCreation",
                "spec": {"rules": [{"enforce": False}]},
            },
        )

    def test_key_creation_policy_is_overridden_then_key_creation_retried(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_root = Path(temp_dir) / "env"
            config_dir = env_root / "gcloud"
            key_dir = env_root / "keys"
            key_dir.mkdir(parents=True)
            key_path = key_dir / "task-1.json"
            key_path.write_text("{}", encoding="utf-8")

            executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
            executor.db = Mock()
            executor.settings = SimpleNamespace(
                service_account_key_policy_wait_seconds=180,
                org_policy_admin_role_wait_seconds=0,
                agent_platform_iam_propagation_wait_seconds=0,
            )
            executor.runner = Mock()
            executor.runner.run.side_effect = [
                result(0),
                result(1, "constraints/iam.disableServiceAccountKeyCreation is enforced"),
                result(0),
                result(0),
                result(0),
            ]
            executor._mark = Mock()
            env = SimpleNamespace(
                id="env-1",
                config_dir=str(config_dir),
                authorized_account="owner@example.com",
                google_account_hint="owner@example.com",
            )
            task = SimpleNamespace(id="task-1")

            with patch("app.services.preset_task_service.time.sleep") as sleep:
                created_path = executor._create_key(
                    env,
                    task,
                    "project-1",
                    "service-account@example.iam.gserviceaccount.com",
                )

            self.assertEqual(created_path, key_path)
            self.assertEqual(executor.runner.run.call_count, 5)
            role_call = executor.runner.run.call_args_list[0]
            self.assertEqual(
                role_call.args[1],
                [
                    "projects", "add-iam-policy-binding", "project-1",
                    "--member", "serviceAccount:service-account@example.iam.gserviceaccount.com",
                    "--role", "roles/aiplatform.user", "--condition=None",
                ],
            )
            self.assertEqual(role_call.kwargs["command_type"], "GRANT_AGENT_PLATFORM_USER_ROLE")
            policy_call = executor.runner.run.call_args_list[2]
            self.assertEqual(
                policy_call.args[1],
                [
                    "resource-manager",
                    "org-policies",
                    "disable-enforce",
                    "constraints/iam.disableServiceAccountKeyCreation",
                    "--project",
                    "project-1",
                ],
            )
            managed_policy_call = executor.runner.run.call_args_list[3]
            self.assertEqual(managed_policy_call.args[1][0:2], ["org-policies", "set-policy"])
            self.assertTrue(managed_policy_call.args[1][2].endswith(".json"))
            self.assertEqual(
                managed_policy_call.kwargs["command_type"],
                "ALLOW_MANAGED_SERVICE_ACCOUNT_KEY_CREATION",
            )
            retry_call = executor.runner.run.call_args_list[4]
            self.assertEqual(retry_call.kwargs["command_type"], "CREATE_SERVICE_ACCOUNT_KEY_RETRY")
            sleep.assert_called_once_with(180)
            self.assertIn(
                "WAIT_KEY_POLICY_PROPAGATION",
                [call.args[2] for call in executor._mark.call_args_list],
            )

    def test_key_creation_enables_org_policy_api_then_retries_policy_update(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_root = Path(temp_dir) / "env"
            config_dir = env_root / "gcloud"
            key_dir = env_root / "keys"
            key_dir.mkdir(parents=True)
            key_path = key_dir / "task-1.json"
            key_path.write_text("{}", encoding="utf-8")

            executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
            executor.db = Mock()
            executor.settings = SimpleNamespace(
                service_account_key_policy_wait_seconds=0,
                org_policy_admin_role_wait_seconds=0,
                org_policy_api_propagation_wait_seconds=0,
                agent_platform_iam_propagation_wait_seconds=0,
            )
            executor.runner = Mock()
            executor.runner.run.side_effect = [
                result(0),
                result(1, "constraints/iam.disableServiceAccountKeyCreation is enforced"),
                result(1, "SERVICE_DISABLED: orgpolicy.googleapis.com has not been used in project"),
                result(0),
                result(0),
                result(0),
                result(0),
            ]
            executor._mark = Mock()
            env = SimpleNamespace(
                id="env-1",
                config_dir=str(config_dir),
                authorized_account="owner@example.com",
                google_account_hint="owner@example.com",
            )
            task = SimpleNamespace(id="task-1")

            created_path = executor._create_key(
                env,
                task,
                "project-1",
                "service-account@example.iam.gserviceaccount.com",
            )

            self.assertEqual(created_path, key_path)
            enable_call = executor.runner.run.call_args_list[3]
            self.assertEqual(
                enable_call.args[1],
                ["services", "enable", "orgpolicy.googleapis.com", "--project", "project-1"],
            )
            self.assertEqual(enable_call.kwargs["command_type"], "ENABLE_ORG_POLICY_API")
            self.assertIn(
                "ENABLE_ORG_POLICY_API",
                [call.args[2] for call in executor._mark.call_args_list],
            )
