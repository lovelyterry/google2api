"""
配额保鲜预热服务 (Quota Warmup Service)
当检测到启用账号 100% 满额且连续闲置超过设定时长（默认 4.5 小时）时，
后台定时向 API 发送极微量探针请求 (hi / maxOutputTokens=1)，
主动激活 Google 官方的 5小时/周 滚动刷新倒计时窗口，避免额度在闲置中浪费。
"""

import asyncio
import time
from typing import Optional

import src.config as config
from src.log import log
from src.storage import get_storage


class QuotaWarmupService:
    """账号配额保鲜预热服务"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._check_interval: int = 5 * 60  # 每 5 分钟轮询检测一轮全量账号

    async def _probe_single_credential(self, filename: str, mode: str = "antigravity", storage_adapter=None) -> bool:
        """对单个闲置满额凭证发送极微量探针以激活 5小时/周 滚动窗口"""
        try:
            if storage_adapter is None:
                storage_adapter = await get_storage()

            credential_data = await storage_adapter.get_credential(filename, mode=mode)
            if not credential_data:
                return False

            from src.auth import Credentials
            from src.utils import ANTIGRAVITY_CLIENT_ID, ANTIGRAVITY_CLIENT_SECRET
            if mode == "antigravity":
                credential_data.setdefault("client_id", ANTIGRAVITY_CLIENT_ID)
                credential_data.setdefault(
                    "client_secret", ANTIGRAVITY_CLIENT_SECRET)

            creds = Credentials.from_dict(credential_data)
            await creds.refresh_if_needed()

            updated_data = dict(credential_data)
            updated_data.update(creds.to_dict())
            if updated_data != credential_data:
                await storage_adapter.store_credential(filename, updated_data, mode=mode)
                credential_data = updated_data

            access_token = credential_data.get(
                "access_token") or credential_data.get("token")
            project_id = credential_data.get("project_id", "")
            if not access_token or not project_id:
                return False

            from src.client import post_async
            from src.config import get_antigravity_api_url, get_code_assist_endpoint
            from src.utils import GEMINICLI_USER_AGENT

            test_model = "gemini-3.6-flash-medium"
            if mode == "antigravity":
                api_base_url = await get_antigravity_api_url()
                from src.api.antigravity import build_antigravity_headers
                headers = build_antigravity_headers(access_token)
            else:
                api_base_url = await get_code_assist_endpoint()
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": GEMINICLI_USER_AGENT,
                }

            response = await post_async(
                url=f"{api_base_url}/v1internal:generateContent",
                json={
                    "model": test_model,
                    "project": project_id,
                    "request": {
                        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                        "generationConfig": {"maxOutputTokens": 1}
                    }
                },
                headers=headers,
                timeout=15.0
            )

            current_time = time.time()
            await storage_adapter.update_credential_state(
                filename,
                {"last_warmup_time": current_time,
                    "last_touch_time": current_time},
                mode=mode
            )

            if response.status_code == 200:
                log.info(
                    f"⚡ [QuotaWarmup] 凭证 {filename} ({mode}) 成功发送微量保鲜探针，已激活 Google 5小时/周 滚动窗口")
                try:
                    from src.panel.quota import quota_refresh_service
                    asyncio.create_task(
                        quota_refresh_service.refresh_single(filename, mode=mode))
                except Exception:
                    pass
                return True
            else:
                log.debug(
                    f"[QuotaWarmup] 探针响应非 200 ({response.status_code}): {filename}")
                return False
        except Exception as e:
            log.warning(f"[QuotaWarmup] 发送保鲜探针异常 {filename}: {e}")
            return False

    async def check_and_warmup(self):
        """检查全量账号，为满额且闲置超过设定时长的账号自动预热"""
        try:
            enabled = await config.get_quota_warmup_enabled()
            if not enabled:
                return

            idle_hours = await config.get_quota_warmup_idle_hours()
            idle_threshold_seconds = max(0.5, float(idle_hours)) * 3600.0
            current_time = time.time()

            storage_adapter = await get_storage()

            for mode in ["antigravity", "geminicli"]:
                filenames = await storage_adapter.list_credentials(mode=mode)
                if not filenames:
                    continue

                states = await storage_adapter.get_all_credential_states(mode=mode)

                for fn in filenames:
                    st = states.get(fn, {})
                    if st.get("disabled", False):
                        continue

                    last_touch = st.get("last_touch_time") or st.get(
                        "last_warmup_time") or st.get("last_used_time") or 0
                    elapsed = current_time - last_touch

                    # 检查是否 100% 满额
                    quota_groups = st.get("quota_groups", [])
                    is_full = True
                    if quota_groups:
                        for g in quota_groups:
                            for b in g.get("buckets", []):
                                frac = b.get("remainingFraction", 1.0)
                                if frac < 0.99:
                                    is_full = False
                                    break
                            if not is_full:
                                break

                    # 当账号满额且连续闲置满设定时长时，触发微量探针打点
                    if is_full and elapsed >= idle_threshold_seconds:
                        log.info(
                            f"发现闲置满额账号 {fn} ({mode})，闲置时间 {elapsed/3600:.2f}h >= {idle_hours}h，开始保鲜打点...")
                        await self._probe_single_credential(fn, mode=mode, storage_adapter=storage_adapter)
                        await asyncio.sleep(2.0)

            # 静默后台预热与更新 Antigravity 可用模型列表缓存
            try:
                from src.api.antigravity import fetch_available_models
                await fetch_available_models(force_refresh=True)
            except Exception:
                pass
        except Exception as e:
            log.error(f"[QuotaWarmup] 配额保鲜预热检查失败: {e}")

    async def _run(self):
        """后台轮询主循环"""
        log.info("账号配额保鲜预热服务已启动")
        while True:
            try:
                await self.check_and_warmup()
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"[QuotaWarmup] 循环任务异常: {e}")
                await asyncio.sleep(60)

    async def start(self):
        """启动定时服务"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name="quota_warmup_service")

    async def stop(self):
        """停止定时服务"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            log.info("[QuotaWarmup] 账号配额保鲜预热服务已停止")
        self._task = None


quota_warmup_service = QuotaWarmupService()
