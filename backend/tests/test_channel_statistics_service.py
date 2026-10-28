from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from app.services.channel_statistics_service import ChannelStatisticsService


class ChannelStatisticsServiceTest(TestCase):
    def test_calculates_disabled_average_and_enabled_remaining(self) -> None:
        channels = [
            {"id": 1, "name": "disabled-a", "group": "Gemini_Vertex_Pro", "status": 2, "used_quota": 100},
            {"id": 2, "name": "disabled-b", "group": "Gemini_Vertex_1,Gemini_Vertex_Pro", "status": 3, "used_quota": 200},
            {"id": 3, "name": "enabled-a", "group": "Gemini_Vertex_Pro", "status": 1, "used_quota": 120},
            {"id": 4, "name": "enabled-full", "group": "Gemini_Vertex_Pro", "status": 1, "used_quota": 300},
            {"id": 5, "name": "enabled-over", "group": "Gemini_Vertex_Pro", "status": 1, "used_quota": 320},
            {"id": 6, "name": "other-group", "group": "Gemini_Vertex_1", "status": 1, "used_quota": 10},
        ]

        result = ChannelStatisticsService.calculate(
            channels,
            group="Gemini_Vertex_Pro",
            quota_per_unit=Decimal(1),
            max_amount=Decimal(300),
            currency="USD",
        )

        self.assertEqual(result.matched_channel_count, 5)
        self.assertEqual(result.total_consumed_amount, Decimal("1040.00"))
        self.assertEqual(result.disabled.total_consumed_amount, Decimal("300.00"))
        self.assertEqual(result.disabled.average_consumed_amount, Decimal("150.00"))
        self.assertEqual(result.disabled.manual_disabled_count, 1)
        self.assertEqual(result.disabled.auto_disabled_count, 1)
        self.assertEqual(result.enabled.within_limit_count, 1)
        self.assertEqual(result.enabled.exhausted_count, 1)
        self.assertEqual(result.enabled.over_limit_count, 1)
        self.assertEqual(result.enabled.total_consumed_amount, Decimal("740.00"))
        self.assertEqual(result.enabled.total_remaining_amount, Decimal("180.00"))
        self.assertEqual(result.enabled.average_remaining_amount, Decimal("90.00"))
        self.assertEqual(result.enabled.total_over_amount, Decimal("20.00"))

    def test_invalid_quota_is_counted_but_excluded_from_amounts(self) -> None:
        result = ChannelStatisticsService.calculate(
            [
                {"id": 1, "name": "disabled", "group": "Gemini_Vertex_Pro", "status": 2, "used_quota": None},
                {"id": 2, "name": "enabled", "group": "Gemini_Vertex_Pro", "status": 1, "used_quota": -1},
                {"id": 3, "name": "unknown", "group": "Gemini_Vertex_Pro", "status": 9, "used_quota": 10},
            ],
            group="Gemini_Vertex_Pro",
            quota_per_unit=Decimal(1),
            max_amount=Decimal(300),
            currency="USD",
        )

        self.assertEqual(result.disabled.invalid_count, 1)
        self.assertIsNone(result.disabled.average_consumed_amount)
        self.assertEqual(result.enabled.invalid_count, 1)
        self.assertIsNone(result.enabled.average_remaining_amount)
        self.assertEqual(result.unknown_status_count, 1)

    def test_quota_is_converted_to_money_before_limit_calculation(self) -> None:
        result = ChannelStatisticsService.calculate(
            [{"id": 1, "name": "channel", "group": "Gemini_Vertex_Pro", "status": 1, "used_quota": 150_000_000}],
            group="Gemini_Vertex_Pro",
            quota_per_unit=Decimal(500_000),
            max_amount=Decimal(300),
            currency="USD",
        )

        self.assertEqual(result.channels[0].used_amount, Decimal("300.00"))
        self.assertEqual(result.channels[0].classification, "EXHAUSTED")

    def test_fetches_paginated_channels_and_deduplicates_ids(self) -> None:
        service = ChannelStatisticsService.__new__(ChannelStatisticsService)
        service.user_id = "user-1"
        service.settings = SimpleNamespace(new_api_channel_stats_page_size=2, new_api_channel_stats_max_pages=5)
        service.channel_service = Mock()
        service.channel_service._request_new_api_json.side_effect = [
            {"success": True, "data": {"items": [{"id": 1}, {"id": 2}], "total": 3}},
            {"success": True, "data": {"items": [{"id": 2}, {"id": 3}], "total": 3}},
        ]
        config = SimpleNamespace(base_url="https://new-api.example")

        channels, partial, warnings = service._fetch_all_channels(config)

        self.assertEqual([item["id"] for item in channels], [1, 2, 3])
        self.assertFalse(partial)
        self.assertEqual(warnings, [])
        self.assertEqual(service.channel_service._request_new_api_json.call_count, 2)
        requested_pages = [
            parse_qs(urlsplit(call.args[1]).query)["p"][0]
            for call in service.channel_service._request_new_api_json.call_args_list
        ]
        self.assertEqual(requested_pages, ["1", "2"])

    def test_total_count_prevents_early_stop_when_server_caps_page_size(self) -> None:
        service = ChannelStatisticsService.__new__(ChannelStatisticsService)
        service.user_id = "user-1"
        service.settings = SimpleNamespace(new_api_channel_stats_page_size=100, new_api_channel_stats_max_pages=5)
        service.channel_service = Mock()
        service.channel_service._request_new_api_json.side_effect = [
            {"success": True, "data": {"items": [{"id": 1}], "total": 2}},
            {"success": True, "data": {"items": [{"id": 2}], "total": 2}},
        ]

        channels, partial, _ = service._fetch_all_channels(SimpleNamespace(base_url="https://new-api.example"))

        self.assertEqual([item["id"] for item in channels], [1, 2])
        self.assertFalse(partial)
        self.assertEqual(service.channel_service._request_new_api_json.call_count, 2)

    def test_without_total_keeps_reading_until_empty_page(self) -> None:
        service = ChannelStatisticsService.__new__(ChannelStatisticsService)
        service.user_id = "user-1"
        service.settings = SimpleNamespace(new_api_channel_stats_page_size=100, new_api_channel_stats_max_pages=5)
        service.channel_service = Mock()
        service.channel_service._request_new_api_json.side_effect = [
            {"success": True, "data": {"items": [{"id": 1}]}},
            {"success": True, "data": {"items": [{"id": 2}]}},
            {"success": True, "data": {"items": []}},
        ]

        channels, partial, warnings = service._fetch_all_channels(
            SimpleNamespace(base_url="https://new-api.example")
        )

        self.assertEqual([item["id"] for item in channels], [1, 2])
        self.assertFalse(partial)
        self.assertEqual(warnings, [])
        self.assertEqual(service.channel_service._request_new_api_json.call_count, 3)
        requested_pages = [
            parse_qs(urlsplit(call.args[1]).query)["p"][0]
            for call in service.channel_service._request_new_api_json.call_args_list
        ]
        self.assertEqual(requested_pages, ["1", "2", "3"])
