"""
SSE (Server-Sent Events) 事件推送服务模块
实现控制面板与后端状态变更的实时事件广播
"""

import asyncio
import json
from typing import Any, Dict, Set
from log import log


class SSEService:
    """SSE 事件推送服务管理器（单例）"""

    def __init__(self):
        self._clients: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def connect(self) -> asyncio.Queue:
        """注册一个新的 SSE 客户端长连接队列"""
        queue = asyncio.Queue()
        async with self._lock:
            self._clients.add(queue)
        log.debug(f"[SSE] 客户端已建立长连接，当前在线监听客户端数: {len(self._clients)}")
        return queue

    async def disconnect(self, queue: asyncio.Queue):
        """注销已断开的 SSE 客户端队列"""
        async with self._lock:
            self._clients.discard(queue)
        log.debug(f"[SSE] 客户端断开连接，当前在线监听客户端数: {len(self._clients)}")

    async def broadcast(self, event_type: str, data: Dict[str, Any] = None):
        """向所有在线客户端广播一个实时变更事件"""
        if not self._clients:
            return

        payload = {
            "type": event_type,
            "data": data or {}
        }

        async with self._lock:
            dead_queues = set()
            for queue in self._clients:
                try:
                    # 避免队列阻塞，非阻塞或超时丢弃
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    dead_queues.add(queue)
                except Exception as e:
                    log.debug(f"[SSE] 事件发送异常: {e}")
                    dead_queues.add(queue)

            for q in dead_queues:
                self._clients.discard(q)

        log.debug(f"[SSE] 广播事件 '{event_type}' 到 {len(self._clients)} 个在线客户端")


# 全局 SSE 管理器单例
sse_manager = SSEService()
