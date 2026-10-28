from unittest import TestCase

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.api.gcp_env_api import start_login
from app.db.session import Base
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.user import UserRole
from app.schemas.gcp_env import StartLoginRequest
from app.services.auth_service import AuthService


class GcpEnvApiTest(TestCase):
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

    def test_duplicate_account_returns_structured_conflict(self) -> None:
        environment = GcpEnvironment(
            env_name="admin-2026-09-11-001",
            google_account_hint="user@example.com",
            config_name="existing-config",
            config_dir="/tmp/existing-config",
            status=GcpEnvironmentStatus.AUTHORIZED,
            owner_user_id=self.user.id,
            created_by=self.user.username,
        )
        self.db.add(environment)
        self.db.commit()
        request = StartLoginRequest.model_validate(
            {"googleAccount": "USER@example.com", "proxy": {"enabled": False}}
        )

        with self.assertRaises(HTTPException) as context:
            start_login(request, BackgroundTasks(), self.db, self.user)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(context.exception.detail["code"], "GOOGLE_ACCOUNT_ALREADY_EXISTS")
        self.assertEqual(context.exception.detail["envId"], environment.id)
        self.assertIn(environment.env_name, context.exception.detail["message"])
