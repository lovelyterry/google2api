"""
Gemini CLI Model List Router - Handles model list requests
Gemini CLI 模型列表路由 - 处理模型列表请求
"""

from src.log import log
from src.schemas import model_to_dict
from src.utils import authenticate_flexible, create_gemini_model_list, create_openai_model_list
from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 第三方库

# 本地模块 - 工具和认证

VERTEX_MODELS = []

# 本地模块 - 基础路由工具


# ==================== 路由器初始化 ====================

router = APIRouter()


# ==================== API 路由 ====================

@router.get("/vertex/v1beta/models")
async def list_gemini_models(token: str = Depends(authenticate_flexible)):
    log.debug("[VERTEX MODEL LIST] 返回 Gemini 格式")
    return JSONResponse(content=create_gemini_model_list(
        VERTEX_MODELS,
        base_name_extractor=lambda m: m
    ))


@router.get("/vertex/v1/models")
async def list_openai_models(token: str = Depends(authenticate_flexible)):
    log.debug("[VERTEX MODEL LIST] 返回 OpenAI 格式")
    model_list = create_openai_model_list(VERTEX_MODELS, owned_by="google")
    return JSONResponse(content={
        "object": "list",
        "data": [model_to_dict(model) for model in model_list.data]
    })
