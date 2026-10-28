#!/usr/bin/env python3
"""Add a model to matching New API channels.

The script is intentionally dry-run by default. Set NEW_API_TOKEN and pass
--execute to perform PUT requests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://10.0.0.4:6790"
DEFAULT_GROUP = "Gemini_Vertex_Pro"
DEFAULT_STATUS = "enabled"
DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_PAGE_SIZE = 100

# These are the editable fields used by the channel PUT endpoint. Read-only
# fields such as balance, status, quota, and channel_info are excluded.
PUT_FIELDS = (
    "id",
    "name",
    "type",
    "base_url",
    "openai_organization",
    "models",
    "group",
    "model_mapping",
    "priority",
    "weight",
    "test_model",
    "auto_ban",
    "status_code_mapping",
    "tag",
    "remark",
    "setting",
    "param_override",
    "header_override",
    "settings",
    "other",
)


class ApiError(RuntimeError):
    """An HTTP or API response error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add a model to disabled channels in a New API group."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("NEW_API_BASE_URL", DEFAULT_BASE_URL),
        help="New API base URL (default: NEW_API_BASE_URL or %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("NEW_API_TOKEN"),
        help="Bearer token (default: NEW_API_TOKEN)",
    )
    parser.add_argument(
        "--group",
        default=os.getenv("NEW_API_GROUP", DEFAULT_GROUP),
        help="Channel group to query (default: %(default)s)",
    )
    parser.add_argument(
        "--status",
        default=os.getenv("NEW_API_STATUS", DEFAULT_STATUS),
        help="Channel status filter (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("NEW_API_MODEL", DEFAULT_MODEL),
        help="Model to append (default: %(default)s)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Number of channels per query (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Seconds to wait between successful PUT requests (default: %(default)s)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually send PUT requests. Without this flag, only preview changes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Only relevant with --execute.",
    )
    return parser.parse_args()


def api_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/channel/"


def request_json(
    method: str,
    url: str,
    token: str,
    timeout: float,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if query:
        url = f"{url}?{urlencode(query)}"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace").strip()
        detail = f": {response_body[:500]}" if response_body else ""
        raise ApiError(f"{method} {url} returned HTTP {exc.code}{detail}") from exc
    except URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ApiError(f"{method} {url} returned a non-object JSON response")
    if result.get("success") is False:
        raise ApiError(
            f"{method} {url} returned success=false: {result.get('message', '')}"
        )
    return result


def fetch_channels(
    base_url: str,
    token: str,
    group: str,
    status: str,
    page_size: int,
    timeout: float,
) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    page = 1
    url = api_url(base_url)

    while True:
        response = request_json(
            "GET",
            url,
            token,
            timeout,
            query={
                "group": group,
                "status": status,
                "tag_mode": "false",
                "id_sort": "false",
                "p": page,
                "page_size": page_size,
            },
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApiError("GET response does not contain an object at data")
        items = data.get("items", [])
        if not isinstance(items, list):
            raise ApiError("GET response data.items is not a list")

        for item in items:
            if isinstance(item, dict):
                channels.append(item)

        if len(items) < page_size:
            break
        page += 1

    return channels


def has_model(models: Any, target_model: str) -> bool:
    if not isinstance(models, str):
        return False
    return target_model in {model.strip() for model in models.split(",") if model.strip()}


def append_model(models: Any, target_model: str) -> str:
    current = models.strip() if isinstance(models, str) else ""
    return f"{current},{target_model}" if current else target_model


def make_put_payload(channel: dict[str, Any], new_models: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in PUT_FIELDS:
        if field in channel:
            payload[field] = channel[field]
    payload["models"] = new_models
    return payload


def channel_label(channel: dict[str, Any]) -> str:
    channel_id = channel.get("id", "?")
    name = str(channel.get("name", "")).strip()
    return f"id={channel_id}" + (f", name={name}" if name else "")


def confirm(count: int, model: str) -> bool:
    answer = input(
        f"将更新 {count} 个渠道，为 models 添加 {model}。继续？[y/N] "
    ).strip().lower()
    return answer in {"y", "yes"}


def main() -> int:
    args = parse_args()
    args.model = args.model.strip()
    if args.page_size <= 0:
        print("--page-size must be greater than 0", file=sys.stderr)
        return 2
    if not args.model.strip():
        print("--model must not be empty", file=sys.stderr)
        return 2
    if not args.token:
        print("请先设置 NEW_API_TOKEN，或使用 --token 传入管理令牌。", file=sys.stderr)
        return 2

    try:
        channels = fetch_channels(
            args.base_url,
            args.token,
            args.group,
            args.status,
            args.page_size,
            args.timeout,
        )
    except ApiError as exc:
        print(f"查询渠道失败：{exc}", file=sys.stderr)
        return 1

    candidates: list[tuple[dict[str, Any], str]] = []
    for channel in channels:
        if not has_model(channel.get("models"), args.model):
            candidates.append((channel, append_model(channel.get("models"), args.model)))

    print(
        f"查询到 {len(channels)} 个渠道，缺少 {args.model} 的渠道有 "
        f"{len(candidates)} 个。"
    )
    for channel, new_models in candidates:
        print(f"  {channel_label(channel)} -> models={new_models}")

    if not candidates:
        print("没有需要更新的渠道。")
        return 0
    if not args.execute:
        print("当前为预览模式；加上 --execute 才会执行 PUT。")
        return 0
    if not args.yes and not confirm(len(candidates), args.model):
        print("已取消。")
        return 0

    failed = 0
    url = api_url(args.base_url)
    for index, (channel, new_models) in enumerate(candidates, start=1):
        label = channel_label(channel)
        payload = make_put_payload(channel, new_models)
        if "id" not in payload:
            print(f"[{index}/{len(candidates)}] {label} 缺少 id，跳过。", file=sys.stderr)
            failed += 1
            continue
        try:
            response = request_json(
                "PUT",
                url,
                args.token,
                args.timeout,
                body=payload,
            )
            if response.get("success") is not True:
                raise ApiError(f"success != true: {response.get('message', '')}")
            print(f"[{index}/{len(candidates)}] {label} 更新成功。")
        except ApiError as exc:
            print(f"[{index}/{len(candidates)}] {label} 更新失败：{exc}", file=sys.stderr)
            failed += 1
        if args.delay > 0 and index < len(candidates):
            time.sleep(args.delay)

    if failed:
        print(f"批量更新完成：成功 {len(candidates) - failed}，失败 {failed}。")
        return 1
    print(f"批量更新完成：成功 {len(candidates)} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
