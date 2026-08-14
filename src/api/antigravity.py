"""
Antigravity API Client - Handles communication with Google's Antigravity API
处理与 Google Antigravity API 的通信
"""

import asyncio
import copy
import hashlib
import json
import uuid
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Callable, Tuple

from fastapi import Response
from src.config import (
    get_antigravity_api_url,
    get_antigravity_stream2nostream,
    get_auto_ban_error_codes,
    get_antigravity_telemetry_enabled,
)
from src.log import log

from src.auth import credential_manager
from src.client import stream_post_async, post_async, get_async
from src.schemas import Model, model_to_dict
from src.utils import ANTIGRAVITY_USER_AGENT, BASE_MODELS

# 导入共同的基础功能
from src.api.utils import (
    handle_error_with_retry,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
    collect_streaming_response,
)

# ==================== 全局凭证管理器 ====================

# 使用全局单例 credential_manager，自动初始化


def _extract_first_user_text(request_payload: Dict[str, Any]) -> str:
    contents = request_payload.get("contents", [])
    if not isinstance(contents, list):
        return ""
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "user":
            continue
        parts = content.get("parts", [])
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
    return ""


def _generate_request_id() -> str:
    return f"agent/{uuid.uuid4()}"


def _build_labels(model: str, trajectory_id: str, step: int) -> Dict[str, str]:
    used_claude = "claude" in model.lower()
    return {
        "last_step_index": str(step),
        "model_enum": model,
        "trajectory_id": trajectory_id,
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
    }


def _should_forward_antigravity_header(header_name: str) -> bool:
    normalized = header_name.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("x-b3-"):
        return True
    return normalized in {
        "accept-language",
        "traceparent",
        "tracestate",
        "x-cloud-trace-context",
        "x-goog-api-client",
        "x-goog-request-params",
        "x-goog-user-project",
        "x-request-id",
    }


def _sanitize_antigravity_headers(extra_headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not extra_headers:
        return {}
    sanitized: Dict[str, str] = {}
    for key, value in extra_headers.items():
        if _should_forward_antigravity_header(key):
            sanitized[key] = value
    return sanitized


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
) -> Tuple[Dict[str, Any], str]:
    """
    将 Gemini 格式请求包装成 Antigravity CLI 格式。
    返回 (payload, request_id)。
    """
    inner = copy.deepcopy(gemini_request)
    first_user_text = _extract_first_user_text(inner)

    # 移除 safetySettings（CLI 不发送）
    inner.pop("safetySettings", None)

    # 注入 sessionId
    session_id = str(inner.get("sessionId") or "").strip()
    if not session_id:
        if first_user_text:
            digest = hashlib.sha256(first_user_text.encode("utf-8")).digest()
            session_id_val = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
            session_id = f"-{session_id_val}"
        else:
            session_id = f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"
        inner["sessionId"] = session_id

    # 注入 labels
    inner["labels"] = _build_labels(model, session_id, 1)

    # toolConfig 默认 VALIDATED
    tool_config = inner.get("toolConfig") or {}
    func_config = tool_config.get("functionCallingConfig") or {}
    func_config["mode"] = "VALIDATED"
    tool_config["functionCallingConfig"] = func_config
    inner["toolConfig"] = tool_config

    request_id = _generate_request_id()

    payload = {
        "project": project_id,
        "requestId": request_id,
        "request": inner,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "agent",
        "enabledCreditTypes": ["GOOGLE_ONE_AI"],
    }
    return payload, request_id


# ==================== 辅助函数 ====================

def build_antigravity_headers(access_token: str, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构建 Antigravity CLI API 请求头。"""
    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Connection": "close",
    }

    for key, value in _sanitize_antigravity_headers(extra_headers).items():
        headers.setdefault(key, value)

    return headers


async def send_background_telemetry(
    access_token: str, project_id: str, request_id: str, model_name: str
) -> None:
    """
    异步并发发送伴随流量请求 (匹配 traffic.log 日志的伴随打点特征)：
    1. v1internal:recordCodeAssistMetrics (代码助手指标上报)
    2. v1internal:listExperiments (实验配置获取)
    3. antigravity-unleash.goog (Unleash 特性心跳)
    """
    try:
        if not await get_antigravity_telemetry_enabled():
            return

        antigravity_url = await get_antigravity_api_url()
        headers = build_antigravity_headers(access_token)

        async def _safe_post(url: str, json_payload: dict, req_headers: dict):
            try:
                await post_async(url=url, json=json_payload, headers=req_headers, timeout=5.0, skip_interval=True)
            except Exception as ex:
                log.debug(
                    f"[ANTIGRAVITY TELEMETRY] 伴随 POST 请求静默忽略异常 ({url}): {ex}")

        async def _safe_get(url: str, req_headers: dict):
            try:
                await get_async(url=url, headers=req_headers, timeout=5.0)
            except Exception as ex:
                log.debug(
                    f"[ANTIGRAVITY TELEMETRY] 伴随 GET 请求静默忽略异常 ({url}): {ex}")

        # 1. 伴随指标上报 (recordCodeAssistMetrics)
        metrics_payload = {
            "project": project_id,
            "requestId": request_id,
            "model": model_name,
            "clientMetadata": {
                "ideName": "vscode",
                "ideVersion": "1.96.0",
                "extensionName": "antigravity",
                "extensionVersion": "1.1.5",
            },
        }
        asyncio.create_task(
            _safe_post(
                url=f"{antigravity_url}/v1internal:recordCodeAssistMetrics",
                json_payload=metrics_payload,
                req_headers=headers,
            )
        )

        # 2. 伴随实验获取 (listExperiments)
        asyncio.create_task(
            _safe_post(
                url=f"{antigravity_url}/v1internal:listExperiments",
                json_payload={},
                req_headers=headers,
            )
        )

        # 3. Unleash 特性获取 (antigravity-unleash.goog)
        unleash_headers = {
            "User-Agent": "codeium-language-server",
            "unleash-appname": "codeium-language-server",
            "unleash-instanceid": "localhost",
            "unleash-connection-id": "02b6e7ac-23b2-4c1b-b40e-ca7690890734",
            "unleash-sdk": "unleash-client-go:4.5.0",
            "authorization": "*:production.e44558998bfc35ea9584dc65858e4485fdaa5d7ef46903e0c67712d1",
        }
        asyncio.create_task(
            _safe_get(
                url="https://antigravity-unleash.goog/api/client/features",
                req_headers=unleash_headers,
            )
        )
    except Exception as e:
        log.debug(f"[TELEMETRY] 伴随流量上报跳过: {e}")


def _is_retryable_status(status_code: int, disable_error_codes: List[int], error_body: str = "") -> bool:
    """统一判断是否属于可重试状态码或可重试错误。"""
    if error_body and ("provisioning" in error_body.lower() or "under provisioning" in error_body.lower()):
        return True
    return status_code in (429, 503) or status_code in disable_error_codes


async def _switch_credential_for_retry(
    *,
    next_cred_task: Optional[asyncio.Task],
    retry_interval: float,
    refresh_credential_fast: Callable[[], Any],
    apply_cred_result: Callable[[Tuple[str, Dict[str, Any]]], bool],
    log_prefix: str,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """优先使用预热凭证，失败后退回同步刷新。"""
    if next_cred_task is not None:
        try:
            cred_result = await next_cred_task
            next_cred_task = None
            if cred_result and apply_cred_result(cred_result):
                await asyncio.sleep(retry_interval)
                return True, next_cred_task
        except Exception as e:
            log.warning(f"{log_prefix} 预热凭证任务失败: {e}")
            next_cred_task = None

    await asyncio.sleep(retry_interval)
    if await refresh_credential_fast():
        return True, next_cred_task

    return False, next_cred_task


# ==================== 新的流式和非流式请求函数 ====================

async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
):
    """
    流式请求函数

    Args:
        body: 请求体
        native: 是否返回原生bytes流，False则返回str流
        headers: 额外的请求头

    Yields:
        Response对象（错误时）或 bytes流/str流（成功时）
    """
    model_name = body.get("model", "")

    # 1. 获取有效凭证
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_name=model_name
    )

    if not cred_result:
        # 如果返回值是None，直接返回错误500
        log.error("[ANTIGRAVITY STREAM] 当前无可用凭证")
        yield Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json"
        )
        return

    current_file, credential_data = cred_result
    access_token = credential_data.get(
        "access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(
            f"[ANTIGRAVITY STREAM] No access token in credential: {current_file}")
        yield Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )
        return

    # 2. 构建URL和请求头
    antigravity_url = await get_antigravity_api_url()
    target_url = f"{antigravity_url}/v1internal:streamGenerateContent?alt=sse"

    auth_headers = build_antigravity_headers(access_token, headers)

    # 构建 CLI 格式请求体
    inner_request = body.get("request", body)
    final_payload, request_id = await wrap_cli_request(inner_request, model_name, project_id)

    # 3. 调用stream_post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    next_cred_task = None  # 预热的下一个凭证任务

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast():
        nonlocal current_file, access_token, auth_headers, project_id, final_payload
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name, force_rotate=True
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        access_token = credential_data.get(
            "access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload
        current_file, credential_data = cred_result
        access_token = credential_data.get(
            "access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        success_recorded = False  # 标记是否已记录成功
        need_retry = False  # 标记是否需要重试

        try:
            async for chunk in stream_post_async(
                url=target_url,
                body=final_payload,
                native=native,
                headers=auth_headers
            ):
                # 判断是否是Response对象
                if isinstance(chunk, Response):
                    status_code = chunk.status_code
                    last_error_response = chunk  # 记录最后一次错误

                    # 缓存错误解析结果,避免重复decode
                    error_body = None
                    try:
                        if isinstance(chunk.body, bytes):
                            error_body = chunk.body.decode(
                                "utf-8", errors="ignore")
                        elif isinstance(chunk.body, str):
                            error_body = chunk.body
                    except Exception:
                        error_body = ""

                    # 解析并记录冷却时间（如果有）
                    cooldown_until = await parse_and_log_cooldown(error_body or "", mode="antigravity")

                    # 判断是否触发禁用凭证
                    if status_code in DISABLE_ERROR_CODES:
                        log.warning(
                            f"[ANTIGRAVITY STREAM] 触发自动禁用凭证 (状态码: {status_code}), 禁用凭证: {current_file}"
                        )
                        await credential_manager.disable_credential(
                            current_file,
                            reason=f"HTTP {status_code}: {error_body[:100] if error_body else ''}",
                            mode="antigravity"
                        )
                    else:
                        # 记录API调用错误(不禁用)
                        await record_api_call_error(
                            credential_manager,
                            current_file,
                            status_code=status_code,
                            cooldown_until=cooldown_until,
                            mode="antigravity",
                            model_name=model_name,
                            error_message=error_body or f"HTTP {status_code}"
                        )

                    # 判断是否需要重试
                    if attempt < max_retries and _is_retryable_status(status_code, DISABLE_ERROR_CODES, error_body or ""):
                        need_retry = True
                        log.warning(
                            f"[ANTIGRAVITY STREAM] 收到错误 {status_code}，触发重试 (尝试 {attempt + 1}/{max_retries + 1})"
                        )

                        # 在准备重试前异步预热下一个凭证
                        next_cred_task = asyncio.create_task(
                            credential_manager.get_valid_credential(
                                mode="antigravity", model_name=model_name, force_rotate=True
                            )
                        )
                        break  # 跳出生成器循环，准备下一次重试

                    # 如果不能重试或已达最大重试次数，把错误 chunk yield 给上层
                    yield chunk
                    return

                # 如果不是Response对象，则是正常数据流
                if not success_recorded:
                    # 第一次收到正常数据时，记录调用成功并触发伴随流量
                    await record_api_call_success(credential_manager, current_file, mode="antigravity", model_name=model_name)
                    asyncio.create_task(
                        send_background_telemetry(
                            access_token, project_id, request_id, model_name
                        )
                    )
                    success_recorded = True

                yield chunk

            # 如果正常结束且没有触发重试，直接退出函数
            if not need_retry:
                return

        except Exception as e:
            log.error(f"[ANTIGRAVITY STREAM] 请求引发异常: {e}")
            await record_api_call_error(
                credential_manager,
                current_file,
                status_code=500,
                error_message=str(e),
                mode="antigravity",
                model_name=model_name
            )

            if attempt < max_retries:
                need_retry = True
                log.warning(
                    f"[ANTIGRAVITY STREAM] 异常触发重试 (尝试 {attempt + 1}/{max_retries + 1})"
                )
            else:
                yield Response(
                    content=json.dumps(
                        {"error": f"Stream request failed: {str(e)}"}),
                    status_code=500,
                    media_type="application/json"
                )
                return

        # 如果需要重试，切换凭证
        if need_retry:
            refreshed, next_cred_task = await _switch_credential_for_retry(
                next_cred_task=next_cred_task,
                retry_interval=retry_interval,
                refresh_credential_fast=refresh_credential_fast,
                apply_cred_result=apply_cred_result,
                log_prefix="[ANTIGRAVITY STREAM]",
            )
            if not refreshed:
                log.error("[ANTIGRAVITY STREAM] 重试时无法获取有效凭证，放弃重试")
                if last_error_response:
                    yield last_error_response
                else:
                    yield Response(
                        content=json.dumps({"error": "重试时无法获取有效凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                return

    # 所有重试都失败后
    log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数 ({max_retries})")
    if last_error_response:
        yield last_error_response
    else:
        yield Response(
            content=json.dumps({"error": f"达到最大重试次数 ({max_retries})"}),
            status_code=500,
            media_type="application/json"
        )


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """
    非流式请求函数

    Args:
        body: 请求体
        headers: 额外的请求头

    Returns:
        Tuple[status_code, response_data]
    """
    model_name = body.get("model", "")

    # 检查是否配置了将流式转换为非流式
    stream2nostream = await get_antigravity_stream2nostream()
    if stream2nostream:
        log.info(
            f"[ANTIGRAVITY NON-STREAM] 配置了 stream2nostream，改用 stream_request 模拟非流式请求 (模型: {model_name})")

        # 收集流式响应
        stream_gen = stream_request(
            body=body,
            native=False,
            headers=headers
        )

        status_code, combined_json, error_response = await collect_streaming_response(stream_gen)

        if error_response is not None:
            return status_code, error_response

        return status_code, combined_json

    # 原有的直接非流式请求逻辑
    cred_result = await credential_manager.get_valid_credential(
        mode="antigravity", model_name=model_name
    )

    if not cred_result:
        log.error("[ANTIGRAVITY NON-STREAM] 当前无可用凭证")
        return 500, {"error": "当前无可用凭证"}

    current_file, credential_data = cred_result
    access_token = credential_data.get(
        "access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")

    if not access_token:
        log.error(
            f"[ANTIGRAVITY NON-STREAM] No access token in credential: {current_file}")
        return 500, {"error": "凭证中没有访问令牌"}

    antigravity_url = await get_antigravity_api_url()
    target_url = f"{antigravity_url}/v1internal:generateContent"

    auth_headers = build_antigravity_headers(access_token, headers)

    # 构建 CLI 格式请求体
    inner_request = body.get("request", body)
    final_payload, request_id = await wrap_cli_request(inner_request, model_name, project_id)

    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_data = None  # 记录最后一次错误数据
    last_status_code = 500  # 记录最后一次状态码
    next_cred_task = None  # 预热的下一个凭证任务

    # 内部函数：快速更新凭证
    async def refresh_credential_fast():
        nonlocal current_file, access_token, auth_headers, project_id, final_payload
        cred_result = await credential_manager.get_valid_credential(
            mode="antigravity", model_name=model_name, force_rotate=True
        )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        access_token = credential_data.get(
            "access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload
        current_file, credential_data = cred_result
        access_token = credential_data.get(
            "access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        return True

    for attempt in range(max_retries + 1):
        try:
            response = await post_async(
                url=target_url,
                json=final_payload,
                headers=auth_headers
            )

            status_code = response.status_code
            last_status_code = status_code

            if status_code == 200:
                await record_api_call_success(credential_manager, current_file, mode="antigravity", model_name=model_name)
                asyncio.create_task(
                    send_background_telemetry(
                        access_token, project_id, request_id, model_name
                    )
                )
                try:
                    return status_code, response.json()
                except Exception as e:
                    log.error(
                        f"[ANTIGRAVITY NON-STREAM] 解析 JSON 响应失败: {e}")
                    return 500, {"error": f"Invalid JSON response: {str(e)}"}

            # 错误处理
            error_body = response.text
            try:
                error_data = response.json()
            except Exception:
                error_data = {"error": error_body or f"HTTP {status_code}"}

            last_error_data = error_data

            # 解析并记录冷却时间（如果有）
            cooldown_until = await parse_and_log_cooldown(error_body or "", mode="antigravity")

            # 判断是否触发禁用凭证
            if status_code in DISABLE_ERROR_CODES:
                log.warning(
                    f"[ANTIGRAVITY NON-STREAM] 触发自动禁用凭证 (状态码: {status_code}), 禁用凭证: {current_file}"
                )
                await credential_manager.disable_credential(
                    current_file,
                    reason=f"HTTP {status_code}: {error_body[:100] if error_body else ''}",
                    mode="antigravity"
                )
            else:
                # 记录API调用错误(不禁用)
                await record_api_call_error(
                    credential_manager,
                    current_file,
                    status_code=status_code,
                    cooldown_until=cooldown_until,
                    mode="antigravity",
                    model_name=model_name,
                    error_message=error_body or f"HTTP {status_code}"
                )

            # 判断是否需要重试
            if attempt < max_retries and _is_retryable_status(status_code, DISABLE_ERROR_CODES, error_body):
                log.warning(
                    f"[ANTIGRAVITY NON-STREAM] 收到错误 {status_code}，触发重试 (尝试 {attempt + 1}/{max_retries + 1})"
                )

                # 预热下一个凭证
                next_cred_task = asyncio.create_task(
                    credential_manager.get_valid_credential(
                        mode="antigravity", model_name=model_name, force_rotate=True
                    )
                )

                # 切换凭证
                refreshed, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY NON-STREAM]",
                )
                if not refreshed:
                    log.error(
                        "[ANTIGRAVITY NON-STREAM] 重试时无法获取有效凭证，放弃重试")
                    return status_code, error_data
                continue

            # 不满足重试条件，直接返回错误
            return status_code, error_data

        except Exception as e:
            log.error(f"[ANTIGRAVITY NON-STREAM] 请求引发异常: {e}")
            await record_api_call_error(
                credential_manager,
                current_file,
                status_code=500,
                error_message=str(e),
                mode="antigravity",
                model_name=model_name
            )

            last_error_data = {"error": f"Request failed: {str(e)}"}
            last_status_code = 500

            if attempt < max_retries:
                log.warning(
                    f"[ANTIGRAVITY NON-STREAM] 异常触发重试 (尝试 {attempt + 1}/{max_retries + 1})"
                )
                refreshed, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY NON-STREAM]",
                )
                if not refreshed:
                    log.error(
                        "[ANTIGRAVITY NON-STREAM] 重试时无法获取有效凭证，放弃重试")
                    return 500, last_error_data
                continue

            return 500, last_error_data

    log.error(f"[ANTIGRAVITY NON-STREAM] 达到最大重试次数 ({max_retries})")
    return last_status_code, last_error_data or {"error": f"达到最大重试次数 ({max_retries})"}


async def get_models_list(
    mode: str = "antigravity"
) -> Tuple[int, Dict[str, Any]]:
    """
    获取可用模型列表
    """
    log.info(f"Using standard local model list for mode: {mode}")
    models_data = [model_to_dict(Model(id=model_id))
                   for model_id in BASE_MODELS]
    return 200, {"object": "list", "data": models_data}


_MODELS_CACHE: List[Dict[str, Any]] = []
_MODELS_CACHE_TIME: float = 0.0


async def fetch_available_models(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    获取可用模型列表，返回 OpenAI API 规范的字典列表。
    系统启动时获取一次或使用内存缓存，提升响应速度。
    """
    global _MODELS_CACHE, _MODELS_CACHE_TIME
    now = time.time()

    if not force_refresh and _MODELS_CACHE:
        return _MODELS_CACHE

    cred_result = await credential_manager.get_valid_credential(mode="antigravity")
    if not cred_result:
        log.error(
            "[ANTIGRAVITY] No valid credentials available for fetching models")
        return _MODELS_CACHE if _MODELS_CACHE else []

    current_file, credential_data = cred_result
    access_token = credential_data.get(
        "access_token") or credential_data.get("token")

    if not access_token:
        log.error(
            f"[ANTIGRAVITY] No access token in credential: {current_file}")
        return _MODELS_CACHE if _MODELS_CACHE else []

    headers = build_antigravity_headers(access_token)

    try:
        antigravity_url = await get_antigravity_api_url()

        response = await post_async(
            url=f"{antigravity_url}/v1internal:fetchAvailableModels",
            json={},
            headers=headers
        )

        if response.status_code == 200:
            data = response.json()
            log.debug(
                f"[ANTIGRAVITY] Raw models response: {json.dumps(data, ensure_ascii=False)}")

            model_list = []
            current_timestamp = int(datetime.now(timezone.utc).timestamp())

            if 'models' in data and isinstance(data['models'], dict):
                raw_model_ids = list(data['models'].keys())

                for model_id in raw_model_ids:
                    model = Model(
                        id=model_id,
                        object='model',
                        created=current_timestamp,
                        owned_by='google'
                    )
                    model_list.append(model_to_dict(model))

            if "claude-sonnet-4-6" in data.get('models', {}):
                model = Model(
                    id='claude-sonnet-4-6-thinking',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(model))
            if "claude-opus-4-6-thinking" in data.get('models', {}):
                claude_opus_model = Model(
                    id='claude-opus-4-6',
                    object='model',
                    created=current_timestamp,
                    owned_by='google'
                )
                model_list.append(model_to_dict(claude_opus_model))

            log.info(
                f"[ANTIGRAVITY] Fetched {len(model_list)} available models")
            _MODELS_CACHE = model_list
            _MODELS_CACHE_TIME = now
            return model_list
        else:
            log.error(
                f"[ANTIGRAVITY] Failed to fetch models ({response.status_code}): {response.text}")
            return _MODELS_CACHE if _MODELS_CACHE else []

    except Exception as e:
        import traceback
        log.error(f"[ANTIGRAVITY] Failed to fetch models: {e}")
        log.error(f"[ANTIGRAVITY] Traceback: {traceback.format_exc()}")
        return _MODELS_CACHE if _MODELS_CACHE else []


def _parse_reset_time_to_beijing(reset_time_raw: str) -> str:
    """将 UTC ISO 时间字符串转换为北京时间格式 (MM-DD HH:MM)"""
    if not reset_time_raw:
        return 'N/A'
    try:
        reset_time_clean = reset_time_raw.rstrip('Z')
        if '.' in reset_time_clean:
            parts = reset_time_clean.split('.')
            reset_time_clean = f"{parts[0]}.{parts[1][:6]}"
            utc_date = datetime.strptime(reset_time_clean, '%Y-%m-%dT%H:%M:%S.%f')
        else:
            utc_date = datetime.strptime(reset_time_clean, '%Y-%m-%dT%H:%M:%S')

        beijing_date = utc_date + timedelta(hours=8)
        return beijing_date.strftime('%m-%d %H:%M')
    except Exception as e:
        log.warning(
            f"[ANTIGRAVITY QUOTA] Failed to parse reset time ({reset_time_raw}): {e}")
        return 'N/A'


def _format_network_error(e: Exception) -> str:
    """将网络/TLS/curl 底层异常转换为人性化的中文提示信息"""
    err_str = str(e)
    if any(k in err_str for k in ["TLS connect error", "SSLError", "OPENSSL_internal", "SSL"]):
        return "网络连接失败 (TLS/SSL握手异常，请检查网络代理或代理节点联通性)"
    if "Could not resolve host" in err_str or "resolve host" in err_str:
        return "网络连接失败 (无法解析域名 DNS，请检查网络/代理设置)"
    if "Connection refused" in err_str or "Failed to connect" in err_str:
        return "网络连接失败 (目标地址或代理拒绝连接)"
    if "Timeout" in err_str or "timed out" in err_str or "CURLE_OPERATION_TIMEDOUT" in err_str:
        return "网络请求超时 (请检查代理节点响应速度或网络延迟)"
    return f"网络请求异常: {err_str}"


async def fetch_quota_info(access_token: str) -> Dict[str, Any]:
    """
    获取指定凭证的额度信息

    Args:
        access_token: Antigravity 访问令牌

    Returns:
        包含额度信息的字典
    """
    headers = build_antigravity_headers(access_token)
    max_retries = 2

    for attempt in range(max_retries):
        try:
            antigravity_url = await get_antigravity_api_url()

            response = await post_async(
                url=f"{antigravity_url}/v1internal:fetchAvailableModels",
                json={},
                headers=headers,
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                log.debug(
                    f"[ANTIGRAVITY QUOTA] Raw response: {json.dumps(data, ensure_ascii=False)}")

                quota_info = {}

                if 'models' in data and isinstance(data['models'], dict):
                    for model_id, model_data in data['models'].items():
                        if isinstance(model_data, dict) and 'quotaInfo' in model_data:
                            quota = model_data['quotaInfo']
                            remaining = quota.get('remainingFraction', 0)
                            reset_time_raw = quota.get('resetTime', '')
                            reset_time_beijing = _parse_reset_time_to_beijing(
                                reset_time_raw)

                            quota_info[model_id] = {
                                "remaining": remaining,
                                "resetTime": reset_time_beijing,
                                "resetTimeRaw": reset_time_raw
                            }

                return {
                    "success": True,
                    "models": quota_info
                }
            else:
                log.error(
                    f"[ANTIGRAVITY QUOTA] Failed to fetch quota ({response.status_code}): {response.text}")
                return {
                    "success": False,
                    "error": f"API返回错误: {response.status_code}"
                }

        except Exception as e:
            err_str = str(e)
            is_net_err = any(k in err_str for k in ["TLS connect error", "SSLError", "OPENSSL_internal", "resolve host", "Connection refused", "CURLE_"])
            if is_net_err and attempt < max_retries - 1:
                log.warning(f"[ANTIGRAVITY QUOTA] 网络/TLS握手异常，正在进行第 {attempt + 1} 次重试...")
                await asyncio.sleep(0.5)
                continue

            friendly_err = _format_network_error(e)
            if is_net_err:
                log.warning(f"[ANTIGRAVITY QUOTA] 获取额度失败: {friendly_err}")
            else:
                import traceback
                log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {e}")
                log.error(f"[ANTIGRAVITY QUOTA] Traceback: {traceback.format_exc()}")
            return {
                "success": False,
                "error": friendly_err
            }


async def fetch_quota_summary(access_token: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    """
    获取指定凭证的详细额度分组信息 (retrieveUserQuotaSummary)

    Args:
        access_token: Antigravity 访问令牌
        project_id: 项目 ID (可选)

    Returns:
        包含额度分组信息的字典
    """
    headers = build_antigravity_headers(access_token)
    payload = {"project": project_id} if project_id else {}
    max_retries = 2

    for attempt in range(max_retries):
        try:
            antigravity_url = await get_antigravity_api_url()
            response = await post_async(
                url=f"{antigravity_url}/v1internal:retrieveUserQuotaSummary",
                json=payload,
                headers=headers,
                timeout=30.0
            )

            if response.status_code == 200:
                data = response.json()
                log.debug(
                    f"[ANTIGRAVITY QUOTA SUMMARY] Raw response: {json.dumps(data, ensure_ascii=False)}")

                groups = []
                if 'groups' in data and isinstance(data['groups'], list):
                    for group in data['groups']:
                        buckets = []
                        if 'buckets' in group and isinstance(group['buckets'], list):
                            for bucket in group['buckets']:
                                remaining = bucket.get('remainingFraction', 0.0)
                                reset_time_raw = bucket.get('resetTime', '')
                                reset_time_beijing = _parse_reset_time_to_beijing(
                                    reset_time_raw)

                                buckets.append({
                                    "bucketId": bucket.get("bucketId", ""),
                                    "window": bucket.get("window", ""),
                                    "remainingFraction": remaining,
                                    "resetTime": reset_time_beijing,
                                    "resetTimeRaw": reset_time_raw,
                                    "displayName": bucket.get("displayName"),
                                    "description": bucket.get("description")
                                })

                        groups.append({
                            "displayName": group.get("displayName", ""),
                            "description": group.get("description"),
                            "buckets": buckets
                        })

                return {
                    "success": True,
                    "groups": groups
                }
            else:
                log.error(
                    f"[ANTIGRAVITY QUOTA SUMMARY] Failed to fetch quota summary ({response.status_code}): {response.text}")
                return {
                    "success": False,
                    "error": f"API返回错误: {response.status_code}"
                }

        except Exception as e:
            err_str = str(e)
            is_net_err = any(k in err_str for k in ["TLS connect error", "SSLError", "OPENSSL_internal", "resolve host", "Connection refused", "CURLE_"])
            if is_net_err and attempt < max_retries - 1:
                log.warning(f"[ANTIGRAVITY QUOTA SUMMARY] 网络/TLS握手异常，正在进行第 {attempt + 1} 次重试...")
                await asyncio.sleep(0.5)
                continue

            friendly_err = _format_network_error(e)
            if is_net_err:
                log.warning(f"[ANTIGRAVITY QUOTA SUMMARY] 获取额度分组失败: {friendly_err}")
            else:
                import traceback
                log.error(f"[ANTIGRAVITY QUOTA SUMMARY] Failed to fetch quota summary: {e}")
                log.error(f"[ANTIGRAVITY QUOTA SUMMARY] Traceback: {traceback.format_exc()}")
            return {
                "success": False,
                "error": friendly_err
            }

