from target_template_fixture import install_default_template
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.gcp_env_api import push_channel
from app.db.session import Base
from app.models.channel_upload import ChannelUpload
from app.models.channel_push_record import ChannelPushRecord, ChannelPushStatus
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus, PresetTaskType
from app.models.user import User, UserRole, UserStatus
from app.schemas.gcp_env import ChannelPushRequest
from app.schemas.channel_upload import NewApiTargetInput
from app.services.channel_vault_service import ChannelVaultService
from app.services.channel_upload_service import run_channel_upload


class ChannelPushRecordTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

    def setUp(self):
        install_default_template(self)
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.secret = patch.dict(os.environ, {"APP_SECRET_KEY": "unified-test-secret"}); self.secret.start()
        self.admin = User(username="admin", password_hash="unused", role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        self.owner = User(username="owner", password_hash="unused", role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.db.add_all([self.admin, self.owner]); self.db.commit()
        vault = ChannelVaultService(self.db, self.admin)
        self.target = vault.save_target(NewApiTargetInput(baseUrl="https://a.example", token="management-token"))
        self.other = vault.save_target(NewApiTargetInput(baseUrl="https://b.example", token="other-management-token"))
        self.owner.default_target_id = self.target.id; self.admin.default_target_id = self.other.id
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        (root / "gcloud").mkdir(); (root / "keys").mkdir()
        key = root / "keys" / "key.json"
        key.write_text(json.dumps({"type": "service_account", "project_id": "test-project", "private_key_id": "key-id",
            "private_key": self.pem, "client_email": "sa@test-project.iam.gserviceaccount.com"}))
        self.env = GcpEnvironment(env_name="owner-env", google_account_hint="account@example.com", config_name="config",
            config_dir=str(root / "gcloud"), status=GcpEnvironmentStatus.AUTHORIZED, owner_user_id=self.owner.id,
            billing_account_id="ABCDEF-123456-ABCDEF")
        self.db.add(self.env); self.db.flush()
        self.task = GcpPresetTask(env_id=self.env.id, task_type=PresetTaskType.BOOTSTRAP_PROJECT,
            status=PresetTaskStatus.SUCCESS, key_file_path=str(key), key_file_name=key.name,
            socks5_url="socks5://user:proxy-secret@127.0.0.1:1080", requested_vm_zone="us-west2-a")
        self.db.add(self.task); self.db.commit()

    def tearDown(self):
        self.secret.stop(); self.db.close(); self.engine.dispose(); self.tmp.cleanup()

    def submit(self, target=None):
        tasks = BackgroundTasks()
        response = push_channel(self.env.id, ChannelPushRequest(targetId=(target or self.target).id), tasks,
                                db=self.db, user=self.admin)
        return response, tasks

    def test_operator_push_defaults_to_binding_and_cannot_select_other_target(self):
        from app.services.auth_service import AuthService
        from app.models.channel_upload import ChannelUpload
        with self.assertRaises(HTTPException) as unbound:
            push_channel(self.env.id, ChannelPushRequest(), BackgroundTasks(), db=self.db, user=self.owner)
        self.assertEqual(unbound.exception.status_code, 403)
        AuthService(self.db).bind_user_target(self.owner, self.target.id, actor=self.admin)
        with self.assertRaises(HTTPException) as denied:
            push_channel(self.env.id, ChannelPushRequest(targetId=self.other.id), BackgroundTasks(), db=self.db, user=self.owner)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(list(self.db.scalars(select(ChannelUpload))), [])
        response = push_channel(self.env.id, ChannelPushRequest(), BackgroundTasks(), db=self.db, user=self.owner)
        self.assertEqual(response.target_id, self.target.id)

    def worker(self, identifier, *, fail=False):
        with patch("app.services.channel_upload_service.SessionLocal", self.sessions), \
             patch("app.services.channel_upload_service.UploadNewApiClient._find_channel", return_value=None), \
             patch("app.services.channel_upload_service.UploadNewApiClient._post_new_api_channel",
                   side_effect=RuntimeError("remote failed") if fail else None,
                   return_value={"success": True, "data": {"id": 42}}) as create, \
             patch("app.services.channel_upload_service.UploadNewApiClient._test_new_api_channel"), \
             patch("app.services.channel_upload_service.UploadNewApiClient._enable_new_api_channel"):
            run_channel_upload(identifier, self.owner.id)
            calls = create.call_count
            payload = create.call_args.args[1] if calls and not fail else None
        self.db.expire_all()
        return calls, payload

    def test_admin_push_uses_explicit_target_and_environment_owner(self):
        response, tasks = self.submit()
        attempt = self.db.scalar(select(ChannelPushRecord))
        self.assertEqual(attempt.user_id, self.owner.id)
        self.assertEqual(attempt.actor_user_id, self.admin.id)
        self.assertEqual(attempt.target_id, self.target.id)
        self.assertNotEqual(attempt.target_id, self.admin.default_target_id)
        self.assertEqual(attempt.status, ChannelPushStatus.PENDING)
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(response.target_id, self.target.id)

    def test_successful_push_is_recorded_and_credentials_and_proxy_stay_encrypted(self):
        response, _ = self.submit()
        calls, payload = self.worker(response.deployment_id)
        attempt = self.db.scalar(select(ChannelPushRecord))
        self.assertEqual(attempt.status, ChannelPushStatus.SUCCESS)
        self.assertEqual(attempt.channel_id, "42")
        source = self.db.scalar(select(ChannelUpload).where(ChannelUpload.source_upload_id.is_(None)))
        self.assertEqual(source.origin, "GCP")
        self.assertEqual(source.user_id, self.owner.id)
        self.assertIsNotNone(source.credential_encrypted)
        self.assertNotIn("proxy-secret", source.proxy_encrypted)
        deployment = self.db.get(ChannelUpload, response.deployment_id)
        self.assertNotIn("proxy-secret", deployment.payload_json)
        self.assertIn("proxy-secret", json.loads(payload["channel"]["setting"])["proxy"])
        self.assertEqual(calls, 1)

    def test_failed_push_is_recorded_without_remote_error_body(self):
        response, _ = self.submit()
        self.worker(response.deployment_id, fail=True)
        attempt = self.db.scalar(select(ChannelPushRecord))
        self.assertEqual(attempt.status, ChannelPushStatus.FAILED)
        self.assertNotIn("remote failed", attempt.message)
        self.assertEqual(self.db.get(ChannelUpload, response.deployment_id).status, "UNKNOWN")

    def test_gcp_key_can_be_pushed_to_two_targets_without_duplicate_root(self):
        first, _ = self.submit(); second, _ = self.submit(self.other)
        roots = list(self.db.scalars(select(ChannelUpload).where(ChannelUpload.source_upload_id.is_(None))))
        self.assertEqual(len(roots), 1)
        self.assertNotEqual(first.deployment_id, second.deployment_id)

    def test_repeated_push_reuses_deployment_and_worker_does_not_recreate(self):
        first, _ = self.submit(); second, _ = self.submit()
        self.assertEqual(first.deployment_id, second.deployment_id)
        self.worker(first.deployment_id)
        calls, _ = self.worker(second.deployment_id)
        self.assertEqual(calls, 0)

    def test_archived_key_remains_pushable_after_original_file_is_missing(self):
        first, _ = self.submit()
        Path(self.task.key_file_path).unlink()
        second, _ = self.submit(self.other)
        self.assertNotEqual(first.deployment_id, second.deployment_id)
        self.assertEqual(len(list(self.db.scalars(select(ChannelUpload).where(ChannelUpload.source_upload_id.is_(None))))), 1)

    def test_missing_or_disabled_destination_does_not_schedule_push(self):
        self.target.enabled = False; self.db.commit()
        with self.assertRaises(HTTPException): self.submit()
        self.assertEqual(len(list(self.db.scalars(select(ChannelPushRecord)))), 0)

    def test_successful_bootstrap_archives_key_without_automatically_pushing(self):
        from app.services.preset_task_service import run_bootstrap_project_task
        self.task.status = PresetTaskStatus.PENDING; self.db.commit()
        def finish(executor):
            task = executor.db.get(GcpPresetTask, executor.task_id)
            task.status = PresetTaskStatus.SUCCESS
            executor.db.commit()
        with patch("app.services.preset_task_service.SessionLocal", self.sessions), \
             patch("app.services.preset_task_service.PresetTaskExecutor.run", finish):
            run_bootstrap_project_task(self.task.id)
        self.db.expire_all()
        source = self.db.scalar(select(ChannelUpload))
        self.assertEqual(source.origin, "GCP")
        self.assertEqual(source.status, "STORED")
        self.assertEqual(source.user_id, self.owner.id)
        self.assertEqual(len(list(self.db.scalars(select(ChannelPushRecord)))), 0)

    def test_legacy_channel_requires_review_and_can_be_adopted_without_creation(self):
        history = ChannelPushRecord(env_id=self.env.id, user_id=self.admin.id,
            channel_id="legacy-42", channel_name="account@example.com", status=ChannelPushStatus.SUCCESS)
        self.db.add(history); self.db.commit()
        response, _ = self.submit()
        from app.services.channel_upload_service import ChannelUploadService
        worker = ChannelUploadService(self.db, self.owner.id)
        with patch.object(worker.remote, "_find_channel", side_effect=[None, {"id": "legacy-42"}]), \
             patch.object(worker.remote, "_post_new_api_channel") as create:
            worker.execute(response.deployment_id)
        create.assert_not_called()
        self.assertEqual(self.db.get(ChannelUpload, response.deployment_id).status, "LEGACY_REVIEW")
        source = self.db.scalar(select(ChannelUpload).where(ChannelUpload.source_upload_id.is_(None)))
        with patch("app.services.channel_upload_service.UploadNewApiClient._find_channel", return_value={
                "id": "legacy-42", "name": history.channel_name, "status": 1, "used_quota": 500000}):
            adopted = ChannelVaultService(self.db, self.admin).adopt_legacy(source.id, history.id, self.target.id)
        self.assertEqual(adopted.remote_channel_id, "legacy-42")
        self.assertEqual(history.user_id, self.owner.id)
        self.assertEqual(history.actor_user_id, self.admin.id)
        self.assertEqual(worker.summary()["totals"]["USD"], "1.00")
        self.assertEqual(len(list(self.db.scalars(select(ChannelPushRecord).where(
            ChannelPushRecord.status == ChannelPushStatus.PENDING)))), 0)

    def test_restart_clears_pending_environment_status_and_keeps_retryable_deployment(self):
        response, _ = self.submit()
        from app.services.channel_upload_service import recover_channel_uploads
        with patch("app.services.channel_upload_service.SessionLocal", self.sessions): recover_channel_uploads()
        self.db.expire_all()
        self.assertEqual(self.db.scalar(select(ChannelPushRecord)).status, ChannelPushStatus.FAILED)
        retry, tasks = self.submit()
        self.assertEqual(retry.deployment_id, response.deployment_id)
        self.assertEqual(len(tasks.tasks), 1)

    def test_uncertain_legacy_push_can_be_confirmed_without_known_channel_id(self):
        history = ChannelPushRecord(env_id=self.env.id, user_id=self.admin.id,
            channel_name="account@example.com", status=ChannelPushStatus.FAILED)
        self.db.add(history); self.db.commit()
        response, _ = self.submit()
        source = self.db.scalar(select(ChannelUpload).where(ChannelUpload.source_upload_id.is_(None)))
        with patch("app.services.channel_upload_service.UploadNewApiClient._find_channel", return_value={
            "id": 99, "name": history.channel_name, "status": 1, "used_quota": 500000}):
            adopted = ChannelVaultService(self.db, self.admin).adopt_legacy(source.id, history.id, self.target.id)
        self.assertEqual(adopted.id, response.deployment_id)
        self.assertEqual(adopted.remote_channel_id, "99")
        self.assertEqual(history.channel_id, "99")
