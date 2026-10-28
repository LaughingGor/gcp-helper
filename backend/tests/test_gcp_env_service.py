from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 - register all relationship targets
from app.db.session import Base
from app.models.gcp_environment import GcpEnvironmentStatus
from app.models.gcp_login_session import LoginSessionStatus
from app.models.user import User, UserRole, UserStatus
from app.schemas.gcp_env import StartLoginRequest
from app.services.gcp_env_service import DuplicateGoogleAccountError, GcpEnvService


class GcpEnvServiceTest(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.service = GcpEnvService(self.db)
        self.service.config_service = Mock()
        self.service.config_service.make_config_name.return_value = "gcp-env-test"
        self.service.config_service.make_config_dir.return_value = "/tmp/gcp-helper-test/env/gcloud"
        self.service.login_runner = Mock()

    def tearDown(self) -> None:
        self.db.close()

    def test_start_login_only_creates_pending_session(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )

        env, session = self.service.start_login(request)

        self.assertEqual(env.status, GcpEnvironmentStatus.CREATED)
        self.assertEqual(session.status, LoginSessionStatus.PENDING)
        self.assertEqual(session.env_id, env.id)
        self.service.login_runner.start_login.assert_not_called()

    def test_environment_name_uses_date_and_daily_sequence(self) -> None:
        self.service.config_service.make_config_name.side_effect = ["gcp-auto-1", "gcp-auto-2"]
        owner = User(
            username="operator-one",
            password_hash="not-used",
            role=UserRole.OPERATOR,
            status=UserStatus.ACTIVE,
        )
        self.db.add(owner)
        self.db.commit()
        first_request = StartLoginRequest.model_validate(
            {"googleAccount": "first@example.com", "proxy": {"enabled": False}}
        )
        second_request = StartLoginRequest.model_validate(
            {"googleAccount": "second@example.com", "proxy": {"enabled": False}}
        )

        first_env, _ = self.service.start_login(first_request, owner=owner)
        second_env, _ = self.service.start_login(second_request, owner=owner)

        self.assertRegex(first_env.env_name, r"^operator-one-\d{4}-\d{2}-\d{2}-001$")
        prefix = first_env.env_name.rsplit("-", 1)[0]
        self.assertEqual(second_env.env_name, f"{prefix}-002")

    def test_same_owner_cannot_create_duplicate_google_account(self) -> None:
        owner = User(
            username="operator-one",
            password_hash="not-used",
            role=UserRole.OPERATOR,
            status=UserStatus.ACTIVE,
        )
        self.db.add(owner)
        self.db.commit()
        first_request = StartLoginRequest.model_validate(
            {"googleAccount": "User@Example.com", "proxy": {"enabled": False}}
        )
        self.service.start_login(first_request, owner=owner)

        duplicate_request = StartLoginRequest.model_validate(
            {"googleAccount": "  user@example.COM  ", "proxy": {"enabled": False}}
        )
        with self.assertRaises(DuplicateGoogleAccountError) as context:
            self.service.start_login(duplicate_request, owner=owner)

        self.assertIn("已存在于环境", str(context.exception))
        self.assertEqual(self.db.query(models.GcpEnvironment).count(), 1)

    def test_different_owners_can_use_the_same_google_account(self) -> None:
        self.service.config_service.make_config_name.side_effect = ["gcp-owner-1", "gcp-owner-2"]
        first_owner = User(
            username="operator-one",
            password_hash="not-used",
            role=UserRole.OPERATOR,
            status=UserStatus.ACTIVE,
        )
        second_owner = User(
            username="operator-two",
            password_hash="not-used",
            role=UserRole.OPERATOR,
            status=UserStatus.ACTIVE,
        )
        self.db.add_all([first_owner, second_owner])
        self.db.commit()
        request = StartLoginRequest.model_validate(
            {"googleAccount": "user@example.com", "proxy": {"enabled": False}}
        )

        first_env, _ = self.service.start_login(request, owner=first_owner)
        second_env, _ = self.service.start_login(request, owner=second_owner)

        self.assertNotEqual(first_env.id, second_env.id)

    def test_begin_submit_code_updates_only_selected_session(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )
        env, session = self.service.start_login(request)
        session.status = LoginSessionStatus.WAITING_CODE
        env.status = GcpEnvironmentStatus.WAITING_USER_CODE
        self.db.commit()
        self.service.login_runner.has_process.return_value = True

        updated_env, updated_session = self.service.begin_submit_code(session.id)

        self.assertEqual(updated_session.status, LoginSessionStatus.CODE_SUBMITTED)
        self.assertEqual(updated_env.status, GcpEnvironmentStatus.AUTHORIZING)
        self.service.login_runner.has_process.assert_called_once_with(session.id)

    def test_list_login_sessions_can_filter_active_sessions(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )
        _, active_session = self.service.start_login(request)

        active_sessions = self.service.list_login_sessions(active_only=True)

        self.assertEqual([session.id for session in active_sessions], [active_session.id])

    def test_failed_session_can_create_independent_retry_session(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )
        env, failed_session = self.service.start_login(request)
        failed_session.status = LoginSessionStatus.FAILED
        env.status = GcpEnvironmentStatus.FAILED
        self.db.commit()

        retry_env, retry_session = self.service.retry_login(failed_session.id)

        self.assertNotEqual(retry_session.id, failed_session.id)
        self.assertEqual(retry_session.status, LoginSessionStatus.PENDING)
        self.assertEqual(retry_env.status, GcpEnvironmentStatus.CREATED)
        self.service.login_runner.cancel.assert_called_once_with(failed_session.id)

        with self.assertRaisesRegex(RuntimeError, "最新授权记录"):
            self.service.retry_login(failed_session.id)

    def test_timed_out_session_can_create_independent_retry_session(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )
        env, timed_out_session = self.service.start_login(request)
        timed_out_session.status = LoginSessionStatus.TIMEOUT
        env.status = GcpEnvironmentStatus.EXPIRED
        self.db.commit()

        retry_env, retry_session = self.service.retry_login(timed_out_session.id)

        self.assertEqual(retry_env.id, env.id)
        self.assertNotEqual(retry_session.id, timed_out_session.id)
        self.assertEqual(retry_session.status, LoginSessionStatus.PENDING)
        self.assertEqual(retry_env.status, GcpEnvironmentStatus.CREATED)
        self.service.login_runner.cancel.assert_called_once_with(timed_out_session.id)

    def test_expired_active_session_is_marked_timeout_and_can_retry(self) -> None:
        request = StartLoginRequest.model_validate(
            {
                "envName": "env-test",
                "googleAccount": "user@example.com",
                "proxy": {"enabled": False},
            }
        )
        env, session = self.service.start_login(request)
        session.status = LoginSessionStatus.WAITING_CODE
        session.expires_at = datetime.utcnow() - timedelta(seconds=1)
        env.status = GcpEnvironmentStatus.WAITING_USER_CODE
        self.db.commit()

        expired = self.service.get_login_session(session.id)

        self.assertEqual(expired.status, LoginSessionStatus.TIMEOUT)
        self.assertEqual(expired.error_code, "LOGIN_TIMEOUT")
        retry_env, retry_session = self.service.retry_login(session.id)
        self.assertEqual(retry_env.status, GcpEnvironmentStatus.CREATED)
        self.assertEqual(retry_session.status, LoginSessionStatus.PENDING)

    def test_auto_bootstrap_selects_the_only_open_billing_account(self) -> None:
        env = Mock(id="env-1", billing_account_id=None)
        only_account = Mock(billing_account_id="AAAAAA-111111-222222")
        with patch("app.services.billing_account_service.BillingAccountService") as billing_service_class:
            billing_service = billing_service_class.return_value
            billing_service.list_available.return_value = [only_account]

            selected = self.service.ensure_billing_for_auto_bootstrap(env)

        self.assertTrue(selected)
        billing_service.select.assert_called_once_with(
            env,
            "AAAAAA-111111-222222",
            actor="SYSTEM_AUTO_BILLING_SELECTION",
        )

    def test_auto_bootstrap_requires_manual_selection_for_multiple_billing_accounts(self) -> None:
        env = Mock(id="env-1", billing_account_id=None)
        with patch("app.services.billing_account_service.BillingAccountService") as billing_service_class:
            billing_service = billing_service_class.return_value
            billing_service.list_available.return_value = [
                Mock(billing_account_id="AAAAAA-111111-222222"),
                Mock(billing_account_id="BBBBBB-222222-333333"),
            ]

            selected = self.service.ensure_billing_for_auto_bootstrap(env)

        self.assertFalse(selected)
        billing_service.select.assert_not_called()
