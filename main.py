"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""

import os
import sys


import asyncio
from contextlib import asynccontextmanager
import signal


from src.log import log


_shutdown_event = None
_main_loop = None
_win32_ctrl_count = 0

# 修复 Windows 平台 asyncio 优雅关闭 Socket 时的 WinError 10054 噪音日志与 Ctrl+C 无法退出问题
if sys.platform == "win32":
    import ctypes
    from asyncio.proactor_events import _ProactorBasePipeTransport

    _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

    def _silenced_call_connection_lost(self, exc=None):
        try:
            _orig_call_connection_lost(self, exc)
        except (OSError, ConnectionResetError):
            pass

    _ProactorBasePipeTransport._call_connection_lost = _silenced_call_connection_lost

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
    def _win32_ctrl_handler(ctrl_type):
        global _win32_ctrl_count, _shutdown_event, _main_loop
        if ctrl_type in (0, 1, 2):  # CTRL_C_EVENT = 0, CTRL_BREAK_EVENT = 1, CTRL_CLOSE_EVENT = 2
            _win32_ctrl_count += 1
            if _win32_ctrl_count == 1:
                log.info("检测到 Ctrl+C / 窗口关闭信号，触发优雅退出...")
                if _main_loop and _shutdown_event and _main_loop.is_running():
                    try:
                        _main_loop.call_soon_threadsafe(_shutdown_event.set)
                    except Exception:
                        pass
                import _thread
                _thread.interrupt_main()
            else:
                log.warning("再次检测到 Ctrl+C 信号，正在强制退出进程...")
                os._exit(0)
            return True
        return False

    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_win32_ctrl_handler, True)
    except Exception as e:
        log.warning(f"注册 Win32 Ctrl Handler 失败: {e}")

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import get_server_host, get_server_port

# Import managers and utilities
from src.auth import credential_manager

# Import all routers
from src.router.antigravity.openai import router as antigravity_openai_router
from src.router.antigravity.gemini import router as antigravity_gemini_router
from src.router.antigravity.anthropic import router as antigravity_anthropic_router
from src.router.antigravity.model_list import router as antigravity_model_list_router
from src.router.geminicli.openai import router as geminicli_openai_router
from src.router.geminicli.gemini import router as geminicli_gemini_router
from src.router.geminicli.anthropic import router as geminicli_anthropic_router
from src.router.geminicli.model_list import router as geminicli_model_list_router
from src.router.vertex.gemini import router as vertex_gemini_router
from src.router.vertex.openai import router as vertex_openai_router
from src.router.vertex.model_list import router as vertex_model_list_router
from src.panel import router as panel_router
from src.panel.quota import quota_refresh_service

# 全局凭证管理器
global_credential_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global global_credential_manager

    log.info("启动 google2api 主服务")

    # 初始化配置缓存（优先执行）
    try:
        import src.config as config
        await config.init_config()
        log.info("配置缓存初始化成功")
    except Exception as e:
        log.error(f"配置缓存初始化失败: {e}")

    # 初始化全局凭证管理器（通过单例工厂）
    try:
        # credential_manager 会在第一次调用时自动初始化
        # 这里预先触发初始化以便在启动时检测错误
        global_credential_manager = await credential_manager._get_or_create()
        log.info("凭证管理器初始化成功")
    except Exception as e:
        log.error(f"凭证管理器初始化失败: {e}")
        global_credential_manager = None

    # OAuth回调服务器将在需要时按需启动

    # 启动 Antigravity 凭证额度 15 分钟定时刷新服务
    try:
        await quota_refresh_service.start()
    except Exception as e:
        log.error(f"额度定时刷新服务启动失败: {e}")

    # 异步预热并初始化 API 动态模型白名单列表
    async def _init_models_bg():
        try:
            from src.api.antigravity import fetch_available_models
            await fetch_available_models()
        except Exception as e:
            log.warning(f"启动预热 API 模型列表提示: {e}")

    asyncio.create_task(_init_models_bg())

    yield

    # 清理资源
    log.info("开始关闭 google2api 主服务")

    try:
        await quota_refresh_service.stop()
    except Exception as e:
        log.error(f"关闭额度定时刷新服务时出错: {e}")

    # 然后关闭凭证管理器
    if global_credential_manager:
        try:
            await global_credential_manager.close()
            log.info("凭证管理器已关闭")
        except Exception as e:
            log.error(f"关闭凭证管理器时出错: {e}")

    log.info("google2api 主服务已停止")


# 创建FastAPI应用
app = FastAPI(
    title="google2api",
    description="Gemini API proxy with OpenAI compatibility",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由器
# OpenAI兼容路由 - 处理OpenAI格式请求
app.include_router(geminicli_openai_router, prefix="", tags=["Geminicli OpenAI API"])

# Gemini原生路由 - 处理Gemini格式请求
app.include_router(geminicli_gemini_router, prefix="", tags=["Geminicli Gemini API"])

# Geminicli模型列表路由 - 处理Gemini格式的模型列表请求
app.include_router(geminicli_model_list_router, prefix="", tags=["Geminicli Model List"])

# Antigravity路由 - 处理OpenAI格式请求并转换为Antigravity API
app.include_router(antigravity_openai_router, prefix="", tags=["Antigravity OpenAI API"])

# Antigravity路由 - 处理Gemini格式请求并转换为Antigravity API
app.include_router(antigravity_gemini_router, prefix="", tags=["Antigravity Gemini API"])

# Antigravity模型列表路由 - 处理Gemini格式的模型列表请求
app.include_router(antigravity_model_list_router, prefix="", tags=["Antigravity Model List"])

# Antigravity Anthropic Messages 路由 - Anthropic Messages 格式兼容
app.include_router(antigravity_anthropic_router, prefix="", tags=["Antigravity Anthropic Messages"])

# Geminicli Anthropic Messages 路由 - Anthropic Messages 格式兼容 (Geminicli)
app.include_router(geminicli_anthropic_router, prefix="", tags=["Geminicli Anthropic Messages"])

# Panel路由 - 包含认证、凭证管理和控制面板功能
app.include_router(panel_router, prefix="", tags=["Panel Interface"])

# Vertex AI 路由 - Gemini 原生格式
app.include_router(vertex_gemini_router, prefix="", tags=["Vertex Gemini API"])

# Vertex AI 路由 - OpenAI 兼容格式
app.include_router(vertex_openai_router, prefix="", tags=["Vertex OpenAI API"])

# Vertex AI 路由 - 模型列表
app.include_router(vertex_model_list_router, prefix="", tags=["Vertex Model List"])

# 静态文件路由 - 服务docs目录下的文件
if os.path.exists("docs"):
    app.mount("/docs", StaticFiles(directory="docs"), name="docs")

# 静态文件路由 - 服务front目录下的文件（HTML、JS、CSS等）
app.mount("/front", StaticFiles(directory="front"), name="front")


_instance_lock_file = None


def ensure_single_instance(lock_name="google2api.lock"):
    """确保程序全局仅单进程运行，若已存在运行实例则提示并退出"""
    global _instance_lock_file
    import tempfile
    lock_file_path = os.path.join(tempfile.gettempdir(), lock_name)
    try:
        lock_file = open(lock_file_path, "a+")
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                print("[-] 错误: google2api 实例已经在运行中，禁止重复启动多进程！", file=sys.stderr)
                sys.exit(1)
        else:
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print("[-] 错误: google2api 实例已经在运行中，禁止重复启动多进程！", file=sys.stderr)
                sys.exit(1)
        _instance_lock_file = lock_file
    except SystemExit:
        raise
    except Exception:
        pass


def main():
    """主启动函数"""
    ensure_single_instance()

    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    workers = 1  # 强制单进程运行模式

    async def _run():
        global _shutdown_event, _main_loop
        _main_loop = asyncio.get_running_loop()
        _shutdown_event = asyncio.Event()

        port = await get_server_port()
        host = await get_server_host()

        log.info("=" * 60)
        log.info("启动 google2api (单进程模式)")
        log.info("=" * 60)
        log.info(f"控制面板: http://127.0.0.1:{port}")
        log.info("=" * 60)

        config = Config()
        config.bind = [f"{host}:{port}"]
        config.access_log_format = '%(h)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
        # 自定义只在错误响应 (4xx, 5xx) 时记录 HTTP 请求日志
        import logging

        class ErrorOnlyAccessLogger(logging.Logger):
            def handle(self, record):
                # Hypercorn access log status code is in record.args['s'] or record.status
                status = getattr(record, 'status', None)
                if status is None and isinstance(record.args, dict):
                    status = record.args.get('s')
                try:
                    if status and int(status) < 400:
                        return
                except (ValueError, TypeError):
                    pass
                super().handle(record)

        access_logger = logging.getLogger("hypercorn.access.error_only")
        access_logger.__class__ = ErrorOnlyAccessLogger
        config.accesslog = access_logger
        config.errorlog = "-"
        config.loglevel = "INFO"

        await serve(app, config, shutdown_trigger=_shutdown_event.wait)

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        log.info("检测到 Ctrl+C 中断信号，正在优雅退出...")


if __name__ == "__main__":
    main()

