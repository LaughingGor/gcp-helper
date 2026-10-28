import copy
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.schemas.channel_model import (
    AddChannelModelRequest,
    ChannelModelApplyResponse,
    ChannelModelCandidate,
    ChannelModelPreviewResponse,
    ChannelModelUpdateResult,
)
from app.services.channel_push_service import ChannelPushService, NewApiChannelConfig

logger = logging.getLogger(__name__)


@dataclass
class _ChannelModelChange:
    channel_id: str
    name: str
    current_models: str
    updated_models: str
    status: int | None
    channel: dict[str, Any]


class ChannelModelService:
    """Admin-only bulk model updates for the current user's new-api instance."""

    page_size = 100
    put_fields = (
        "id", "name", "type", "base_url", "openai_organization", "models", "group",
        "model_mapping", "priority", "weight", "test_model", "auto_ban", "status_code_mapping",
        "tag", "remark", "setting", "param_override", "header_override", "settings", "other",
    )

    def __init__(self, db: Session, *, user_id: str, actor_username: str, target_id: str | None = None) -> None:
        self.db = db
        self.actor_username = actor_username
        self.push_service = ChannelPushService(db, user_id=user_id, target_id=target_id)

    def preview(self, request: AddChannelModelRequest) -> ChannelModelPreviewResponse:
        changes, total_channels, already_present_count = self._changes(request)
        return ChannelModelPreviewResponse(
            group=request.group,
            status=request.status,
            model=request.model,
            totalChannels=total_channels,
            alreadyPresentCount=already_present_count,
            candidateCount=len(changes),
            candidates=[self._candidate(change) for change in changes],
        )

    def apply(self, request: AddChannelModelRequest) -> ChannelModelApplyResponse:
        # Re-read immediately before writing. Preview data is never trusted as a PUT payload.
        changes, _, _ = self._changes(request)
        config = self._config()
        results: list[ChannelModelUpdateResult] = []
        for change in changes:
            try:
                payload = self._make_put_payload(change.channel, change.updated_models)
                response = self._request(config, "PUT", "api/channel/", body=payload)
                if response.get("success") is not True:
                    raise RuntimeError(f"new-api 更新失败: {response.get('message') or response}")
                results.append(ChannelModelUpdateResult(
                    channelId=change.channel_id,
                    name=change.name,
                    status="SUCCESS",
                    message="模型已添加",
                ))
            except Exception as exc:
                logger.warning(
                    "channel model update failed channel_id=%s model=%s error=%s",
                    change.channel_id,
                    request.model,
                    exc,
                )
                results.append(ChannelModelUpdateResult(
                    channelId=change.channel_id,
                    name=change.name,
                    status="FAILED",
                    message=str(exc)[:1000],
                ))

        success_count = sum(item.status == "SUCCESS" for item in results)
        failed_count = sum(item.status == "FAILED" for item in results)
        self._audit(request, len(changes), success_count, failed_count)
        self.db.commit()
        return ChannelModelApplyResponse(
            group=request.group,
            status=request.status,
            model=request.model,
            requestedCount=len(changes),
            successCount=success_count,
            skippedCount=0,
            failedCount=failed_count,
            results=results,
        )

    def _changes(self, request: AddChannelModelRequest) -> tuple[list[_ChannelModelChange], int, int]:
        channels = self._list_channels(request.group, request.status)
        changes: list[_ChannelModelChange] = []
        already_present_count = 0
        for channel in channels:
            channel_id = self._channel_id(channel)
            if not channel_id:
                logger.warning("skip new-api channel without id while previewing model=%s", request.model)
                continue
            current_models = self._models(channel.get("models"))
            if request.model in self._model_set(current_models):
                already_present_count += 1
                continue
            changes.append(_ChannelModelChange(
                channel_id=channel_id,
                name=str(channel.get("name") or channel_id).strip() or channel_id,
                current_models=current_models,
                updated_models=self._append_model(current_models, request.model),
                status=self._parse_status(channel.get("status")),
                channel=channel,
            ))
        return changes, len(channels), already_present_count

    def _list_channels(self, group: str, status: str) -> list[dict[str, Any]]:
        config = self._config()
        channels: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 1
        max_pages = self.push_service.settings.new_api_channel_stats_max_pages
        while page <= max_pages:
            query: dict[str, str | int] = {
                "group": group,
                "tag_mode": "false",
                "id_sort": "false",
                "p": page,
                "page_size": self.page_size,
            }
            if status != "all":
                query["status"] = status
            response = self._request(config, "GET", "api/channel/", query=query)
            data = response.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("new-api 渠道列表返回格式不正确")
            items = data.get("items")
            if not isinstance(items, list):
                raise RuntimeError("new-api 渠道列表缺少 items")
            new_items = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                channel_id = self._channel_id(item)
                dedupe_key = channel_id or f"page-{page}-{len(channels)}"
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                channels.append(item)
                new_items += 1
            total = data.get("total")
            if isinstance(total, int) and len(channels) >= total:
                break
            if not items or new_items == 0 or len(items) < self.page_size:
                break
            page += 1
        else:
            raise RuntimeError(f"渠道分页超过最大页数 {max_pages}，请缩小筛选范围")
        return channels

    def _config(self) -> NewApiChannelConfig:
        config = self.push_service._load_push_config()
        self.push_service._require_base_url(config)
        return config

    def _request(
        self,
        config: NewApiChannelConfig,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = urljoin(config.base_url.rstrip("/") + "/", path)
        if query:
            url = f"{url}?{urlencode(query)}"
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        return self.push_service._request_new_api_json(config, url, method=method, body=encoded_body)

    def _make_put_payload(self, channel: dict[str, Any], updated_models: str) -> dict[str, Any]:
        payload = {field: copy.deepcopy(channel[field]) for field in self.put_fields if field in channel}
        if not self._channel_id(payload):
            raise RuntimeError("渠道缺少 id，不能更新")
        payload["models"] = updated_models
        return payload

    def _audit(self, request: AddChannelModelRequest, requested_count: int, success_count: int, failed_count: int) -> None:
        self.db.add(AuditLog(
            actor=self.actor_username,
            action="CHANNEL_MODEL_ADD",
            target_type="new_api_channel_model",
            detail_json=json.dumps({
                "group": request.group,
                "status": request.status,
                "model": request.model,
                "requestedCount": requested_count,
                "successCount": success_count,
                "failedCount": failed_count,
            }, ensure_ascii=False),
        ))

    @staticmethod
    def _candidate(change: _ChannelModelChange) -> ChannelModelCandidate:
        return ChannelModelCandidate(
            channelId=change.channel_id,
            name=change.name,
            status=change.status,
            currentModels=change.current_models,
            updatedModels=change.updated_models,
        )

    @staticmethod
    def _channel_id(channel: dict[str, Any]) -> str:
        value = channel.get("id")
        return "" if value is None or isinstance(value, bool) else str(value).strip()

    @staticmethod
    def _models(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _model_set(models: str) -> set[str]:
        return {item.strip() for item in models.split(",") if item.strip()}

    @staticmethod
    def _append_model(models: str, model: str) -> str:
        current = [item.strip() for item in models.split(",") if item.strip()]
        return ",".join([*current, model])

    @staticmethod
    def _parse_status(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
