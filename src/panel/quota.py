"""
Antigravity 凭证额度定时刷新服务
默认一个账号每 1 分钟自动向 API 查询并更新 Antigravity 凭证的最新额度
"""

import asyncio
from typing import Optional

from log import log
from src.storage import get_storage


class QuotaRefreshService:
    """Antigravity 额度定时刷新服务"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._interval: int = 1 * 60  # 1 分钟 (60秒)

    async def _refresh_credential_quota(self, filename: str, storage_adapter) -> bool:
        try:
            credential_data = await storage_adapter.get_credential(filename, mode="antigravity")
            if not credential_data:
                return False

            from src.auth import Credentials
            from src.utils import ANTIGRAVITY_CLIENT_ID, ANTIGRAVITY_CLIENT_SECRET
            credential_data.setdefault("client_id", ANTIGRAVITY_CLIENT_ID)
            credential_data.setdefault("client_secret", ANTIGRAVITY_CLIENT_SECRET)
            creds = Credentials.from_dict(credential_data)
            await creds.refresh_if_needed()

            updated_data = dict(credential_data)
            updated_data.update(creds.to_dict())
            if updated_data != credential_data:
                await storage_adapter.store_credential(filename, updated_data, mode="antigravity")
                credential_data = updated_data

            access_token = credential_data.get("access_token") or credential_data.get("token")
            if not access_token:
                return False

            from src.api.antigravity import fetch_quota_summary
            project_id = credential_data.get("project_id")
            quota_summary = await fetch_quota_summary(access_token, project_id=project_id)

            if quota_summary.get("success"):
                groups = quota_summary.get("groups", [])
                gemini_groups = [g for g in groups if "GEMINI" in g.get("displayName", "").upper()] or groups
                await storage_adapter.update_credential_state(
                    filename,
                    {"quota_groups": gemini_groups},
                    mode="antigravity"
                )
                return True
        except Exception as e:
            log.warning(f"[QuotaRefresh] 刷新 {filename} 额度失败: {e}")
        return False

    async def refresh_all(self):
        """均衡平滑地在 1 分钟内完成所有 Antigravity 凭证额度刷新"""
        try:
            storage_adapter = await get_storage()
            filenames = await storage_adapter.list_credentials(mode="antigravity")
            if not filenames:
                return

            states = await storage_adapter.get_all_credential_states(mode="antigravity")
            active_filenames = [
                fn for fn in filenames
                if not states.get(fn, {}).get("disabled", False)
            ]

            count = len(active_filenames)
            if count == 0:
                return

            # 将 1 分钟 (60 秒) 均匀平摊给所有活跃账号
            step_delay = max(1.0, self._interval / count)

            log.debug(f"⏰ [QuotaRefresh] 开始平滑定时刷新 {count} 个 Antigravity 凭证额度 (平均每 {step_delay:.1f} 秒刷新 1 个账号)...")
            for fn in active_filenames:
                await self._refresh_credential_quota(fn, storage_adapter)
                await asyncio.sleep(step_delay)
            log.debug("✅ [QuotaRefresh] 完成本轮 Antigravity 凭证额度平滑刷新")
        except Exception as e:
            log.error(f"[QuotaRefresh] 自动刷新额度过程出错: {e}")

    async def _run(self):
        """后台轮询主循环"""
        log.debug("[QuotaRefresh] Antigravity 额度 1 分钟均匀平滑刷新任务已启动")
        while True:
            try:
                await self.refresh_all()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"[QuotaRefresh] 循环任务异常: {e}")
                await asyncio.sleep(30)

    async def start(self):
        """启动定时服务"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="antigravity_quota_refresh_service")

    async def stop(self):
        """停止定时服务"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("[QuotaRefresh] 定时刷新额度服务已停止")
        self._task = None


quota_refresh_service = QuotaRefreshService()
