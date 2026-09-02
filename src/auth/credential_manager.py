"""
凭证管理器
"""

import asyncio
import collections
from contextvars import ContextVar
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.log import log

from .google_oauth import Credentials
from src.storage import get_storage

# 线程/协程级别的当前调度账号上下文
_current_account_var: ContextVar[Optional[str]] = ContextVar(
    "current_scheduled_account", default=None)


class CredentialManager:
    """
    统一凭证管理器
    所有存储操作通过storage_adapter进行
    """

    def set_current_account(self, account: Optional[str]) -> None:
        """设置当前协程/任务上下文调度的账号标识"""
        _current_account_var.set(account)

    def get_current_account(self) -> Optional[str]:
        """获取当前协程/任务上下文调度的账号标识"""
        return _current_account_var.get()

    def __init__(self):
        # 核心状态
        self._initialized = False
        self._storage_adapter = None
        self._last_selected_account: Dict[str, str] = {}
        # 进行中并发流计数: {filename: in_flight_count}
        self._in_flight: Dict[str, int] = collections.defaultdict(int)

    def acquire_in_flight(self, filename: str) -> None:
        """增加指定账号的进行中并发计数"""
        if filename:
            self._in_flight[os.path.basename(filename)] += 1

    def release_in_flight(self, filename: str) -> None:
        """减少指定账号的进行中并发计数"""
        if filename:
            base = os.path.basename(filename)
            self._in_flight[base] = max(0, self._in_flight[base] - 1)

    def get_in_flight(self, filename: str) -> int:
        """获取指定账号当前正在进行中的流式/请求并发数"""
        if not filename:
            return 0
        return self._in_flight.get(os.path.basename(filename), 0)

    async def _ensure_initialized(self):
        """确保管理器已初始化（内部使用）"""
        if not self._initialized or self._storage_adapter is None:
            await self.initialize()

    async def initialize(self):
        """初始化凭证管理器"""
        if self._initialized and self._storage_adapter is not None:
            return

        # 初始化统一存储
        self._storage_adapter = await get_storage()
        self._initialized = True

    async def _resolve_account_email(
        self,
        filename: str,
        mode: str = "geminicli",
        st: Optional[Dict[str, Any]] = None,
        cred_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """从 state.json 或凭证内容中读取真正的邮箱地址"""
        if st and st.get("user_email"):
            return st["user_email"]
        if not st and self._storage_adapter:
            st = await self._storage_adapter.get_credential_state(filename, mode=mode)
            if st and st.get("user_email"):
                return st["user_email"]
        if cred_data:
            c_email = cred_data.get("client_email") or cred_data.get(
                "user_email") or cred_data.get("email")
            if c_email and isinstance(c_email, str):
                return c_email
        return None

    async def close(self):
        """清理资源"""
        log.debug("Closing credential manager...")
        self._initialized = False
        log.debug("Credential manager closed")

    async def get_valid_credential(
        self, mode: str = "geminicli", model_name: Optional[str] = None, force_rotate: bool = False
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        获取有效的凭证 (账号并发感知与智能隔离调度版)
        1. 若当前激活账号空闲（in_flight == 0）、未禁用、未冷却且 Token 有效，直接复用当前账号；
        2. 若当前激活账号已有请求正在占用（in_flight > 0，如 Plan Mode 子代理并发），
           自动触发并发隔离调度，优先为新子代理分配空闲账号，彻底避免单账号并发踩踏 429。
        """
        await self._ensure_initialized()
        current_time = time.time()

        # 1. 如果没有强制要求重新调度，且当前激活账号空闲 (in_flight == 0)，优先复用当前激活账号
        if not force_rotate:
            active_filename = self._last_selected_account.get(mode)
            if active_filename and self.get_in_flight(active_filename) == 0:
                # 检查该账号目前的状态 (是否禁用、模型冷却)
                st = await self._storage_adapter.get_credential_state(active_filename, mode=mode)
                is_disabled = st.get("disabled", False)

                cooldown_until = 0
                if model_name:
                    cooldown_until = st.get(
                        "model_cooldowns", {}).get(model_name, 0)

                # 若未禁用且未在该模型的冷却中，尝试读取凭证数据
                if not is_disabled and cooldown_until <= current_time:
                    cred_data = await self._storage_adapter.get_credential(active_filename, mode=mode)
                    if cred_data:
                        # 检查 Token 是否需要刷新
                        user_acc = await self._resolve_account_email(active_filename, mode=mode, st=st, cred_data=cred_data)
                        if await self._should_refresh_token(cred_data):
                            refreshed_data = await self._refresh_token(cred_data, active_filename, mode=mode)
                            if refreshed_data:
                                cred_data = refreshed_data
                                self.set_current_account(user_acc)
                                return active_filename, cred_data
                            else:
                                log.warning(
                                    f"当前激活账号 Token 刷新失败，将重新调度: {active_filename}")
                                self._last_selected_account.pop(mode, None)
                        else:
                            # Token 有效，直接复用当前激活账号 (零重新调度、零 SSE 广播)
                            self.set_current_account(user_acc)
                            return active_filename, cred_data

        # 2. 当前激活账号忙碌 / 不存在 / 禁用 / 处于冷却 / 报错重试 -> 执行调度算法选中新账号 (优先空闲账号)
        max_retries = 3
        for attempt in range(max_retries):
            result = await self._storage_adapter.get_next_available_credential(
                mode=mode, model_name=model_name, busy_checker=self.get_in_flight
            )

            if not result:
                if attempt == 0:
                    log.warning(
                        f"没有可用凭证 (mode={mode}, model_name={model_name})")
                self._last_selected_account.pop(mode, None)
                return None

            filename, credential_data = result
            st = await self._storage_adapter.get_credential_state(filename, mode=mode)
            user_acc = await self._resolve_account_email(filename, mode=mode, st=st, cred_data=credential_data)

            if await self._should_refresh_token(credential_data):
                log.debug(f"Token需要刷新 - 文件: {filename} (mode={mode})")
                refreshed_data = await self._refresh_token(credential_data, filename, mode=mode)
                if refreshed_data:
                    credential_data = refreshed_data
                    log.debug(f"Token刷新成功: {filename} (mode={mode})")
                    self._notify_dispatch(mode, filename)
                    self.set_current_account(user_acc)
                    return filename, credential_data
                else:
                    log.warning(
                        f"Token刷新失败，尝试获取下一个凭证: {filename} (mode={mode}, attempt={attempt+1}/{max_retries})")
                    continue
            else:
                self._notify_dispatch(mode, filename)
                self.set_current_account(user_acc)
                return filename, credential_data

        self._last_selected_account.pop(mode, None)
        log.error(
            f"重试{max_retries}次后仍无可用凭证 (mode={mode}, model_name={model_name})")
        return None

    def _notify_dispatch(self, mode: str, filename: str):
        """异步广播 SSE 调度高亮通知"""
        try:
            self._last_selected_account[mode] = os.path.basename(filename)
            from src.panel.sse import sse_manager
            asyncio.create_task(sse_manager.broadcast(
                "dispatch_updated", {"mode": mode, "selected": filename}))
        except Exception:
            pass

    async def add_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """
        新增或更新一个凭证
        存储层会自动处理轮换顺序
        """
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data)
        log.info(f"Credential added/updated: {credential_name}")

    async def add_antigravity_credential(self, credential_name: str, credential_data: Dict[str, Any]):
        """
        新增或更新一个Antigravity凭证
        存储层会自动处理轮换顺序
        """
        await self._ensure_initialized()
        await self._storage_adapter.store_credential(credential_name, credential_data, mode="antigravity")
        log.info(f"Antigravity credential added/updated: {credential_name}")

    async def remove_credential(self, credential_name: str, mode: str = "geminicli") -> bool:
        """删除一个凭证"""
        await self._ensure_initialized()
        try:
            await self._storage_adapter.delete_credential(credential_name, mode=mode)
            log.info(f"Credential removed: {credential_name} (mode={mode})")
            return True
        except Exception as e:
            log.error(f"Error removing credential {credential_name}: {e}")
            return False

    async def update_credential_state(self, credential_name: str, state_updates: Dict[str, Any], mode: str = "geminicli"):
        """更新凭证状态"""
        log.debug(
            f"[CredMgr] update_credential_state 开始: credential_name={credential_name}, state_updates={state_updates}, mode={mode}")
        log.debug(f"[CredMgr] 调用 _ensure_initialized...")
        await self._ensure_initialized()
        log.debug(f"[CredMgr] _ensure_initialized 完成")
        try:
            log.debug(
                f"[CredMgr] 调用 storage_adapter.update_credential_state...")
            success = await self._storage_adapter.update_credential_state(
                credential_name, state_updates, mode=mode
            )
            log.debug(
                f"[CredMgr] storage_adapter.update_credential_state 返回: {success}")
            if success:
                log.debug(
                    f"Updated credential state: {credential_name} (mode={mode})")
            else:
                log.warning(
                    f"Failed to update credential state: {credential_name} (mode={mode})")
            return success
        except Exception as e:
            log.error(
                f"Error updating credential state {credential_name}: {e}")
            return False

    async def set_cred_disabled(self, credential_name: str, disabled: bool, mode: str = "geminicli"):
        """设置凭证的启用/禁用状态"""
        try:
            log.info(
                f"[CredMgr] set_cred_disabled 开始: credential_name={credential_name}, disabled={disabled}, mode={mode}")
            success = await self.update_credential_state(
                credential_name, {"disabled": disabled}, mode=mode
            )
            log.info(
                f"[CredMgr] update_credential_state 返回: success={success}")
            if success:
                action = "disabled" if disabled else "enabled"
                log.info(
                    f"Credential {action}: {credential_name} (mode={mode})")
                # 只有当禁用的账号属于当前正在使用的账号时，才重新触发调度切走账号
                if disabled:
                    target_name = os.path.basename(credential_name)
                    current_context_acc = self.get_current_account()
                    last_active_acc = self._last_selected_account.get(mode)

                    is_in_use = (
                        (current_context_acc and (current_context_acc == target_name or os.path.basename(current_context_acc) == target_name)) or
                        (last_active_acc and os.path.basename(
                            last_active_acc) == target_name)
                    )

                    if is_in_use:
                        log.info(
                            f"[CredMgr] 被禁用的账号 {target_name} 为当前在用账号，触发重新调度...")
                        asyncio.create_task(
                            self.get_valid_credential(mode=mode))
            else:
                log.warning(
                    f"[CredMgr] 设置禁用状态失败: credential_name={credential_name}, disabled={disabled}")
            return success
        except Exception as e:
            log.error(
                f"Error setting credential disabled state {credential_name}: {e}")
            return False

    async def set_active_account(self, credential_name: str, mode: str = "geminicli"):
        """手动设定指定模式的当前激活/调度账号，并广播 SSE 高亮"""
        filename = os.path.basename(credential_name)
        self._last_selected_account[mode] = filename
        self._notify_dispatch(mode, filename)

    async def get_active_account_filename(self, mode: str = "geminicli") -> Optional[str]:
        """获取当前激活的账号文件名（只读检索，若已手动/上次调度且有效则返回，否则返回 None，绝对不触发自动调度）"""
        await self._ensure_initialized()
        active_filename = self._last_selected_account.get(mode)

        if active_filename:
            st = await self._storage_adapter.get_credential_state(active_filename, mode=mode)
            if not st.get("disabled", False):
                cred_data = await self._storage_adapter.get_credential(active_filename, mode=mode)
                if cred_data:
                    return active_filename
            # 若设定的账号已被禁用或删除，清除失效激活记录
            self._last_selected_account.pop(mode, None)

        return None

    async def get_creds_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证的状态"""
        await self._ensure_initialized()
        try:
            return await self._storage_adapter.get_all_credential_states()
        except Exception as e:
            log.error(f"Error getting credential statuses: {e}")
            return {}

    async def get_creds_summary(self) -> List[Dict[str, Any]]:
        """
        获取所有凭证的摘要信息（轻量级，不包含完整凭证数据）
        使用后端的高性能查询
        """
        await self._ensure_initialized()
        try:
            return await self._storage_adapter.get_credentials_summary()
        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return []

    async def get_or_fetch_user_email(self, credential_name: str, mode: str = "geminicli") -> Optional[str]:
        """获取或获取用户邮箱地址"""
        try:
            # 确保已初始化
            await self._ensure_initialized()

            # 从状态中获取缓存的邮箱
            state = await self._storage_adapter.get_credential_state(credential_name, mode=mode)
            cached_email = state.get("user_email") if state else None

            if cached_email:
                return cached_email

            # 如果没有缓存，从凭证数据获取
            credential_data = await self._storage_adapter.get_credential(credential_name, mode=mode)
            if not credential_data:
                return None

            # 创建凭证对象并自动刷新 token
            from .google_oauth import Credentials, get_user_email

            credentials = Credentials.from_dict(credential_data)
            if not credentials:
                return None

            # 自动刷新 token（如果需要）
            token_refreshed = await credentials.refresh_if_needed()

            # 如果 token 被刷新了，更新存储
            if token_refreshed:
                log.info(f"Token已自动刷新: {credential_name} (mode={mode})")
                updated_data = credentials.to_dict()
                await self._storage_adapter.store_credential(credential_name, updated_data, mode=mode)

            # 获取邮箱
            email = await get_user_email(credentials)

            if email:
                # 缓存邮箱地址
                await self._storage_adapter.update_credential_state(
                    credential_name, {"user_email": email}, mode=mode
                )
                return email

            return None

        except Exception as e:
            log.error(f"Error fetching user email for {credential_name}: {e}")
            return None

    async def record_api_call_result(
        self,
        credential_name: str,
        success: bool,
        error_code: Optional[int] = None,
        cooldown_until: Optional[float] = None,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """
        记录API调用结果

        Args:
            credential_name: 凭证名称
            success: 是否成功
            error_code: 错误码（如果失败）
            cooldown_until: 冷却截止时间戳（Unix时间戳，针对429 QUOTA_EXHAUSTED）
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 模型名（用于设置模型级冷却）
            error_message: 错误信息（如果失败）
        """
        await self._ensure_initialized()
        try:
            if success:
                # 条件写入：仅当凭证有错误状态或模型冷却时才写 DB，零内存缓存
                # fire-and-forget，不阻塞请求链路
                asyncio.create_task(
                    self._storage_adapter.record_success(
                        credential_name, model_name=model_name, mode=mode
                    )
                )

            elif error_code:
                # 记录错误码和错误信息
                error_messages = {}
                if error_message:
                    error_messages[str(error_code)] = error_message

                state_updates = {
                    "error_codes": [error_code],
                    "error_messages": error_messages,
                }

                await self.update_credential_state(credential_name, state_updates, mode=mode)

                # 针对 429/503 等限流/服务不可用错误：若未能从响应体解析出具体 reset 时间，使用默认 15 秒快速恢复冷却
                if (error_code in (429, 503)) and (cooldown_until is None or cooldown_until <= time.time()):
                    cooldown_until = time.time() + 15

                # 设置模型级冷却
                if cooldown_until is not None:
                    target_model = model_name or "default"
                    if hasattr(self._storage_adapter, 'set_model_cooldown'):
                        await self._storage_adapter.set_model_cooldown(
                            credential_name, target_model, cooldown_until, mode=mode
                        )
                        log.info(
                            f"[CredMgr] 设置模型级冷却: {credential_name}, model_name={target_model}, "
                            f"冷却至: {datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat()}"
                        )

        except Exception as e:
            log.error(
                f"Error recording API call result for {credential_name}: {e}")

    async def _should_refresh_token(self, credential_data: Dict[str, Any]) -> bool:
        """检查token是否需要刷新"""
        try:
            # 如果没有access_token或过期时间，需要刷新
            if not credential_data.get("access_token") and not credential_data.get("token"):
                log.debug("没有access_token，需要刷新")
                return True

            expiry_str = credential_data.get("expiry")
            if not expiry_str:
                log.debug("没有过期时间，需要刷新")
                return True

            # 解析过期时间
            try:
                if isinstance(expiry_str, str):
                    if "+" in expiry_str:
                        file_expiry = datetime.fromisoformat(expiry_str)
                    elif expiry_str.endswith("Z"):
                        file_expiry = datetime.fromisoformat(
                            expiry_str.replace("Z", "+00:00"))
                    else:
                        file_expiry = datetime.fromisoformat(expiry_str)
                else:
                    log.debug("过期时间格式无效，需要刷新")
                    return True

                # 确保时区信息
                if file_expiry.tzinfo is None:
                    file_expiry = file_expiry.replace(tzinfo=timezone.utc)

                # 检查是否还有至少5分钟有效期
                now = datetime.now(timezone.utc)
                time_left = (file_expiry - now).total_seconds()

                log.debug(
                    f"Token时间检查: "
                    f"当前UTC时间={now.isoformat()}, "
                    f"过期时间={file_expiry.isoformat()}, "
                    f"剩余时间={int(time_left/60)}分{int(time_left % 60)}秒"
                )

                if time_left > 300:  # 5分钟缓冲
                    return False
                else:
                    log.debug(f"Token即将过期（剩余{int(time_left/60)}分钟），需要刷新")
                    return True

            except Exception as e:
                log.warning(f"解析过期时间失败: {e}，需要刷新")
                return True

        except Exception as e:
            log.error(f"检查token过期时出错: {e}")
            return True

    async def _refresh_token(
        self, credential_data: Dict[str, Any], filename: str, mode: str = "geminicli"
    ) -> Optional[Dict[str, Any]]:
        """刷新token并更新存储"""
        await self._ensure_initialized()
        try:
            # 创建Credentials对象
            creds = Credentials.from_dict(credential_data)

            # 检查是否可以刷新
            if not creds.refresh_token:
                log.error(f"没有refresh_token，无法刷新: {filename} (mode={mode})")
                # 自动禁用没有refresh_token的凭证
                try:
                    await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    log.warning(f"凭证已自动禁用（缺少refresh_token）: {filename}")
                except Exception as e:
                    log.error(f"禁用凭证失败 {filename}: {e}")
                return None

            # 刷新token
            log.debug(f"正在刷新token: {filename} (mode={mode})")
            await creds.refresh()

            # 更新凭证数据
            if creds.access_token:
                credential_data["access_token"] = creds.access_token
                # 保持兼容性
                credential_data["token"] = creds.access_token

            if creds.expires_at:
                credential_data["expiry"] = creds.expires_at.isoformat()

            # 保存到存储
            await self._storage_adapter.store_credential(filename, credential_data, mode=mode)
            log.info(f"Token刷新成功并已保存: {filename} (mode={mode})")

            return credential_data

        except Exception as e:
            error_msg = str(e)
            log.error(f"Token刷新失败 {filename} (mode={mode}): {error_msg}")

            # 尝试提取HTTP状态码（TokenError可能携带status_code属性）
            status_code = None
            if hasattr(e, 'status_code'):
                status_code = e.status_code

            # 检查是否是凭证永久失效的错误（只有明确的400/403等才判定为永久失效）
            is_permanent_failure = self._is_permanent_refresh_failure(
                error_msg, status_code)

            if is_permanent_failure:
                log.warning(f"检测到凭证永久失效 (HTTP {status_code}): {filename}")
                # 记录失效状态
                if status_code:
                    await self.record_api_call_result(filename, False, status_code, mode=mode)
                else:
                    await self.record_api_call_result(filename, False, 400, mode=mode)

                # 禁用失效凭证
                try:
                    # 直接禁用该凭证（随机选择机制会自动跳过它）
                    disabled_ok = await self.update_credential_state(filename, {"disabled": True}, mode=mode)
                    if disabled_ok:
                        log.warning(f"永久失效凭证已禁用: {filename}")
                    else:
                        log.warning("永久失效凭证禁用失败，将由上层逻辑继续处理")
                except Exception as e2:
                    log.error(f"禁用永久失效凭证时出错 {filename}: {e2}")
            else:
                # 网络错误或其他临时性错误，不封禁凭证
                log.warning(
                    f"Token刷新失败但非永久性错误 (HTTP {status_code})，不封禁凭证: {filename}")

            return None

    def _is_permanent_refresh_failure(self, error_msg: str, status_code: Optional[int] = None) -> bool:
        """
        判断是否是凭证永久失效的错误

        Args:
            error_msg: 错误信息
            status_code: HTTP状态码（如果有）

        Returns:
            True表示凭证永久失效应封禁，False表示临时错误不应封禁
        """
        # 优先使用HTTP状态码判断
        if status_code is not None:
            # 400/401/403 明确表示凭证有问题，应该封禁
            if status_code in [400, 401, 403]:
                log.debug(f"检测到客户端错误状态码 {status_code}，判定为永久失效")
                return True
            # 500/502/503/504 是服务器错误，不应封禁凭证
            elif status_code in [500, 502, 503, 504]:
                log.debug(f"检测到服务器错误状态码 {status_code}，不应封禁凭证")
                return False
            # 429 (限流) 不应封禁凭证
            elif status_code == 429:
                log.debug("检测到限流错误 429，不应封禁凭证")
                return False

        # 如果没有状态码，回退到错误信息匹配（谨慎判断）
        # 只有明确的凭证失效错误才判定为永久失效
        permanent_error_patterns = [
            "invalid_grant",
            "refresh_token_expired",
            "invalid_refresh_token",
            "unauthorized_client",
            "access_denied",
        ]

        error_msg_lower = error_msg.lower()
        for pattern in permanent_error_patterns:
            if pattern.lower() in error_msg_lower:
                log.debug(f"错误信息匹配到永久失效模式: {pattern}")
                return True

        # 默认认为是临时错误（如网络问题），不应封禁凭证
        log.debug("未匹配到明确的永久失效模式，判定为临时错误")
        return False


class _CredentialManagerSingleton:
    """单例包装器，支持懒加载和自动初始化"""

    _instance: Optional[CredentialManager] = None
    _lock = None

    def __init__(self):
        self._manager = None

    async def _get_or_create(self) -> CredentialManager:
        """获取或创建单例实例（线程安全）"""
        if self._instance is None:
            # 简单的实例创建（异步环境下一般不需要复杂的锁）
            if self._instance is None:
                self._instance = CredentialManager()
                await self._instance.initialize()
                log.debug("CredentialManager singleton initialized")

        return self._instance

    def get_current_account(self) -> Optional[str]:
        """获取当前协程/任务上下文调度的账号标识"""
        if self._instance:
            return self._instance.get_current_account()
        return _current_account_var.get()

    def set_current_account(self, account: Optional[str]) -> None:
        """设置当前协程/任务上下文调度的账号标识"""
        if self._instance:
            self._instance.set_current_account(account)
        else:
            _current_account_var.set(account)

    def acquire_in_flight(self, filename: str) -> None:
        if self._instance:
            self._instance.acquire_in_flight(filename)

    def release_in_flight(self, filename: str) -> None:
        if self._instance:
            self._instance.release_in_flight(filename)

    def get_in_flight(self, filename: str) -> int:
        if self._instance:
            return self._instance.get_in_flight(filename)
        return 0

    def __getattr__(self, name):
        """代理所有方法调用到真实的 CredentialManager 实例"""
        async def _async_wrapper(*args, **kwargs):
            manager = await self._get_or_create()
            method = getattr(manager, name)
            return await method(*args, **kwargs)

        return _async_wrapper


# 全局单例实例 - 直接导入即可使用
credential_manager = _CredentialManagerSingleton()
