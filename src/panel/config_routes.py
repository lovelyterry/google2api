"""
配置路由模块 - 处理 /config/* 相关的HTTP请求
"""

from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

import src.config as config
from src.log import log
from src.schemas import ConfigSaveRequest
from src.storage import get_storage
from src.utils import verify_panel_token
from .utils import get_env_locked_keys


# 创建路由器
router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
@router.get("/")
async def get_config(token: str = Depends(verify_panel_token)):
    """获取当前配置"""
    try:

        # 读取当前配置（包括环境变量和TOML文件中的配置）
        current_config = {}

        # 基础配置
        current_config["code_assist_endpoint"] = await config.get_code_assist_endpoint()
        current_config["credentials_dir"] = await config.get_credentials_dir()
        current_config["proxy"] = await config.get_proxy_config() or ""

        # 代理端点配置
        current_config["oauth_proxy_url"] = await config.get_oauth_proxy_url()
        current_config["googleapis_proxy_url"] = await config.get_googleapis_proxy_url()
        current_config["resource_manager_api_url"] = await config.get_resource_manager_api_url()
        current_config["service_usage_api_url"] = await config.get_service_usage_api_url()
        current_config["antigravity_api_url"] = await config.get_antigravity_api_url()

        # 自动封禁配置
        current_config["auto_ban_enabled"] = await config.get_auto_ban_enabled()
        current_config["auto_ban_error_codes"] = await config.get_auto_ban_error_codes()

        # 429重试配置
        current_config["retry_429_max_retries"] = await config.get_retry_429_max_retries()
        current_config["retry_429_enabled"] = await config.get_retry_429_enabled()
        current_config["retry_429_interval"] = await config.get_retry_429_interval()
        current_config["request_min_interval"] = await config.get_request_min_interval()

        # 思维链返回配置
        current_config["return_thoughts_to_frontend"] = await config.get_return_thoughts_to_frontend()

        # Antigravity配置
        current_config["antigravity_switch_credential_enabled"] = await config.get_antigravity_switch_credential_enabled()
        current_config["antigravity_telemetry_enabled"] = await config.get_antigravity_telemetry_enabled()

        # 配额保鲜预热配置
        current_config["quota_warmup_enabled"] = await config.get_quota_warmup_enabled()
        current_config["quota_warmup_idle_hours"] = await config.get_quota_warmup_idle_hours()

        # 自适应思考默认预算配置 (用于 Claude Code / Extended Thinking)
        current_config["adaptive_thinking_budget"] = await config.get_adaptive_thinking_budget()

        # 服务器配置
        current_config["host"] = await config.get_server_host()
        current_config["port"] = await config.get_server_port()
        current_config["api_password"] = await config.get_api_password()
        current_config["panel_password"] = await config.get_panel_password()
        current_config["password"] = await config.get_server_password()

        # 从存储系统读取配置
        storage_adapter = await get_storage()
        storage_config = await storage_adapter.get_all_config()

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        # 合并存储系统配置（不覆盖环境变量）
        for key, value in storage_config.items():
            if key not in env_locked_keys:
                current_config[key] = value

        return JSONResponse(content={"config": current_config, "env_locked": list(env_locked_keys)})

    except Exception as e:
        log.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
@router.post("/")
async def save_config(request: ConfigSaveRequest, token: str = Depends(verify_panel_token)):
    """保存配置"""
    try:

        new_config = request.config

        log.debug(f"收到的配置数据: {list(new_config.keys())}")
        log.debug(f"收到的password值: {new_config.get('password', 'NOT_FOUND')}")

        # 验证配置项
        if "retry_429_max_retries" in new_config:
            if (
                not isinstance(new_config["retry_429_max_retries"], int)
                or new_config["retry_429_max_retries"] < 0
            ):
                raise HTTPException(
                    status_code=400, detail="最大429重试次数必须是大于等于0的整数")

        if "retry_429_enabled" in new_config:
            if not isinstance(new_config["retry_429_enabled"], bool):
                raise HTTPException(status_code=400, detail="429重试开关必须是布尔值")

        # 验证新的配置项
        if "retry_429_interval" in new_config:
            try:
                interval = float(new_config["retry_429_interval"])
                if interval < 0.01 or interval > 10:
                    raise HTTPException(
                        status_code=400, detail="429重试间隔必须在0.01-10秒之间")
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="429重试间隔必须是有效的数字")

        if "return_thoughts_to_frontend" in new_config:
            if not isinstance(new_config["return_thoughts_to_frontend"], bool):
                raise HTTPException(status_code=400, detail="思维链返回开关必须是布尔值")

        if "antigravity_switch_credential_enabled" in new_config:
            if not isinstance(new_config["antigravity_switch_credential_enabled"], bool):
                raise HTTPException(
                    status_code=400, detail="Antigravity切换凭证开关必须是布尔值")

        if "antigravity_telemetry_enabled" in new_config:
            if not isinstance(new_config["antigravity_telemetry_enabled"], bool):
                raise HTTPException(
                    status_code=400, detail="Antigravity伴随流量开关必须是布尔值")

        # 验证服务器配置
        if "host" in new_config:
            if not isinstance(new_config["host"], str) or not new_config["host"].strip():
                raise HTTPException(status_code=400, detail="服务器主机地址不能为空")

        if "port" in new_config:
            if (
                not isinstance(new_config["port"], int)
                or new_config["port"] < 1
                or new_config["port"] > 65535
            ):
                raise HTTPException(
                    status_code=400, detail="端口号必须是1-65535之间的整数")

        if "api_password" in new_config:
            if not isinstance(new_config["api_password"], str):
                raise HTTPException(status_code=400, detail="API访问密码必须是字符串")

        if "panel_password" in new_config:
            if not isinstance(new_config["panel_password"], str):
                raise HTTPException(status_code=400, detail="控制面板密码必须是字符串")

        if "password" in new_config:
            if not isinstance(new_config["password"], str):
                raise HTTPException(status_code=400, detail="访问密码必须是字符串")

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        # 直接使用存储适配器保存配置
        storage_adapter = await get_storage()
        for key, value in new_config.items():
            if key not in env_locked_keys:
                await storage_adapter.set_config(key, value)
                if key in ("password", "api_password", "panel_password"):
                    log.debug(f"设置{key}字段为: {value}")

        # 重新加载配置缓存（关键！）
        await config.reload_config()

        # 验证保存后的结果
        test_api_password = await config.get_api_password()
        test_panel_password = await config.get_panel_password()
        test_password = await config.get_server_password()
        log.debug(f"保存后立即读取的API密码: {test_api_password}")
        log.debug(f"保存后立即读取的面板密码: {test_panel_password}")
        log.debug(f"保存后立即读取的通用密码: {test_password}")

        # 构建响应消息
        response_data = {
            "message": "配置保存成功",
            "saved_config": {k: v for k, v in new_config.items() if k not in env_locked_keys},
        }

        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class LatencyTestRequest(BaseModel):
    proxy: Optional[str] = None


COUNTRY_NAMES = {
    "US": "美国", "JP": "日本", "HK": "中国香港", "TW": "中国台湾", "SG": "新加坡",
    "KR": "韩国", "GB": "英国", "DE": "德国", "FR": "法国", "CA": "加拿大",
    "AU": "澳大利亚", "NL": "荷兰", "CN": "中国大陆", "RU": "俄罗斯", "IN": "印度"
}


@router.post("/diagnose-network")
async def diagnose_network(
    request: Optional[LatencyTestRequest] = None,
    token: str = Depends(verify_panel_token)
):
    """
    全面网络诊断接口：
    涵盖代理 TCP 可达性、DNS 解析与 Fake-IP 识别、外网出口 IP 与归属地探测、
    Google 各核心端点并发 TLS 握手与延时探测，以及自动化故障排查行动指南。
    """
    import asyncio
    import socket
    import urllib.parse
    import time
    from src.client import CurlAsyncSession, _get_default_curl_options
    from src.api.utils import format_network_error

    # 1. 确定测试使用的代理
    test_proxy = None
    if request and request.proxy and request.proxy.strip():
        test_proxy = request.proxy.strip()
    else:
        test_proxy = await config.get_proxy_config()

    diag = {
        "proxy_info": {
            "is_configured": bool(test_proxy),
            "proxy_url": test_proxy or "直连 (未启用代理)",
            "tcp_reachable": None,
            "tcp_latency_ms": None,
            "tcp_error": None,
        },
        "dns_info": {
            "domain": "oauth2.googleapis.com",
            "ips": [],
            "is_fake_ip": False,
            "hint": "",
        },
        "egress_info": {
            "ip": None,
            "country_code": None,
            "country_name": "未知地区",
            "is_cn": False,
            "error": None,
        },
        "endpoints": [],
        "summary": {
            "status": "healthy",
            "title": "",
            "description": "",
            "actions": [],
        }
    }

    # 2. 检查代理本地 TCP 连通性
    if test_proxy:
        try:
            parsed = urllib.parse.urlparse(test_proxy)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (80 if parsed.scheme == "http" else 1080)
            t0 = time.perf_counter()
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.5)
            writer.close()
            await writer.wait_closed()
            diag["proxy_info"]["tcp_reachable"] = True
            diag["proxy_info"]["tcp_latency_ms"] = round((time.perf_counter() - t0) * 1000)
        except Exception as e:
            diag["proxy_info"]["tcp_reachable"] = False
            diag["proxy_info"]["tcp_error"] = str(e)

    # 3. DNS 解析与 Fake-IP 判定
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.run_in_executor(None, socket.getaddrinfo, "oauth2.googleapis.com", 443)
        ips = list(set([item[4][0] for item in addr_info]))
        diag["dns_info"]["ips"] = ips
        is_fake = any(ip.startswith("198.18.") or ip.startswith("198.19.") for ip in ips)
        diag["dns_info"]["is_fake_ip"] = is_fake
        if is_fake:
            diag["dns_info"]["hint"] = "检测到 198.18.0.0/15 Fake-IP 网段 (Clash/Mihomo/TUN 虚拟地址)。注意：在此模式下命令行 ping 仅测试本机虚拟网卡，无法反映真实外网连通性，请以 TLS 探测为准。"
    except Exception as e:
        diag["dns_info"]["hint"] = f"DNS 解析发生异常: {e}"

    # 4. 探测外网出口 IP 与归属地
    try:
        session_kwargs = {
            "timeout": 5.0,
            "verify": False,
            "impersonate": "chrome120",
            "curl_options": _get_default_curl_options(),
        }
        if test_proxy:
            session_kwargs["proxy"] = test_proxy

        async with CurlAsyncSession(**session_kwargs) as session:
            resp = await session.get("https://cloudflare.com/cdn-cgi/trace")
            lines = dict(l.split("=", 1) for l in resp.text.strip().split("\n") if "=" in l)
            ip_val = lines.get("ip")
            loc_val = lines.get("loc", "").upper()
            diag["egress_info"]["ip"] = ip_val
            diag["egress_info"]["country_code"] = loc_val
            diag["egress_info"]["country_name"] = COUNTRY_NAMES.get(loc_val, loc_val or "海外")
            diag["egress_info"]["is_cn"] = (loc_val == "CN")
    except Exception as e:
        diag["egress_info"]["error"] = format_network_error(e)

    # 5. 并发探测 Google 各核心端点
    endpoint_defs = [
        {
            "key": "oauth",
            "name": "Google OAuth2 认证服务",
            "url": await config.get_oauth_proxy_url(),
            "desc": "账号 Token 刷新与 OAuth 鉴权核心通道"
        },
        {
            "key": "antigravity",
            "name": "Antigravity 核心端点",
            "url": await config.get_antigravity_api_url(),
            "desc": "反重力模型对话与内容生成核心服务"
        },
        {
            "key": "googleapis",
            "name": "Google APIs 全局服务",
            "url": await config.get_googleapis_proxy_url(),
            "desc": "Google Cloud 资源管理与额度刷新网关"
        },
        {
            "key": "code_assist",
            "name": "Code Assist 节点",
            "url": await config.get_code_assist_endpoint(),
            "desc": "Gemini CLI 代码助手后端服务"
        },
    ]

    async def probe_single(ep):
        target_url = ep["url"]
        start_time = time.perf_counter()
        session_kwargs = {
            "timeout": 10.0,
            "verify": False,
            "impersonate": "chrome120",
            "curl_options": _get_default_curl_options(),
        }
        if test_proxy:
            session_kwargs["proxy"] = test_proxy

        try:
            async with CurlAsyncSession(**session_kwargs) as session:
                resp = await session.get(target_url)
                latency_ms = round((time.perf_counter() - start_time) * 1000)
                return {
                    "key": ep["key"],
                    "name": ep["name"],
                    "url": target_url,
                    "desc": ep["desc"],
                    "status": "ok",
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                    "error_detail": None,
                    "suggestion": None,
                }
        except Exception as e:
            latency_ms = round((time.perf_counter() - start_time) * 1000)
            friendly_err = format_network_error(e)
            raw_err = str(e)
            raw_lower = raw_err.lower()

            suggestion = ""
            if "connection closed abruptly" in raw_lower or "curl: (35)" in raw_lower:
                suggestion = "代理节点在 TLS 握手阶段主动掐断了连接。说明该代理节点出口 IP 被 Google 阻断或节点不稳定，请在代理软件中切换为其他地区的优质节点。"
            elif "timeout" in raw_lower or "curl: (28)" in raw_lower:
                suggestion = "连接超时。说明服务器未能连通该端点，请检查网络或配置有效海外代理。"
            elif "connection refused" in raw_lower or "curl: (7)" in raw_lower:
                suggestion = "连接被拒绝。请检查代理软件是否运行或端口是否输入正确。"
            elif "could not resolve host" in raw_lower or "curl: (6)" in raw_lower:
                suggestion = "DNS 域名解析失败。请检查系统 DNS 配置或代理设置。"

            return {
                "key": ep["key"],
                "name": ep["name"],
                "url": target_url,
                "desc": ep["desc"],
                "status": "error",
                "status_code": 0,
                "latency_ms": latency_ms,
                "error_detail": friendly_err,
                "raw_error": raw_err,
                "suggestion": suggestion,
            }

    # 并发执行
    results = await asyncio.gather(*(probe_single(ep) for ep in endpoint_defs))
    diag["endpoints"] = list(results)

    # 6. 综合智能排错与结论生成
    all_passed = all(ep["status"] == "ok" for ep in results)
    has_ssl_closed = any("connection closed abruptly" in (ep.get("raw_error") or "").lower() or "curl: (35)" in (ep.get("raw_error") or "").lower() for ep in results)
    has_timeout = any("timeout" in (ep.get("raw_error") or "").lower() or "curl: (28)" in (ep.get("raw_error") or "").lower() for ep in results)
    has_refused = any("connection refused" in (ep.get("raw_error") or "").lower() or "curl: (7)" in (ep.get("raw_error") or "").lower() for ep in results)
    proxy_unreachable = (diag["proxy_info"]["tcp_reachable"] is False)
    is_cn = diag["egress_info"]["is_cn"]

    ok_latencies = [ep["latency_ms"] for ep in results if ep["status"] == "ok"]
    avg_latency = round(sum(ok_latencies) / len(ok_latencies)) if ok_latencies else 0

    if all_passed:
        loc_str = diag["egress_info"]["country_name"] or "国际节点"
        diag["summary"] = {
            "status": "healthy",
            "title": "🎉 网络环境与 Google 各核心端点通信良好",
            "description": f"成功连通所有 Google 服务核心端点，公网出站归属地为【{loc_str}】，平均往返响应延迟约为 {avg_latency}ms。",
            "actions": [
                "当前网络链路健康，Token 刷新与模型调用均可正常运作，无需任何调整。"
            ]
        }
    elif proxy_unreachable:
        diag["summary"] = {
            "status": "critical",
            "title": "🚨 本地代理服务不可达 (TCP 端口连接被拒绝)",
            "description": f"后端尝试连接本地代理 [{test_proxy}] 失败，代理软件未运行或端口未监听。",
            "actions": [
                "请检查您的代理客户端 (如 Clash、v2ray、Mihomo) 是否已开启运行。",
                "核对填写的代理端口是否准确 (常见端口如 7890、10809、1080)。",
                "如果 google2api 部署在 Docker 容器或 WSL 子系统内，请勿使用 127.0.0.1，应填入宿主机局域网 IP 或 host.docker.internal，并在代理软件中开启【允许局域网连接 (Allow LAN)】。"
            ]
        }
    elif is_cn:
        diag["summary"] = {
            "status": "critical",
            "title": "🚨 当前出站出口位于中国大陆 (无法访问 Google)",
            "description": "服务端检测到的外网出站 IP 归属地为中国大陆，无法直接建立与 Google 服务的连接。",
            "actions": [
                "请在上方配置有效的海外网络代理 (如 http://127.0.0.1:7890)。",
                "确保代理节点选用美国、日本、新加坡等支持 Google 服务的地区。"
            ]
        }
    elif has_ssl_closed:
        diag["summary"] = {
            "status": "critical",
            "title": "🚨 代理节点在 TLS 握手阶段被切断 (Google 阻断或节点异常)",
            "description": "本地到代理客户端通信正常，但在向 Google 服务器发起 TLS 握手时，连接被对方立即掐断 (SSL_ERROR_SYSCALL / curl 35)。这通常是因为当前代理节点的出口 IP 被 Google 认证/云服务列入黑名单，或节点不支持相关 TLS 流量转发。",
            "actions": [
                "打开您的代理客户端 (Clash / Mihomo 等)，切换至其他地区或不同的节点（建议切换到日本、美国或新加坡的优质节点）。",
                "避免使用被大量滥用的公共机房节点，优先选用高质量家宽/优质 BGP 节点。",
                "切换节点后，点击本窗口的【重新诊断】以确认握手是否恢复正常。"
            ]
        }
    elif has_timeout:
        diag["summary"] = {
            "status": "critical",
            "title": "🚨 与 Google 端点连接超时 (curl: 28)",
            "description": "向 Google 核心端点发送请求时超过 10 秒无响应，网络丢包或路由中断。",
            "actions": [
                "检查服务器的外网连通性是否正常。",
                "若配置了代理，请检查代理节点是否已断线或延迟极高。",
                "在代理软件中测试节点延迟，更换为低延迟的可用节点。"
            ]
        }
    elif has_refused:
        diag["summary"] = {
            "status": "critical",
            "title": "🚨 目标端点或代理拒绝连接 (curl: 7)",
            "description": "向端点发出的 TCP 连接被拒绝。",
            "actions": [
                "请检查代理软件配置及端口监听状态。",
                "检查是否有本地防火墙或安全软件拦截了 Python/curl 出站流量。"
            ]
        }
    else:
        diag["summary"] = {
            "status": "warning",
            "title": "⚠️ 部分端点握手或访问异常",
            "description": "部分 Google 端点未能正常响应，请查看下方各端点的详细诊断报错信息。",
            "actions": [
                "检查异常端点的报错信息，对照具体原因进行排查。",
                "尝试切换代理节点或在配置页检查端点 URL 镜像设置。"
            ]
        }

    return JSONResponse(content=diag)


@router.post("/test-latency")
async def test_network_latency(
    request: Optional[LatencyTestRequest] = None,
    token: str = Depends(verify_panel_token)
):
    """
    为了保持向前兼容保留此端点，直接委托给 diagnose_network 处理。
    """
    diag_res = await diagnose_network(request, token)
    import json
    data = json.loads(diag_res.body.decode("utf-8"))
    return JSONResponse(content={
        "all_passed": data["summary"]["status"] == "healthy",
        "proxy_used": data["proxy_info"]["proxy_url"],
        "results": data["endpoints"],
        "diag": data,
    })

