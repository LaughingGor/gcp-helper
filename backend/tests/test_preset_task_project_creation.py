from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from app.runners.gcloud_runner import CommandResult
from app.services.preset_task_service import PresetTaskExecutor


class PresetTaskProjectCreationTest(TestCase):
    def test_remote_managed_project_is_selected(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.runner = Mock()
        executor.runner.run.return_value = CommandResult(
            args=[],
            exit_code=0,
            stdout="customer-project\ngcph-2609081200-abcdef\ngcph-older\n",
            stderr="",
        )
        env = SimpleNamespace(id="env-1")

        project_id = executor._resolve_managed_project_from_remote_list(env)

        self.assertEqual(project_id, "gcph-2609081200-abcdef")
        self.assertEqual(
            executor.runner.run.call_args.args[1],
            [
                "projects",
                "list",
                "--filter=lifecycleState=ACTIVE",
                "--sort-by=~createTime",
                "--format=value(projectId)",
            ],
        )

    def test_missing_remote_managed_project_requires_creation(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.runner = Mock()
        executor.runner.run.return_value = CommandResult(
            args=[],
            exit_code=0,
            stdout="customer-project\nanother-project\n",
            stderr="",
        )

        project_id = executor._resolve_managed_project_from_remote_list(SimpleNamespace(id="env-1"))

        self.assertIsNone(project_id)

    def test_unmanaged_current_project_is_cleared_before_creation(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.db = Mock()
        executor._mark = Mock()
        executor.runner = Mock()
        executor.runner.run.side_effect = [
            CommandResult(args=[], exit_code=0, stdout="customer-project\n", stderr=""),
            CommandResult(args=[], exit_code=0, stdout="", stderr=""),
        ]
        env = SimpleNamespace(id="env-1", default_project_id="customer-project")
        task = SimpleNamespace(
            id="task-1",
            project_id="customer-project",
            service_account_email="service@example.com",
            key_file_path="key.json",
            key_file_name="key.json",
            vm_name="vm-1",
            vm_zone="us-west2-a",
            vm_external_ip="127.0.0.1",
            firewall_rule_name="rule-1",
            socks5_port="1080",
            socks5_url="socks5://example",
        )

        cleared = executor._clear_unmanaged_current_project(env, task)

        self.assertTrue(cleared)
        self.assertIsNone(env.default_project_id)
        self.assertIsNone(task.project_id)
        self.assertIsNone(task.vm_name)
        self.assertEqual(
            executor.runner.run.call_args_list[1].args[1],
            ["config", "unset", "project"],
        )
        executor.db.commit.assert_called_once_with()

    def test_managed_current_project_is_not_cleared(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.db = Mock()
        executor._mark = Mock()
        executor.runner = Mock()
        executor.runner.run.return_value = CommandResult(
            args=[],
            exit_code=0,
            stdout="gcph-2609081200-abcdef\n",
            stderr="",
        )
        env = SimpleNamespace(id="env-1", default_project_id="gcph-2609081200-abcdef")
        task = SimpleNamespace(id="task-1", project_id=None)

        cleared = executor._clear_unmanaged_current_project(env, task)

        self.assertFalse(cleared)
        self.assertEqual(executor.runner.run.call_count, 1)
        executor._mark.assert_not_called()
        executor.db.commit.assert_not_called()

    def test_create_project_omits_organization_and_folder(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.runner = Mock()
        executor.runner.run.return_value = CommandResult(
            args=[],
            exit_code=0,
            stdout="",
            stderr="",
        )
        executor._generate_project_id = Mock(return_value="gcph-2609081200-abcdef")
        env = SimpleNamespace(id="env-1")

        project_id = executor._create_project(env)

        self.assertEqual(project_id, "gcph-2609081200-abcdef")
        args = executor.runner.run.call_args.args[1]
        self.assertEqual(args[:3], ["projects", "create", project_id])
        self.assertNotIn("--organization", args)
        self.assertNotIn("--folder", args)
        self.assertEqual(executor.runner.run.call_args.kwargs["command_type"], "CREATE_PROJECT")

    def test_replacement_project_creates_a_new_project(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.db = Mock()
        executor._mark = Mock()
        executor._clear_project_related_data = Mock()
        executor._create_project = Mock(return_value="new-project")
        env = SimpleNamespace(id="env-1", default_project_id="old-project")
        task = SimpleNamespace(id="task-1", project_id="old-project")

        project_id = executor._recreate_project_after_api_enable_failure(
            env,
            task,
            "old-project",
            RuntimeError("enable failed"),
        )

        self.assertEqual(project_id, "new-project")
        self.assertEqual(task.project_id, "new-project")
        self.assertEqual(env.default_project_id, "new-project")
        executor._create_project.assert_called_once_with(env)
