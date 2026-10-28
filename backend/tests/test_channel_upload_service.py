import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.db.session import Base
from app.models.channel_upload import ChannelUpload, ChannelUsageSnapshot
from app.models.user import User, UserRole, UserStatus
from app.schemas.channel_upload import ChannelUploadInput, CreateChannelUpload
from app.services.channel_push_service import NewApiChannelConfig
from app.services.channel_upload_service import ChannelUploadService, recover_channel_uploads, validate_credential


class ChannelUploadServiceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="alice", password_hash="unused", role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.other = User(username="bob", password_hash="unused", role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.db.add_all([self.user, self.other]); self.db.commit()
        self.env = patch.dict(os.environ, {"APP_SECRET_KEY": "upload-tests-stable-secret"})
        self.env.start()
        self.service = ChannelUploadService(self.db, self.user.id)
        self.config = NewApiChannelConfig(base_url="https://new-api.example", token="admin-secret")
        self.service.config = Mock(return_value=self.config)
        self.service.remote._find_channel = Mock(return_value=None)
        self.service.remote._post_new_api_channel = Mock(return_value={"success": True, "data": {"id": 42}})
        self.service.remote._test_new_api_channel = Mock()
        self.service.remote._enable_new_api_channel = Mock()
        self.service.remote._disable_new_api_channel = Mock()

    def tearDown(self):
        self.env.stop(); self.db.close(); self.engine.dispose()

    def req(self, credential="test-google-api-key-123456", **kwargs):
        return CreateChannelUpload(kind="gemini_key", credential=credential, requestId=str(uuid.uuid4()), **kwargs)

    def legacy(self):
        row = self.service.create(self.req())
        row.base_url = self.config.base_url
        row.config_source = "personal"
        row.status = "PENDING"
        row.payload_json = json.dumps(self.service.payload(row.kind, row.name))
        self.db.commit()
        return row

    def json_input(self, **changes):
        data = {"type": "service_account", "project_id": "project-a", "client_email": "sa@project-a.iam.gserviceaccount.com",
                "private_key_id": "key-1", "private_key": self.pem}
        data.update(changes)
        return ChannelUploadInput(kind="vertex_json", credential=json.dumps(data))

    def test_json_normalization_and_private_key_validation(self):
        value, identity = validate_credential(self.json_input())
        self.assertIn("project-a", identity)
        self.assertEqual(json.loads(value)["token_uri"], "https://oauth2.googleapis.com/token")
        for req in (self.json_input(private_key="fake"), self.json_input(type="authorized_user"),
                    self.json_input(token_uri="http://127.0.0.1/token")):
            with self.assertRaises(ValueError): validate_credential(req)

    def test_json_storage_preserves_original_optional_fields_for_admin(self):
        from app.services.channel_vault_service import ChannelVaultService
        original = self.json_input(client_id="original-client-id", client_x509_cert_url="https://www.googleapis.com/cert")
        row = self.service.create(CreateChannelUpload(kind="vertex_json", credential=original.credential, requestId=str(uuid.uuid4())))
        _, plaintext = ChannelVaultService(self.db, self.user).reveal(row.id)
        data = json.loads(plaintext)
        self.assertEqual(data["client_id"], "original-client-id")
        self.assertEqual(data["client_x509_cert_url"], "https://www.googleapis.com/cert")
        self.assertEqual(data["private_key"], self.pem)

    def test_api_key_preview_is_masked_and_rejects_multiple_keys(self):
        _, identity = validate_credential(self.req())
        self.assertEqual(identity, "API Key ····3456")
        with self.assertRaises(ValueError): validate_credential(self.req("key-one\nkey-two-123456"))

    def test_fingerprint_is_stable_across_json_formatting(self):
        value, _ = validate_credential(self.json_input())
        value2, _ = validate_credential(self.json_input(client_id="ignored-optional-field"))
        self.assertEqual(self.service.fingerprint("vertex_json", value), self.service.fingerprint("vertex_json", value2))

    def test_idempotency_and_changed_credential(self):
        req = self.req()
        row = self.service.create(req)
        self.assertEqual(self.service.create(req).id, row.id)
        changed = req.model_copy(update={"credential": self.req("another-key-123456789").credential})
        with self.assertRaises(ValueError): self.service.create(changed)
        self.assertEqual(len(list(self.db.scalars(select(ChannelUpload)))), 1)

    def test_duplicate_credential_rejected_even_for_different_user(self):
        self.service.create(self.req())
        self.service.user_id = self.other.id
        with self.assertRaisesRegex(ValueError, "已上传"): self.service.create(self.req())

    def test_credentials_encrypted_not_present_in_payload_or_public_response(self):
        req = self.req()
        row = self.service.create(req)
        secret = req.credential.get_secret_value()
        self.assertNotIn(secret, row.credential_encrypted)
        self.assertNotIn(secret, row.payload_json)
        self.assertNotIn(secret, json.dumps(self.service.public(row), default=str))

    def test_other_user_cannot_read_record(self):
        row = self.legacy()
        self.service.user_id = self.other.id
        with self.assertRaises(KeyError): self.service.owned(row.id)
        self.assertEqual(self.service.summary()["channelCount"], 0)

    def test_success_retains_secret_and_records_remote_id_before_test(self):
        row = self.legacy()
        def check(*args):
            self.assertEqual(row.remote_channel_id, "42")
            self.assertIsNotNone(row.credential_encrypted)
        self.service.remote._test_new_api_channel.side_effect = check
        self.service.execute(row.id)
        self.assertEqual(row.status, "ACTIVE")
        self.assertIsNotNone(row.credential_encrypted)
        self.service.execute(row.id)
        self.service.remote._post_new_api_channel.assert_called_once()

    def test_test_failure_disables_and_retry_does_not_recreate(self):
        row = self.legacy()
        self.service.remote._test_new_api_channel.side_effect = RuntimeError("secret must never be exposed")
        self.service.execute(row.id)
        self.assertEqual(row.status, "TEST_FAILED")
        self.assertNotIn("secret must", row.message)
        self.service.remote._disable_new_api_channel.assert_called_once()
        self.service.remote._test_new_api_channel.side_effect = None
        self.service.execute(row.id)
        self.assertEqual(row.status, "ACTIVE")
        self.service.remote._post_new_api_channel.assert_called_once()

    def test_creation_timeout_is_not_blindly_retried(self):
        row = self.legacy()
        self.service.remote._post_new_api_channel.side_effect = TimeoutError()
        self.service.execute(row.id)
        self.assertEqual(row.status, "UNKNOWN")
        self.service.execute(row.id)
        self.assertEqual(row.status, "UNKNOWN")
        self.service.remote._post_new_api_channel.assert_called_once()

    def test_timeout_reconciles_existing_remote_channel(self):
        row = self.legacy()
        row.status = "UNKNOWN"; self.db.commit()
        self.service.remote._find_channel.return_value = {"id": 99}
        self.service.execute(row.id)
        self.assertEqual(row.status, "ACTIVE")
        self.assertEqual(row.remote_channel_id, "99")
        self.service.remote._post_new_api_channel.assert_not_called()

    def test_connection_failure_does_not_erase_uncertain_creation_state(self):
        row = self.legacy()
        row.status = "UNKNOWN"; self.db.commit()
        self.service.remote._find_channel.side_effect = TimeoutError()
        self.service.execute(row.id)
        self.assertEqual(row.status, "UNKNOWN")
        self.service.remote._find_channel.side_effect = None
        self.service.execute(row.id)
        self.service.remote._post_new_api_channel.assert_not_called()

    def test_old_credential_is_retained_and_can_be_used(self):
        row = self.legacy()
        row.created_at = datetime.utcnow() - timedelta(days=2); self.db.commit()
        self.service.execute(row.id)
        self.assertEqual(row.status, "ACTIVE")
        self.assertIsNotNone(row.credential_encrypted)

    def test_usage_only_counts_owned_channels_including_disabled(self):
        row = self.legacy(); row.remote_channel_id = "42"; self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", return_value=([
            {"id": 42, "status": 2, "used_quota": 750000},
            {"id": 777, "status": 1, "used_quota": 999999999},
        ], False, [])):
            self.service.refresh()
        self.assertEqual(row.used_quota, Decimal("750000"))
        self.assertEqual(self.service.summary()["totals"], {"USD": "1.50"})
        self.assertEqual(self.service.summary()["activeCount"], 0)
        self.assertEqual(len(list(self.db.scalars(select(ChannelUsageSnapshot)))), 1)

    def test_sync_failure_keeps_last_good_value(self):
        row = self.legacy(); row.remote_channel_id = "42"
        row.used_quota = Decimal("500000"); self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", side_effect=RuntimeError("upstream")):
            self.service.refresh()
        self.assertEqual(row.used_quota, Decimal("500000"))
        self.assertIsNotNone(row.sync_error)
        self.assertEqual(self.service.summary()["totals"]["USD"], "1.00")

    def test_partial_page_does_not_mark_channel_deleted(self):
        row = self.legacy(); row.remote_channel_id = "42"; row.remote_status = "1"; self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", return_value=([], True, [])):
            self.service.refresh()
        self.assertEqual(row.remote_status, "1")
        self.assertIsNotNone(row.sync_error)

    def test_counter_reset_preserves_last_value_and_signals_error(self):
        row = self.legacy(); row.remote_channel_id = "42"; row.used_quota = Decimal("500000"); self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", return_value=([
            {"id": 42, "status": 1, "used_quota": 100}], False, [])):
            self.service.refresh()
        self.assertEqual(row.used_quota, Decimal("500000"))
        self.assertIn("重置", row.sync_error)

    def test_sync_cooldown(self):
        row = self.legacy(); row.remote_channel_id = "42"; row.last_attempt_at = datetime.utcnow(); self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels") as fetch:
            self.service.refresh()
        fetch.assert_not_called()

    def test_target_change_does_not_redirect_existing_channel(self):
        row = self.legacy()
        service = ChannelUploadService(self.db, self.user.id)
        service.remote._load_push_config = Mock(return_value=NewApiChannelConfig(base_url="https://another.example", token="other"))
        with self.assertRaises(ValueError): service.config(row)

    def test_default_secret_rejected(self):
        with patch.dict(os.environ, {"APP_SECRET_KEY": ""}):
            with self.assertRaises(ValueError): self.service.create(self.req())

    def test_restart_marks_uncertain_creation_for_reconciliation(self):
        row = self.legacy(); row.status = "CREATING"; self.db.commit()
        with patch("app.services.channel_upload_service.SessionLocal", sessionmaker(bind=self.engine)):
            recover_channel_uploads()
        self.db.refresh(row)
        self.assertEqual(row.status, "UNKNOWN")
        row.remote_channel_id = "42"; row.status = "TESTING"; self.db.commit()
        with patch("app.services.channel_upload_service.SessionLocal", sessionmaker(bind=self.engine)):
            recover_channel_uploads()
        self.db.refresh(row)
        self.assertEqual(row.status, "TEST_FAILED")

    def test_shared_backend_overrides_personal_destination(self):
        service = ChannelUploadService(self.db, self.user.id)
        with patch.object(service.settings, "new_api_upload_base_url", "https://shared.example/"), \
             patch.object(service.settings, "new_api_upload_token", "shared-token"):
            config = service.config()
        self.assertEqual(config.base_url, "https://shared.example")
        self.assertEqual(config.token, "shared-token")

    def test_shared_cycle_fetches_once_and_never_caches_secrets(self):
        row = self.legacy(); row.remote_channel_id = "42"; self.db.commit()
        cache = {}
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", return_value=([
            {"id": 42, "status": 1, "usedQuota": 500000, "key": "remote-secret"}], False, [])) as fetch:
            self.service.refresh(cache)
            row.last_attempt_at = None; self.db.commit()
            self.service.refresh(cache)
        fetch.assert_called_once()
        self.assertEqual(row.used_quota, Decimal("500000"))
        self.assertNotIn("remote-secret", str(cache))
