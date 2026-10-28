from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.api.user_settings_api import get_user_settings, update_user_settings
from app.db.session import Base
from app.models.user import UserRole
from app.schemas.user_settings import UpdateUserSettingsRequest
from app.services.auth_service import AuthService
from app.services.preset_task_service import PresetTaskExecutor


class UserSettingsTest(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = AuthService(self.db).create_user(
            username="admin",
            password="a-secure-password",
            role=UserRole.ADMIN,
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_zone_uses_default_until_user_saves_override(self) -> None:
        initial = get_user_settings(user=self.user)
        self.assertTrue(initial.using_default_zone)

        updated = update_user_settings(
            UpdateUserSettingsRequest.model_validate({"computeVmZone": "us-central1-b"}),
            user=self.user,
            db=self.db,
        )

        self.assertFalse(updated.using_default_zone)
        self.assertEqual(updated.compute_vm_zone, "us-central1-b")

    def test_task_executor_prefers_environment_owner_zone(self) -> None:
        env = SimpleNamespace(owner=SimpleNamespace(compute_vm_zone="asia-east1-a"))
        self.assertEqual(
            PresetTaskExecutor._resolve_compute_vm_zone(env, "us-west2-a"),
            "asia-east1-a",
        )

    def test_task_executor_falls_back_to_system_zone(self) -> None:
        env = SimpleNamespace(owner=SimpleNamespace(compute_vm_zone=None))
        self.assertEqual(
            PresetTaskExecutor._resolve_compute_vm_zone(env, "us-west2-a"),
            "us-west2-a",
        )

    def test_compute_api_propagation_waits_configured_seconds(self) -> None:
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.settings = SimpleNamespace(compute_api_propagation_wait_seconds=10)

        with patch("app.services.preset_task_service.time.sleep") as sleep:
            executor._wait_for_compute_api_propagation(
                SimpleNamespace(id="env-1"),
                SimpleNamespace(id="task-1"),
            )

        sleep.assert_called_once_with(10)
