from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from app.runners.gcloud_runner import CommandResult
from app.services.preset_task_service import PresetTaskExecutor


def result(exit_code: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(args=[], exit_code=exit_code, stdout=stdout, stderr=stderr)


class ComputeNetworkTest(TestCase):
    def test_creates_auto_subnet_network_and_waits_for_region_subnet(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.settings = SimpleNamespace(compute_vm_network_name="gcp-helper-network")
        executor.compute_vm_zone = "us-west2-a"
        executor.runner = Mock()
        executor.runner.run.side_effect = [
            result(1, stderr="not found"),
            result(),
            result(
                stdout=(
                    "https://compute.googleapis.com/compute/v1/projects/project-1/regions/"
                    "us-west2/subnetworks/gcp-helper-network\n"
                )
            ),
        ]

        network_name, subnet_self_link = executor._ensure_compute_network(
            SimpleNamespace(id="env-1"), "project-1"
        )

        self.assertEqual(network_name, "gcp-helper-network")
        self.assertEqual(
            subnet_self_link,
            "https://compute.googleapis.com/compute/v1/projects/project-1/regions/"
            "us-west2/subnetworks/gcp-helper-network",
        )
        self.assertEqual(
            executor.runner.run.call_args_list[1].args[1],
            [
                "compute",
                "networks",
                "create",
                "gcp-helper-network",
                "--subnet-mode=auto",
                "--bgp-routing-mode=regional",
                "--project",
                "project-1",
            ],
        )
        self.assertIn("us-west2", executor.runner.run.call_args_list[2].args[1])
        self.assertIn("--format=value(selfLink)", executor.runner.run.call_args_list[2].args[1])

    def test_reuses_existing_auto_subnet_network(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.settings = SimpleNamespace(compute_vm_network_name="gcp-helper-network")
        executor.compute_vm_zone = "asia-east1-b"
        executor.runner = Mock()
        executor.runner.run.side_effect = [
            result(stdout="True\n"),
            result(
                stdout=(
                    "https://compute.googleapis.com/compute/v1/projects/project-1/regions/"
                    "asia-east1/subnetworks/gcp-helper-network\n"
                )
            ),
        ]

        network_name, subnet_self_link = executor._ensure_compute_network(
            SimpleNamespace(id="env-1"), "project-1"
        )

        self.assertEqual(network_name, "gcp-helper-network")
        self.assertIn("/regions/asia-east1/subnetworks/", subnet_self_link)
        self.assertEqual(executor.runner.run.call_count, 2)
        self.assertIn("asia-east1", executor.runner.run.call_args_list[1].args[1])

    def test_rejects_same_name_custom_network_without_auto_subnets(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.settings = SimpleNamespace(compute_vm_network_name="gcp-helper-network")
        executor.compute_vm_zone = "us-west2-a"
        executor.runner = Mock()
        executor.runner.run.return_value = result(stdout="False\n")

        with self.assertRaisesRegex(RuntimeError, "不是自动子网模式"):
            executor._ensure_compute_network(SimpleNamespace(id="env-1"), "project-1")

    def test_vm_creation_explicitly_uses_managed_network_and_subnet(self) -> None:
        with TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "startup.sh"
            script_path.write_text("#!/bin/sh\n", encoding="utf-8")
            executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
            executor.settings = SimpleNamespace(
                socks5_startup_script_path=script_path,
                compute_vm_machine_type="e2-micro",
                compute_vm_image_family="debian-12",
                compute_vm_image_project="debian-cloud",
                compute_vm_boot_disk_size="10GB",
                compute_vm_boot_disk_type="pd-standard",
                compute_vm_network_tag="socks5-server",
            )
            executor.compute_vm_zone = "us-west2-a"
            executor.runner = Mock()
            executor.runner.run.side_effect = [result(), result(stdout="203.0.113.10\n")]
            executor._generate_socks5_credentials = Mock(return_value=("20001", "user", "password"))

            executor._create_compute_vm(
                SimpleNamespace(id="env-1"),
                SimpleNamespace(id="task-1"),
                "project-1",
                "gcp-helper-network",
                "https://compute.googleapis.com/compute/v1/projects/project-1/regions/"
                "us-west2/subnetworks/gcp-helper-network",
            )

            create_args = executor.runner.run.call_args_list[0].args[1]
            network_index = create_args.index("--network")
            self.assertEqual(create_args[network_index + 1], "gcp-helper-network")
            subnet_index = create_args.index("--subnet")
            self.assertEqual(
                create_args[subnet_index + 1],
                "https://compute.googleapis.com/compute/v1/projects/project-1/regions/"
                "us-west2/subnetworks/gcp-helper-network",
            )

    def test_zone_to_region(self) -> None:
        self.assertEqual(PresetTaskExecutor._zone_to_region("us-west2-a"), "us-west2")
        self.assertEqual(PresetTaskExecutor._zone_to_region("asia-east1-b"), "asia-east1")
