"""
模型映射路由模块 - 处理 /model-mappings/* 相关的HTTP请求
"""

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import JSONResponse
from typing import Dict, Any

from src.utils import verify_panel_token
from src.model_mapping import model_mapping_manager
from src.log import log

router = APIRouter(prefix="/model-mappings", tags=["model-mappings"])


@router.get("")
@router.get("/")
async def get_model_mappings(token: str = Depends(verify_panel_token)):
    """获取所有动态模型映射与自定义映射规则"""
    try:
        data = model_mapping_manager.get_all_mappings()
        return JSONResponse(content={"success": True, "data": data})
    except Exception as e:
        log.error(f"获取模型映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
@router.post("/")
async def set_custom_mapping(
    payload: Dict[str, Any] = Body(...),
    token: str = Depends(verify_panel_token)
):
    """设置或覆盖自定义模型映射规则"""
    try:
        requested_model = payload.get(
            "requested_model") or payload.get("original_model")
        target_model = payload.get(
            "target_model") or payload.get("mapped_model")
        router_type = payload.get("router_type", "antigravity")

        if not requested_model or not target_model:
            raise HTTPException(
                status_code=400, detail="requested_model 与 target_model 不能为空")

        model_mapping_manager.set_custom_mapping(
            requested_model=requested_model,
            target_model=target_model,
            router_type=router_type
        )
        from .sse import sse_manager
        await sse_manager.broadcast("models_updated")
        return JSONResponse(content={"success": True, "message": "映射规则更新成功"})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"更新模型映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("")
@router.delete("/")
async def delete_custom_mapping(
    payload: Dict[str, Any] = Body(default={}),
    token: str = Depends(verify_panel_token)
):
    """删除指定的自定义模型映射规则（若未传参则清空所有规则）"""
    try:
        requested_model = payload.get(
            "requested_model") or payload.get("original_model")
        router_type = payload.get("router_type", "antigravity")

        if not requested_model:
            model_mapping_manager.clear_custom_mappings()
            from .sse import sse_manager
            await sse_manager.broadcast("models_updated")
            return JSONResponse(content={"success": True, "message": "已成功清空所有自定义映射记录"})

        model_mapping_manager.remove_custom_mapping(
            requested_model=requested_model,
            router_type=router_type
        )
        from .sse import sse_manager
        await sse_manager.broadcast("models_updated")
        return JSONResponse(content={"success": True, "message": "映射规则删除成功"})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"删除模型映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{requested_model}")
async def delete_custom_mapping_by_path(
    requested_model: str,
    router_type: str = "antigravity",
    token: str = Depends(verify_panel_token)
):
    """通过路径参数删除指定自定义模型映射规则"""
    try:
        model_mapping_manager.remove_custom_mapping(
            requested_model=requested_model,
            router_type=router_type
        )
        return JSONResponse(content={"success": True, "message": "映射规则删除成功"})
    except Exception as e:
        log.error(f"删除模型映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clear-custom")
async def clear_custom_mappings_endpoint(token: str = Depends(verify_panel_token)):
    """清空所有自定义模型映射记录"""
    try:
        model_mapping_manager.clear_custom_mappings()
        return JSONResponse(content={"success": True, "message": "已成功清空所有自定义映射记录"})
    except Exception as e:
        log.error(f"清空自定义映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/set-fallback")
async def set_fallback_mapping(
    payload: Dict[str, Any] = Body(...),
    token: str = Depends(verify_panel_token)
):
    """设置兜底模型"""
    try:
        fallback_model = payload.get("fallback_model", "")
        router_type = payload.get("router_type", "antigravity")

        model_mapping_manager.set_fallback_model(
            fallback_model=fallback_model,
            router_type=router_type
        )
        return JSONResponse(content={"success": True, "message": "兜底模型设置成功"})
    except Exception as e:
        log.error(f"设置兜底模型失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clear-dynamic")
async def clear_dynamic_mappings(token: str = Depends(verify_panel_token)):
    """清空动态抓取的实时模型映射记录"""
    try:
        model_mapping_manager.clear_dynamic_mappings()
        return JSONResponse(content={"success": True, "message": "已成功清空所有动态映射记录"})
    except Exception as e:
        log.error(f"清空动态映射失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
