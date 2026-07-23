import os
import json
import asyncio
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse

from log import log
from .sse import sse_manager


# 创建路由器
router = APIRouter(tags=["root"])


@router.get("/", response_class=HTMLResponse)
async def serve_control_panel(request: Request):
    """提供统一控制面板"""
    try:
        html_file_path = os.path.join("front", "dashboard.html")
        if not os.path.exists(html_file_path):
            raise HTTPException(status_code=500, detail="控制面板 HTML 文件未找到")

        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"加载控制面板页面失败: {e}")
        raise HTTPException(status_code=500, detail="服务器内部错误")


@router.get("/favicon.ico", include_in_schema=False)
async def serve_favicon_ico():
    """提供 favicon.ico"""
    favicon_path = os.path.join("front", "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="Favicon not found")


@router.get("/oauth-callback", response_class=HTMLResponse)
@router.get("/callback", response_class=HTMLResponse)
async def handle_root_oauth_callback(request: Request):
    """根路径自动接收 Google OAuth 重定向回调"""
    try:
        full_url = str(request.url)
        log.info(f"收到根路径 Google OAuth 回调重定向: {full_url}")
        from src.auth import complete_auth_flow_from_callback_url
        result = await complete_auth_flow_from_callback_url(full_url)
        if result.get("success"):
            return HTMLResponse(content="""<!DOCTYPE html>
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
</html>""")
        else:
            err_msg = result.get("error", "未知错误")
            return HTMLResponse(status_code=400, content=f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>授权失败</title></head>
<body style="font-family: system-ui, -apple-system, sans-serif; text-align: center; padding: 60px 20px; background: #0f172a; color: #f8fafc;">
    <div style="max-width: 480px; margin: 0 auto; background: #1e293b; padding: 36px 24px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); border: 1px solid #ef4444;">
        <div style="font-size: 54px; margin-bottom: 16px;">❌</div>
        <h2 style="color: #ef4444; margin-bottom: 8px; font-size: 22px;">授权失败</h2>
        <p style="color: #94a3b8; font-size: 14px; margin-bottom: 24px;">{err_msg}</p>
        <p style="font-size: 12px; color: #64748b;">请关闭窗口并返回控制面板重试。</p>
    </div>
</body>
</html>""")
    except Exception as e:
        log.error(f"处理根路径 OAuth 回调异常: {e}")
        return HTMLResponse(status_code=500, content="<h1>500 Internal Server Error</h1>")


@router.get("/sse")
async def sse_stream(request: Request, token: str = None):
    """控制面板 SSE 实时事件推流端点"""
    async def event_generator():
        queue = await sse_manager.connect()
        try:
            # 建立连接时推送初始化成功事件
            yield "event: init\ndata: {\"connected\": true}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    event_type = event.get("type", "message")
                    data_str = json.dumps(event.get("data", {}), ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data_str}\n\n"
                except asyncio.TimeoutError:
                    # 15 秒心跳包，维持长连接活跃
                    yield ": ping\n\n"
        finally:
            await sse_manager.disconnect(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

