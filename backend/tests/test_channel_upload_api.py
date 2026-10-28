from target_template_fixture import install_default_template
import os
import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.channel_upload_api import router
from app.core.auth import AuthContext, get_auth_context
from app.core.config import get_settings
from app.db.session import Base, get_db
from app.models.user import User, UserRole, UserStatus
from app.services.auth_service import AuthService
from app.services.channel_push_service import NewApiChannelConfig
from app.services.channel_upload_service import ChannelUploadService


class ChannelUploadApiTest(TestCase):
    def setUp(self):
        install_default_template(self)
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.alice = User(username="alice", password_hash="unused", role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.bob = User(username="bob", password_hash="unused", role=UserRole.OPERATOR, status=UserStatus.ACTIVE)
        self.db.add_all([self.alice, self.bob]); self.db.commit()
        self.current = self.alice
        self.app = FastAPI(); self.app.include_router(router, prefix="/api")
        from app.api.auth_api import admin_router
        self.app.include_router(admin_router, prefix='/api')
        def authentication():
            if self.current is None:
                raise HTTPException(401, "请先登录")
            return AuthContext(self.current, SimpleNamespace(csrf_token_hash=AuthService.hash_token("test-csrf")))
        def database():
            yield self.db
        self.app.dependency_overrides[get_auth_context] = authentication
        self.app.dependency_overrides[get_db] = database
        self.client = TestClient(self.app)
        self.client.cookies.set(get_settings().auth_csrf_cookie_name, "test-csrf")
        self.headers = {"X-CSRF-Token": "test-csrf"}
        self.secret = patch.dict(os.environ, {"APP_SECRET_KEY": "http-tests-secret"}); self.secret.start()
        self.config = patch.object(ChannelUploadService, "config", return_value=NewApiChannelConfig(base_url="https://new-api.example", token="admin-token"))
        self.config.start()
        self.worker = patch("app.api.channel_upload_api.run_channel_upload"); self.job = self.worker.start()

    def tearDown(self):
        self.worker.stop(); self.config.stop(); self.secret.stop()
        self.client.close(); self.db.close(); self.engine.dispose()

    def submit(self, **changes):
        data = {"kind": "gemini_key", "credential": "api-key-never-expose-123456", "requestId": str(uuid.uuid4())}
        data.update(changes)
        return self.client.post("/api/channel-uploads", json=data, headers=self.headers)

    def test_unauthenticated_requests_rejected(self):
        self.current = None
        self.assertEqual(self.client.get("/api/my-channels").status_code, 401)

    def test_binding_requires_admin_csrf_and_enabled_target(self):
        self.current.role = UserRole.ADMIN
        target = self.client.post('/api/admin/new-api-targets', json={
            'baseUrl': 'https://binding.example', 'token': 'private-token'
        }, headers=self.headers).json()
        url = f'/api/admin/users/{self.bob.id}/target'
        body = {'boundTargetId': target['id']}
        self.assertEqual(self.client.put(url, json=body).status_code, 403)
        self.assertEqual(self.client.put(url, json={'boundTargetId': str(uuid.uuid4())}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.put(f'/api/admin/users/{uuid.uuid4()}/target', json=body, headers=self.headers).status_code, 404)
        response = self.client.put(url, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['boundTargetId'], target['id'])
        self.assertNotIn('private-token', response.text)
        self.current = self.bob
        for role in (UserRole.OPERATOR, UserRole.CHANNEL_USER):
            self.current.role = role
            self.assertEqual(self.client.put(url, json={'boundTargetId': None}, headers=self.headers).status_code, 403)
        self.current = self.alice
        from app.models.channel_upload import NewApiTarget
        self.db.get(NewApiTarget, target['id']).enabled = False
        self.db.commit()
        self.assertEqual(self.client.put(url, json=body, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.put(url, json={'boundTargetId': None}, headers=self.headers).status_code, 200)
        self.assertIsNone(self.bob.bound_target_id)

    def test_binding_controls_target_list_and_statistics_despite_request_parameters(self):
        from app.api.channel_statistics_api import router as statistics_router
        self.app.include_router(statistics_router, prefix='/api')
        self.current.role = UserRole.ADMIN
        targets = [self.client.post('/api/admin/new-api-targets', json={
            'baseUrl': f'https://binding-{i}.example', 'token': 'private-token'
        }, headers=self.headers).json() for i in (1, 2)]
        self.bob.default_target_id = targets[1]['id']
        self.db.commit()
        self.current = self.bob
        self.assertEqual(self.client.get('/api/new-api-targets').json(), [])
        self.assertEqual(self.client.get('/api/channel-statistics').status_code, 403)
        self.current = self.alice
        url = f'/api/admin/users/{self.bob.id}/target'
        self.client.put(url, json={'boundTargetId': targets[0]['id']}, headers=self.headers)
        self.current = self.bob
        self.assertEqual([t['id'] for t in self.client.get('/api/new-api-targets').json()], [targets[0]['id']])
        with patch('app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels', return_value=([], False, [])) as remote:
            self.assertEqual(self.client.get('/api/channel-statistics', params={'refresh': True}).status_code, 200)
            remote.reset_mock()
            self.assertEqual(self.client.get('/api/channel-statistics', params={'targetId': targets[1]['id']}).status_code, 403)
            remote.assert_not_called()
        self.current = self.alice
        self.assertEqual(len(self.client.get('/api/new-api-targets').json()), 2)
        self.client.put(url, json={'boundTargetId': targets[1]['id']}, headers=self.headers)
        self.current = self.bob
        self.assertEqual(self.client.get('/api/channel-statistics', params={'targetId': targets[0]['id']}).status_code, 403)

    def test_statistics_use_saved_group_independently_of_push_group(self):
        from app.api.channel_statistics_api import router as stats_router
        self.app.include_router(stats_router, prefix='/api')
        self.current.role = UserRole.ADMIN
        target = self.client.post('/api/admin/new-api-targets', json={
            'baseUrl': 'https://stats-group.example', 'token': 'private-management-token',
            'statsGroup': ' stats-only '
        }, headers=self.headers).json()
        self.assertEqual(target['statsGroup'], 'stats-only')
        self.assertEqual(target['group'], 'template-group')
        channels = [
            {'id': 1, 'name': 'included', 'group': 'stats-only', 'status': 1, 'used_quota': 500000},
            {'id': 2, 'name': 'excluded', 'group': 'template-group', 'status': 1, 'used_quota': 9000000},
        ]
        self.current.bound_target_id = target["id"]
        self.db.commit()
        self.current.role = UserRole.OPERATOR
        with patch('app.services.channel_statistics_service.ChannelStatisticsService._fetch_all_channels',
                   return_value=(channels, False, [])):
            result = self.client.get('/api/channel-statistics', params={
                'targetId': target['id'], 'group': 'template-group', 'refresh': True
            })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['group'], 'stats-only')
        self.assertEqual([c['channelId'] for c in result.json()['channels']], ['1'])
        self.assertEqual(result.json()['totalConsumedAmount'], '1.00')
        self.current.role = UserRole.ADMIN
        response = self.client.put(f'/api/admin/new-api-targets/{target["id"]}', json={
            'baseUrl': target['baseUrl'], 'statsGroup': 'new-stats'
        }, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['statsGroup'], 'new-stats')
        self.assertEqual(response.json()['group'], 'template-group')
        for invalid in ('', '  ', 'x' * 129):
            response = self.client.put(f'/api/admin/new-api-targets/{target["id"]}', json={
                'baseUrl': target['baseUrl'], 'statsGroup': invalid
            }, headers=self.headers)
            self.assertEqual(response.status_code, 422)

    def test_invalid_target_token_is_reported_without_saving_or_leaking_secrets(self):
        self.current.role = UserRole.ADMIN
        data = {'baseUrl': 'https://token-check.example', 'token': 'secret-candidate-token'}
        self.token_request.return_value = {'success': False, 'message': data['token']}
        response = self.client.post('/api/admin/new-api-targets', json=data, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('Access Token 验证失败', response.json()['detail'])
        self.assertNotIn(data['token'], response.text)
        self.assertEqual(self.client.get('/api/admin/new-api-targets').json(), [])
        self.token_request.return_value = {'success': True, 'data': {'items': []}}
        target = self.client.post('/api/admin/new-api-targets', json=data, headers=self.headers).json()
        self.token_request.return_value = {'success': False}
        response = self.client.put(f'/api/admin/new-api-targets/{target["id"]}',
                                   json={**data, 'token': 'bad-replacement-token'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('bad-replacement-token', response.text)
        self.assertEqual(self.client.get('/api/admin/new-api-targets').json(), [target])

    def test_delete_target_requires_admin_and_csrf_and_clears_default(self):
        self.current.role = UserRole.ADMIN
        target = self.client.post('/api/admin/new-api-targets', json={
            'baseUrl': 'https://delete.example', 'token': 'management-token-never-expose'
        }, headers=self.headers).json()
        url = f'/api/admin/new-api-targets/{target["id"]}'
        self.alice.default_target_id = target['id']
        self.db.commit()
        self.current = None
        self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 401)
        self.current = self.bob
        for role in (UserRole.OPERATOR, UserRole.CHANNEL_USER):
            self.current.role = role
            self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 403)
        self.current.role = UserRole.ADMIN
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(self.client.get('/api/admin/new-api-targets').json()[0]['id'], target['id'])
        response = self.client.delete(url, headers=self.headers)
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b'')
        self.db.refresh(self.alice)
        self.assertIsNone(self.alice.default_target_id)
        self.assertEqual(self.client.get('/api/admin/new-api-targets').json(), [])
        self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 404)

    def test_delete_target_with_deployment_is_rejected(self):
        identifier = self.submit().json()['id']
        self.current.role = UserRole.ADMIN
        target = self.client.post('/api/admin/new-api-targets', json={
            'baseUrl': 'https://used.example', 'token': 'management-token'
        }, headers=self.headers).json()
        self.client.post(f'/api/admin/channel-uploads/{identifier}/push',
                         json={'targetIds': [target['id']]}, headers=self.headers)
        response = self.client.delete(f'/api/admin/new-api-targets/{target["id"]}', headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.get('/api/admin/new-api-targets').json()[0]['id'], target['id'])

    def test_target_accepts_only_connection_fields_and_requires_admin_csrf(self):
        data = {"baseUrl": "https://target.example", "token": "private-management-token"}
        self.assertEqual(self.client.post("/api/admin/new-api-targets", json=data, headers=self.headers).status_code, 403)
        self.current.role = UserRole.ADMIN
        self.assertEqual(self.client.post("/api/admin/new-api-targets", json=data).status_code, 403)
        created = self.client.post("/api/admin/new-api-targets", json=data, headers=self.headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["group"], "template-group")
        self.assertNotIn(data["token"], created.text)
        for field, value in (("name", "custom"), ("models", "custom"), ("group", "custom"),
                             ("enabled", False), ("vertexRegion", "custom"),
                             ("vertexTemplate", "custom"), ("geminiTemplate", "custom"),
                             ("quotaPerUnit", "1"), ("currency", "CNY")):
            with self.subTest(field=field):
                response = self.client.put(f'/api/admin/new-api-targets/{created.json()["id"]}',
                                           json={**data, field: value}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(data["token"], response.text)

    def test_csrf_is_required_for_submission_and_refresh(self):
        response = self.client.post("/api/channel-uploads/preview", json={"kind": "gemini_key", "credential": "secret-key-123456789"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/my-channel-usage/refresh").status_code, 403)

    def test_submission_stores_without_remote_push_and_returns_no_secrets(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("api-key-never-expose", response.text)
        self.assertNotIn("credential_encrypted", response.text)
        self.job.assert_not_called()
        self.assertEqual(response.json()["status"], "STORED")

    def test_ownership_is_enforced_on_list_detail_and_retry(self):
        identifier = self.submit().json()["id"]
        self.current = self.bob
        self.assertEqual(self.client.get("/api/my-channels").json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/channel-uploads/{identifier}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/channel-uploads/{identifier}/retry", headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get("/api/my-channel-usage").json()["channelCount"], 0)

    def test_admin_summary_requires_admin_permission(self):
        self.submit()
        self.assertEqual(self.client.get("/api/admin/channel-usage").status_code, 403)
        self.current.role = UserRole.ADMIN
        response = self.client.get("/api/admin/channel-usage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["username"], "alice")
        self.assertEqual(response.json()[0]["channelCount"], 1)
        self.assertEqual(self.client.get(f"/api/admin/channel-usage/{self.alice.id}").json()["total"], 1)

    def test_cannot_supply_owner_or_channel_configuration(self):
        self.assertEqual(self.submit(user_id=self.bob.id).status_code, 422)
        self.assertEqual(self.submit(group="admin").status_code, 422)

    def test_pagination_validation(self):
        self.assertEqual(self.client.get("/api/my-channels?page=0").status_code, 422)
        self.assertEqual(self.client.get("/api/my-channels?pageSize=1000").status_code, 422)

    def test_validation_errors_do_not_echo_credentials_or_extra_fields(self):
        response = self.submit(credential="sensitive-value-" * 5000)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-value", response.text)
        response = self.submit(private_key="sensitive-extra-field")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-extra-field", response.text)

    def test_channel_user_can_upload_view_own_usage_but_cannot_push(self):
        self.current.role = UserRole.CHANNEL_USER
        preview = self.client.post("/api/channel-uploads/preview", json={
            "kind": "gemini_key", "credential": "api-key-never-expose-123456"}, headers=self.headers)
        self.assertEqual(preview.status_code, 200)
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        identifier = response.json()["id"]
        self.assertEqual(self.client.get("/api/my-channels").json()["total"], 1)
        self.assertEqual(self.client.get("/api/my-channel-usage").json()["channelCount"], 1)
        self.assertEqual(self.client.get(f"/api/channel-uploads/{identifier}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/channel-uploads/{identifier}/retry", headers=self.headers).status_code, 403)
        with patch("app.api.channel_upload_api.refresh_channel_uploads"):
            self.assertEqual(self.client.post("/api/my-channel-usage/refresh", headers=self.headers).status_code, 202)
        self.current = self.bob
        self.current.role = UserRole.CHANNEL_USER
        self.assertEqual(self.client.get("/api/my-channels").json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/channel-uploads/{identifier}").status_code, 404)

    def test_channel_user_cannot_access_gcp_or_other_management_endpoints(self):
        from app.api.gcp_env_api import router as environments
        from app.api.preset_task_api import router as tasks
        from app.api.channel_statistics_api import router as statistics
        from app.api.channel_config_api import router as config
        from app.api.user_settings_api import router as settings
        from app.api.auth_api import admin_router
        for restricted_router in (environments, tasks, statistics, config, settings, admin_router):
            self.app.include_router(restricted_router, prefix="/api")
        self.current.role = UserRole.CHANNEL_USER
        for path in ("/api/gcp-envs", "/api/gcp-envs/login-sessions", "/api/preset-tasks",
                     "/api/channel-statistics", "/api/channel-config", "/api/user-settings",
                     "/api/admin/users", "/api/admin/channel-usage"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        for path, data in (
            ("/api/gcp-envs/login/start", {"googleAccount": "user@example.com", "proxy": {"enabled": False}}),
            ("/api/gcp-envs/env-1/preset-tasks/bootstrap", {}),
            ("/api/gcp-envs/env-1/channel-push", {}),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=data, headers=self.headers).status_code, 403)

    def test_admin_can_view_and_download_another_users_retained_key(self):
        identifier = self.submit().json()["id"]
        self.current = self.bob; self.current.role = UserRole.ADMIN
        url = f"/api/admin/channel-uploads/{identifier}"
        self.assertEqual(self.client.post(url + "/credential").status_code, 403)
        response = self.client.post(url + "/credential", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["credential"], "api-key-never-expose-123456")
        self.assertEqual(response.headers["cache-control"], "no-store")
        download = self.client.post(url + "/download", headers=self.headers)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, "api-key-never-expose-123456")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.headers["cache-control"], "no-store")
        listing = self.client.get("/api/admin/channel-uploads")
        self.assertEqual(listing.json()["items"][0]["username"], "alice")
        self.assertNotIn("api-key-never-expose", listing.text)

    def test_non_admin_cannot_view_keys_manage_destinations_or_push(self):
        identifier = self.submit().json()["id"]
        for role in (UserRole.CHANNEL_USER, UserRole.OPERATOR):
            self.current.role = role
            with self.subTest(role=role):
                self.assertEqual(self.client.get("/api/admin/channel-uploads").status_code, 403)
                self.assertEqual(self.client.get("/api/admin/new-api-targets").status_code, 403)
                for action in ("credential", "download"):
                    self.assertEqual(self.client.post(f"/api/admin/channel-uploads/{identifier}/{action}", headers=self.headers).status_code, 403)
                self.assertEqual(self.client.post(f"/api/admin/channel-uploads/{identifier}/push", json={"targetIds": [str(uuid.uuid4())]}, headers=self.headers).status_code, 403)
                self.assertEqual(self.client.post("/api/admin/new-api-targets", json={"baseUrl": "https://x.example", "token": "secret-token"}, headers=self.headers).status_code, 403)

    def test_admin_pushes_one_retained_credential_to_multiple_targets(self):
        identifier = self.submit().json()["id"]
        self.job.assert_not_called()
        self.current = self.bob; self.current.role = UserRole.ADMIN
        targets = []
        for name in ("a", "b"):
            response = self.client.post("/api/admin/new-api-targets", json={"baseUrl": f"https://{name}.example", "token": "secret-management-token"}, headers=self.headers)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertNotIn("secret-management-token", response.text)
            targets.append(response.json()["id"])
        response = self.client.post(f"/api/admin/channel-uploads/{identifier}/push", json={"targetIds": targets}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(response.json()["items"]), 2)
        self.assertEqual(self.job.call_count, 2)
        self.assertNotIn("api-key-never-expose", response.text)
        listing = self.client.get(f"/api/admin/channel-usage/{self.alice.id}").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(len(listing["items"][0]["deployments"]), 2)
        self.assertTrue(listing["items"][0]["credentialStored"])
        self.assertEqual(self.client.get("/api/admin/channel-usage").json()[0]["deploymentCount"], 2)

    def test_available_targets_do_not_expose_connection_secrets_and_channel_user_cannot_list_them(self):
        self.current.role = UserRole.ADMIN
        created = self.client.post("/api/admin/new-api-targets", json={"baseUrl": "https://shared.example",
            "token": "private-management-token"}, headers=self.headers).json()
        self.current.bound_target_id = created["id"]
        self.db.commit()
        self.current.role = UserRole.OPERATOR
        available = self.client.get("/api/new-api-targets")
        self.assertEqual(available.status_code, 200)
        self.assertEqual(available.json()[0]["id"], created["id"])
        self.assertNotIn("private-management-token", available.text)
        self.assertNotIn("baseUrl", available.text)
        self.assertNotIn("tokenConfigured", available.text)
        self.current.role = UserRole.CHANNEL_USER
        self.assertEqual(self.client.get("/api/new-api-targets").status_code, 403)

    def test_user_preferences_cannot_change_admin_binding(self):
        from app.api.user_settings_api import router as settings
        self.app.include_router(settings, prefix="/api")
        self.current.role = UserRole.ADMIN
        created = self.client.post("/api/admin/new-api-targets", json={"baseUrl": "https://shared.example",
            "token": "private-management-token"}, headers=self.headers).json()
        self.current.bound_target_id = created["id"]
        self.db.commit()
        self.current.role = UserRole.OPERATOR
        for target_id in (created['id'], None, str(uuid.uuid4())):
            response = self.client.put('/api/user-settings', json={'computeVmZone': 'us-central1-b', 'defaultTargetId': target_id}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.put('/api/user-settings', json={'computeVmZone': 'us-central1-b'}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['defaultTargetId'], created['id'])
        self.assertEqual(self.current.bound_target_id, created['id'])

    def test_historical_gcp_import_is_admin_only_and_requires_csrf(self):
        self.assertEqual(self.client.post("/api/admin/gcp-credentials/import", headers=self.headers).status_code, 403)
        self.current.role = UserRole.ADMIN
        self.assertEqual(self.client.post("/api/admin/gcp-credentials/import").status_code, 403)
        response = self.client.post("/api/admin/gcp-credentials/import", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], 0)
