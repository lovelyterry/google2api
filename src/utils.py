import os
import sys
from typing import Any, AsyncIterator, List, Optional

from fastapi import Response
from fastapi.responses import StreamingResponse

from src.schemas import Model, ModelList
from src.auth import (
    ANTIGRAVITY_CLIENT_ID,
    ANTIGRAVITY_CLIENT_SECRET,
    ANTIGRAVITY_SCOPES,
    ANTIGRAVITY_USER_AGENT,
    CALLBACK_HOST,
    CLIENT_ID,
    CLIENT_SECRET,
    GEMINICLI_USER_AGENT,
    SCOPES,
    TOKEN_URL,
    authenticate_bearer,
    authenticate_flexible,
    authenticate_gemini_flexible,
    get_geminicli_user_agent,
    security,
    verify_panel_token,
)

# Antigravity CLI 客户端仿真常量
ANTIGRAVITY_CLI_VERSION = "1.1.9"
ANTIGRAVITY_CLI_PLATFORM = "windows/amd64"
ANTIGRAVITY_USER_AGENT = f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} {ANTIGRAVITY_CLI_PLATFORM}"


def get_resource_path(relative_path: str) -> str:
    """
    获取资源文件的绝对路径（兼容源码运行、Nuitka 和 PyInstaller 打包应用）
    """
    if hasattr(sys, "_MEIPASS"):
        path = os.path.join(getattr(sys, "_MEIPASS"), relative_path)
        if os.path.exists(path):
            return path

    main_module = sys.modules.get('__main__')
    main_file = getattr(main_module, '__file__',
                        __file__) if main_module else __file__
    main_dir = os.path.dirname(os.path.abspath(main_file))

    candidates = [
        os.path.join(main_dir, relative_path),
        os.path.join(os.path.dirname(main_dir), relative_path),
        os.path.join(os.getcwd(), relative_path),
    ]

    for cand in candidates:
        if os.path.exists(cand):
            return cand

    return relative_path


# Model name lists for different features
BASE_MODELS = []


# ====================== Model Helper Functions ======================

def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    return model_name


def get_available_models(router_type: str = "openai") -> List[str]:
    """
    Get available models with feature prefixes and suffixes.

    Args:
        router_type: "openai" or "gemini"

    Returns:
        List of model names
    """
    models = []

    for base_model in BASE_MODELS:
        # 基础模型
        models.append(base_model)

        # 定义思考后缀（根据模型系列不同）
        thinking_suffixes = []

        # Gemini 2.5 系列: 使用思考预算后缀
        if "gemini-2.5" in base_model:
            thinking_suffixes = ["-max", "-high",
                                 "-medium", "-low", "-minimal"]
        # Gemini 3 系列: 使用思考等级后缀
        elif "gemini-3" in base_model:
            if "flash" in base_model:
                # 3-flash-preview: 支持 high/medium/low/minimal
                thinking_suffixes = ["-high", "-medium", "-low", "-minimal"]
            elif "pro" in base_model:
                # 3-pro-preview: 支持 high/low
                thinking_suffixes = ["-high", "-low"]

        search_suffix = "-search"

        # 1. 单独的 thinking 后缀
        for thinking_suffix in thinking_suffixes:
            models.append(f"{base_model}{thinking_suffix}")

        # 2. 单独的 search 后缀
        models.append(f"{base_model}{search_suffix}")

        # 3. thinking + search 组合后缀
        for thinking_suffix in thinking_suffixes:
            combined_suffix = f"{thinking_suffix}{search_suffix}"
            models.append(f"{base_model}{combined_suffix}")

    return models


# ====================== Model List Helper Functions ======================

def create_openai_model_list(
    model_ids: List[str],
    owned_by: str = "google"
) -> ModelList:
    """
    创建OpenAI格式的模型列表

    Args:
        model_ids: 模型ID列表
        owned_by: 模型所有者

    Returns:
        ModelList对象
    """
    from datetime import datetime, timezone
    current_timestamp = int(datetime.now(timezone.utc).timestamp())

    models = [
        Model(
            id=model_id,
            object='model',
            created=current_timestamp,
            owned_by=owned_by
        )
        for model_id in model_ids
    ]

    return ModelList(data=models)


def create_gemini_model_list(
    model_ids: List[str],
    base_name_extractor=None
) -> dict:
    """
    创建Gemini格式的模型列表

    Args:
        model_ids: 模型ID列表
        base_name_extractor: 可选的基础模型名提取函数

    Returns:
        包含模型列表的字典
    """
    gemini_models = []

    for model_id in model_ids:
        base_model = model_id
        if base_name_extractor:
            try:
                base_model = base_name_extractor(model_id)
            except Exception:
                pass

        model_info = {
            "name": f"models/{model_id}",
            "baseModelId": base_model,
            "version": "001",
            "displayName": model_id,
            "description": f"Gemini {base_model} model",
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
        }
        gemini_models.append(model_info)

    return {"models": gemini_models}


# ====================== Streaming Passthrough Helpers ======================

async def prepend_async_item(first_item: Any, iterator: AsyncIterator[Any]):
    """Yield a prefetched item before continuing the original iterator."""
    yield first_item
    async for item in iterator:
        yield item


async def read_first_async_item(iterator: AsyncIterator[Any]) -> Any:
    """Python 3.9-compatible async equivalent of built-in anext()."""
    return await iterator.__anext__()


async def build_streaming_response_or_error(
    iterator: AsyncIterator[Any],
    media_type: str = "text/event-stream",
):
    """
    Prefetch the first async item so router code can return an upstream error
    response directly before FastAPI commits a 200 streaming response.
    """
    try:
        first_item = await read_first_async_item(iterator)
    except StopAsyncIteration:
        return Response(status_code=204)

    if isinstance(first_item, Response):
        return first_item

    return StreamingResponse(
        prepend_async_item(first_item, iterator),
        media_type=media_type,
    )

