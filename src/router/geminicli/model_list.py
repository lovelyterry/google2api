"""
Gemini CLI Model List Router - Handles model list requests
Gemini CLI 模型列表路由 - 处理模型列表请求
"""

from src.log import log
from src.schemas import model_to_dict
from src.api.antigravity import fetch_available_models
from src.utils import (
    get_base_model_from_feature_model,
    authenticate_flexible,
    create_gemini_model_list,
    create_openai_model_list,
)
from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends
import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


router = APIRouter()


async def get_geminicli_models():
    """获取可用模型列表"""
    base_models_data = await fetch_available_models()
    if not base_models_data:
        return []
    return [model['id'] for model in base_models_data if 'id' in model]


@router.get("/v1beta/models")
async def list_gemini_models(token: str = Depends(authenticate_flexible)):
    """
    返回 Gemini 格式的模型列表
    """
    models = await get_geminicli_models()
    log.debug("[GEMINICLI MODEL LIST] 返回 Gemini 格式")
    return JSONResponse(content=create_gemini_model_list(
        models,
        base_name_extractor=get_base_model_from_feature_model
    ))


@router.get("/v1/models")
async def list_openai_models(token: str = Depends(authenticate_flexible)):
    """
    返回 OpenAI 格式的模型列表
    """
    models = await get_geminicli_models()
    log.debug("[GEMINICLI MODEL LIST] 返回 OpenAI 格式")
    model_list = create_openai_model_list(models, owned_by="google")
    return JSONResponse(content={
        "object": "list",
        "data": [model_to_dict(model) for model in model_list.data]
    })

