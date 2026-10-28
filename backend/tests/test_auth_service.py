from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 - register models
from app.db.session import Base
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.user import UserRole
from app.services.auth_service import AuthService, PasswordHasher


class AuthServiceTest(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.service = AuthService(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_only_admin_can_bind_enabled_targets_and_binding_is_audited(self):
        from app.models.channel_upload import NewApiTarget
        from app.models.audit_log import AuditLog
        from sqlalchemy import select
        admin = self.service.create_user(username='admin', password='a-secure-password', role=UserRole.ADMIN)
        operator = self.service.create_user(username='operator', password='a-secure-password', role=UserRole.OPERATOR)
        target = NewApiTarget(name='test', base_url='https://bound.example', token_encrypted='ciphertext', enabled=True)
        self.db.add(target)
        self.db.commit()
        with self.assertRaises(ValueError):
            self.service.bind_user_target(operator, target.id, actor=operator)
        self.assertIsNone(operator.bound_target_id)
        self.service.bind_user_target(operator, target.id, actor=admin)
        audit = self.db.scalar(select(AuditLog).where(AuditLog.action == 'USER_TARGET_BIND'))
        self.assertEqual(audit.actor, admin.username)
        self.assertEqual(audit.target_id, operator.id)
        self.assertNotIn('ciphertext', audit.detail_json)
        self.assertEqual(operator.bound_target_id, target.id)
        self.service.bind_user_target(operator, None, actor=admin)
        self.assertIsNone(operator.bound_target_id)

    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = PasswordHasher.hash("a-secure-password")
        second = PasswordHasher.hash("a-secure-password")

        self.assertNotEqual(first, second)
        self.assertTrue(PasswordHasher.verify("a-secure-password", first))
        self.assertFalse(PasswordHasher.verify("wrong-password", first))

    def test_session_can_be_created_resolved_and_revoked(self) -> None:
        user = self.service.create_user(
            username="admin",
            password="a-secure-password",
            role=UserRole.ADMIN,
        )
        self.assertEqual(self.service.authenticate("ADMIN", "a-secure-password").id, user.id)

        created = self.service.create_session(user, ip_address="127.0.0.1", user_agent="test")
        resolved = self.service.get_session(created.raw_token)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.user_id, user.id)

        self.service.revoke_session(resolved)
        self.assertIsNone(self.service.get_session(created.raw_token))

    def test_operator_only_lists_owned_environments(self) -> None:
        self.service.create_user(
            username="admin",
            password="a-secure-password",
            role=UserRole.ADMIN,
        )
        first_user = self.service.create_user(
            username="operator-one",
            password="a-secure-password",
            role=UserRole.OPERATOR,
        )
        second_user = self.service.create_user(
            username="operator-two",
            password="a-secure-password",
            role=UserRole.OPERATOR,
        )
        self.db.add_all(
            [
                GcpEnvironment(
                    env_name="first",
                    google_account_hint="first@example.com",
                    config_name="first-config",
                    config_dir="/tmp/first",
                    status=GcpEnvironmentStatus.CREATED,
                    owner_user_id=first_user.id,
                ),
                GcpEnvironment(
                    env_name="second",
                    google_account_hint="second@example.com",
                    config_name="second-config",
                    config_dir="/tmp/second",
                    status=GcpEnvironmentStatus.CREATED,
                    owner_user_id=second_user.id,
                ),
            ]
        )
        self.db.commit()

        from app.services.gcp_env_service import GcpEnvService

        environments = GcpEnvService(self.db).list_envs(user=first_user)
        self.assertEqual([environment.env_name for environment in environments], ["first"])

        matched = GcpEnvService(self.db).list_envs(user=first_user, google_account="FIRST@EXAMPLE")
        self.assertEqual([environment.env_name for environment in matched], ["first"])

    def test_channel_user_can_be_created_and_has_only_upload_permissions(self) -> None:
        self.service.create_user(username="admin", password="a-secure-password", role=UserRole.ADMIN)
        user = self.service.create_user(username="uploader", password="a-secure-password", role=UserRole.CHANNEL_USER)
        self.db.expire_all()
        authenticated = self.service.authenticate("uploader", "a-secure-password")
        self.assertEqual(authenticated.role, UserRole.CHANNEL_USER)
        self.assertEqual(set(self.service.permissions_for(user)), {"channel:upload", "channel:usage"})
        for permission in ("env:read", "env:create", "task:run", "secret:read", "gcloud:execute",
                           "channel:push", "channel:stats", "channel:config", "settings:write", "user:manage"):
            self.assertFalse(self.service.has_permission(user, permission), permission)

    def test_operator_can_be_changed_to_channel_user(self) -> None:
        admin = self.service.create_user(username="admin", password="a-secure-password", role=UserRole.ADMIN)
        user = self.service.create_user(username="operator", password="a-secure-password", role=UserRole.OPERATOR)
        self.service.update_user(user, role=UserRole.CHANNEL_USER, actor=admin)
        self.assertEqual(self.service.permissions_for(user), ["channel:upload", "channel:usage"])

    def test_existing_roles_retain_uploaded_channel_access(self) -> None:
        from app.models.user import User
        for role in (UserRole.ADMIN, UserRole.OPERATOR):
            user = User(role=role)
            self.assertTrue(self.service.has_permission(user, "channel:upload"))
            self.assertTrue(self.service.has_permission(user, "channel:usage"))
            self.assertTrue(self.service.has_permission(user, "env:create"))
