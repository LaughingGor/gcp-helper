import os
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.channel_upload import NewApiTarget
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus
from app.models.user import User, UserRole, UserStatus
from app.models.user_channel_config import UserChannelConfig
from app.services.preset_task_service import PresetTaskService, PresetTaskExecutor, recover_interrupted_preset_tasks
from app.services.new_api_target_service import migrate_legacy_targets, resolve_target
from app.services.channel_statistics_service import ChannelStatisticsService
from app.services.channel_push_service import ChannelPushService
from app.services.secret_service import SecretService


class UnifiedConfigurationTest(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine); self.db = self.sessions()
        self.envvars = patch.dict(os.environ, {"APP_SECRET_KEY": "unified-config-test-secret"}); self.envvars.start()
        self.owner = User(username="owner", password_hash="unused", role=UserRole.OPERATOR,
                          status=UserStatus.ACTIVE, compute_vm_zone="asia-east1-a")
        self.db.add(self.owner); self.db.flush()
        self.env = GcpEnvironment(env_name="test", google_account_hint="test@example.com", config_name="test",
            config_dir="/tmp/unified-test-unused", owner_user_id=self.owner.id, billing_account_id="ABCDEF-123456-ABCDEF",
            status=GcpEnvironmentStatus.AUTHORIZED)
        self.db.add(self.env); self.db.commit()
        self.service = PresetTaskService(self.db)

    def tearDown(self):
        self.envvars.stop(); self.db.close(); self.engine.dispose()

    def test_upgrade_adds_binding_without_granting_existing_preferences(self):
        from app.db.init_db import _ensure_unified_workflow_columns
        from sqlalchemy import text, inspect
        old_engine = create_engine('sqlite:///:memory:')
        self.addCleanup(old_engine.dispose)
        with old_engine.begin() as conn:
            for table in ('users', 'gcp_preset_tasks', 'channel_uploads', 'channel_push_records'):
                conn.execute(text(f'CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)'))
            conn.execute(text('ALTER TABLE users ADD COLUMN default_target_id VARCHAR(36)'))
            conn.execute(text("INSERT INTO users (id, default_target_id) VALUES ('old', 'previous-choice')"))
        with patch('app.db.init_db.engine', old_engine):
            _ensure_unified_workflow_columns()
            _ensure_unified_workflow_columns()
        self.assertIn('bound_target_id', {c['name'] for c in inspect(old_engine).get_columns('users')})
        with old_engine.connect() as conn:
            row = conn.execute(text('SELECT default_target_id, bound_target_id FROM users')).one()
            self.assertEqual(row, ('previous-choice', None))

    def test_vm_zone_precedence_and_default_changes_do_not_change_queued_task(self):
        self.env.default_zone = "us-central1-b"; self.db.commit()
        task = self.service.start_bootstrap_project(self.env.id, vm_zone="europe-west1-c")
        self.assertEqual(task.requested_vm_zone, "europe-west1-c")
        self.owner.compute_vm_zone = "asia-east2-b"; self.env.default_zone = "us-east1-b"; self.db.commit()
        executor = PresetTaskExecutor(self.db, task.id)
        with patch.object(executor.runner, "run", side_effect=RuntimeError("stop before real cloud request")), \
             patch("app.services.preset_task_service.logger"):
            executor.run()
        self.assertEqual(executor.compute_vm_zone, "europe-west1-c")
        self.assertEqual(task.requested_vm_zone, "europe-west1-c")

    def test_environment_default_precedes_owner_and_system_defaults(self):
        self.env.default_zone = "us-central1-b"; self.db.commit()
        self.assertEqual(self.service.start_bootstrap_project(self.env.id).requested_vm_zone, "us-central1-b")

    def test_owner_default_is_resolved_when_task_created(self):
        task = self.service.start_bootstrap_project(self.env.id)
        self.assertEqual(task.requested_vm_zone, "asia-east1-a")
        self.assertIsNone(task.vm_zone)

    def test_retry_and_restart_preserve_snapshot(self):
        task = self.service.start_bootstrap_project(self.env.id, vm_zone="us-central1-b")
        with patch("app.services.preset_task_service.SessionLocal", self.sessions): recover_interrupted_preset_tasks()
        self.db.refresh(task)
        self.assertEqual(task.status, PresetTaskStatus.FAILED)
        self.owner.compute_vm_zone = "us-east1-c"; self.db.commit()
        retry, created = self.service.retry_failed_task(task.id)
        self.assertTrue(created)
        self.assertEqual(retry.requested_vm_zone, "us-central1-b")

    def test_duplicate_task_submission_reuses_active_task(self):
        task = self.service.start_bootstrap_project(self.env.id)
        other = self.service.start_bootstrap_project(self.env.id, vm_zone="us-east1-b")
        self.assertEqual(task.id, other.id)
        self.assertEqual(task.requested_vm_zone, "asia-east1-a")

    def test_legacy_connection_import_is_repeatable_disabled_and_does_not_overwrite_existing_token(self):
        encrypted = SecretService().encrypt_optional("old-token")
        self.db.add(UserChannelConfig(user_id=self.owner.id, base_url="https://legacy.example/", username="",
            password_encrypted="", token_encrypted=encrypted))
        self.db.commit()
        migrate_legacy_targets(self.db); migrate_legacy_targets(self.db)
        targets = list(self.db.scalars(select(NewApiTarget)))
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0].enabled)
        self.assertEqual(targets[0].token_encrypted, encrypted)
        self.assertEqual(self.owner.default_target_id, targets[0].id)
        with self.assertRaises(ValueError): resolve_target(self.db, self.owner.id)
        targets[0].token_encrypted = SecretService().encrypt_optional("rotated-token"); targets[0].enabled = True
        self.owner.bound_target_id = targets[0].id
        self.db.commit(); migrate_legacy_targets(self.db)
        self.assertEqual(ChannelPushService(self.db, user_id=self.owner.id)._load_push_config().token, "rotated-token")

    def test_statistics_use_selected_backends_currency_group_and_quota_conversion(self):
        target = NewApiTarget(name="CNY backend", base_url="https://stats.example", token_encrypted=SecretService().encrypt_optional("token"),
                             enabled=True, models="model", group="group-b", quota_per_unit=Decimal("1000000"), currency="CNY")
        self.db.add(target); self.db.commit()
        self.owner.bound_target_id = target.id
        self.db.commit()
        service = ChannelStatisticsService(self.db, user_id=self.owner.id, target_id=target.id)
        with patch.object(service, "_fetch_all_channels", return_value=([
            {"id": 7, "name": "test", "status": 1, "group": "group-b", "used_quota": 2000000}], False, [])):
            result = service.get_statistics(refresh=True)
        self.assertEqual(result.currency, "CNY")
        self.assertEqual(result.total_consumed_amount, Decimal("2.00"))
        self.assertEqual(result.group, "group-b")

    def test_statistics_group_change_does_not_reuse_previous_group_cache(self):
        import uuid
        target = NewApiTarget(name='stats', base_url='https://cache.example',
                             token_encrypted=SecretService().encrypt_optional('token'),
                             group='push-group', stats_group='first-' + str(uuid.uuid4()))
        self.db.add(target)
        self.db.commit()
        self.owner.bound_target_id = target.id
        self.db.commit()
        service = ChannelStatisticsService(self.db, user_id=self.owner.id, target_id=target.id)
        first = target.stats_group
        second = 'second-' + str(uuid.uuid4())
        channels = [
            {'id': 1, 'group': first, 'status': 1, 'used_quota': 500000},
            {'id': 2, 'group': second, 'status': 1, 'used_quota': 1000000},
        ]
        with patch.object(service, '_fetch_all_channels', return_value=(channels, False, [])) as fetch:
            self.assertEqual(service.get_statistics().group, first)
            target.stats_group = second
            self.db.commit()
            result = service.get_statistics()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.group, second)
        self.assertEqual([row.channel_id for row in result.channels], ['2'])
