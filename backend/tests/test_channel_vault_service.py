from target_template_fixture import install_default_template
import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.audit_log import AuditLog
from app.models.channel_upload import ChannelUpload, NewApiTarget
from app.models.user import User, UserRole, UserStatus
from app.schemas.channel_upload import CreateChannelUpload, NewApiTargetInput
from app.services.channel_upload_service import ChannelUploadService, recover_channel_uploads
from app.services.channel_vault_service import ChannelVaultService


class ChannelVaultServiceTest(TestCase):
    def setUp(self):
        install_default_template(self)
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.admin = User(username="admin", password_hash="unused", role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        self.owner = User(username="uploader", password_hash="unused", role=UserRole.CHANNEL_USER, status=UserStatus.ACTIVE)
        self.db.add_all([self.admin, self.owner]); self.db.commit()
        self.env = patch.dict(os.environ, {"APP_SECRET_KEY": "vault-tests-stable-key"}); self.env.start()
        self.service = ChannelUploadService(self.db, self.owner.id)
        self.vault = ChannelVaultService(self.db, self.admin)
        self.secret = "upload-google-key-persist-123456"
        self.req = CreateChannelUpload(kind="gemini_key", credential=self.secret, requestId=str(uuid.uuid4()))
        self.upload = self.service.create(self.req)

    def tearDown(self):
        self.env.stop(); self.db.close(); self.engine.dispose()

    def target(self, name="A", **changes):
        fields = {"baseUrl": f"https://{name.lower()}.example", "token": "target-management-token"}
        target = self.vault.save_target(NewApiTargetInput(**fields))
        # Existing databases may retain per-target settings and disabled targets.
        for key, value in changes.items():
            setattr(target, {"quotaPerUnit": "quota_per_unit"}.get(key, key), value)
        self.db.commit()
        return target

    def worker(self):
        service = ChannelUploadService(self.db, self.owner.id)
        service.remote._find_channel = Mock(return_value=None)
        service.remote._post_new_api_channel = Mock(return_value={"success": True, "data": {"id": 7}})
        service.remote._test_new_api_channel = Mock()
        service.remote._enable_new_api_channel = Mock()
        service.remote._disable_new_api_channel = Mock()
        return service

    def test_target_uses_default_template_and_system_accounting(self):
        from app.core.config import get_settings
        settings = get_settings()
        with patch.object(settings, "new_api_quota_per_unit", Decimal("1000000")), \
                patch.object(settings, "new_api_currency", "CNY"):
            target = self.target()
        self.assertEqual(target.name, target.base_url)
        self.assertEqual(target.models, "template-model")
        self.assertEqual(target.group, "template-group")
        self.assertEqual(target.vertex_region, "us-central1")
        self.assertEqual(target.vertex_template, "default")
        self.assertEqual(target.quota_per_unit, Decimal("1000000"))
        self.assertEqual(target.currency, "CNY")
        payload = self.service.payload("vertex_json", "generated-name", target)
        self.assertEqual(payload["channel"]["models"], "template-model")
        self.assertEqual(payload["channel"]["priority"], 17)
        self.assertEqual(json.loads(payload["channel"]["setting"]), {"force_format": True})

    def test_target_can_be_saved_with_key_from_settings(self):
        from pydantic import SecretStr
        from app.core.config import get_settings
        from app.services.secret_service import SecretService
        with patch.dict(os.environ), patch.object(get_settings(), "app_secret_key", SecretStr("dotenv-configured-key")):
            os.environ.pop("APP_SECRET_KEY", None)
            target = self.target()
            self.assertEqual(SecretService().decrypt_optional(target.token_encrypted), "target-management-token")

    def test_statistics_group_is_preserved_when_omitted_and_does_not_change_pushes(self):
        target = self.target()
        self.assertEqual(target.stats_group, 'template-group')
        self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url, statsGroup='reporting'), target.id)
        self.assertEqual(target.stats_group, 'reporting')
        self.assertEqual(target.group, 'template-group')
        self.assertEqual(self.service.payload('vertex_json', 'test', target)['channel']['group'], 'template-group')
        self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url), target.id)
        self.assertEqual(target.stats_group, 'reporting')

    def test_statistics_group_migration_preserves_scope_and_is_repeatable(self):
        from app.db.init_db import _ensure_target_stats_group
        old_engine = create_engine('sqlite:///:memory:')
        self.addCleanup(old_engine.dispose)
        with old_engine.begin() as conn:
            conn.execute(text('CREATE TABLE new_api_targets (id VARCHAR(36) PRIMARY KEY, "group" VARCHAR(128))'))
            conn.execute(text('INSERT INTO new_api_targets VALUES (\'old\', \'previous-group\')'))
        with patch('app.db.init_db.engine', old_engine):
            _ensure_target_stats_group()
            with old_engine.begin() as conn:
                self.assertEqual(conn.execute(text('SELECT stats_group FROM new_api_targets')).scalar(), 'previous-group')
                conn.execute(text("UPDATE new_api_targets SET stats_group = 'configured-group'"))
            _ensure_target_stats_group()
        with old_engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT stats_group FROM new_api_targets')).scalar(), 'configured-group')

    def test_invalid_token_cannot_create_target(self):
        self.token_request.return_value = {"success": False, "message": "target-management-token"}
        with self.assertRaisesRegex(ValueError, 'Access Token 验证失败'):
            self.target()
        self.db.commit()
        self.assertEqual(list(self.db.scalars(select(NewApiTarget))), [])
        self.assertEqual(list(self.db.scalars(select(AuditLog).where(AuditLog.action == 'NEW_API_TARGET_CREATE'))), [])

    def test_invalid_replacement_token_preserves_original_configuration(self):
        target = self.target(enabled=False)
        identifier, encrypted = target.id, target.token_encrypted
        self.token_request.return_value = {"success": False}
        with self.assertRaisesRegex(ValueError, 'Access Token 验证失败'):
            self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url, token='invalid-new-token'), identifier)
        self.assertEqual(self.token_request.call_args.args[0].get_header('Authorization'), 'Bearer invalid-new-token')
        # Even a later commit must not persist partial changes from a failed save.
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(target.token_encrypted, encrypted)
        self.assertFalse(target.enabled)
        self.assertEqual(list(self.db.scalars(select(AuditLog).where(AuditLog.action == 'NEW_API_TARGET_UPDATE'))), [])

    def test_blank_token_on_edit_validates_original_token(self):
        target = self.target()
        encrypted = target.token_encrypted
        self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url), target.id)
        self.assertEqual(self.token_request.call_args.args[0].get_header('Authorization'), 'Bearer target-management-token')
        self.assertEqual(target.token_encrypted, encrypted)
        self.token_request.return_value = {"success": False}
        with self.assertRaisesRegex(ValueError, 'Access Token 验证失败'):
            self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url), target.id)

    def test_delete_target_is_audited_without_secrets_and_clears_all_defaults(self):
        target = self.target()
        identifier = target.id
        self.owner.default_target_id = identifier
        self.owner.bound_target_id = identifier
        self.admin.default_target_id = identifier
        self.db.commit()
        self.vault.delete_target(identifier)
        self.assertIsNone(self.db.get(NewApiTarget, identifier))
        self.assertIsNone(self.owner.default_target_id)
        self.assertIsNone(self.owner.bound_target_id)
        self.assertIsNone(self.admin.default_target_id)
        audit = self.db.scalar(select(AuditLog).where(AuditLog.action == 'NEW_API_TARGET_DELETE'))
        self.assertEqual(audit.actor, self.admin.username)
        self.assertEqual(audit.target_id, identifier)
        self.assertIsNone(audit.detail_json)
        self.assertIsNotNone(self.db.get(ChannelUpload, self.upload.id))

    def test_delete_target_preserves_referenced_deployments(self):
        target = self.target()
        push = self.vault.prepare_pushes(self.upload.id, [target.id])[0]
        push.remote_channel_id = 'remote-channel'
        self.owner.default_target_id = target.id
        self.db.commit()
        with self.assertRaisesRegex(ValueError, '关联'):
            self.vault.delete_target(target.id)
        self.assertEqual(push.remote_channel_id, 'remote-channel')
        self.assertEqual(self.owner.default_target_id, target.id)
        self.assertEqual(self.vault.reveal(self.upload.id)[1], self.secret)

    def test_delete_target_rejects_legacy_configuration(self):
        from app.models.user_channel_config import UserChannelConfig
        target = self.target()
        self.db.add(UserChannelConfig(user_id=self.owner.id, base_url=target.base_url + '/',
                                     username='', password_encrypted='', token_encrypted='legacy-ciphertext'))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, '旧版个人后台配置'):
            self.vault.delete_target(target.id)
        self.assertIsNotNone(self.db.get(NewApiTarget, target.id))

    def test_delete_target_rejects_history_without_deployment(self):
        from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
        from app.models.gcp_environment import GcpEnvironment
        target = self.target()
        env = GcpEnvironment(env_name='history-env', google_account_hint='test@example.com',
                             config_name='history', config_dir='/tmp/history', owner_user_id=self.owner.id)
        self.db.add(env)
        self.db.flush()
        self.db.add(ChannelPushRecord(env_id=env.id, target_id=target.id, channel_name='historical',
                                      status=ChannelPushStatus.SUCCESS, channel_id='7'))
        self.db.commit()
        with self.assertRaisesRegex(ValueError, '历史渠道记录'):
            self.vault.delete_target(target.id)
        self.assertIsNotNone(self.db.get(NewApiTarget, target.id))

    def test_edit_keeps_token_and_historical_usage_snapshot(self):
        target = self.target(quotaPerUnit=Decimal("1000000"), currency="CNY", enabled=False)
        target.enabled = True
        self.db.commit()
        push = self.vault.prepare_pushes(self.upload.id, [target.id])[0]
        push.remote_channel_id = "historical-channel"
        self.db.commit()
        encrypted = target.token_encrypted
        self.vault.save_target(NewApiTargetInput(baseUrl=target.base_url), target.id)
        self.assertEqual(target.token_encrypted, encrypted)
        self.assertEqual(push.remote_channel_id, "historical-channel")
        self.assertEqual(push.quota_per_unit, Decimal("1000000"))
        self.assertEqual(push.currency, "CNY")

    def test_missing_default_template_rejects_target(self):
        from app.core.config import get_settings
        with patch.object(get_settings(), "new_api_channel_default_template", "missing"):
            with self.assertRaisesRegex(ValueError, "默认 Vertex 模板不可用"):
                self.target()
        self.db.rollback()
        self.assertEqual(list(self.db.scalars(select(NewApiTarget))), [])

    def test_upload_requires_no_destination_and_never_pushes_automatically(self):
        self.assertEqual(self.upload.status, "STORED")
        self.assertEqual(self.upload.base_url, "vault")
        self.assertIsNone(self.upload.remote_channel_id)
        worker = self.worker(); worker.execute(self.upload.id)
        worker.remote._post_new_api_channel.assert_not_called()

    def test_admin_reveal_and_download_are_audited_without_plaintext_in_audit(self):
        _, value = self.vault.reveal(self.upload.id)
        self.assertEqual(value, self.secret)
        self.vault.reveal(self.upload.id, download=True)
        logs = list(self.db.scalars(select(AuditLog)))
        self.assertIn("CHANNEL_CREDENTIAL_VIEW", [log.action for log in logs])
        self.assertIn("CHANNEL_CREDENTIAL_DOWNLOAD", [log.action for log in logs])
        self.assertNotIn(self.secret, str([(log.action, log.detail_json) for log in logs]))

    def test_old_secret_is_kept_after_restart(self):
        self.upload.created_at = datetime.utcnow() - timedelta(days=365); self.db.commit()
        with patch("app.services.channel_upload_service.SessionLocal", sessionmaker(bind=self.engine)):
            recover_channel_uploads()
        self.db.refresh(self.upload)
        self.assertEqual(self.vault.reveal(self.upload.id)[1], self.secret)

    def test_erased_legacy_credential_cannot_be_revealed_or_pushed(self):
        self.upload.credential_encrypted = None; self.db.commit()
        target = self.target()
        with self.assertRaisesRegex(ValueError, "已被清除"): self.vault.reveal(self.upload.id)
        with self.assertRaisesRegex(ValueError, "已清除"): self.vault.prepare_pushes(self.upload.id, [target.id])

    def test_reupload_restores_erased_legacy_credential_without_duplicate(self):
        self.upload.credential_encrypted = None; self.upload.base_url = "https://old.example"
        self.upload.config_source = "personal"; self.upload.remote_channel_id = "legacy-id"; self.db.commit()
        req = self.req.model_copy(update={"request_id": uuid.uuid4()})
        restored = self.service.create(req)
        self.assertEqual(restored.id, self.upload.id)
        self.assertEqual(restored.remote_channel_id, "legacy-id")
        self.assertEqual(self.vault.reveal(restored.id)[1], self.secret)
        self.assertEqual(len(list(self.db.scalars(select(ChannelUpload)))), 1)

    def test_two_targets_have_independent_channels_and_one_retained_credential(self):
        a, b = self.target(), self.target("B", quotaPerUnit="1000000", currency="CNY")
        pushes = self.vault.prepare_pushes(self.upload.id, [a.id, b.id])
        self.assertEqual(len(pushes), 2)
        worker = self.worker()
        for push in pushes:
            worker.execute(push.id)
            self.assertEqual(push.remote_channel_id, "7")  # same ID is valid on different servers
            self.assertEqual(push.status, "ACTIVE")
            self.assertIsNone(push.credential_encrypted)
        self.assertEqual(worker.remote._post_new_api_channel.call_count, 2)
        self.assertEqual(self.vault.reveal(self.upload.id)[1], self.secret)
        self.assertEqual(pushes[1].quota_per_unit, Decimal("1000000"))
        self.assertEqual(self.service.summary()["channelCount"], 1)
        self.assertEqual(self.service.summary()["deploymentCount"], 2)

    def test_repeated_target_submission_reuses_push_record(self):
        target = self.target()
        first = self.vault.prepare_pushes(self.upload.id, [target.id, target.id])
        second = self.vault.prepare_pushes(self.upload.id, [target.id])
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].id, second[0].id)

    def test_timeout_on_one_target_does_not_block_another_or_erase_key(self):
        a, b = self.target(), self.target("B")
        first, second = self.vault.prepare_pushes(self.upload.id, [a.id, b.id])
        worker = self.worker(); worker.remote._post_new_api_channel.side_effect = TimeoutError()
        worker.execute(first.id)
        self.assertEqual(first.status, "UNKNOWN")
        worker.remote._post_new_api_channel.side_effect = None
        worker.execute(first.id)
        self.assertEqual(worker.remote._post_new_api_channel.call_count, 1)
        worker.execute(second.id)
        self.assertEqual(second.status, "ACTIVE")
        self.assertEqual(self.vault.reveal(self.upload.id)[1], self.secret)

    def test_failed_test_retains_source_key_and_retries_without_duplicate(self):
        push = self.vault.prepare_pushes(self.upload.id, [self.target().id])[0]
        worker = self.worker(); worker.remote._test_new_api_channel.side_effect = RuntimeError()
        worker.execute(push.id)
        self.assertEqual(push.status, "TEST_FAILED")
        worker.remote._disable_new_api_channel.assert_called_once()
        self.assertEqual(self.vault.reveal(self.upload.id)[1], self.secret)
        worker.remote._test_new_api_channel.side_effect = None
        worker.execute(push.id)
        worker.remote._post_new_api_channel.assert_called_once()

    def test_target_tokens_are_encrypted_and_never_in_metadata(self):
        target = self.target()
        self.assertNotIn("target-management-token", target.token_encrypted)
        self.assertNotIn("target-management-token", json.dumps(self.vault.public_target(target)))
        self.assertNotIn("target-management-token", self.service.public_with_deployments(self.upload).__repr__())

    def test_target_url_is_immutable_and_token_can_be_rotated(self):
        target = self.target()
        req = NewApiTargetInput(baseUrl=target.base_url, token="new-management-token")
        self.vault.save_target(req, target.id)
        push = self.vault.prepare_pushes(self.upload.id, [target.id])[0]
        self.assertEqual(self.service.config(push).token, "new-management-token")
        req.base_url = "https://different.example"
        with self.assertRaisesRegex(ValueError, "不可修改"): self.vault.save_target(req, target.id)

    def test_target_disabled_and_invalid_batch_do_not_create_partial_pushes(self):
        a = self.target(); b = self.target("B", enabled=False)
        with self.assertRaises(ValueError): self.vault.prepare_pushes(self.upload.id, [a.id, b.id])
        self.assertEqual(len(list(self.db.scalars(select(ChannelUpload)))), 1)

    def test_existing_legacy_remote_is_adopted_without_duplicate_usage(self):
        target = self.target()
        self.upload.base_url = target.base_url; self.upload.config_source = "personal"
        self.upload.remote_channel_id = "42"; self.upload.status = "ACTIVE"
        self.upload.used_quota = Decimal("500000"); self.db.commit()
        result = self.vault.prepare_pushes(self.upload.id, [target.id])
        self.assertEqual(result[0].id, self.upload.id)
        self.assertEqual(self.service.summary()["totals"]["USD"], "1.00")
        worker = self.worker(); worker.execute(result[0].id)
        worker.remote._post_new_api_channel.assert_not_called()

    def test_legacy_url_normalization_does_not_create_duplicate_channel(self):
        target = self.target()
        self.upload.base_url = "https://A.EXAMPLE:443/"; self.upload.config_source = "personal"
        self.upload.remote_channel_id = "42"; self.upload.status = "ACTIVE"; self.db.commit()
        rows = self.vault.prepare_pushes(self.upload.id, [target.id])
        self.assertEqual(rows[0].id, self.upload.id)
        self.assertEqual(rows[0].base_url, target.base_url)
        self.assertEqual(len(list(self.db.scalars(select(ChannelUpload)))), 1)

    def test_usage_is_summed_by_owner_across_targets_and_currencies(self):
        a, b = self.target(), self.target("B", currency="CNY", quotaPerUnit="1000000")
        first, second = self.vault.prepare_pushes(self.upload.id, [a.id, b.id])
        for push in (first, second): push.remote_channel_id = "7"
        self.db.commit()
        with patch("app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels", return_value=([
            {"id": 7, "status": 2, "used_quota": 1000000}], False, [])):
            self.service.refresh()
        self.assertEqual(self.service.summary()["totals"], {"USD": "2.00", "CNY": "1.00"})
        self.assertEqual(ChannelUploadService(self.db, self.admin.id).summary()["totals"], {})

    def test_additive_migration_preserves_old_rows_and_is_repeatable(self):
        from app.db.init_db import _ensure_channel_upload_columns
        old_engine = create_engine("sqlite:///:memory:")
        with old_engine.begin() as conn:
            conn.execute(text("CREATE TABLE channel_uploads (id VARCHAR(36) PRIMARY KEY, credential_encrypted TEXT)"))
            conn.execute(text("INSERT INTO channel_uploads VALUES ('old-row', 'old-encrypted-key')"))
        with patch("app.db.init_db.engine", old_engine):
            _ensure_channel_upload_columns(); _ensure_channel_upload_columns()
        self.assertIn("source_upload_id", {c["name"] for c in inspect(old_engine).get_columns("channel_uploads")})
        with old_engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT credential_encrypted FROM channel_uploads")).scalar(), "old-encrypted-key")
        old_engine.dispose()
