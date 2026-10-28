from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock

from app.models.gcp_environment import GcpEnvironmentStatus
from app.models.user import UserRole
from app.runners.gcloud_runner import CommandResult
from app.schemas.gcloud_command import ExecuteGcloudCommandRequest
from app.services.auth_service import AuthService
from app.services.gcloud_command_service import (
    GcloudCommandConfirmationRequired,
    GcloudCommandPolicyError,
    GcloudCommandService,
)


class GcloudCommandServiceTest(TestCase):
    def setUp(self) -> None:
        self.db = Mock()
        self.db.scalar.return_value = None
        self.db.scalars.return_value.first.return_value = None
        self.service = GcloudCommandService(self.db)
        self.env = Mock(
            id="env-1",
            status=GcpEnvironmentStatus.AUTHORIZED,
            config_name="gcp-env-1",
            config_dir="/tmp/gcp-env-1",
        )

    def test_only_admin_has_execute_permission(self) -> None:
        user = Mock(role=UserRole.ADMIN)
        self.assertTrue(AuthService.has_permission(user, "gcloud:execute"))
        user.role = UserRole.OPERATOR
        self.assertFalse(AuthService.has_permission(user, "gcloud:execute"))

    def test_parse_allows_quoted_arguments(self) -> None:
        self.assertEqual(
            self.service.parse_command('projects list --format="table(projectId,name)"'),
            ["projects", "list", "--format=table(projectId,name)"],
        )

    def test_parse_rejects_shell_and_environment_override(self) -> None:
        with self.assertRaises(GcloudCommandPolicyError):
            self.service.parse_command("projects list; compute instances list")
        with self.assertRaises(GcloudCommandPolicyError):
            self.service.parse_command("projects list --configuration=other")
        with self.assertRaises(GcloudCommandPolicyError):
            self.service.parse_command("gcloud projects list")

    def test_sensitive_command_is_rejected(self) -> None:
        with self.assertRaises(GcloudCommandPolicyError):
            self.service.execute(
                self.env,
                "admin",
                ExecuteGcloudCommandRequest(command="auth print-access-token", timeoutSeconds=60),
            )

    def test_high_risk_command_requires_confirmation(self) -> None:
        with self.assertRaises(GcloudCommandConfirmationRequired):
            self.service.execute(
                self.env,
                "admin",
                ExecuteGcloudCommandRequest(command="compute instances delete vm-1", timeoutSeconds=60),
            )

    def test_execute_uses_current_environment_and_returns_output(self) -> None:
        self.service.runner = Mock()
        self.service.runner.run.return_value = CommandResult(
            args=["gcloud", "--quiet", "--configuration", "gcp-env-1", "projects", "list"],
            exit_code=0,
            stdout="project-1\n",
            stderr="",
        )

        result = self.service.execute(
            self.env,
            "admin",
            ExecuteGcloudCommandRequest(command="projects list", timeoutSeconds=60),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.stdout, "project-1\n")
        self.assertEqual(result.command, "gcloud projects list")
        self.service.runner.run.assert_called_once_with(
            self.env,
            ["projects", "list"],
            timeout=60,
            command_type="MANUAL_GCLOUD",
            max_output_chars=self.service.settings.gcloud_console_max_output_chars,
            store_output_preview=False,
        )
        self.db.add.assert_called_once()
        self.db.commit.assert_called_once()

    def test_history_hides_internal_configuration_prefix(self) -> None:
        log = Mock(
            id="log-1",
            command_args_json='["gcloud", "--quiet", "--configuration", "gcp-env-1", "projects", "list"]',
            exit_code=0,
            status="SUCCESS",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
        self.db.scalars.return_value.all.return_value = [log]

        result = self.service.history("env-1", 20)

        self.assertEqual(result[0].command, "gcloud projects list")
