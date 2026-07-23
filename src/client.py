"""
通用的 HTTP 客户端模块
使用 curl_cffi 模拟底层 TLS/JA3 指纹（impersonate="chrome120"）。
为所有需要与上游 Google / 外部服务发起 HTTP 请求的模块提供统一的客户端配置和方法。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from config import get_proxy_config
from log import log

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession, Response as CurlResponse
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    log.error("curl_cffi 未安装，请安装 curl_cffi 依赖！")

async def _close_session(session: Any):
    if session is None:
        return
    try:
        if hasattr(session, "close"):
            res = session.close()
            if asyncio.iscoroutine(res):
                await res
        elif hasattr(session, "aclose"):
            res = session.aclose()
            if asyncio.iscoroutine(res):
                await res
    except Exception as e:
        log.warning(f"Error closing session: {e}")

class IsolatedClientPool:
    """按账号/凭证标识隔离的 HTTP 客户端池，确保不同凭证的请求拥有独立的 TLS/TCP 上下文"""

    def __init__(self, idle_timeout: float = 300.0):
        self._pool: Dict[str, Tuple[Any, float]] = {}
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()

    async def get_session(self, session_key: Optional[str] = None, proxy: Optional[str] = None, impersonate: str = "chrome120") -> Any:
        # 如果未指定 session_key，使用默认临时 Key
        key = session_key or "default"
        async with self._lock:
            now = time.time()
            if key in self._pool:
                session, _ = self._pool[key]
                self._pool[key] = (session, now)
                return session

            session_kwargs: Dict[str, Any] = {"impersonate": impersonate}
            if proxy:
                session_kwargs["proxy"] = proxy

            session = CurlAsyncSession(**session_kwargs)
            self._pool[key] = (session, now)
            return session

    async def cleanup_idle(self):
        """定期清理长久未使用的空闲 Session 连接"""
        async with self._lock:
            now = time.time()
            keys_to_remove = []
            for key, (session, last_used) in self._pool.items():
                if now - last_used > self._idle_timeout:
                    keys_to_remove.append(key)
                    await _close_session(session)
            for key in keys_to_remove:
                del self._pool[key]

# 实例池管理
client_pool = IsolatedClientPool()

class HttpClientManager:
    """通用 HTTP 客户端管理器（集成 TLS 指纹伪装与动态代理支持）"""

    async def get_proxy(self) -> Optional[str]:
        """动态读取代理配置，支持热更新"""
        return await get_proxy_config()

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, impersonate: str = "chrome120", session_key: Optional[str] = None, **kwargs
    ) -> AsyncGenerator[Any, None]:
        """获取配置好的异步 HTTP 客户端（使用 curl_cffi 进行 TLS 指纹伪装）"""
        proxy = await self.get_proxy()

        if session_key:
            session = await client_pool.get_session(session_key=session_key, proxy=proxy, impersonate=impersonate)
            yield session
        else:
            session_kwargs: Dict[str, Any] = {
                "timeout": timeout,
                "impersonate": impersonate,
            }
            if proxy:
                session_kwargs["proxy"] = proxy

            async with CurlAsyncSession(**session_kwargs) as session:
                yield session

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: Optional[float] = None, impersonate: str = "chrome120", session_key: Optional[str] = None, **kwargs
    ) -> AsyncGenerator[Any, None]:
        """获取用于流式请求的异步 HTTP 客户端"""
        proxy = await self.get_proxy()

        if session_key:
            session = await client_pool.get_session(session_key=session_key, proxy=proxy, impersonate=impersonate)
            yield session
        else:
            session_kwargs: Dict[str, Any] = {
                "impersonate": impersonate,
            }
            if timeout is not None:
                session_kwargs["timeout"] = timeout
            if proxy:
                session_kwargs["proxy"] = proxy

            session = CurlAsyncSession(**session_kwargs)
            try:
                yield session
            finally:
                await _close_session(session)


# 全局 HTTP 客户端管理器实例
http_client = HttpClientManager()


import json as json_lib

def _format_payload(data: Any, max_len: int = 4000) -> str:
    """格式化 Payload 为可读字符串日志（超出 4000 字符时智能截断）"""
    if data is None:
        return "<Empty>"
    try:
        if isinstance(data, (dict, list)):
            formatted = json_lib.dumps(data, ensure_ascii=False, indent=2)
        elif isinstance(data, bytes):
            formatted = data.decode("utf-8", errors="replace")
        else:
            formatted = str(data)
        if len(formatted) > max_len:
            return formatted[:max_len] + f"\n... [已截断，总共 {len(formatted)} 字符]"
        return formatted
    except Exception as e:
        return f"<不可解析数据: {e}>"


# 通用的异步 GET 方法
async def get_async(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0, **kwargs
) -> Any:
    """通用异步 GET 请求（记录调用与响应日志）"""
    log.debug(f"[HTTP GET] 请求 URL: {url}")
    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        response = await client.get(url, headers=headers)
        resp_text = getattr(response, "text", "")
        status_code = getattr(response, "status_code", 0)
        log.debug(f"[HTTP GET] 响应 URL: {url} | Status: {status_code}\nResponse Body:\n{_format_payload(resp_text)}")
        return response


# 通用的异步 POST 方法
async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    **kwargs,
) -> Any:
    """通用异步 POST 请求（记录调用与响应日志）"""
    payload = json if json is not None else data
    log.debug(f"[HTTP POST] 请求 URL: {url}\nPayload:\n{_format_payload(payload)}")

    async with http_client.get_client(timeout=timeout, **kwargs) as client:
        response = await client.post(url, data=data, json=json, headers=headers)
        resp_text = getattr(response, "text", "")
        if not resp_text and hasattr(response, "content"):
            try:
                resp_text = response.content.decode("utf-8", errors="replace")
            except Exception:
                resp_text = "<Binary Content>"

        status_code = getattr(response, "status_code", 0)
        log.debug(f"[HTTP POST] 响应 URL: {url} | Status: {status_code}\nResponse Body:\n{_format_payload(resp_text)}")
        return response


def _filter_response_headers(headers: Any) -> Dict[str, str]:
    """过滤响应头，移除导致客户端解压或传输异常的 Headers"""
    skip_headers = {"content-encoding", "content-length", "transfer-encoding", "connection", "server"}
    filtered = {}
    if not headers:
        return filtered
    items = headers.items() if hasattr(headers, "items") else (dict(headers).items() if isinstance(headers, dict) else [])
    for k, v in items:
        if str(k).lower() not in skip_headers:
            filtered[str(k)] = str(v)
    return filtered


# 调试用：设为 True 时所有流式请求都返回 429
_MOCK_STREAM_429 = False


async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    **kwargs,
):
    """流式异步 POST 请求（记录调用与响应日志，支持 curl_cffi TLS 指纹伪装与流式响应迭代）"""
    if _MOCK_STREAM_429:
        from fastapi import Response
        log.warning("[MOCK] stream_post_async: 返回模拟 429 错误")
        yield Response(
            content=json_lib.dumps({"error": {"code": 429, "message": "mock rate limit", "status": "RESOURCE_EXHAUSTED"}}),
            status_code=429,
        )
        return

    log.debug(f"[HTTP STREAM POST] 请求 URL: {url}\nPayload:\n{_format_payload(body)}")

    async with http_client.get_streaming_client(**kwargs) as client:
        if CURL_CFFI_AVAILABLE and isinstance(client, CurlAsyncSession):
            async with client.stream("POST", url, json=body, headers=headers) as r:
                if r.status_code != 200:
                    from fastapi import Response
                    resp_content = await r.aread() if hasattr(r, "aread") else getattr(r, "content", b"")
                    log.error(f"[HTTP STREAM RESPONSE ERROR] URL: {url} | Status: {r.status_code}\nResponse Body:\n{_format_payload(resp_content)}")
                    yield Response(resp_content, r.status_code, _filter_response_headers(r.headers))
                    return

                log.debug(f"[HTTP STREAM RESPONSE START] URL: {url} | Status: 200 OK")
                if native:
                    async for chunk in r.aiter_content():
                        yield chunk
                else:
                    async for line in r.aiter_lines():
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        yield line
        else:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                if r.status_code != 200:
                    from fastapi import Response
                    resp_content = await r.aread() if hasattr(r, "aread") else getattr(r, "content", b"")
                    log.error(f"[HTTP STREAM RESPONSE ERROR] URL: {url} | Status: {r.status_code}\nResponse Body:\n{_format_payload(resp_content)}")
                    yield Response(resp_content, r.status_code, _filter_response_headers(r.headers))
                    return

                log.debug(f"[HTTP STREAM RESPONSE START] URL: {url} | Status: 200 OK")
                if native:
                    async for chunk in r.aiter_bytes():
                        yield chunk
                else:
                    async for line in r.aiter_lines():
                        yield line
