"""
Token 看板后台路由端点 (Token Dashboard Router)
提供 /token-dashboard/stats 数据接口与 /token-dashboard/clear 清空接口
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.token_usage import token_tracker
from src.utils import verify_panel_token

router = APIRouter(tags=["token-dashboard"])


@router.get("/token-dashboard/stats")
@router.get("/token-dashboard")
async def get_token_dashboard_stats(token: str = Depends(verify_panel_token)):
    """获取 Token 看板监控统计数据"""
    try:
        stats = await token_tracker.get_dashboard_stats()
        return JSONResponse(content={"success": True, "data": stats})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "detail": f"获取 Token 看板数据失败: {str(e)}"},
        )


@router.post("/token-dashboard/clear")
async def clear_token_dashboard_stats(token: str = Depends(verify_panel_token)):
    """清空 Token 历史统计记录"""
    try:
        await token_tracker.clear_stats()
        return JSONResponse(content={"success": True, "message": "已清空 Token 历史统计记录"})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "detail": f"清空失败: {str(e)}"},
        )
