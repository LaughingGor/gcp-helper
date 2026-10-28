import copy
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.project_replacement_api import CreateProjectReplacement, router
from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.db.session import Base, get_db
from app.models.channel_upload import ChannelUpload, ChannelUsageSnapshot, NewApiTarget
from app.models.gcp_environment import GcpEnvironment, GcpEnvironmentStatus
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus, PresetTaskType
from app.models.project_replacement import ProjectReplacement
from app.models.user import User, UserRole, UserStatus
from app.runners.gcloud_runner import CommandResult
from app.schemas.channel_upload import CreateChannelUpload
from app.services.auth_service import AuthService
from app.services.channel_push_service import ChannelPushService
from app.services.channel_upload_service import ChannelUploadService, UploadNewApiClient
from app.services.channel_vault_service import ChannelVaultService
from app.services.preset_task_service import PresetTaskExecutor, PresetTaskService
from app.services.project_replacement_service import ProjectReplacementService, ensure_no_replacement, recover_project_replacements
from app.services.secret_service import SecretService


class ProjectReplacementTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode() for _ in range(2)]

    def credential(self, project, index=0):
        return json.dumps({"type": "service_account", "project_id": project,
            "client_email": f"sa@{project}.iam.gserviceaccount.com", "private_key_id": f"key-{index}",
            "private_key": self.keys[index]})

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        secret = patch.dict(os.environ, {"APP_SECRET_KEY": "replacement-tests-stable-secret"})
        secret.start(); self.addCleanup(secret.stop)
        self.engine = create_engine('sqlite:///:memory:', connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.addCleanup(self.engine.dispose); self.addCleanup(self.db.close)
        self.admin = User(username='admin', password_hash='unused', role=UserRole.ADMIN, status=UserStatus.ACTIVE)
        self.owner = User(username='owner', password_hash='unused', role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.other = User(username='other', password_hash='unused', role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.target = NewApiTarget(name='target', base_url='https://new-api.example',
            token_encrypted=SecretService().encrypt_optional('management-token'), enabled=True)
        self.db.add_all([self.admin, self.owner, self.other, self.target]); self.db.flush()
        self.owner.bound_target_id = self.target.id
        self.env = GcpEnvironment(env_name='env', google_account_hint='owner@example.com', config_name='env-config',
            config_dir=str(Path(self.temp.name) / 'gcloud'), owner_user_id=self.owner.id,
            status=GcpEnvironmentStatus.AUTHORIZED, default_project_id='gcph-old', billing_account_id='AAAAAA-BBBBBB-CCCCCC')
        self.db.add(self.env); self.db.flush()
        self.old_task = GcpPresetTask(env_id=self.env.id, task_type=PresetTaskType.BOOTSTRAP_PROJECT,
            status=PresetTaskStatus.SUCCESS, project_id='gcph-old', requested_vm_zone='us-west2-a', finished_at=datetime.utcnow())
        self.db.add(self.old_task); self.db.flush()
        for name in ('new_api_upload_vertex_template', 'new_api_upload_gemini_template'):
            p = patch.object(get_settings(), name, ''); p.start(); self.addCleanup(p.stop)
        self.old = ChannelUploadService(self.db, self.owner.id).create(CreateChannelUpload(kind='vertex_json',
            credential=self.credential('gcph-old'), requestId=str(uuid.uuid4())))
        self.old.origin, self.old.origin_env_id, self.old.origin_task_id = 'GCP', self.env.id, self.old_task.id
        self.old.proxy_encrypted = SecretService().encrypt_optional('socks5://old:secret@192.0.2.1:1080')
        self.old_task.credential_upload_id = self.old.id
        self.db.commit()
        self.row = ChannelVaultService(self.db, self.admin).prepare_pushes(self.old.id, [self.target.id])[0]
        self.row.remote_channel_id, self.row.status, self.row.remote_status = '42', 'ACTIVE', '1'
        self.row.used_quota, self.row.quota_per_unit, self.row.currency = Decimal('123456'), Decimal('100000'), 'CNY'
        self.db.add(ChannelUsageSnapshot(upload_id=self.row.id, used_quota=self.row.used_quota))
        self.db.commit()
        self.remote_state = {'id': 42, 'name': self.row.name, 'type': 41, 'status': 1,
            'models': 'custom-model', 'group': 'custom-group', 'priority': 17, 'weight': 0, 'other': 'global',
            'setting': json.dumps({'force_format': True, 'proxy': 'socks5://old:secret@192.0.2.1:1080'}),
            'used_quota': 123456, 'created_time': 123, 'key': ''}
        self.updates = []
        transport = patch.object(UploadNewApiClient, '_perform_json_request', side_effect=self.request)
        transport.start(); self.addCleanup(transport.stop)
        bootstrap = patch.object(PresetTaskExecutor, 'run', autospec=True, side_effect=self.prepare)
        self.bootstrap_mock = bootstrap.start(); self.addCleanup(bootstrap.stop)
        self.workflow = ProjectReplacementService(self.db, self.admin)

    def prepare(self, executor):
        task = self.db.get(GcpPresetTask, executor.task_id)
        key_path = Path(self.temp.name) / 'keys' / f'{task.id}.json'
        key_path.parent.mkdir(exist_ok=True)
        key_path.write_text(self.credential(task.project_id, 1))
        task.key_file_path, task.key_file_name = str(key_path), key_path.name
        task.vm_name, task.vm_zone, task.socks5_port = 'new-vm', task.requested_vm_zone, '20000'
        task.socks5_url = 'socks5://new:secret@192.0.2.2:20000'
        task.status, task.finished_at = PresetTaskStatus.SUCCESS, datetime.utcnow()
        self.db.commit()

    def request(self, request):
        if request.method == 'GET' and request.full_url.endswith('/api/channel/42'):
            return {'success': True, 'data': copy.deepcopy(self.remote_state)}
        if request.method == 'PUT':
            self.assertTrue(request.full_url.endswith('/api/channel/'))
            payload = json.loads(request.data)
            self.assertNotIn('status', payload)
            self.updates.append(payload)
            self.remote_state.update(payload)
            return {'success': True}
        if '/api/channel/test/' in request.full_url:
            return {'success': True}
        if request.full_url.endswith('/status'):
            self.remote_state['status'] = json.loads(request.data)['status']
            return {'success': True, 'data': True}
        raise AssertionError(f'Unexpected request {request.method} {request.full_url}')

    def req(self, **changes):
        values = dict(targetId=self.target.id, deploymentId=self.row.id, requestId=str(uuid.uuid4()))
        values.update(changes)
        return CreateProjectReplacement(**values)

    def create(self):
        return self.workflow.create(self.env.id, self.req())

    def test_success_preserves_channel_configuration_usage_and_old_credentials(self):
        op = self.create()
        self.workflow.execute(op)
        self.assertEqual(op.status, 'SUCCESS', op.message)
        self.assertEqual(self.row.remote_channel_id, '42')
        self.assertEqual(self.row.used_quota, Decimal('123456'))
        self.assertEqual(self.row.quota_per_unit, Decimal('100000'))
        self.assertEqual(self.row.currency, 'CNY')
        self.assertEqual(self.db.query(ChannelUsageSnapshot).count(), 1)
        self.assertEqual(self.db.query(ChannelUpload).filter(ChannelUpload.remote_channel_id.is_not(None)).count(), 1)
        self.assertNotEqual(self.row.source_upload_id, self.old.id)
        self.assertEqual(json.loads(self.updates[0]['key'])['project_id'], self.env.default_project_id)
        self.assertEqual(json.loads(self.updates[0]['setting']), {'force_format': True, 'proxy': 'socks5://new:secret@192.0.2.2:20000'})
        self.assertEqual(self.updates[0]['models'], 'custom-model')
        self.assertEqual(self.updates[0]['group'], 'custom-group')
        self.assertEqual(self.updates[0]['weight'], 0)
        self.assertEqual(self.updates[0]['priority'], 17)
        self.assertNotIn('used_quota', self.updates[0])
        self.assertNotIn('created_time', self.updates[0])
        self.assertEqual(json.loads(SecretService().decrypt_optional(self.old.credential_encrypted))['private_key'], self.keys[0])
        self.assertNotIn(self.keys[0], op.snapshot_encrypted)
        self.workflow.execute(op)
        self.assertEqual(op.status, 'SUCCESS')
        self.assertEqual(len(self.updates), 1)
        self.assertEqual(ChannelPushService(self.db)._get_latest_success_task(self.env.id).id, op.task_id)

    def test_test_failure_restores_old_key_proxy_and_status_then_retry_reuses_project(self):
        self.remote_state['status'] = 2
        op = self.create()
        with patch.object(UploadNewApiClient, '_test_new_api_channel', side_effect=RuntimeError('secret-echo')):
            self.workflow.execute(op)
        self.assertEqual(op.status, 'ROLLED_BACK')
        self.assertEqual(self.row.source_upload_id, self.old.id)
        self.assertEqual(self.env.default_project_id, 'gcph-old')
        self.assertEqual(json.loads(self.updates[-1]['key'])['project_id'], 'gcph-old')
        self.assertEqual(self.remote_state['status'], 2)
        self.assertEqual(json.loads(self.remote_state['setting'])['proxy'], 'socks5://old:secret@192.0.2.1:1080')
        task_id = op.task_id
        self.workflow.retry(self.env.id, op.id)
        self.workflow.execute(op)
        self.assertEqual(op.status, 'SUCCESS')
        self.assertEqual(op.task_id, task_id)
        self.assertEqual(self.bootstrap_mock.call_count, 1)

    def test_update_and_rollback_timeout_is_unknown_and_retry_restores_first(self):
        op = self.create()
        with patch.object(UploadNewApiClient, 'update_channel', side_effect=TimeoutError('private-response')):
            self.workflow.execute(op)
        self.assertEqual(op.status, 'UNKNOWN')
        self.assertEqual(self.row.status, 'UNKNOWN')
        self.assertNotIn('private-response', json.dumps(self.workflow.public(op), default=str))
        self.workflow.retry(self.env.id, op.id)
        self.assertEqual(op.status, 'ROLLING_BACK')
        self.workflow.execute(op)
        self.assertEqual(op.status, 'ROLLED_BACK')
        self.assertEqual(json.loads(self.updates[-1]['key'])['project_id'], 'gcph-old')
        self.assertEqual(self.row.source_upload_id, self.old.id)

    def test_admin_can_restore_after_original_operator_loses_binding(self):
        owner_workflow = ProjectReplacementService(self.db, self.owner)
        op = owner_workflow.create(self.env.id, self.req())
        with patch.object(UploadNewApiClient, 'update_channel', side_effect=TimeoutError()):
            owner_workflow.execute(op)
        self.owner.bound_target_id = None
        self.db.commit()
        self.workflow.retry(self.env.id, op.id)
        self.assertEqual(op.actor_user_id, self.owner.id)
        self.assertEqual(op.executor_user_id, self.admin.id)
        self.workflow.execute(op)
        self.assertEqual(op.status, 'ROLLED_BACK')

    def test_same_request_can_retrieve_unknown_operation_without_starting_another(self):
        req = self.req()
        op = self.workflow.create(self.env.id, req)
        with patch.object(UploadNewApiClient, 'update_channel', side_effect=TimeoutError()):
            self.workflow.execute(op)
        self.assertEqual(self.workflow.create(self.env.id, req).id, op.id)
        self.assertEqual(self.db.query(ProjectReplacement).count(), 1)

    def test_multi_key_channel_is_rejected_before_cloud_resources_are_created(self):
        self.remote_state['channel_info'] = {'is_multi_key': True}
        op = self.create()
        self.workflow.execute(op)
        self.assertEqual(op.status, 'FAILED')
        self.bootstrap_mock.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_preflight_mismatch_does_not_create_project_or_update_channel(self):
        op = self.create()
        self.remote_state['name'] = 'someone-elses-channel'
        self.workflow.execute(op)
        self.assertEqual(op.status, 'FAILED')
        self.bootstrap_mock.assert_not_called()
        self.assertEqual(self.updates, [])

    def test_failed_preparation_reuses_task_and_leaves_channel_unchanged(self):
        op = self.create()
        with patch.object(PresetTaskExecutor, 'run', side_effect=RuntimeError('cloud-error')):
            self.workflow.execute(op)
        self.assertEqual(op.status, 'FAILED')
        self.assertEqual(self.updates, [])
        task = self.db.get(GcpPresetTask, op.task_id)
        task.status = PresetTaskStatus.FAILED
        self.db.commit()
        project_id = task.project_id
        self.workflow.retry(self.env.id, op.id)
        self.workflow.execute(op)
        self.assertEqual(op.status, 'SUCCESS')
        self.assertEqual(task.project_id, project_id)

    def test_in_progress_rotation_blocks_other_mutations(self):
        self.create()
        with self.assertRaises(RuntimeError): ensure_no_replacement(self.db, self.env.id)
        with self.assertRaises(RuntimeError): PresetTaskService(self.db).start_bootstrap_project(self.env.id)
        with self.assertRaises(RuntimeError): ChannelVaultService(self.db, self.admin).prepare_pushes(self.old.id, [self.target.id])
        from app.services.billing_account_service import BillingAccountService
        with self.assertRaises(RuntimeError): BillingAccountService(self.db)._ensure_no_active_task(self.env.id)
        from app.services.gcp_env_service import GcpEnvService
        with self.assertRaises(RuntimeError): GcpEnvService(self.db).delete_env(self.env.id)

    def test_idempotency_and_binding_change_are_enforced(self):
        self.workflow = ProjectReplacementService(self.db, self.owner)
        req = self.req()
        first = self.workflow.create(self.env.id, req)
        self.assertEqual(self.workflow.create(self.env.id, req).id, first.id)
        self.assertEqual(self.db.query(ProjectReplacement).count(), 1)
        self.owner.bound_target_id = None
        self.db.commit()
        self.workflow.execute(first)
        self.assertEqual(first.status, 'FAILED')
        self.bootstrap_mock.assert_not_called()

    def test_uncommitted_new_task_cannot_be_used_for_ordinary_push(self):
        op = self.create()
        self.prepare(SimpleNamespace(task_id=op.task_id))
        self.assertEqual(ChannelPushService(self.db)._get_latest_success_task(self.env.id).id, self.old_task.id)
        self.db.get(GcpPresetTask, op.task_id).status = PresetTaskStatus.FAILED
        self.db.commit()
        with self.assertRaisesRegex(RuntimeError, '替换操作重试'):
            PresetTaskService(self.db).retry_failed_task(op.task_id)

    def test_startup_recovery_preserves_uncertain_snapshot(self):
        op = self.create()
        op.status, op.snapshot_encrypted = 'TESTING', 'encrypted-snapshot'
        self.db.commit()
        with patch('app.services.project_replacement_service.SessionLocal', self.sessions):
            recover_project_replacements()
        self.db.refresh(op)
        self.assertEqual(op.status, 'UNKNOWN')
        self.assertEqual(op.snapshot_encrypted, 'encrypted-snapshot')
        with self.assertRaises(RuntimeError): ensure_no_replacement(self.db, self.env.id)

    def client(self):
        app = FastAPI(); app.include_router(router, prefix='/api')
        self.current = self.owner
        def auth():
            if self.current is None: raise HTTPException(401)
            return AuthContext(self.current, SimpleNamespace(csrf_token_hash=AuthService.hash_token('csrf')))
        def database(): yield self.db
        app.dependency_overrides[get_auth_context] = auth
        app.dependency_overrides[get_db] = database
        client = TestClient(app)
        self.addCleanup(client.close)
        client.cookies.set(get_settings().auth_csrf_cookie_name, 'csrf')
        return client

    def test_api_permissions_csrf_cross_user_target_and_no_secrets(self):
        client = self.client()
        url = f'/api/gcp-envs/{self.env.id}/project-replacements'
        req = self.req().model_dump(by_alias=True, mode='json')
        self.assertEqual(client.post(url, json=req).status_code, 403)
        self.current = self.other
        self.assertEqual(client.get(url).status_code, 404)
        self.current = self.owner
        with patch('app.api.project_replacement_api.run_project_replacement') as worker:
            response = client.post(url, json=req, headers={'X-CSRF-Token': 'csrf'})
            self.assertEqual(response.status_code, 202, response.text)
            worker.assert_called_once()
        data = response.json()
        self.assertEqual(data['channelId'], '42')
        for secret in ('snapshot_encrypted', 'private_key', 'socks5://', 'management-token'):
            self.assertNotIn(secret, response.text)
        self.current = self.other
        self.other.bound_target_id = self.target.id
        self.assertEqual(client.get(f'{url}/{data["id"]}').status_code, 404)
        self.current = self.owner
        self.owner.bound_target_id = None
        self.assertEqual(client.post(f'{url}/{data["id"]}/retry', headers={'X-CSRF-Token': 'csrf'}).status_code, 403)
        self.owner.role = UserRole.CHANNEL_USER
        self.assertEqual(client.get(url).status_code, 403)
        self.current = None
        self.assertEqual(client.get(url).status_code, 401)

    def test_additive_migration_preserves_tasks_and_is_repeatable(self):
        from app.db.init_db import _ensure_unified_workflow_columns
        with self.engine.begin() as conn:
            conn.execute(text('ALTER TABLE gcp_preset_tasks DROP COLUMN force_new_project'))
            conn.execute(text('ALTER TABLE gcp_preset_tasks DROP COLUMN resource_plan_encrypted'))
        with patch('app.db.init_db.engine', self.engine):
            _ensure_unified_workflow_columns(); _ensure_unified_workflow_columns()
        self.db.expire_all()
        self.assertFalse(self.old_task.force_new_project)
        self.assertEqual(self.old_task.project_id, 'gcph-old')


class FreshProjectIdentityTest(TestCase):
    def executor(self):
        executor = PresetTaskExecutor.__new__(PresetTaskExecutor)
        executor.db, executor.runner, executor._mark = Mock(), Mock(), Mock()
        return executor

    def test_project_timeout_retries_same_id_without_listing_remote_projects(self):
        executor = self.executor()
        task = SimpleNamespace(project_id='gcph-fixed', id='task', resource_plan_encrypted=SecretService().encrypt_optional(json.dumps({'project_create_attempted': True})))
        executor.runner.run.side_effect = [CommandResult([], 1, '', 'NOT_FOUND'), CommandResult([], 1, '', 'timeout')]
        with self.assertRaises(RuntimeError):
            executor._ensure_replacement_project(SimpleNamespace(id='env'), task)
        self.assertEqual(task.project_id, 'gcph-fixed')
        executor.runner.run.reset_mock()
        executor.runner.run.side_effect = [CommandResult([], 0, 'ACTIVE\n', '')]
        self.assertEqual(executor._ensure_replacement_project(SimpleNamespace(id='env'), task), 'gcph-fixed')
        self.assertEqual(executor.runner.run.call_count, 1)
        self.assertEqual(executor.runner.run.call_args.args[1][:3], ['projects', 'describe', 'gcph-fixed'])

    def test_permission_error_does_not_create_another_project(self):
        executor = self.executor()
        executor.runner.run.return_value = CommandResult([], 1, '', 'PERMISSION_DENIED')
        with self.assertRaises(RuntimeError):
            executor._ensure_replacement_project(SimpleNamespace(id='env'), SimpleNamespace(id='task', project_id='gcph-fixed', resource_plan_encrypted=SecretService().encrypt_optional(json.dumps({'project_create_attempted': True}))))
        self.assertEqual(executor.runner.run.call_count, 1)

    def test_fresh_project_persists_creation_attempt_before_remote_write(self):
        executor = self.executor()
        executor.runner.run.return_value = CommandResult([], 0, '', '')
        task = SimpleNamespace(project_id='gcph-fresh', id='task', resource_plan_encrypted=None)
        executor._ensure_replacement_project(SimpleNamespace(id='env'), task)
        self.assertTrue(json.loads(SecretService().decrypt_optional(task.resource_plan_encrypted))['project_create_attempted'])
        executor.db.commit.assert_called_once()
        self.assertEqual(executor.runner.run.call_args.args[1][:3], ['projects', 'create', 'gcph-fresh'])

    def test_lost_vm_response_reuses_persisted_name_and_proxy_credentials(self):
        with TemporaryDirectory() as root:
            script = Path(root) / 'startup.sh'
            script.write_text('#!/bin/sh')
            executor = self.executor()
            executor.compute_vm_zone = 'us-west2-a'
            executor.settings = SimpleNamespace(socks5_startup_script_path=script, compute_vm_machine_type='e2-micro',
                compute_vm_image_family='debian-12', compute_vm_image_project='debian-cloud',
                compute_vm_boot_disk_size='10GB', compute_vm_boot_disk_type='pd-standard', compute_vm_network_tag='socks5-server')
            task = SimpleNamespace(id='task', force_new_project=True,
                resource_plan_encrypted=SecretService().encrypt_optional(json.dumps({'project_create_attempted': True})))
            executor.runner.run.side_effect = [CommandResult([], 1, '', 'was not found'), CommandResult([], 1, '', 'timeout')]
            with self.assertRaises(RuntimeError):
                executor._create_compute_vm(SimpleNamespace(id='env'), task, 'gcph-fresh', 'network', 'subnet')
            encrypted_plan = task.resource_plan_encrypted
            plan = json.loads(SecretService().decrypt_optional(encrypted_plan))
            self.assertTrue(plan['project_create_attempted'])
            executor.runner.run.reset_mock()
            executor.runner.run.side_effect = [CommandResult([], 0, plan['vm_name'], '')]
            executor._get_vm_external_ip = Mock(return_value='192.0.2.5')
            vm_name, port, url, _ = executor._create_compute_vm(SimpleNamespace(id='env'), task, 'gcph-fresh', 'network', 'subnet')
            self.assertEqual(vm_name, plan['vm_name'])
            self.assertEqual(port, plan['port'])
            self.assertIn(plan['password'], url)
            self.assertEqual(task.resource_plan_encrypted, encrypted_plan)
            self.assertEqual(executor.runner.run.call_count, 1)

    def test_missing_local_json_with_new_remote_key_does_not_create_another_key(self):
        with TemporaryDirectory() as root:
            executor = self.executor()
            executor._run_create_service_account_key = Mock()
            executor.runner.run.return_value = CommandResult([], 0, 'before\nafter\n', '')
            task = SimpleNamespace(id='task', force_new_project=True,
                resource_plan_encrypted=SecretService().encrypt_optional(json.dumps({'keys_before_creation': ['before']})))
            with self.assertRaisesRegex(RuntimeError, '不会盲目重复创建'):
                executor._create_key(SimpleNamespace(id='env', config_dir=str(Path(root) / 'gcloud')),
                    task, 'gcph-fresh', 'sa@example.com')
            executor._run_create_service_account_key.assert_not_called()
