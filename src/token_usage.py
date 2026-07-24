"""
Token 统计、估算与看板持久化服务 (Token Usage & Tracker Module)
包含：
1. tiktoken BPE 输入 Prompt Token 估算 (estimate_input_tokens)
2. API 响应包 usageMetadata 解析与日志记录 (count_token_usage)
3. Token 流量与请求次数的按天/按账号/按模型持久化统计与看板 API 支持 (token_tracker)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
from src.log import log

# 北京时间区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))


# ==============================================================================
# 1. Token 持久化统计与看板服务 (Token Tracker & Dashboard Service)
# ==============================================================================

class TokenTracker:
    """Token 消耗统计与持久化管理类"""

    def __init__(self):
        self._stats_dir: Optional[str] = None
        self._stats_file: Optional[str] = None
        self._lock = asyncio.Lock()
        self._initialized = False

        self._data: Dict[str, Any] = {
            "daily": {},
            "accounts": {},
            "models": {},
        }

    async def initialize(self) -> None:
        """初始化存储与数据加载"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                creds_dir = os.getenv("CREDENTIALS_DIR", "./creds")
                self._stats_dir = creds_dir
                os.makedirs(self._stats_dir, exist_ok=True)
                self._stats_file = os.path.join(self._stats_dir, "token_stats.json")

                if os.path.exists(self._stats_file):
                    try:
                        async with aiofiles.open(self._stats_file, "r", encoding="utf-8") as f:
                            content = await f.read()
                            if content.strip():
                                loaded = json.loads(content)
                                if isinstance(loaded, dict):
                                    self._data["daily"] = loaded.get("daily", {})
                                    self._data["accounts"] = loaded.get("accounts", {})
                                    self._data["models"] = loaded.get("models", {})
                    except Exception as e:
                        log.error(f"[TokenTracker] 读取统计文件 {self._stats_file} 失败: {e}")

                self._initialized = True
            except Exception as e:
                log.error(f"[TokenTracker] 初始化失败: {e}")

    async def _save(self) -> None:
        """保存统计数据到磁盘文件"""
        if not self._stats_file:
            return
        temp_file = self._stats_file + ".tmp"
        try:
            content = json.dumps(self._data, ensure_ascii=False, indent=2)
            async with aiofiles.open(temp_file, "w", encoding="utf-8") as f:
                await f.write(content)
            os.replace(temp_file, self._stats_file)
        except Exception as e:
            log.error(f"[TokenTracker] 保存统计数据失败: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    async def record_usage(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        thoughts_tokens: int = 0,
        model: str = "unknown",
        user_info: Optional[str] = None,
    ) -> None:
        """记录一次调用的 Token 消耗"""
        if not self._initialized:
            await self.initialize()

        prompt_cnt = max(0, int(prompt_tokens or 0))
        comp_cnt = max(0, int(completion_tokens or 0))
        cached_cnt = max(0, int(cached_tokens or 0))
        thoughts_cnt = max(0, int(thoughts_tokens or 0))
        net_prompt_cnt = max(0, prompt_cnt - cached_cnt)
        total_cnt = net_prompt_cnt + comp_cnt

        today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        account_name = str(user_info or "anonymous").strip()
        model_name = str(model or "unknown").strip()

        async with self._lock:
            # 1. 每日统计
            if today_str not in self._data["daily"]:
                self._data["daily"][today_str] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                    "request_count": 0,
                }
            d_item = self._data["daily"][today_str]
            d_item["prompt_tokens"] += prompt_cnt
            d_item["completion_tokens"] += comp_cnt
            d_item["cached_tokens"] = d_item.get("cached_tokens", 0) + cached_cnt
            d_item["thoughts_tokens"] = d_item.get("thoughts_tokens", 0) + thoughts_cnt
            d_item["total_tokens"] += total_cnt
            d_item["request_count"] += 1

            # 2. 账号统计
            if account_name not in self._data["accounts"]:
                self._data["accounts"][account_name] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                    "request_count": 0,
                }
            a_item = self._data["accounts"][account_name]
            a_item["prompt_tokens"] += prompt_cnt
            a_item["completion_tokens"] += comp_cnt
            a_item["cached_tokens"] = a_item.get("cached_tokens", 0) + cached_cnt
            a_item["thoughts_tokens"] = a_item.get("thoughts_tokens", 0) + thoughts_cnt
            a_item["total_tokens"] += total_cnt
            a_item["request_count"] += 1

            # 3. 模型统计
            if model_name not in self._data["models"]:
                self._data["models"][model_name] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "thoughts_tokens": 0,
                    "total_tokens": 0,
                    "request_count": 0,
                }
            m_item = self._data["models"][model_name]
            m_item["prompt_tokens"] += prompt_cnt
            m_item["completion_tokens"] += comp_cnt
            m_item["cached_tokens"] = m_item.get("cached_tokens", 0) + cached_cnt
            m_item["thoughts_tokens"] = m_item.get("thoughts_tokens", 0) + thoughts_cnt
            m_item["total_tokens"] += total_cnt
            m_item["request_count"] += 1

            await self._save()

        try:
            from src.panel.sse import sse_manager
            asyncio.create_task(sse_manager.broadcast("tokens_updated", {}))
        except Exception:
            pass

    async def get_dashboard_stats(self) -> Dict[str, Any]:
        """组装前端 TOKEN 看板所需的所有聚合指标与趋势数据"""
        if not self._initialized:
            await self.initialize()

        async with self._lock:
            daily_dict = self._data.get("daily", {})
            accounts_dict = self._data.get("accounts", {})
            models_dict = self._data.get("models", {})

            total_tokens = sum(item.get("total_tokens", 0) for item in daily_dict.values())
            prompt_tokens = sum(item.get("prompt_tokens", 0) for item in daily_dict.values())
            completion_tokens = sum(item.get("completion_tokens", 0) for item in daily_dict.values())
            cached_tokens = sum(item.get("cached_tokens", 0) for item in daily_dict.values())
            thoughts_tokens = sum(item.get("thoughts_tokens", 0) for item in daily_dict.values())
            total_requests = sum(item.get("request_count", 0) for item in daily_dict.values())

            now = datetime.now(BEIJING_TZ)
            today_str = now.strftime("%Y-%m-%d")
            today_tokens = daily_dict.get(today_str, {}).get("total_tokens", 0)

            monday = now - timedelta(days=now.weekday())
            monday_str = monday.strftime("%Y-%m-%d")
            this_week_tokens = sum(
                item.get("total_tokens", 0)
                for d_str, item in daily_dict.items()
                if d_str >= monday_str
            )

            month_prefix = now.strftime("%Y-%m")
            this_month_tokens = sum(
                item.get("total_tokens", 0)
                for d_str, item in daily_dict.items()
                if d_str.startswith(month_prefix)
            )

            summary = {
                "total_tokens": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "thoughts_tokens": thoughts_tokens,
                "total_requests": total_requests,
                "today_tokens": today_tokens,
                "this_week_tokens": this_week_tokens,
                "this_month_tokens": this_month_tokens,
            }

            account_ranking = []
            for acc, item in accounts_dict.items():
                account_ranking.append({
                    "account": acc,
                    "prompt_tokens": item.get("prompt_tokens", 0),
                    "completion_tokens": item.get("completion_tokens", 0),
                    "cached_tokens": item.get("cached_tokens", 0),
                    "thoughts_tokens": item.get("thoughts_tokens", 0),
                    "total_tokens": item.get("total_tokens", 0),
                    "request_count": item.get("request_count", 0),
                })
            account_ranking.sort(key=lambda x: x["total_tokens"], reverse=True)

            model_ranking = []
            for mdl, item in models_dict.items():
                model_ranking.append({
                    "model": mdl,
                    "prompt_tokens": item.get("prompt_tokens", 0),
                    "completion_tokens": item.get("completion_tokens", 0),
                    "cached_tokens": item.get("cached_tokens", 0),
                    "thoughts_tokens": item.get("thoughts_tokens", 0),
                    "total_tokens": item.get("total_tokens", 0),
                    "request_count": item.get("request_count", 0),
                })
            model_ranking.sort(key=lambda x: x["total_tokens"], reverse=True)

            daily_list = []
            for i in range(29, -1, -1):
                day_date = now - timedelta(days=i)
                d_str = day_date.strftime("%Y-%m-%d")
                day_item = daily_dict.get(d_str, {})
                daily_list.append({
                    "date": d_str,
                    "prompt_tokens": day_item.get("prompt_tokens", 0),
                    "completion_tokens": day_item.get("completion_tokens", 0),
                    "cached_tokens": day_item.get("cached_tokens", 0),
                    "thoughts_tokens": day_item.get("thoughts_tokens", 0),
                    "total_tokens": day_item.get("total_tokens", 0),
                    "request_count": day_item.get("request_count", 0),
                })

            weekly_dict: Dict[str, Dict[str, int]] = {}
            for d_str, item in daily_dict.items():
                try:
                    dt = datetime.strptime(d_str, "%Y-%m-%d")
                    w_monday = dt - timedelta(days=dt.weekday())
                    w_str = w_monday.strftime("%Y-%m-%d")
                    if w_str not in weekly_dict:
                        weekly_dict[w_str] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "request_count": 0}
                    weekly_dict[w_str]["prompt_tokens"] += item.get("prompt_tokens", 0)
                    weekly_dict[w_str]["completion_tokens"] += item.get("completion_tokens", 0)
                    weekly_dict[w_str]["total_tokens"] += item.get("total_tokens", 0)
                    weekly_dict[w_str]["request_count"] += item.get("request_count", 0)
                except Exception:
                    pass

            weekly_list = []
            for i in range(11, -1, -1):
                w_monday = (now - timedelta(days=now.weekday())) - timedelta(weeks=i)
                w_str = w_monday.strftime("%Y-%m-%d")
                w_item = weekly_dict.get(w_str, {})
                weekly_list.append({
                    "date": f"周({w_str[5:]})",
                    "prompt_tokens": w_item.get("prompt_tokens", 0),
                    "completion_tokens": w_item.get("completion_tokens", 0),
                    "total_tokens": w_item.get("total_tokens", 0),
                    "request_count": w_item.get("request_count", 0),
                })

            monthly_dict: Dict[str, Dict[str, int]] = {}
            for d_str, item in daily_dict.items():
                m_str = d_str[:7]
                if m_str not in monthly_dict:
                    monthly_dict[m_str] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "request_count": 0}
                monthly_dict[m_str]["prompt_tokens"] += item.get("prompt_tokens", 0)
                monthly_dict[m_str]["completion_tokens"] += item.get("completion_tokens", 0)
                monthly_dict[m_str]["total_tokens"] += item.get("total_tokens", 0)
                monthly_dict[m_str]["request_count"] += item.get("request_count", 0)

            monthly_list = []
            for m_str, m_item in sorted(monthly_dict.items())[-12:]:
                monthly_list.append({
                    "date": m_str,
                    "prompt_tokens": m_item.get("prompt_tokens", 0),
                    "completion_tokens": m_item.get("completion_tokens", 0),
                    "total_tokens": m_item.get("total_tokens", 0),
                    "request_count": m_item.get("request_count", 0),
                })

            trend = {
                "daily": daily_list,
                "weekly": weekly_list,
                "monthly": monthly_list,
            }

            return {
                "summary": summary,
                "account_ranking": account_ranking,
                "model_ranking": model_ranking,
                "trend": trend,
            }

    async def clear_stats(self) -> None:
        """清空统计数据"""
        if not self._initialized:
            await self.initialize()

        async with self._lock:
            self._data = {
                "daily": {},
                "accounts": {},
                "models": {},
            }
            await self._save()

        try:
            from src.panel.sse import sse_manager
            asyncio.create_task(sse_manager.broadcast("tokens_updated", {}))
        except Exception:
            pass


# 全局单例
token_tracker = TokenTracker()


# ==============================================================================
# 2. Token 预估 (Estimator) - 基于 tiktoken (BPE 词表) 与结构化 Payload 解析
# ==============================================================================

_tiktoken_encoding = None
_tiktoken_initialized = False


def _get_tiktoken_encoding() -> Optional[Any]:
    """懒加载 tiktoken (cl100k_base) 编码器"""
    global _tiktoken_encoding, _tiktoken_initialized
    if _tiktoken_initialized:
        return _tiktoken_encoding

    _tiktoken_initialized = True
    try:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        log.info("成功初始化 tiktoken (cl100k_base) 分词器")
    except Exception as e:
        log.warning(f"初始化 tiktoken 失败: {e}，将采用加权降级算法")

    return _tiktoken_encoding


def _count_text_tokens(text: str) -> int:
    """使用 tiktoken 计算单个文本字符串的 Token 数"""
    if not text:
        return 0
    encoding = _get_tiktoken_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            return _fallback_text_tokens(text)
    return _fallback_text_tokens(text)


def _parse_image_dimensions(base64_str: str) -> Tuple[Optional[int], Optional[int]]:
    """从 Base64 文本中轻量解析图片的像素宽度和高度 (支持 PNG, JPEG, WEBP, GIF)"""
    if not base64_str:
        return None, None
    try:
        if "," in base64_str:
            base64_str = base64_str.split(",", 1)[1]
        data_bytes = base64.b64decode(base64_str[:344])
        if len(data_bytes) < 16:
            return None, None

        if data_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            w, h = struct.unpack(">II", data_bytes[16:24])
            return w, h

        if data_bytes.startswith(b"GIF87a") or data_bytes.startswith(b"GIF89a"):
            w, h = struct.unpack("<HH", data_bytes[6:10])
            return w, h

        if data_bytes.startswith(b"RIFF") and data_bytes[8:12] == b"WEBP":
            if data_bytes[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", data_bytes[26:30])
                return w & 0x3FFF, h & 0x3FFF
            elif data_bytes[12:16] == b"VP8L":
                b0, b1, b2, b3 = data_bytes[21:25]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0xF) << 12) | (b2 << 4) | ((b1 & 0xC0) >> 6))
                return w, h
            elif data_bytes[12:16] == b"VP8X":
                w = 1 + struct.unpack("<I", data_bytes[24:27] + b"\x00")[0]
                h = 1 + struct.unpack("<I", data_bytes[27:30] + b"\x00")[0]
                return w, h

        if data_bytes.startswith(b"\xff\xd8"):
            idx = 2
            while idx + 4 < len(data_bytes):
                marker, length = struct.unpack(">HH", data_bytes[idx:idx + 4])
                if marker in (0xFFC0, 0xFFC1, 0xFFC2):
                    if idx + 9 <= len(data_bytes):
                        h, w = struct.unpack(">HH", data_bytes[idx + 5:idx + 9])
                        return w, h
                    break
                idx += 2 + length
    except Exception:
        pass
    return None, None


def _calculate_image_tokens(image_item: Dict[str, Any]) -> int:
    """根据图片实际分辨率计算 Token 消耗"""
    base64_data = ""
    if "source" in image_item and isinstance(image_item["source"], dict):
        base64_data = image_item["source"].get("data", "")
    elif "inlineData" in image_item and isinstance(image_item["inlineData"], dict):
        base64_data = image_item["inlineData"].get("data", "")
    elif "image_url" in image_item:
        url_val = image_item.get("image_url")
        if isinstance(url_val, dict):
            base64_data = url_val.get("url", "")
        elif isinstance(url_val, str):
            base64_data = url_val

    width, height = _parse_image_dimensions(base64_data)
    if width and height and width > 0 and height > 0:
        max_dim = max(width, height)
        if max_dim > 1568:
            scale = 1568.0 / max_dim
            width = int(width * scale)
            height = int(height * scale)
        tokens = int((width * height) / 750)
        return max(85, tokens)

    return 768


def estimate_input_tokens(payload: Dict[str, Any]) -> int:
    """高精度估算 payload 输入的 Prompt Token 数"""
    if not isinstance(payload, dict):
        return 0

    total_tokens = 0
    message_count = 0

    system = payload.get("system") or payload.get("systemInstruction")
    if system:
        if isinstance(system, str):
            total_tokens += _count_text_tokens(system) + 3
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, str):
                    total_tokens += _count_text_tokens(block)
                elif isinstance(block, dict):
                    if block.get("text"):
                        total_tokens += _count_text_tokens(str(block.get("text")))
                    elif block.get("parts"):
                        for part in block.get("parts", []):
                            if isinstance(part, dict) and part.get("text"):
                                total_tokens += _count_text_tokens(str(part["text"]))
            total_tokens += 3

    messages = payload.get("messages") or payload.get("contents")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message_count += 1
            total_tokens += 4

            content = msg.get("content") or msg.get("parts")
            if isinstance(content, str):
                total_tokens += _count_text_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        total_tokens += _count_text_tokens(item)
                    elif isinstance(item, dict):
                        item_type = item.get("type")

                        if item_type == "text" or ("text" in item and not item_type):
                            text_val = item.get("text", "")
                            if text_val:
                                total_tokens += _count_text_tokens(str(text_val))
                        elif item_type == "tool_use":
                            name = item.get("name", "")
                            input_data = item.get("input", {})
                            tool_text = f"tool_use {name}: {json.dumps(input_data, ensure_ascii=False)}"
                            total_tokens += _count_text_tokens(tool_text) + 4
                        elif item_type == "tool_result":
                            res_content = item.get("content", "")
                            if isinstance(res_content, str):
                                total_tokens += _count_text_tokens(res_content)
                            elif isinstance(res_content, list):
                                for res_item in res_content:
                                    if isinstance(res_item, dict) and res_item.get("text"):
                                        total_tokens += _count_text_tokens(str(res_item["text"]))
                        elif item_type in ("image", "image_url") or "inlineData" in item or "source" in item:
                            total_tokens += _calculate_image_tokens(item)

    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                tool_str = json.dumps({
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                    "parameters": tool.get("input_schema") or tool.get("parameters")
                }, ensure_ascii=False)
                total_tokens += _count_text_tokens(tool_str) + 6

    if message_count > 0:
        total_tokens += 3

    return max(1, total_tokens)


def _fallback_text_tokens(text: str) -> int:
    """多语言字符加权估算（保底算法）"""
    tokens = 0.0
    for char in text:
        if '\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u30ff' or '\uac00' <= char <= '\ud7af':
            tokens += 1.5
        elif char.isascii():
            tokens += 0.25
        else:
            tokens += 0.5
    return max(1, int(tokens))


def format_token(count: int) -> str:
    """将 Token 数量格式化为带 k/M 后缀的可读字符串"""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


# ==============================================================================
# 3. Token 日志与结果解析 (Logger & Usage Parser)
# ==============================================================================

def count_token_usage(
    usage_metadata: Any,
    model: str,
    is_final: bool = True,
) -> None:
    """
    统计与打印 UsageMetadata 日志并记录到持久化服务

    :param usage_metadata: Gemini 响应中的 usageMetadata 字典
    :param model: 调用的模型名称
    :param is_final: 是否为流式结束包或单次非流式响应包（is_final=False 时原地单行刷新，is_final=True 时换行并落库）
    """
    if not isinstance(usage_metadata, dict) or not usage_metadata:
        return

    from src.auth import credential_manager
    user_info = credential_manager.get_current_account()

    model_str = model if model else "未知模型"
    user_str = f" | 用户={user_info}" if user_info else ""

    prompt_tokens = int(usage_metadata.get("promptTokenCount") or 0)
    completion_tokens = int(usage_metadata.get("candidatesTokenCount") or 0)
    cached_tokens = int(usage_metadata.get("cachedContentTokenCount") or 0)
    thoughts_tokens = int(usage_metadata.get("thoughtsTokenCount") or 0)

    # 计算与前端/仪表盘一致的总 Token 消耗 (扣除缓存的净输入 Token + 输出 Token)
    net_prompt_tokens = max(0, prompt_tokens - cached_tokens)
    total_tokens = net_prompt_tokens + completion_tokens

    parts = [
        f"输入={format_token(prompt_tokens):>6}",
        f"输出={format_token(completion_tokens):>6}",
        f"缓存={format_token(cached_tokens):>6}",
        f"思考={format_token(thoughts_tokens):>6}",
        f"总计={format_token(total_tokens):>6}",
    ]

    log_msg = f"模型={model_str}{user_str} | {', '.join(parts)}"

    now_time = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    if not is_final:
        # 当流式未结束时：使用 \r 行首复位 + \033[K 清除残影（不换行，动态刷新）
        sys.stdout.write(f"\r[{now_time}] [INFO] {log_msg}\033[K")
        sys.stdout.flush()
    else:
        # 当流结束或非流式完成时：原位覆盖输出最终结果并加 \n 换行
        sys.stdout.write(f"\r[{now_time}] [INFO] {log_msg}\033[K\n")
        sys.stdout.flush()

        # 仅在 is_final=True 时落库写盘
        try:
            asyncio.create_task(
                token_tracker.record_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                    thoughts_tokens=thoughts_tokens,
                    model=model_str,
                    user_info=user_info,
                )
            )
        except Exception as e:
            log.warning(f"[count_token_usage] 异步记录 Token 仪表盘数据失败: {e}")
