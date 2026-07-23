"""
认证API模块
"""

import asyncio
import socket
import threading
import time
import uuid
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from config import get_config_value, get_antigravity_api_url
from log import log

from .google_oauth import (
    Credentials,
    Flow,
    enable_required_apis,
    fetch_project_id_and_tier,
    get_user_projects,
    select_default_project,
)
from src.storage import get_storage
from .constants import (
    ANTIGRAVITY_CLIENT_ID,
    ANTIGRAVITY_CLIENT_SECRET,
    ANTIGRAVITY_SCOPES,
    ANTIGRAVITY_USER_AGENT,
    CALLBACK_HOST,
    CLIENT_ID,
    CLIENT_SECRET,
    SCOPES,
    TOKEN_URL,
)


async def get_callback_port():
    """获取OAuth回调端口"""
    return int(await get_config_value("oauth_callback_port", "11451", "OAUTH_CALLBACK_PORT"))


def _prepare_credentials_data(credentials: Credentials, project_id: str, mode: str = "geminicli", subscription_tier: str = None) -> Dict[str, Any]:
    """准备凭证数据字典（统一函数）"""
    if mode == "antigravity":
        creds_data = {
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
            "scopes": ANTIGRAVITY_SCOPES,
            "token_uri": TOKEN_URL,
            "project_id": project_id,
        }
    else:
        creds_data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
            "scopes": SCOPES,
            "token_uri": TOKEN_URL,
            "project_id": project_id,
        }

    if credentials.expires_at:
        if credentials.expires_at.tzinfo is None:
            expiry_utc = credentials.expires_at.replace(tzinfo=timezone.utc)
        else:
            expiry_utc = credentials.expires_at
        creds_data["expiry"] = expiry_utc.isoformat()

    return creds_data


def _cleanup_auth_flow_server(state: str):
    """清理认证流程的服务器资源"""
    if state in auth_flows:
        flow_data_to_clean = auth_flows[state]
        try:
            if flow_data_to_clean.get("server"):
                server = flow_data_to_clean["server"]
                port = flow_data_to_clean.get("callback_port")
                async_shutdown_server(server, port)
        except Exception as e:
            log.debug(f"关闭服务器时出错: {e}")
        del auth_flows[state]


class _OAuthLibPatcher:
    """oauthlib参数验证补丁的上下文管理器"""
    def __init__(self):
        import oauthlib.oauth2.rfc6749.parameters
        self.module = oauthlib.oauth2.rfc6749.parameters
        self.original_validate = None

    def __enter__(self):
        self.original_validate = self.module.validate_token_parameters

        def patched_validate(params):
            try:
                return self.original_validate(params)
            except Warning:
                pass

        self.module.validate_token_parameters = patched_validate
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.original_validate:
            self.module.validate_token_parameters = self.original_validate


# 全局状态管理 - 严格限制大小
auth_flows = {}  # 存储进行中的认证流程
MAX_AUTH_FLOWS = 20  # 严格限制最大认证流程数
DEFAULT_PROJECT_ID = "gemini-pro-1751713012-07fc4dfd"


def cleanup_auth_flows_for_memory():
    """清理认证流程以释放内存"""
    global auth_flows
    cleanup_expired_flows()
    # 如果还是太多，强制清理一些旧的流程
    if len(auth_flows) > 10:
        # 按创建时间排序，保留最新的10个
        sorted_flows = sorted(
            auth_flows.items(), key=lambda x: x[1].get("created_at", 0), reverse=True
        )
        new_auth_flows = dict(sorted_flows[:10])

        # 清理被移除的流程
        for state, flow_data in auth_flows.items():
            if state not in new_auth_flows:
                try:
                    if flow_data.get("server"):
                        server = flow_data["server"]
                        port = flow_data.get("callback_port")
                        async_shutdown_server(server, port)
                except Exception:
                    pass
                flow_data.clear()

        auth_flows = new_auth_flows
        log.info(f"强制清理认证流程，保留 {len(auth_flows)} 个最新流程")

    return len(auth_flows)


async def find_available_port(start_port: int = None) -> int:
    """动态查找可用端口"""
    if start_port is None:
        start_port = await get_callback_port()

    # 首先尝试默认端口
    for port in range(start_port, start_port + 100):  # 尝试100个端口
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                log.info(f"找到可用端口: {port}")
                return port
        except OSError:
            continue

    # 如果都不可用，让系统自动分配端口
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 0))
            port = s.getsockname()[1]
            log.info(f"系统分配可用端口: {port}")
            return port
    except OSError as e:
        log.error(f"无法找到可用端口: {e}")
        raise RuntimeError("无法找到可用端口")


def create_callback_server(port: int) -> HTTPServer:
    """创建指定端口的回调服务器，优化快速关闭"""
    try:
        # 服务器监听0.0.0.0
        server = HTTPServer(("0.0.0.0", port), AuthCallbackHandler)

        # 设置socket选项以支持快速关闭
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 设置较短的超时时间
        server.timeout = 1.0

        log.info(f"创建OAuth回调服务器，监听端口: {port}")
        return server
    except OSError as e:
        log.error(f"创建端口{port}的服务器失败: {e}")
        raise


class AuthCallbackHandler(BaseHTTPRequestHandler):
    """OAuth回调处理器，自动进行 Token 交换与落盘保存"""

    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        code = query_components.get("code", [None])[0]
        state = query_components.get("state", [None])[0]

        log.info(f"收到OAuth回调: code={'已获取' if code else '未获取'}, state={state}")

        if code and state and state in auth_flows:
            # 更新流程状态
            auth_flows[state]["code"] = code
            auth_flows[state]["completed"] = True

            # 自动在后台异步进行 Token 交换与账号落盘
            full_callback_url = f"http://localhost{self.path}"
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()

                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        complete_auth_flow_from_callback_url(full_callback_url),
                        loop
                    )
            except Exception as e:
                log.warning(f"自动触发凭证落盘警告: {e}")

            log.info(f"OAuth回调自动处理成功: state={state}")

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()

            # 返回现代化且可自动倒计时关闭的成功提示页面
            success_html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>授权成功</title></head>
<body style="font-family: system-ui, -apple-system, sans-serif; text-align: center; padding: 60px 20px; background: #0f172a; color: #f8fafc;">
    <div style="max-width: 480px; margin: 0 auto; background: #1e293b; padding: 36px 24px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); border: 1px solid #334155;">
        <div style="font-size: 54px; margin-bottom: 16px;">✅</div>
        <h2 style="color: #10b981; margin-bottom: 8px; font-size: 22px;">Google 账号授权成功！</h2>
        <p style="color: #94a3b8; font-size: 14px; line-height: 1.6; margin-bottom: 8px;">凭证已自动获取并保存，系统已自动激活该账号。</p>
        <p style="color: #64748b; font-size: 13px;">您可以直接关闭此窗口返回控制面板。</p>
    </div>
    <script>
        if (window.opener) {
            try { window.opener.postMessage({ type: 'oauth-success' }, '*'); } catch(e) {}
        }
    </script>
</body>
</html>"""
            self.wfile.write(success_html.encode("utf-8"))
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Authentication failed.</h1><p>Please try again.</p>")

    def log_message(self, format, *args):
        # 减少日志噪音
        pass


async def create_auth_url(
    project_id: Optional[str] = None, user_session: str = None, mode: str = "geminicli"
) -> Dict[str, Any]:
    """创建认证URL，支持动态端口分配"""
    try:
        # 动态分配端口并构造标准的 127.0.0.1 IPv4 直连 /oauth-callback 路径（避免 localhost IPv6 解析失败）
        callback_port = await find_available_port()
        callback_url = f"http://127.0.0.1:{callback_port}/oauth-callback"

        # 立即启动回调服务器
        try:
            callback_server = create_callback_server(callback_port)
            # 在后台线程中运行服务器
            server_thread = threading.Thread(
                target=callback_server.serve_forever,
                daemon=True,
                name=f"OAuth-Server-{callback_port}",
            )
            server_thread.start()
            log.info(f"OAuth回调服务器已启动，端口: {callback_port}")
        except Exception as e:
            log.error(f"启动回调服务器失败: {e}")
            return {
                "success": False,
                "error": f"无法启动OAuth回调服务器，端口{callback_port}: {str(e)}",
            }

        # 创建OAuth流程
        # 根据模式选择配置
        if mode == "antigravity":
            client_id = ANTIGRAVITY_CLIENT_ID
            client_secret = ANTIGRAVITY_CLIENT_SECRET
            scopes = ANTIGRAVITY_SCOPES
        else:
            client_id = CLIENT_ID
            client_secret = CLIENT_SECRET
            scopes = SCOPES

        flow = Flow(
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            redirect_uri=callback_url,
        )

        # 生成状态标识符，包含用户会话信息
        if user_session:
            state = f"{user_session}_{str(uuid.uuid4())}"
        else:
            state = str(uuid.uuid4())

        # 生成认证URL
        auth_url = flow.get_auth_url(state=state)

        # 严格控制认证流程数量 - 超过限制时立即清理最旧的
        if len(auth_flows) >= MAX_AUTH_FLOWS:
            # 清理最旧的认证流程
            oldest_state = min(auth_flows.keys(), key=lambda k: auth_flows[k].get("created_at", 0))
            try:
                # 清理服务器资源
                old_flow = auth_flows[oldest_state]
                if old_flow.get("server"):
                    server = old_flow["server"]
                    port = old_flow.get("callback_port")
                    async_shutdown_server(server, port)
            except Exception as e:
                log.warning(f"Failed to cleanup old auth flow {oldest_state}: {e}")

            del auth_flows[oldest_state]
            log.debug(f"Removed oldest auth flow: {oldest_state}")

        # 保存流程状态
        auth_flows[state] = {
            "flow": flow,
            "project_id": project_id,  # 可能为None，稍后在回调时确定
            "user_session": user_session,
            "callback_port": callback_port,  # 存储分配的端口
            "callback_url": callback_url,  # 存储完整回调URL
            "server": callback_server,  # 存储服务器实例
            "server_thread": server_thread,  # 存储服务器线程
            "code": None,
            "completed": False,
            "created_at": time.time(),
            "auto_project_detection": project_id is None,  # 标记是否需要自动检测项目ID
            "mode": mode,  # 凭证模式
        }

        # 清理过期的流程（30分钟）
        cleanup_expired_flows()

        log.info(f"OAuth流程已创建: state={state}, project_id={project_id}")
        log.info(f"用户需要访问认证URL，然后OAuth会回调到 {callback_url}")
        log.info(f"为此认证流程分配的端口: {callback_port}")

        return {
            "auth_url": auth_url,
            "state": state,
            "callback_port": callback_port,
            "success": True,
            "auto_project_detection": project_id is None,
            "detected_project_id": project_id,
        }

    except Exception as e:
        log.error(f"创建认证URL失败: {e}")
        return {"success": False, "error": str(e)}


def wait_for_callback_sync(state: str, timeout: int = 300) -> Optional[str]:
    """同步等待OAuth回调完成，使用对应流程的专用服务器"""
    if state not in auth_flows:
        log.error(f"未找到状态为 {state} 的认证流程")
        return None

    flow_data = auth_flows[state]
    callback_port = flow_data["callback_port"]

    # 服务器已经在create_auth_url时启动了，这里只需要等待
    log.info(f"等待OAuth回调完成，端口: {callback_port}")

    # 等待回调完成
    start_time = time.time()
    while time.time() - start_time < timeout:
        if flow_data.get("code"):
            log.info("OAuth回调成功完成")
            return flow_data["code"]
        time.sleep(0.5)  # 每0.5秒检查一次

        # 刷新flow_data引用
        if state in auth_flows:
            flow_data = auth_flows[state]

    log.warning(f"等待OAuth回调超时 ({timeout}秒)")
    return None


async def complete_auth_flow(
    project_id: Optional[str] = None, user_session: str = None
) -> Dict[str, Any]:
    """完成认证流程并保存凭证，支持自动检测项目ID"""
    try:
        # 查找对应的认证流程
        state = None
        flow_data = None

        # 如果指定了project_id，先尝试匹配指定的项目
        if project_id:
            for s, data in auth_flows.items():
                if data["project_id"] == project_id:
                    # 如果指定了用户会话，优先匹配相同会话的流程
                    if user_session and data.get("user_session") == user_session:
                        state = s
                        flow_data = data
                        break
                    # 如果没有指定会话，或没找到匹配会话的流程，使用第一个匹配项目ID的
                    elif not state:
                        state = s
                        flow_data = data

        # 如果没有指定项目ID或没找到匹配的，查找需要自动检测项目ID的流程
        if not state:
            for s, data in auth_flows.items():
                if data.get("auto_project_detection", False):
                    # 如果指定了用户会话，优先匹配相同会话的流程
                    if user_session and data.get("user_session") == user_session:
                        state = s
                        flow_data = data
                        break
                    # 使用第一个找到的需要自动检测的流程
                    elif not state:
                        state = s
                        flow_data = data

        if not state or not flow_data:
            return {"success": False, "error": "未找到对应的认证流程，请先点击获取认证链接"}

        if not project_id:
            project_id = flow_data.get("project_id")
            if not project_id:
                project_id = DEFAULT_PROJECT_ID
                log.warning(f"未获取到project_id，使用默认project_id: {project_id}")

        flow = flow_data["flow"]

        # 如果还没有授权码，需要等待回调
        if not flow_data.get("code"):
            log.info(f"等待用户完成OAuth授权 (state: {state})")
            auth_code = wait_for_callback_sync(state)

            if not auth_code:
                return {
                    "success": False,
                    "error": "未接收到授权回调，请确保完成了浏览器中的OAuth认证",
                }

            # 更新流程数据
            auth_flows[state]["code"] = auth_code
            auth_flows[state]["completed"] = True
        else:
            auth_code = flow_data["code"]

        # 使用认证代码获取凭证
        with _OAuthLibPatcher():
            try:
                credentials = await flow.exchange_code(auth_code)
                # credentials 已经在 exchange_code 中获得

                # 如果需要自动检测项目ID且没有提供项目ID
                if flow_data.get("auto_project_detection", False) and not project_id:
                    log.info("尝试通过API获取用户项目列表...")
                    log.info(f"使用的token: {credentials.access_token[:20]}...")
                    log.info(f"Token过期时间: {credentials.expires_at}")
                    user_projects = await get_user_projects(credentials)

                    if user_projects:
                        # 如果只有一个项目，自动使用
                        if len(user_projects) == 1:
                            # Google API returns projectId in camelCase
                            project_id = user_projects[0].get("projectId")
                            if project_id:
                                flow_data["project_id"] = project_id
                                log.info(f"自动选择唯一项目: {project_id}")
                        # 如果有多个项目，尝试选择默认项目
                        else:
                            project_id = await select_default_project(user_projects)
                            if project_id:
                                flow_data["project_id"] = project_id
                                log.info(f"自动选择默认项目: {project_id}")
                            else:
                                # 返回项目列表让用户选择
                                return {
                                    "success": False,
                                    "error": "请从以下项目中选择一个",
                                    "requires_project_selection": True,
                                    "available_projects": [
                                        {
                                            # Google API returns projectId in camelCase
                                            "project_id": p.get("projectId"),
                                            "name": p.get("displayName") or p.get("projectId"),
                                            "projectNumber": p.get("projectNumber"),
                                        }
                                        for p in user_projects
                                    ],
                                }
                    else:
                        # 如果无法获取项目列表，使用默认project_id
                        project_id = DEFAULT_PROJECT_ID
                        flow_data["project_id"] = project_id
                        log.warning(f"无法获取项目列表，使用默认project_id: {project_id}")

                # 如果仍然没有项目ID，返回错误
                if not project_id:
                    project_id = DEFAULT_PROJECT_ID
                    flow_data["project_id"] = project_id
                    log.warning(f"仍未获取到project_id，使用默认project_id: {project_id}")

                # 保存凭证
                saved_filename = await save_credentials(credentials, project_id)

                # 准备返回的凭证数据
                creds_data = _prepare_credentials_data(credentials, project_id, mode="geminicli")

                # 清理使用过的流程
                _cleanup_auth_flow_server(state)

                log.info("OAuth认证成功，凭证已保存")
                return {
                    "success": True,
                    "credentials": creds_data,
                    "file_path": saved_filename,
                    "auto_detected_project": flow_data.get("auto_project_detection", False),
                }

            except Exception as e:
                log.error(f"获取凭证失败: {e}")
                return {"success": False, "error": f"获取凭证失败: {str(e)}"}

    except Exception as e:
        log.error(f"完成认证流程失败: {e}")
        return {"success": False, "error": str(e)}


async def _execute_code_exchange_once(state: str, code: str, mode: str = "antigravity") -> Dict[str, Any]:
    """
    单例防并发 Code 交换闭环逻辑。
    避免浏览器 HTTP 回调线程与前端 POST /auth/complete 并发对同一个 OAuth Code 发起二次 exchange，
    从而引发 Google API 400 (invalid_grant: Code has already been used) 错误。
    """
    if state not in auth_flows:
        if auth_flows:
            state = max(auth_flows.keys(), key=lambda k: auth_flows[k].get("created_at", 0))
        else:
            return {"success": False, "error": "未找到对应的认证流程，请重新获取授权链接"}

    flow_data = auth_flows[state]

    # 若已有交换完成的缓存结果，直接返回
    if "exchange_result" in flow_data:
        log.info(f"复用 state={state} 已获取的授权完成结果")
        return flow_data["exchange_result"]

    # 若已有正在进行的 Token 交换，等待该 Task 完成
    if flow_data.get("exchanging_event"):
        log.info(f"state={state} 正在由并发任务进行 Token 交换，等待其完成...")
        await flow_data["exchanging_event"].wait()
        if "exchange_result" in flow_data:
            return flow_data["exchange_result"]
        return {"success": False, "error": flow_data.get("exchange_error", "Token 交换处理失败")}

    # 标记为当前 Task 正在处理，并创建等待事件
    event = asyncio.Event()
    flow_data["exchanging_event"] = event

    try:
        flow = flow_data["flow"]
        project_id = flow_data.get("project_id")
        cred_mode = flow_data.get("mode", mode)

        with _OAuthLibPatcher():
            log.info(f"调用 flow.exchange_code(code)... state={state}")
            credentials = await flow.exchange_code(code)
            log.info("成功从 Google 获取 Access & Refresh Token")

            if cred_mode == "antigravity":
                antigravity_url = await get_antigravity_api_url()
                project_id, subscription_tier = await fetch_project_id_and_tier(
                    credentials.access_token,
                    ANTIGRAVITY_USER_AGENT,
                    antigravity_url
                )
                if not project_id:
                    project_id = DEFAULT_PROJECT_ID

                saved_filename = await save_credentials(credentials, project_id, mode="antigravity", subscription_tier=subscription_tier)
                creds_data = _prepare_credentials_data(credentials, project_id, mode="antigravity", subscription_tier=subscription_tier)
                result = {
                    "success": True,
                    "credentials": creds_data,
                    "file_path": saved_filename,
                    "auto_detected_project": False,
                    "mode": "antigravity",
                    "message": "凭证获取并保存成功！",
                }
            else:
                if not project_id:
                    project_id = DEFAULT_PROJECT_ID
                saved_filename = await save_credentials(credentials, project_id, mode="geminicli")
                creds_data = _prepare_credentials_data(credentials, project_id, mode="geminicli")
                result = {
                    "success": True,
                    "credentials": creds_data,
                    "file_path": saved_filename,
                    "auto_detected_project": False,
                    "mode": "geminicli",
                    "message": "凭证获取并保存成功！",
                }

            flow_data["exchange_result"] = result
            _cleanup_auth_flow_server(state)
            return result

    except Exception as e:
        err_msg = f"获取token失败: {str(e)}"
        log.error(f"OAuth Code 交换出错 (state={state}): {err_msg}")
        flow_data["exchange_error"] = err_msg
        return {"success": False, "error": err_msg}
    finally:
        event.set()


async def asyncio_complete_auth_flow(
    project_id: Optional[str] = None, user_session: str = None, mode: str = "geminicli"
) -> Dict[str, Any]:
    """异步完成认证流程，支持自动检测项目ID"""
    try:
        log.info(
            f"asyncio_complete_auth_flow开始执行: project_id={project_id}, user_session={user_session}"
        )

        state = None
        flow_data = None

        if project_id:
            for s, data in auth_flows.items():
                if data.get("project_id") == project_id:
                    if user_session and data.get("user_session") == user_session:
                        state = s
                        flow_data = data
                        break
                    elif not state:
                        state = s
                        flow_data = data

        if not state:
            completed_flows = []
            for s, data in auth_flows.items():
                if data.get("code") or data.get("exchange_result"):
                    score = data.get("created_at", 0)
                    if data.get("mode") == mode:
                        score += 10000000000.0
                    completed_flows.append((s, data, score))

            if completed_flows:
                completed_flows.sort(key=lambda x: x[2], reverse=True)
                state, flow_data, _ = completed_flows[0]
            else:
                pending_flows = []
                for s, data in auth_flows.items():
                    score = data.get("created_at", 0)
                    if data.get("mode") == mode:
                        score += 10000000000.0
                    pending_flows.append((s, data, score))

                if pending_flows:
                    pending_flows.sort(key=lambda x: x[2], reverse=True)
                    state, flow_data, _ = pending_flows[0]

        if not state or not flow_data:
            return {"success": False, "error": "未找到对应的认证流程，请先点击获取认证链接"}

        # 若已有防重单例交换结果，直接返回
        if "exchange_result" in flow_data:
            return flow_data["exchange_result"]

        max_wait_time = 10
        wait_interval = 1
        waited = 0

        while waited < max_wait_time:
            if flow_data.get("code") or "exchange_result" in flow_data:
                break
            await asyncio.sleep(wait_interval)
            waited += wait_interval
            if state in auth_flows:
                flow_data = auth_flows[state]

        if "exchange_result" in flow_data:
            return flow_data["exchange_result"]

        if not flow_data.get("code"):
            return {
                "success": False,
                "error": "未检测到授权回调，请确保已在浏览器中完成授权。",
            }

        auth_code = flow_data["code"]
        return await _execute_code_exchange_once(state, auth_code, mode=mode)

    except Exception as e:
        log.error(f"异步完成认证流程失败: {e}")
        return {"success": False, "error": str(e)}


async def complete_auth_flow_from_callback_url(
    callback_url: str, project_id: Optional[str] = None, mode: str = "geminicli"
) -> Dict[str, Any]:
    """从回调URL直接完成认证流程，无需启动本地服务器"""
    try:
        log.info(f"开始从回调URL完成认证: {callback_url}")

        # 解析回调URL
        parsed_url = urlparse(callback_url)
        query_params = parse_qs(parsed_url.query)

        # 验证必要参数
        if "state" not in query_params or "code" not in query_params:
            return {"success": False, "error": "回调URL缺少必要参数 (state 或 code)"}

        state = query_params["state"][0]
        code = query_params["code"][0]

        log.info(f"从URL解析到: state={state}, code=xxx...")

        # 检查是否有对应的认证流程
        if state not in auth_flows:
            log.warning(f"state '{state}' 未在 auth_flows 找到，尝试使用最新创建的授权流程...")
            if auth_flows:
                latest_state = max(auth_flows.keys(), key=lambda k: auth_flows[k].get("created_at", 0))
                flow_data = auth_flows[latest_state]
                state = latest_state
            else:
                return {
                    "success": False,
                    "error": f"未找到对应的认证流程，请先点击获取认证链接 (state: {state})",
                }
        else:
            flow_data = auth_flows[state]

        # 将 code 存在 flow_data 中供状态共享
        flow_data["code"] = code
        return await _execute_code_exchange_once(state, code, mode=mode)

    except Exception as e:
        log.error(f"从回调URL完成认证流程失败: {e}")
        return {"success": False, "error": str(e)}


async def save_credentials(creds: Credentials, project_id: str, mode: str = "geminicli", subscription_tier: str = None) -> str:
    """通过统一存储系统保存凭证"""
    # 自动获取用户信息（邮箱与用户名）
    user_email = None
    user_name = None
    try:
        from .google_oauth import get_user_info
        user_info = await get_user_info(creds)
        if user_info:
            user_email = user_info.get("email")
            user_name = user_info.get("name") or user_info.get("given_name")
            log.info(f"自动获取用户信息成功: email={user_email}, name={user_name}")
    except Exception as e:
        log.warning(f"自动获取用户信息失败: {e}")

    # 检查账号是否已存在，如存在则做更新日志提示
    if user_email:
        storage_adapter = await get_storage()
        all_states = await storage_adapter.get_all_credential_states(mode=mode)
        for state in all_states.values():
            if state.get("user_email") == user_email:
                log.info(f"账号 {user_email} 已存在，将覆盖更新现有凭证数据")

    # 生成文件名
    timestamp = int(time.time())
    if user_email:
        filename = f"{user_email}.json"
    else:
        prefix = "ag_" if mode == "antigravity" else ""
        filename = f"{prefix}{project_id}-{timestamp}.json"

    # 准备凭证数据
    creds_data = _prepare_credentials_data(creds, project_id, mode, subscription_tier)

    # 通过存储适配器保存
    storage_adapter = await get_storage()
    success = await storage_adapter.store_credential(filename, creds_data, mode=mode)

    if success:
        # 更新/重置凭证状态记录（解封账号并清除错误）
        try:
            state_update = {
                "disabled": False,
                "error_codes": [],
                "error_messages": [],
                "user_email": user_email,
                "user_name": user_name,
            }
            if subscription_tier:
                state_update["tier"] = subscription_tier

            await storage_adapter.update_credential_state(filename, state_update, mode=mode)
            log.info(f"凭证和状态已保存并激活: {filename} (mode={mode})")

            # 自动触发新账号额度刷新
            try:
                from .credential_manager import credential_manager
                asyncio.create_task(credential_manager.refresh_credential_quota(filename, mode=mode))
            except Exception as e:
                log.warning(f"触发新账号额度刷新警告: {e}")
        except Exception as e:
            log.warning(f"更新状态记录失败 {filename}: {e}")

        return filename
    else:
        raise Exception(f"保存凭证失败: {filename}")


def async_shutdown_server(server, port):
    """异步关闭OAuth回调服务器，避免阻塞主流程"""

    def shutdown_server_async():
        try:
            # 设置一个标志来跟踪关闭状态
            shutdown_completed = threading.Event()

            def do_shutdown():
                try:
                    server.shutdown()
                    server.server_close()
                    shutdown_completed.set()
                    log.info(f"已关闭端口 {port} 的OAuth回调服务器")
                except Exception as e:
                    shutdown_completed.set()
                    log.debug(f"关闭服务器时出错: {e}")

            # 在单独线程中执行关闭操作
            shutdown_worker = threading.Thread(target=do_shutdown, daemon=True)
            shutdown_worker.start()

            # 等待最多5秒，如果超时就放弃等待
            if shutdown_completed.wait(timeout=5):
                log.debug(f"端口 {port} 服务器关闭完成")
            else:
                log.warning(f"端口 {port} 服务器关闭超时，但不阻塞主流程")

        except Exception as e:
            log.debug(f"异步关闭服务器时出错: {e}")

    # 在后台线程中关闭服务器，不阻塞主流程
    shutdown_thread = threading.Thread(target=shutdown_server_async, daemon=True)
    shutdown_thread.start()
    log.debug(f"开始异步关闭端口 {port} 的OAuth回调服务器")


def cleanup_expired_flows():
    """清理过期的认证流程"""
    current_time = time.time()
    EXPIRY_TIME = 600  # 10分钟过期

    # 直接遍历删除，避免创建额外列表
    states_to_remove = [
        state
        for state, flow_data in auth_flows.items()
        if current_time - flow_data["created_at"] > EXPIRY_TIME
    ]

    # 批量清理，提高效率
    cleaned_count = 0
    for state in states_to_remove:
        flow_data = auth_flows.get(state)
        if flow_data:
            # 快速关闭可能存在的服务器
            try:
                if flow_data.get("server"):
                    server = flow_data["server"]
                    port = flow_data.get("callback_port")
                    async_shutdown_server(server, port)
            except Exception as e:
                log.debug(f"清理过期流程时启动异步关闭服务器失败: {e}")

            # 显式清理流程数据，释放内存
            flow_data.clear()
            del auth_flows[state]
            cleaned_count += 1

    if cleaned_count > 0:
        log.info(f"清理了 {cleaned_count} 个过期的认证流程")

    # 更积极的垃圾回收触发条件
    if len(auth_flows) > 20:  # 降低阈值
        import gc

        gc.collect()
        log.debug(f"触发垃圾回收，当前活跃认证流程数: {len(auth_flows)}")


def get_auth_status(project_id: str) -> Dict[str, Any]:
    """获取认证状态"""
    for state, flow_data in auth_flows.items():
        if flow_data["project_id"] == project_id:
            return {
                "status": "completed" if flow_data["completed"] else "pending",
                "state": state,
                "created_at": flow_data["created_at"],
            }

    return {"status": "not_found"}


# 鉴权功能 - 使用更小的数据结构
auth_tokens = {}  # 存储有效的认证令牌
TOKEN_EXPIRY = 3600  # 1小时令牌过期时间


async def verify_password(password: str) -> bool:
    """验证密码（面板登录使用）"""
    from config import get_panel_password

    correct_password = await get_panel_password()
    return password == correct_password
