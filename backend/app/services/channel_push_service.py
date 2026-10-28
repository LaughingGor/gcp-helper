import copy
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.channel_push_record import ChannelPushAction
from app.models.gcp_environment import GcpEnvironment
from app.models.gcp_preset_task import GcpPresetTask, PresetTaskStatus, PresetTaskType
from app.services.preset_task_service import PresetTaskService

logger = logging.getLogger(__name__)


@dataclass
class ChannelPushResult:
    success: bool
    message: str
    env_id: str
    task_id: str
    channel_name: str
    template_name: str
    channel_id: str
    action: ChannelPushAction


@dataclass
class NewApiChannelConfig:
    base_url: str
    token: str = field(default="", repr=False)
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"


class ChannelPushService:
    stringified_fields = {"setting", "settings", "other"}

    def __init__(self, db: Session, *, user_id: str | None = None, target_id: str | None = None) -> None:
        self.db = db
        self.user_id = user_id
        self.settings = get_settings()
        self.target_id = target_id

    def push_env_channel(self, env_id: str, template_name: str | None = None) -> ChannelPushResult:
        env = self.db.get(GcpEnvironment, env_id)
        if not env:
            raise KeyError("环境不存在")

        task = self._get_latest_success_task(env_id)
        if not task:
            raise RuntimeError("没有可推送的成功预设任务")
        if not task.socks5_url or task.socks5_url == "-":
            raise RuntimeError("SOCKS5 URL 不存在，不能推送渠道")

        selected_template = template_name or self.settings.new_api_channel_default_template
        channel_name = env.authorized_account or env.google_account_hint
        config = self._load_push_config()
        self._require_base_url(config)
        existing_channel = self._find_channel(config, channel_name)
        if existing_channel:
            channel_id = self._normalize_channel_id(existing_channel.get("id"))
            if not channel_id:
                raise RuntimeError(f"渠道已存在，但无法获取渠道 ID：{channel_name}")
            if self._is_channel_enabled(existing_channel):
                action = ChannelPushAction.ALREADY_EXISTS
                message = f"渠道已存在且处于启用状态，跳过推送：{channel_name}"
                logger.info(
                    "skip enabled existing new-api channel env_id=%s task_id=%s channel_name=%s channel_id=%s",
                    env.id,
                    task.id,
                    channel_name,
                    channel_id,
                )
            else:
                action = ChannelPushAction.REACTIVATED
                logger.info(
                    "reactivate disabled existing new-api channel env_id=%s task_id=%s channel_name=%s channel_id=%s status=%s",
                    env.id,
                    task.id,
                    channel_name,
                    channel_id,
                    existing_channel.get("status"),
                )
                self._test_new_api_channel(config, channel_id)
                self._enable_new_api_channel(config, channel_id)
                message = f"渠道已存在，测试成功并重新启用：{channel_name}"
            return ChannelPushResult(
                success=True,
                message=message,
                env_id=env.id,
                task_id=task.id,
                channel_name=channel_name,
                template_name=selected_template,
                channel_id=channel_id,
                action=action,
            )

        key_path = PresetTaskService(self.db).get_key_file_path(env.id, task.id)
        key_content = key_path.read_text(encoding="utf-8")
        self._validate_service_account_json(key_content, key_path)

        template = self._load_template(selected_template)
        payload = self._render_payload(
            template,
            {
                "google_account": channel_name,
                "service_account_json": key_content,
                "socks5_url": task.socks5_url,
                "project_id": task.project_id or "",
                "service_account_email": task.service_account_email or "",
                "env_name": env.env_name,
            },
        )
        self._normalize_new_api_payload(payload)

        logger.info(
            "push new-api channel start env_id=%s task_id=%s template=%s channel_name=%s",
            env.id,
            task.id,
            selected_template,
            channel_name,
        )
        response = self._post_new_api_channel(config, payload)
        success = bool(response.get("success"))
        message = str(response.get("message") or "")
        if not success:
            raise RuntimeError(f"new-api 推送失败: {message or response}")

        channel_id = self._resolve_created_channel_id(config, response, channel_name)
        self._test_created_enabled_channel(config, channel_id)
        message = message or f"渠道已按启用状态创建并测试通过：{channel_name}"

        logger.info(
            "push new-api channel success env_id=%s task_id=%s template=%s channel_name=%s channel_id=%s",
            env.id,
            task.id,
            selected_template,
            channel_name,
            channel_id,
        )
        return ChannelPushResult(
            success=True,
            message=message,
            env_id=env.id,
            task_id=task.id,
            channel_name=channel_name,
            template_name=selected_template,
            channel_id=channel_id,
            action=ChannelPushAction.CREATED,
        )

    def validate_config(self, config: NewApiChannelConfig) -> None:
        self._require_base_url(config)
        if not config.token:
            raise RuntimeError("渠道 Access Token 未配置")
        url = urljoin(config.base_url.rstrip("/") + "/", "api/channel/?p=1&page_size=1")
        try:
            response = self._request_new_api_json(config, url, method="GET")
        except TimeoutError:
            raise RuntimeError("Access Token 验证超时，请检查后台连接后重试") from None
        except (ValueError, OSError):
            raise RuntimeError("Access Token 验证请求失败，请检查后台地址和连接配置") from None
        if not isinstance(response, dict) or response.get("success") is not True:
            raise RuntimeError("Access Token 验证失败：后台未允许读取渠道列表，请检查 Token 及渠道查看权限")
        data = response.get("data")
        valid_list = isinstance(data, list) or (
            isinstance(data, dict) and any(isinstance(data.get(key), list) for key in ("items", "channels", "data"))
        )
        if not valid_list:
            raise RuntimeError("Access Token 验证失败：后台返回的渠道列表格式不正确，请检查后台地址")

    def _get_latest_success_task(self, env_id: str) -> GcpPresetTask | None:
        from app.models.project_replacement import ProjectReplacement
        return self.db.scalars(
            select(GcpPresetTask)
            .where(
                GcpPresetTask.env_id == env_id,
                GcpPresetTask.task_type == PresetTaskType.BOOTSTRAP_PROJECT,
                GcpPresetTask.status == PresetTaskStatus.SUCCESS,
                (GcpPresetTask.force_new_project.is_(False) | GcpPresetTask.id.in_(
                    select(ProjectReplacement.task_id).where(ProjectReplacement.status == "SUCCESS"))),
            )
            .order_by(GcpPresetTask.finished_at.desc(), GcpPresetTask.created_at.desc())
            .limit(1)
        ).first()

    def _load_template(self, template_name: str) -> dict[str, Any]:
        if "/" in template_name or "\\" in template_name or ".." in template_name:
            raise RuntimeError("模板名称不合法")
        path = (self.settings.new_api_channel_template_dir / f"{template_name}.json").resolve()
        root = self.settings.new_api_channel_template_dir.resolve()
        if not path.is_relative_to(root):
            raise RuntimeError("模板路径不合法")
        if not path.is_file():
            raise FileNotFoundError(f"渠道模板不存在: {template_name}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"渠道模板 JSON 格式错误: {template_name}") from exc

    def _render_payload(self, template: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
        return self._render_value(copy.deepcopy(template), values)

    def _render_value(self, value: Any, values: dict[str, str]) -> Any:
        if isinstance(value, str):
            rendered = value
            for key, replacement in values.items():
                rendered = rendered.replace(f"{{{{ {key} }}}}", replacement)
            return rendered
        if isinstance(value, list):
            return [self._render_value(item, values) for item in value]
        if isinstance(value, dict):
            return {key: self._render_value(item, values) for key, item in value.items()}
        return value

    def _normalize_new_api_payload(self, payload: dict[str, Any]) -> None:
        channel = payload.get("channel")
        if not isinstance(channel, dict):
            raise RuntimeError("渠道模板缺少 channel 对象")
        for field in self.stringified_fields:
            value = channel.get(field)
            if isinstance(value, (dict, list)):
                channel[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        channel["status"] = 1

    def _channel_exists(self, config: NewApiChannelConfig, channel_name: str) -> bool:
        return self._find_channel(config, channel_name) is not None

    def _find_channel(self, config: NewApiChannelConfig, channel_name: str) -> dict[str, Any] | None:
        query = urlencode({"keyword": channel_name})
        url = urljoin(config.base_url.rstrip("/") + "/", f"api/channel/search?{query}")
        response = self._request_new_api_json(config, url, method="GET")
        if response.get("success") is False:
            raise RuntimeError(f"new-api 查询渠道失败: {response.get('message') or response}")

        channels = self._extract_search_channels(response)
        if not isinstance(channels, list):
            raise RuntimeError(f"new-api 查询渠道返回格式不正确: {response}")

        expected_name = channel_name.strip()
        return next(
            (
                channel
                for channel in channels
                if isinstance(channel, dict)
                and str(channel.get("name") or "").strip() == expected_name
            ),
            None,
        )

    @staticmethod
    def _extract_search_channels(response: dict[str, Any]) -> Any:
        data = response.get("data")
        if isinstance(data, dict):
            if "id" in data and "name" in data:
                return [data]
            return data.get("items") or data.get("channels") or data.get("data") or []
        return data or []

    def _post_new_api_channel(self, config: NewApiChannelConfig, payload: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(config.base_url.rstrip("/") + "/", "api/channel/")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request_new_api_json(config, url, method="POST", body=body)

    def _resolve_created_channel_id(
        self,
        config: NewApiChannelConfig,
        create_response: dict[str, Any],
        channel_name: str,
    ) -> str:
        channel_id = self._extract_channel_id(create_response)
        if channel_id:
            return channel_id

        # 部分 new-api 版本创建接口只返回 data=true，创建后按精确名称查询 ID。
        channel = self._find_channel(config, channel_name)
        channel_id = self._normalize_channel_id(channel.get("id") if channel else None)
        if not channel_id:
            raise RuntimeError(f"渠道推送成功，但无法获取渠道 ID：{channel_name}")
        return channel_id

    @classmethod
    def _extract_channel_id(cls, response: dict[str, Any]) -> str:
        data = response.get("data")
        candidates: list[Any] = [response.get("id")]
        if isinstance(data, dict):
            candidates.append(data.get("id"))
            channel = data.get("channel")
            if isinstance(channel, dict):
                candidates.append(channel.get("id"))
        else:
            candidates.append(data)
        return next((value for item in candidates if (value := cls._normalize_channel_id(item))), "")

    @staticmethod
    def _normalize_channel_id(value: Any) -> str:
        if value is None or isinstance(value, bool):
            return ""
        return str(value).strip()

    @staticmethod
    def _is_channel_enabled(channel: dict[str, Any]) -> bool:
        status = channel.get("status")
        if isinstance(status, bool):
            return status
        return str(status).strip() == "1"

    def _test_new_api_channel(self, config: NewApiChannelConfig, channel_id: str) -> None:
        encoded_id = quote(channel_id, safe="")
        url = urljoin(config.base_url.rstrip("/") + "/", f"api/channel/test/{encoded_id}")
        logger.info("test new-api channel start channel_id=%s", channel_id)
        response = self._request_new_api_json(config, url, method="GET")
        if response.get("success") is not True:
            raise RuntimeError(f"渠道测试失败: {response.get('message') or response}")
        logger.info("test new-api channel success channel_id=%s", channel_id)

    def _test_created_enabled_channel(self, config: NewApiChannelConfig, channel_id: str) -> None:
        try:
            self._test_new_api_channel(config, channel_id)
        except Exception as test_error:
            logger.warning(
                "newly created enabled channel test failed, disabling channel channel_id=%s error=%s",
                channel_id,
                test_error,
            )
            try:
                self._disable_new_api_channel(config, channel_id)
            except Exception as disable_error:
                raise RuntimeError(
                    f"渠道测试失败，且尝试禁用渠道失败: test={test_error}; disable={disable_error}"
                ) from test_error
            raise RuntimeError(f"渠道测试失败，已将渠道禁用: {test_error}") from test_error

    def _disable_new_api_channel(self, config: NewApiChannelConfig, channel_id: str) -> None:
        encoded_id = quote(channel_id, safe="")
        url = urljoin(config.base_url.rstrip("/") + "/", f"api/channel/{encoded_id}/status")
        body = json.dumps({"status": 2}).encode("utf-8")
        logger.info("disable new-api channel start channel_id=%s", channel_id)
        response = self._request_new_api_json(config, url, method="POST", body=body)
        if response.get("success") is not True or response.get("data") is not True:
            raise RuntimeError(f"禁用渠道失败: {response.get('message') or response}")
        logger.info("disable new-api channel success channel_id=%s", channel_id)

    def _enable_new_api_channel(self, config: NewApiChannelConfig, channel_id: str) -> None:
        encoded_id = quote(channel_id, safe="")
        url = urljoin(config.base_url.rstrip("/") + "/", f"api/channel/{encoded_id}/status")
        body = json.dumps({"status": 1}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, 4):
            logger.info("enable new-api channel start channel_id=%s attempt=%s", channel_id, attempt)
            try:
                response = self._request_new_api_json(config, url, method="POST", body=body)
                if response.get("success") is True and response.get("data") is True:
                    logger.info("enable new-api channel success channel_id=%s attempt=%s", channel_id, attempt)
                    return
                last_error = RuntimeError(f"启用渠道失败: {response.get('message') or response}")
            except Exception as exc:
                last_error = exc

            if attempt < 3:
                logger.warning(
                    "enable new-api channel retry channel_id=%s attempt=%s delay=2s error=%s",
                    channel_id,
                    attempt,
                    last_error,
                )
                time.sleep(2)

        raise RuntimeError(f"启用渠道失败，已尝试 3 次: {last_error}") from last_error

    def _request_new_api_json(
        self,
        config: NewApiChannelConfig,
        url: str,
        *,
        method: str,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if config.token:
            headers[config.auth_header] = f"{config.auth_scheme} {config.token}".strip()

        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = self._perform_json_request(request)
            if isinstance(response, dict) and response.get("success") is False:
                response["message"] = "后台拒绝操作，请管理员检查后台日志"
            return response
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"后台管理 Token 无效或权限不足，HTTP {exc.code}") from None
            raise RuntimeError(f"new-api HTTP {exc.code}，请管理员检查后台日志") from None
        except URLError as exc:
            raise RuntimeError("new-api 网络请求失败，请管理员检查连接配置") from None

    @staticmethod
    def _perform_json_request(request: Request) -> dict[str, Any]:
        with urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
        try:
            return json.loads(response_body) if response_body else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("new-api 返回不是有效 JSON，请管理员检查后台日志") from None

    def _require_base_url(self, config: NewApiChannelConfig) -> None:
        if not config.base_url:
            raise RuntimeError("未配置渠道推送地址，不能推送渠道")

    def _load_push_config(self) -> NewApiChannelConfig:
        from app.services.new_api_target_service import resolve_target
        from app.services.secret_service import SecretService
        target = resolve_target(self.db, self.user_id, self.target_id)
        return NewApiChannelConfig(
            base_url=target.base_url,
            token=SecretService().decrypt_optional(target.token_encrypted) or "",
        )

    @staticmethod
    def _validate_service_account_json(value: str, path: Path) -> None:
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"JSON key 文件格式错误: {path.name}") from exc
        if data.get("type") != "service_account":
            raise RuntimeError(f"JSON key 文件不是 service_account 类型: {path.name}")
