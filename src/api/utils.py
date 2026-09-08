"""
Base API Client - 共用的 API 客户端基础功能
提供错误处理、自动封禁、重试逻辑等共同功能
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

from fastapi import Response

from src.config import (
    get_auto_ban_enabled,
    get_auto_ban_error_codes,
    get_retry_429_enabled,
    get_retry_429_interval,
    get_retry_429_max_retries,
)
from src.log import log

if TYPE_CHECKING:
    from src.auth import CredentialManager


# ==================== 错误检查与处理 ====================

async def check_should_auto_ban(status_code: int) -> bool:
    """
    检查是否应该触发自动封禁

    Args:
        status_code: HTTP状态码

    Returns:
        bool: 是否应该触发自动封禁
    """
    return (
        await get_auto_ban_enabled()
        and status_code in await get_auto_ban_error_codes()
    )


async def handle_auto_ban(
    credential_manager: CredentialManager,
    status_code: int,
    credential_name: str,
    mode: str = "geminicli"
) -> None:
    """
    处理自动封禁：直接禁用凭证

    Args:
        credential_manager: 凭证管理器实例
        status_code: HTTP状态码
        credential_name: 凭证名称
        mode: 模式（geminicli 或 antigravity）
    """
    if credential_manager and credential_name:
        log.warning(
            f"[{mode.upper()} AUTO_BAN] Status {status_code} triggers auto-ban for credential: {credential_name}"
        )
        await credential_manager.set_cred_disabled(
            credential_name, True, mode=mode
        )


async def handle_error_with_retry(
    credential_manager: CredentialManager,
    status_code: int,
    credential_name: str,
    retry_enabled: bool,
    attempt: int,
    max_retries: int,
    retry_interval: float,
    mode: str = "geminicli"
) -> bool:
    """
    统一处理错误和重试逻辑

    仅在以下情况下进行自动重试:
    1. 429错误(速率限制)
    2. 503错误(服务不可用)
    3. 500错误(服务临时不可用)
    4. 导致凭证封禁的错误(AUTO_BAN_ERROR_CODES配置)

    Args:
        credential_manager: 凭证管理器实例
        status_code: HTTP状态码
        credential_name: 凭证名称
        retry_enabled: 是否启用重试
        attempt: 当前重试次数
        max_retries: 最大重试次数
        retry_interval: 重试间隔
        mode: 模式（geminicli 或 antigravity）

    Returns:
        bool: True表示需要继续重试，False表示不需要重试
    """
    # 优先检查自动封禁
    should_auto_ban = await check_should_auto_ban(status_code)

    if should_auto_ban:
        # 触发自动封禁
        await handle_auto_ban(credential_manager, status_code, credential_name, mode)

        # 自动封禁后，仍然尝试重试（会在下次循环中自动获取新凭证）
        if retry_enabled and attempt < max_retries:
            log.info(
                f"[{mode.upper()} RETRY] Retrying with next credential after auto-ban "
                f"(status {status_code}, attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(retry_interval)
            return True
        return False

    # 如果不触发自动封禁，对400、429、500和503错误进行重试（自动切换账号池中下一个可用凭证）
    if status_code in (400, 429, 500, 503) and retry_enabled and attempt < max_retries:
        log.info(
            f"[{mode.upper()} RETRY] {status_code} error encountered, retrying "
            f"(attempt {attempt + 1}/{max_retries})"
        )
        await asyncio.sleep(retry_interval)
        return True

    # 其他错误不进行重试
    return False


# ==================== 重试配置获取 ====================

async def get_retry_config() -> Dict[str, Any]:
    """
    获取重试配置

    Returns:
        包含重试配置的字典
    """
    return {
        "retry_enabled": await get_retry_429_enabled(),
        "max_retries": await get_retry_429_max_retries(),
        "retry_interval": await get_retry_429_interval(),
    }


# ==================== API调用结果记录 ====================

async def record_api_call_success(
    credential_manager: CredentialManager,
    credential_name: str,
    mode: str = "geminicli",
    model_name: Optional[str] = None
) -> None:
    """
    记录API调用成功

    Args:
        credential_manager: 凭证管理器实例
        credential_name: 凭证名称
        mode: 模式（geminicli 或 antigravity）
        model_name: 模型名称（用于模型级CD）
    """
    if credential_manager and credential_name:
        await credential_manager.record_api_call_result(
            credential_name, True, mode=mode, model_name=model_name
        )


async def record_api_call_error(
    credential_manager: CredentialManager,
    credential_name: str,
    status_code: int,
    cooldown_until: Optional[float] = None,
    mode: str = "geminicli",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None
) -> None:
    """
    记录API调用错误

    Args:
        credential_manager: 凭证管理器实例
        credential_name: 凭证名称
        status_code: HTTP状态码
        cooldown_until: 冷却截止时间（Unix时间戳）
        mode: 模式（geminicli 或 antigravity）
        model_name: 模型名称（用于模型级CD）
        error_message: 错误信息（可选）
    """
    if credential_manager and credential_name:
        await credential_manager.record_api_call_result(
            credential_name,
            False,
            status_code,
            cooldown_until=cooldown_until,
            mode=mode,
            model_name=model_name,
            error_message=error_message
        )


# ==================== 429错误处理 ====================

async def parse_and_log_cooldown(
    error_text: str,
    mode: str = "geminicli"
) -> Optional[float]:
    """
    解析并记录冷却时间

    Args:
        error_text: 错误响应文本
        mode: 模式（geminicli 或 antigravity）

    Returns:
        冷却截止时间（Unix时间戳），如果解析失败则返回None
    """
    try:
        error_data = json.loads(error_text)
        cooldown_until = parse_quota_reset_timestamp(error_data, mode=mode)
        if cooldown_until:
            log.info(
                f"[{mode.upper()}] 检测到quota冷却时间: "
                f"{datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
            )
            return cooldown_until
    except Exception as parse_err:
        log.debug(
            f"[{mode.upper()}] Failed to parse cooldown time: {parse_err}")
    return None




def parse_quota_reset_timestamp(error_response: dict, mode: str = "geminicli") -> Optional[float]:
    """
    从Google API错误响应中提取quota重置时间戳

    Args:
        error_response: Google API返回的错误响应字典
        mode: 请求模式 (geminicli / antigravity)

    Returns:
        Unix时间戳（秒），如果无法解析则返回None

    支持的错误响应格式:
    1. QUOTA_EXHAUSTED / INSUFFICIENT_G1_CREDITS_BALANCE - 含 quotaResetTimeStamp:
    {
      "error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "details": [{
          "@type": "type.googleapis.com/google.rpc.ErrorInfo",
          "reason": "INSUFFICIENT_G1_CREDITS_BALANCE",
          "metadata": {
            "quotaResetTimeStamp": "2026-09-08T13:00:00Z",
            "quotaResetDelay": "2h17m30s"
          }
        }]
      }
    }
    2. RATE_LIMIT_EXCEEDED - 含 message 中的时间描述:
       "Your quota will reset after 2h 17m 30s."
    """
    try:
        error_obj = error_response.get("error", {})
        details = error_obj.get("details", [])

        # 遍历所有 ErrorInfo detail，统一提取重置时间
        # INSUFFICIENT_G1_CREDITS_BALANCE 是5小时滚动限额，与普通 QUOTA_EXHAUSTED 一样
        # 优先读取 metadata 中的精确时间戳，不硬编码固定冷却时长
        for detail in details:
            if detail.get("@type") != "type.googleapis.com/google.rpc.ErrorInfo":
                continue

            reason = detail.get("reason", "")
            metadata = detail.get("metadata", {})

            # 记录日志方便调试
            if reason == "INSUFFICIENT_G1_CREDITS_BALANCE":
                log.warning(
                    f"[{mode.upper()}] 检测到 G1 积分限额耗尽 (INSUFFICIENT_G1_CREDITS_BALANCE)，"
                    f"尝试从 metadata 读取精确重置时间..."
                )

            # 1. 优先读取精确的 quotaResetTimeStamp
            reset_timestamp_str = metadata.get("quotaResetTimeStamp")
            if reset_timestamp_str:
                if reset_timestamp_str.endswith("Z"):
                    reset_timestamp_str = reset_timestamp_str.replace("Z", "+00:00")
                try:
                    reset_dt = datetime.fromisoformat(reset_timestamp_str)
                    if reset_dt.tzinfo is None:
                        reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                    cooldown_until = reset_dt.astimezone(timezone.utc).timestamp()
                    log.info(
                        f"[{mode.upper()}] 读取到精确重置时间 (quotaResetTimeStamp): "
                        f"{reset_dt.isoformat()}"
                    )
                    return cooldown_until
                except Exception:
                    pass

            # 2. 次选：解析 quotaResetDelay（如 "2h17m30.5s"）
            reset_delay_str = metadata.get("quotaResetDelay")
            if reset_delay_str:
                unit_to_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
                parts = re.findall(r"(\d+(?:\.\d+)?)([smhd])", reset_delay_str)
                if parts:
                    cooldown_seconds = sum(
                        float(value) * unit_to_seconds[unit] for value, unit in parts
                    )
                    if cooldown_seconds > 0:
                        cooldown_until = time.time() + cooldown_seconds
                        log.info(
                            f"[{mode.upper()}] 读取到重置延迟 (quotaResetDelay): "
                            f"{reset_delay_str} → 冷却 {cooldown_seconds:.0f}s"
                        )
                        return cooldown_until

        # 3. 解析 message 中的 "Your quota will reset after Xh Ym Zs." 格式
        message = error_obj.get("message", "")
        reset_match = re.search(r"Your quota will reset after (.+?)\.", message)
        if reset_match:
            duration_str = reset_match.group(1).strip()
            unit_to_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            parts = re.findall(r"(\d+)([smhd])", duration_str)
            if parts:
                cooldown_seconds = sum(
                    int(value) * unit_to_seconds[unit] for value, unit in parts
                )
                if cooldown_seconds > 0:
                    cooldown_until = time.time() + cooldown_seconds
                    return cooldown_until

        # 4. 从错误响应无法解析出任何时间 → 返回 None
        # 调用方 (credential_manager.record_api_call_result) 会进一步从
        # 该账号的 quota_groups.resetTimeRaw 中读取真实重置时间；
        # 若 quota_groups 也没有数据，才最终兜底为 15 秒快速恢复。
        # 对于明确是 RESOURCE_EXHAUSTED/429 的情况，也不要在这里猜时长了。
        err_status = str(error_obj.get("status", "")).upper()
        if (
            err_status == "RESOURCE_EXHAUSTED"
            or error_obj.get("code") == 429
        ):
            log.debug(
                f"[{mode.upper()}] 错误响应中无精确重置时间，交由 quota_groups 兜底"
            )
            return None


        return None

    except Exception:
        return None




# ==================== 网络异常识别与退避重试辅助 ====================

def is_network_error(e: Exception) -> bool:
    """判断是否属于网络/超时/TLS/DNS/代理连接异常"""
    err_str = str(e).lower()
    err_cls = type(e).__name__.lower()
    keywords = [
        "tls connect error", "sslerror", "openssl_internal", "ssl",
        "could not resolve host", "resolve host", "connection refused",
        "connection reset", "broken pipe", "failed to connect",
        "network is unreachable", "timeout", "timed out", "timedout",
        "curle_", "curl: (28)", "curl: (7)", "curl: (35)", "curl: (56)",
        "curl: (52)", "curl: (6)", "0 bytes received", "failed to perform, curl:"
    ]
    if any(k in err_str for k in keywords):
        return True
    if any(cls_name in err_cls for cls_name in ["timeout", "curlerror", "requestexception", "connectionerror"]):
        return True
    return False


def format_network_error(e: Exception) -> str:
    """将网络/TLS/curl 底层异常转换为人性化的中文提示信息"""
    err_str = str(e)
    err_str_lower = err_str.lower()
    if any(k in err_str_lower for k in ["timeout", "timed out", "curle_operation_timedout", "curl: (28)", "0 bytes received"]):
        return "网络请求超时 (代理节点响应过慢或底层连接静默中断)"
    if any(k in err_str_lower for k in ["tls connect error", "sslerror", "openssl_internal", "ssl", "curl: (35)"]):
        return "网络连接失败 (TLS/SSL握手异常，请检查代理节点联通性)"
    if any(k in err_str_lower for k in ["could not resolve host", "resolve host", "curl: (6)"]):
        return "网络连接失败 (无法解析域名 DNS，请检查网络/代理设置)"
    if any(k in err_str_lower for k in ["connection refused", "failed to connect", "curl: (7)"]):
        return "网络连接失败 (目标地址或代理拒绝连接)"
    return f"网络请求异常: {err_str}"


def calculate_backoff_delay(attempt: int, base: float = 0.5, max_delay: float = 3.0) -> float:
    """计算带随机抖动的指数退避重试延迟"""
    import random
    delay = min(base * (2 ** attempt), max_delay)
    # 添加 10% ~ 30% 随机抖动
    jitter = random.uniform(0.1, 0.3) * delay
    return delay + jitter

