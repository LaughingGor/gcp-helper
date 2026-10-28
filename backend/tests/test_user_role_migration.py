from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.db.init_db import _migrate_removed_user_roles
from app.db.session import Base
from app.models.gcp_environment import GcpEnvironment
from app.models.user import User, UserRole, UserStatus
from app.models.user_auth_session import UserAuthSession
from app.schemas.auth import CreateUserRequest, UpdateUserRequest
from app.services.auth_service import AuthService, ROLE_PERMISSIONS


class UserRoleMigrationTest(TestCase):
    def test_only_supported_roles_are_accepted(self) -> None:
        self.assertEqual({role.value for role in UserRole}, {"ADMIN", "OPERATOR", "CHANNEL_USER"})
        self.assertEqual(set(ROLE_PERMISSIONS), set(UserRole))
        for schema, payload in (
            (CreateUserRequest, {"username": "legacy", "password": "secure-password"}),
            (UpdateUserRequest, {}),
        ):
            with self.subTest(schema=schema.__name__), self.assertRaises(ValidationError):
                schema(**payload, role="VIEWER")

    def test_migration_disables_legacy_accounts_preserves_ownership_and_revokes_sessions(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            legacy = User(id="legacy", username="legacy", password_hash="unused", role=UserRole.OPERATOR)
            disabled = User(id="disabled", username="disabled", password_hash="unused",
                            role=UserRole.OPERATOR, status=UserStatus.DISABLED)
            supported = [User(id=role.value, username=role.value, password_hash="unused", role=role)
                         for role in UserRole]
            db.add_all([legacy, disabled, *supported])
            db.flush()
            db.add(GcpEnvironment(id="env", env_name="legacy-env", google_account_hint="legacy@example.com",
                                  config_name="legacy-config", config_dir="/tmp/legacy-config", owner_user_id=legacy.id))
            for user in [legacy, disabled, *supported]:
                db.add(UserAuthSession(id=user.id, user_id=user.id,
                                       token_hash=AuthService.hash_token(user.id), csrf_token_hash="unused",
                                       expires_at=datetime.utcnow() + timedelta(hours=1)))
            db.commit()
            db.execute(text("UPDATE users SET role = 'VIEWER' WHERE id IN ('legacy', 'disabled')"))
            db.commit()

        with patch("app.db.init_db.engine", engine):
            _migrate_removed_user_roles()
            _migrate_removed_user_roles()

        with Session(engine) as db:
            for identifier in ("legacy", "disabled"):
                user = db.get(User, identifier)
                self.assertEqual(user.role, UserRole.OPERATOR)
                self.assertEqual(user.status, UserStatus.DISABLED)
                self.assertIsNotNone(db.get(UserAuthSession, identifier).revoked_at)
                self.assertIsNone(AuthService(db).get_session(identifier))
            self.assertEqual(db.get(GcpEnvironment, "env").owner_user_id, "legacy")
            for role in UserRole:
                user = db.get(User, role.value)
                self.assertEqual(user.role, role)
                self.assertEqual(user.status, UserStatus.ACTIVE)
                self.assertIsNotNone(AuthService(db).get_session(role.value))
            db.get(User, "legacy").status = UserStatus.ACTIVE
            db.commit()

        with patch("app.db.init_db.engine", engine):
            _migrate_removed_user_roles()
        with Session(engine) as db:
            self.assertEqual(db.get(User, "legacy").status, UserStatus.ACTIVE)
            self.assertIsNone(AuthService(db).get_session("legacy"))
