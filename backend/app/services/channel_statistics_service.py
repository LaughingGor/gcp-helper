import logging
import hashlib
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlencode, urljoin

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.channel_statistics import (
    ChannelStatisticItem,
    ChannelStatisticsResponse,
    DisabledChannelStatistics,
    EnabledChannelStatistics,
)
from app.services.channel_push_service import ChannelPushService, NewApiChannelConfig

logger = logging.getLogger(__name__)
MONEY_QUANTUM = Decimal("0.01")
_cache_lock = threading.RLock()
_statistics_cache: dict[tuple[str, ...], tuple[float, ChannelStatisticsResponse]] = {}


class ChannelStatisticsService:
    def __init__(self, db: Session, *, user_id: str, target_id: str | None = None) -> None:
        self.db = db
        self.user_id = user_id
        self.settings = get_settings()
        self.target_id = target_id
        self.channel_service = ChannelPushService(db, user_id=user_id, target_id=target_id)

    def get_statistics(self, *, refresh: bool = False) -> ChannelStatisticsResponse:
        from app.services.new_api_target_service import resolve_target
        target = resolve_target(self.db, self.user_id, self.target_id)
        selected_group = (target.stats_group or target.group).strip()
        if not selected_group:
            raise ValueError("渠道分组不能为空")
        if target.quota_per_unit <= 0:
            raise ValueError("目标后台的 quota 换算比例必须大于 0")
        if self.settings.new_api_channel_budget_amount <= 0:
            raise RuntimeError("NEW_API_CHANNEL_BUDGET_AMOUNT 必须大于 0")

        config = self.channel_service._load_push_config()
        cache_key = (self.user_id, target.id, selected_group, str(target.quota_per_unit), target.currency, str(self.settings.new_api_channel_budget_amount),
                     hashlib.sha256(config.token.encode()).hexdigest())
        if not refresh:
            cached = self._get_cached(cache_key)
            if cached:
                return cached

        channels, partial, warnings = self._fetch_all_channels(config)
        result = self.calculate(
            channels,
            group=selected_group,
            quota_per_unit=target.quota_per_unit,
            max_amount=self.settings.new_api_channel_budget_amount,
            currency=target.currency,
            partial=partial,
            warnings=warnings,
        )
        with _cache_lock:
            _statistics_cache[cache_key] = (time.monotonic(), result)
        return result

    def _get_cached(self, cache_key: tuple[str, ...]) -> ChannelStatisticsResponse | None:
        with _cache_lock:
            cached = _statistics_cache.get(cache_key)
            if not cached:
                return None
            cached_at, result = cached
            if time.monotonic() - cached_at > max(0, self.settings.new_api_channel_stats_cache_seconds):
                _statistics_cache.pop(cache_key, None)
                return None
            return result

    def _fetch_all_channels(
        self,
        config: NewApiChannelConfig,
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        page_size = max(1, min(self.settings.new_api_channel_stats_page_size, 500))
        max_pages = max(1, self.settings.new_api_channel_stats_max_pages)
        channels_by_id: dict[str, dict[str, Any]] = {}
        partial = False
        warnings: list[str] = []

        # new-api 的 p 参数从 1 开始；p=0 会被部分版本归一为第一页，
        # 随后请求 p=1 时再次返回相同数据，导致误判为重复页。
        for page_index in range(max_pages):
            page = page_index + 1
            query = urlencode({"p": page, "page_size": page_size})
            url = urljoin(config.base_url.rstrip("/") + "/", f"api/channel/?{query}")
            try:
                response = self.channel_service._request_new_api_json(config, url, method="GET")
            except Exception as exc:
                if page_index == 0:
                    raise RuntimeError(f"读取 new-api 渠道列表失败: {exc}") from exc
                partial = True
                warnings.append(f"第 {page} 页渠道读取失败，当前结果可能不完整")
                logger.warning("channel statistics page failed user_id=%s page=%s error=%s", self.user_id, page, exc)
                break

            if response.get("success") is False:
                message = str(response.get("message") or response)
                if page_index == 0:
                    raise RuntimeError(f"读取 new-api 渠道列表失败: {message}")
                partial = True
                warnings.append(f"第 {page} 页返回失败，当前结果可能不完整")
                break

            items, total = self._extract_channel_page(response)
            if not items:
                break
            previous_count = len(channels_by_id)
            for index, channel in enumerate(items):
                if not isinstance(channel, dict):
                    continue
                channel_id = self._channel_id(channel) or f"page-{page}-item-{index}"
                channels_by_id[channel_id] = channel

            if total is not None:
                if len(channels_by_id) >= total:
                    break
            if page_index > 0 and len(channels_by_id) == previous_count:
                partial = True
                warnings.append("new-api 分页未继续前进，已停止读取以避免重复请求")
                break
        else:
            partial = True
            warnings.append(f"渠道数量超过最大读取页数 {max_pages}，当前结果可能不完整")

        return list(channels_by_id.values()), partial, warnings

    @classmethod
    def _extract_channel_page(cls, response: dict[str, Any]) -> tuple[list[Any], int | None]:
        data = response.get("data")
        total_candidates = [response.get("total"), response.get("total_count")]
        if isinstance(data, list):
            return data, cls._parse_int(next((value for value in total_candidates if value is not None), None))
        if not isinstance(data, dict):
            return [], None

        items = data.get("items") or data.get("channels") or data.get("data") or []
        total_candidates = [data.get("total"), data.get("total_count"), *total_candidates]
        total = cls._parse_int(next((value for value in total_candidates if value is not None), None))
        return items if isinstance(items, list) else [], total

    @classmethod
    def calculate(
        cls,
        channels: list[dict[str, Any]],
        *,
        group: str,
        quota_per_unit: Decimal,
        max_amount: Decimal,
        currency: str,
        partial: bool = False,
        warnings: list[str] | None = None,
    ) -> ChannelStatisticsResponse:
        items: list[ChannelStatisticItem] = []
        disabled_amounts: list[Decimal] = []
        enabled_amounts: list[Decimal] = []
        enabled_remaining: list[Decimal] = []
        enabled_over: list[Decimal] = []
        disabled_count = manual_disabled_count = auto_disabled_count = disabled_invalid_count = 0
        enabled_count = within_limit_count = exhausted_count = over_limit_count = enabled_invalid_count = 0
        unknown_status_count = 0

        for channel in channels:
            groups = cls._groups(channel.get("group"))
            if group not in groups:
                continue
            channel_id = cls._channel_id(channel)
            name = str(channel.get("name") or channel_id or "未命名渠道")
            status = cls._parse_int(channel.get("status"))
            channel_type = cls._parse_int(channel.get("type"))
            used_quota, invalid_reason = cls._parse_quota(channel.get("used_quota", channel.get("usedQuota")))
            used_amount = used_quota / quota_per_unit if used_quota is not None else None
            remaining_amount: Decimal | None = None
            over_amount: Decimal | None = None

            if status in (2, 3):
                classification = "DISABLED" if used_amount is not None else "DATA_INVALID"
                disabled_count += 1
                manual_disabled_count += int(status == 2)
                auto_disabled_count += int(status == 3)
                if used_amount is None:
                    disabled_invalid_count += 1
                else:
                    disabled_amounts.append(used_amount)
            elif status == 1:
                enabled_count += 1
                if used_amount is None:
                    classification = "DATA_INVALID"
                    enabled_invalid_count += 1
                elif used_amount > max_amount:
                    enabled_amounts.append(used_amount)
                    classification = "OVER_LIMIT"
                    over_limit_count += 1
                    over_amount = used_amount - max_amount
                    enabled_over.append(over_amount)
                elif used_amount == max_amount:
                    enabled_amounts.append(used_amount)
                    classification = "EXHAUSTED"
                    exhausted_count += 1
                    remaining_amount = Decimal(0)
                    enabled_remaining.append(remaining_amount)
                else:
                    enabled_amounts.append(used_amount)
                    classification = "WITHIN_LIMIT"
                    within_limit_count += 1
                    remaining_amount = max_amount - used_amount
                    enabled_remaining.append(remaining_amount)
            else:
                classification = "UNKNOWN_STATUS"
                unknown_status_count += 1

            items.append(
                ChannelStatisticItem(
                    channelId=channel_id,
                    name=name,
                    channelType=channel_type,
                    groups=groups,
                    status=status,
                    statusLabel=cls._status_label(status),
                    usedQuota=used_quota,
                    usedAmount=cls._money(used_amount) if used_amount is not None else None,
                    maxAmount=cls._money(max_amount),
                    remainingAmount=cls._money(remaining_amount) if remaining_amount is not None else None,
                    overAmount=cls._money(over_amount) if over_amount is not None else None,
                    classification=classification,
                    invalidReason=invalid_reason,
                )
            )

        disabled_total = sum(disabled_amounts, Decimal(0))
        enabled_total = sum(enabled_amounts, Decimal(0))
        remaining_total = sum(enabled_remaining, Decimal(0))
        over_total = sum(enabled_over, Decimal(0))
        items.sort(key=lambda item: (item.used_amount is None, -(item.used_amount or Decimal(0)), item.name))
        return ChannelStatisticsResponse(
            group=group,
            currency=currency.strip().upper() or "USD",
            quotaPerUnit=quota_per_unit,
            maxAmountPerChannel=cls._money(max_amount),
            generatedAt=datetime.utcnow(),
            partial=partial,
            matchedChannelCount=len(items),
            unknownStatusCount=unknown_status_count,
            totalConsumedAmount=cls._money(disabled_total + enabled_total),
            disabled=DisabledChannelStatistics(
                channelCount=disabled_count,
                validCount=len(disabled_amounts),
                invalidCount=disabled_invalid_count,
                manualDisabledCount=manual_disabled_count,
                autoDisabledCount=auto_disabled_count,
                totalConsumedAmount=cls._money(disabled_total),
                averageConsumedAmount=(cls._money(disabled_total / len(disabled_amounts)) if disabled_amounts else None),
            ),
            enabled=EnabledChannelStatistics(
                channelCount=enabled_count,
                totalConsumedAmount=cls._money(enabled_total),
                withinLimitCount=within_limit_count,
                exhaustedCount=exhausted_count,
                overLimitCount=over_limit_count,
                invalidCount=enabled_invalid_count,
                totalRemainingAmount=cls._money(remaining_total),
                averageRemainingAmount=(cls._money(remaining_total / len(enabled_remaining)) if enabled_remaining else None),
                totalOverAmount=cls._money(over_total),
            ),
            channels=items,
            warnings=warnings or [],
        )

    @staticmethod
    def _groups(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    @staticmethod
    def _channel_id(channel: dict[str, Any]) -> str:
        value = channel.get("id")
        return "" if value is None or isinstance(value, bool) else str(value).strip()

    @staticmethod
    def _parse_quota(value: Any) -> tuple[Decimal | None, str | None]:
        if value is None or isinstance(value, bool):
            return None, "缺少 used_quota"
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None, "used_quota 格式无效"
        if not parsed.is_finite() or parsed < 0:
            return None, "used_quota 不能为负数或非有限值"
        return parsed, None

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return int(value) if isinstance(value, bool) else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _status_label(status: int | None) -> str:
        return {1: "启用", 2: "手动禁用", 3: "自动禁用"}.get(status, "未知状态")

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
