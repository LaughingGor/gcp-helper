import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services.channel_push_service import ChannelPushService, NewApiChannelConfig


class ChannelPushServiceTest(TestCase):
    def setUp(self) -> None:
        self.service = ChannelPushService.__new__(ChannelPushService)
        self.config = NewApiChannelConfig(base_url="https://new-api.example", token="token")

    def test_upstream_error_body_cannot_leak_shared_management_token(self):
        from io import BytesIO
        from urllib.error import HTTPError
        self.service._perform_json_request = Mock(side_effect=HTTPError(
            "https://new-api.example/api/channel/", 403, "Forbidden", {}, BytesIO(b"sensitive-shared-management-token")))
        with self.assertRaises(RuntimeError) as raised:
            self.service._request_new_api_json(self.config, "https://new-api.example/api/channel/", method="GET")
        self.assertNotIn("sensitive-shared-management-token", str(raised.exception))
        self.assertIn("403", str(raised.exception))

    def test_token_validation_reads_channel_list_with_bearer_header(self):
        self.service._perform_json_request = Mock(return_value={"success": True, "data": {"items": []}})
        self.service.validate_config(self.config)
        request = self.service._perform_json_request.call_args.args[0]
        self.assertEqual(request.full_url, 'https://new-api.example/api/channel/?p=1&page_size=1')
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.get_header('Authorization'), 'Bearer token')
        self.assertIsNone(request.data)
        self.service._perform_json_request.return_value = {"success": True, "data": []}
        self.service.validate_config(self.config)

    def test_token_validation_rejects_false_or_malformed_success_without_echoing_response(self):
        for response in ({"success": False, "message": "sensitive-token"}, {}, [],
                         {"success": True}, {"success": "true", "data": []},
                         {"success": True, "data": {"items": "sensitive-token"}}):
            with self.subTest(response=response):
                self.service._perform_json_request = Mock(return_value=response)
                with self.assertRaises(RuntimeError) as raised:
                    self.service.validate_config(self.config)
                self.assertNotIn('sensitive-token', str(raised.exception))

    def test_token_validation_handles_http_network_timeout_and_invalid_json(self):
        from urllib.error import HTTPError, URLError
        for error in (HTTPError(self.config.base_url, 401, 'sensitive-token', {}, None),
                      HTTPError(self.config.base_url, 403, 'sensitive-token', {}, None),
                      HTTPError(self.config.base_url, 500, 'sensitive-token', {}, None),
                      URLError('sensitive-token'), TimeoutError('sensitive-token'),
                      RuntimeError('new-api 返回不是有效 JSON，请管理员检查后台日志')):
            with self.subTest(error=type(error).__name__):
                self.service._perform_json_request = Mock(side_effect=error)
                with self.assertRaises(RuntimeError) as raised:
                    self.service.validate_config(self.config)
                self.assertNotIn('sensitive-token', str(raised.exception))

    def test_channel_exists_requires_exact_name_match(self) -> None:
        self.service._request_new_api_json = Mock(
            return_value={
                "success": True,
                "data": [
                    {"name": "user@example.com-copy"},
                    {"name": "user@example.com"},
                ],
            }
        )

        exists = self.service._channel_exists(self.config, "user@example.com")

        self.assertTrue(exists)
        requested_url = self.service._request_new_api_json.call_args.args[1]
        self.assertIn("api/channel/search?", requested_url)
        self.assertIn("keyword=user%40example.com", requested_url)

    def test_channel_exists_rejects_fuzzy_match(self) -> None:
        self.service._request_new_api_json = Mock(
            return_value={"success": True, "data": [{"name": "user@example.com-copy"}]}
        )

        self.assertFalse(self.service._channel_exists(self.config, "user@example.com"))

    def test_existing_channel_skips_key_read_and_post(self) -> None:
        env = SimpleNamespace(
            id="env-1",
            authorized_account="user@example.com",
            google_account_hint="user@example.com",
        )
        task = SimpleNamespace(id="task-1", socks5_url="socks5://example")
        self.service.db = SimpleNamespace(get=Mock(return_value=env))
        self.service.settings = SimpleNamespace(
            new_api_channel_default_template="default",
        )
        self.service._get_latest_success_task = Mock(return_value=task)
        self.service._load_push_config = Mock(return_value=self.config)
        self.service._require_base_url = Mock()
        self.service._find_channel = Mock(
            return_value={"id": 42, "name": "user@example.com", "status": 1}
        )
        self.service._load_template = Mock(side_effect=AssertionError("不应读取渠道模板"))
        self.service._post_new_api_channel = Mock(side_effect=AssertionError("不应重复推送"))

        result = self.service.push_env_channel("env-1")

        self.assertTrue(result.success)
        self.assertEqual(result.channel_name, "user@example.com")
        self.assertIn("跳过推送", result.message)
        self.service._load_template.assert_not_called()
        self.service._post_new_api_channel.assert_not_called()

    def test_existing_disabled_channel_is_tested_and_reenabled_without_duplicate_push(self) -> None:
        env = SimpleNamespace(
            id="env-disabled",
            authorized_account="user@example.com",
            google_account_hint="user@example.com",
        )
        task = SimpleNamespace(id="task-disabled", socks5_url="socks5://example")
        self.service.db = SimpleNamespace(get=Mock(return_value=env))
        self.service.settings = SimpleNamespace(new_api_channel_default_template="default")
        self.service._get_latest_success_task = Mock(return_value=task)
        self.service._load_push_config = Mock(return_value=self.config)
        self.service._require_base_url = Mock()
        self.service._find_channel = Mock(
            return_value={"id": 43, "name": "user@example.com", "status": 2}
        )
        self.service._test_new_api_channel = Mock()
        self.service._enable_new_api_channel = Mock()
        self.service._load_template = Mock(side_effect=AssertionError("不应创建重复渠道"))
        self.service._post_new_api_channel = Mock(side_effect=AssertionError("不应创建重复渠道"))

        result = self.service.push_env_channel("env-disabled")

        self.assertTrue(result.success)
        self.assertIn("重新启用", result.message)
        self.service._test_new_api_channel.assert_called_once_with(self.config, "43")
        self.service._enable_new_api_channel.assert_called_once_with(self.config, "43")
        self.service._load_template.assert_not_called()
        self.service._post_new_api_channel.assert_not_called()

    def test_resolve_created_channel_id_falls_back_to_exact_name_search(self) -> None:
        self.service._find_channel = Mock(return_value={"id": 123, "name": "user@example.com"})

        channel_id = self.service._resolve_created_channel_id(
            self.config,
            {"success": True, "data": True},
            "user@example.com",
        )

        self.assertEqual(channel_id, "123")
        self.service._find_channel.assert_called_once_with(self.config, "user@example.com")

    def test_channel_is_tested_before_it_is_enabled(self) -> None:
        self.service._request_new_api_json = Mock(
            side_effect=[
                {"message": "", "success": True, "time": 2.262},
                {"data": True, "message": "", "success": True},
            ]
        )

        self.service._test_new_api_channel(self.config, "42")
        self.service._enable_new_api_channel(self.config, "42")

        test_call = self.service._request_new_api_json.call_args_list[0]
        self.assertEqual(test_call.args[1], "https://new-api.example/api/channel/test/42")
        self.assertEqual(test_call.kwargs, {"method": "GET"})
        enable_call = self.service._request_new_api_json.call_args_list[1]
        self.assertEqual(enable_call.args[1], "https://new-api.example/api/channel/42/status")
        self.assertEqual(enable_call.kwargs["method"], "POST")
        self.assertEqual(json.loads(enable_call.kwargs["body"].decode("utf-8")), {"status": 1})

    def test_new_channel_payload_is_forced_enabled(self) -> None:
        payload = {"channel": {"name": "test", "status": 2}}

        self.service._normalize_new_api_payload(payload)

        self.assertEqual(payload["channel"]["status"], 1)

    def test_failed_new_channel_test_attempts_to_disable_channel(self) -> None:
        self.service._test_new_api_channel = Mock(side_effect=RuntimeError("connection failed"))
        self.service._disable_new_api_channel = Mock()

        with self.assertRaisesRegex(RuntimeError, "已将渠道禁用"):
            self.service._test_created_enabled_channel(self.config, "42")

        self.service._disable_new_api_channel.assert_called_once_with(self.config, "42")

    def test_disable_channel_sets_status_two(self) -> None:
        self.service._request_new_api_json = Mock(
            return_value={"data": True, "message": "", "success": True}
        )

        self.service._disable_new_api_channel(self.config, "42")

        disable_call = self.service._request_new_api_json.call_args
        self.assertEqual(disable_call.args[1], "https://new-api.example/api/channel/42/status")
        self.assertEqual(disable_call.kwargs["method"], "POST")
        self.assertEqual(json.loads(disable_call.kwargs["body"].decode("utf-8")), {"status": 2})

    def test_failed_channel_test_does_not_enable_channel(self) -> None:
        self.service._request_new_api_json = Mock(
            return_value={"message": "connection failed", "success": False}
        )

        with self.assertRaisesRegex(RuntimeError, "渠道测试失败"):
            self.service._test_new_api_channel(self.config, "42")

        self.assertEqual(self.service._request_new_api_json.call_count, 1)

    def test_enable_channel_retries_three_times_with_two_second_delay(self) -> None:
        self.service._request_new_api_json = Mock(
            side_effect=[
                {"data": False, "message": "not ready", "success": False},
                RuntimeError("temporary failure"),
                {"data": True, "message": "", "success": True},
            ]
        )

        with patch("app.services.channel_push_service.time.sleep") as sleep:
            self.service._enable_new_api_channel(self.config, "42")

        self.assertEqual(self.service._request_new_api_json.call_count, 3)
        self.assertEqual([item.args for item in sleep.call_args_list], [(2,), (2,)])
