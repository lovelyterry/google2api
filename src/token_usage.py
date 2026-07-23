"""
Token 统计与估算器模块 (Token Tracker)
包含：
1. tiktoken BPE 输入 Prompt Token 估算 (estimate_input_tokens)
2. API 响应包 usageMetadata 结构体解析与中文日志打印 (extract_usage_tokens, log_usage_metadata)
"""
from __future__ import annotations

import base64
import json
import struct
from typing import Any, Dict, List, Optional, Tuple

from log import log

# ==============================================================================
# 1. Token 预估 (Estimator) - 基于 tiktoken (BPE 词表) 与结构化 Payload 解析
# ==============================================================================

_tiktoken_encoding = None
_tiktoken_initialized = False


def _get_tiktoken_encoding() -> Optional[Any]:
    """懒加载 tiktoken (cl100k_base) 编码器"""
    global _tiktoken_encoding, _tiktoken_initialized
    if _tiktoken_initialized:
        return _tiktoken_encoding

    _tiktoken_initialized = True
    try:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        log.info("[TokenUsage] 成功初始化 tiktoken (cl100k_base) 分词器")
    except Exception as e:
        log.warning(f"[TokenUsage] 初始化 tiktoken 失败: {e}，将采用加权降级算法")

    return _tiktoken_encoding


def _count_text_tokens(text: str) -> int:
    """使用 tiktoken 计算单个文本字符串的 Token 数"""
    if not text:
        return 0
    encoding = _get_tiktoken_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            return _fallback_text_tokens(text)
    return _fallback_text_tokens(text)


def _parse_image_dimensions(base64_str: str) -> Tuple[Optional[int], Optional[int]]:
    """从 Base64 文本中轻量解析图片的像素宽度和高度 (支持 PNG, JPEG, WEBP, GIF)"""
    if not base64_str:
        return None, None
    try:
        if "," in base64_str:
            base64_str = base64_str.split(",", 1)[1]
        data_bytes = base64.b64decode(base64_str[:344])
        if len(data_bytes) < 16:
            return None, None

        # PNG
        if data_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            w, h = struct.unpack(">II", data_bytes[16:24])
            return w, h

        # GIF
        if data_bytes.startswith(b"GIF87a") or data_bytes.startswith(b"GIF89a"):
            w, h = struct.unpack("<HH", data_bytes[6:10])
            return w, h

        # WEBP
        if data_bytes.startswith(b"RIFF") and data_bytes[8:12] == b"WEBP":
            if data_bytes[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", data_bytes[26:30])
                return w & 0x3FFF, h & 0x3FFF
            elif data_bytes[12:16] == b"VP8L":
                b0, b1, b2, b3 = data_bytes[21:25]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0xF) << 12) | (b2 << 4) | ((b1 & 0xC0) >> 6))
                return w, h
            elif data_bytes[12:16] == b"VP8X":
                w = 1 + struct.unpack("<I", data_bytes[24:27] + b"\x00")[0]
                h = 1 + struct.unpack("<I", data_bytes[27:30] + b"\x00")[0]
                return w, h

        # JPEG
        if data_bytes.startswith(b"\xff\xd8"):
            idx = 2
            while idx + 4 < len(data_bytes):
                marker, length = struct.unpack(">HH", data_bytes[idx:idx + 4])
                if marker in (0xFFC0, 0xFFC1, 0xFFC2):
                    if idx + 9 <= len(data_bytes):
                        h, w = struct.unpack(">HH", data_bytes[idx + 5:idx + 9])
                        return w, h
                    break
                idx += 2 + length
    except Exception:
        pass
    return None, None


def _calculate_image_tokens(image_item: Dict[str, Any]) -> int:
    """根据图片实际分辨率计算 Token 消耗"""
    base64_data = ""
    if "source" in image_item and isinstance(image_item["source"], dict):
        base64_data = image_item["source"].get("data", "")
    elif "inlineData" in image_item and isinstance(image_item["inlineData"], dict):
        base64_data = image_item["inlineData"].get("data", "")
    elif "image_url" in image_item:
        url_val = image_item.get("image_url")
        if isinstance(url_val, dict):
            base64_data = url_val.get("url", "")
        elif isinstance(url_val, str):
            base64_data = url_val

    width, height = _parse_image_dimensions(base64_data)
    if width and height and width > 0 and height > 0:
        max_dim = max(width, height)
        if max_dim > 1568:
            scale = 1568.0 / max_dim
            width = int(width * scale)
            height = int(height * scale)
        tokens = int((width * height) / 750)
        return max(85, tokens)

    return 768


def estimate_input_tokens(payload: Dict[str, Any]) -> int:
    """
    高精度估算 payload 输入的 Prompt Token 数（基于 tiktoken BPE）。
    精细解析 system、messages/contents、tools 纯文本，动态换算图片 Token 并补充角色 Overhead。
    """
    if not isinstance(payload, dict):
        return 0

    total_tokens = 0
    message_count = 0

    # 1. System Prompt
    system = payload.get("system") or payload.get("systemInstruction")
    if system:
        if isinstance(system, str):
            total_tokens += _count_text_tokens(system) + 3
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, str):
                    total_tokens += _count_text_tokens(block)
                elif isinstance(block, dict):
                    if block.get("text"):
                        total_tokens += _count_text_tokens(str(block.get("text")))
                    elif block.get("parts"):
                        for part in block.get("parts", []):
                            if isinstance(part, dict) and part.get("text"):
                                total_tokens += _count_text_tokens(str(part["text"]))
            total_tokens += 3

    # 2. Messages / Contents
    messages = payload.get("messages") or payload.get("contents")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message_count += 1
            total_tokens += 4  # 消息角色 Tag 开销

            content = msg.get("content") or msg.get("parts")
            if isinstance(content, str):
                total_tokens += _count_text_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        total_tokens += _count_text_tokens(item)
                    elif isinstance(item, dict):
                        item_type = item.get("type")

                        # 文本
                        if item_type == "text" or ("text" in item and not item_type):
                            text_val = item.get("text", "")
                            if text_val:
                                total_tokens += _count_text_tokens(str(text_val))

                        # 工具调用
                        elif item_type == "tool_use":
                            name = item.get("name", "")
                            input_data = item.get("input", {})
                            tool_text = f"tool_use {name}: {json.dumps(input_data, ensure_ascii=False)}"
                            total_tokens += _count_text_tokens(tool_text) + 4
                        elif item_type == "tool_result":
                            res_content = item.get("content", "")
                            if isinstance(res_content, str):
                                total_tokens += _count_text_tokens(res_content)
                            elif isinstance(res_content, list):
                                for res_item in res_content:
                                    if isinstance(res_item, dict) and res_item.get("text"):
                                        total_tokens += _count_text_tokens(str(res_item["text"]))

                        # 多模态图片
                        elif item_type in ("image", "image_url") or "inlineData" in item or "source" in item:
                            total_tokens += _calculate_image_tokens(item)

    # 3. Tools 定义
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                tool_str = json.dumps({
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                    "parameters": tool.get("input_schema") or tool.get("parameters")
                }, ensure_ascii=False)
                total_tokens += _count_text_tokens(tool_str) + 6

    # 4. 对话末尾 Assistant 引言引导
    if message_count > 0:
        total_tokens += 3

    return max(1, total_tokens)


def _fallback_text_tokens(text: str) -> int:
    """多语言字符加权估算（保底算法）"""
    tokens = 0.0
    for char in text:
        if '\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u30ff' or '\uac00' <= char <= '\ud7af':
            tokens += 1.5
        elif char.isascii():
            tokens += 0.25
        else:
            tokens += 0.5
    return max(1, int(tokens))


# ==============================================================================
# 2. Token 日志与结果解析 (Logger & Usage Parser)
# ==============================================================================

def extract_usage_tokens(usage_metadata: Any) -> Dict[str, Optional[int]]:
    """
    从 usageMetadata 结构体/字典中提取 Token 详细数据

    Args:
        usage_metadata: 包含 promptTokenCount 等字段的字典或对象

    Returns:
        包含 prompt_token_count, candidates_token_count, total_token_count,
        cached_content_token_count, thoughts_token_count 的字典
    """
    if not isinstance(usage_metadata, dict):
        return {
            "prompt_token_count": None,
            "candidates_token_count": None,
            "total_token_count": None,
            "cached_content_token_count": None,
            "thoughts_token_count": None,
        }

    return {
        "prompt_token_count": usage_metadata.get("promptTokenCount"),
        "candidates_token_count": usage_metadata.get("candidatesTokenCount"),
        "total_token_count": usage_metadata.get("totalTokenCount"),
        "cached_content_token_count": usage_metadata.get("cachedContentTokenCount"),
        "thoughts_token_count": usage_metadata.get("thoughtsTokenCount"),
    }


def log_usage_metadata(usage_metadata: Any, model: str, format_name: str = "Gemini") -> None:
    """
    统一打印 UsageMetadata 中文日志

    Args:
        usage_metadata: 包含 promptTokenCount 等字段的字典
        model: 实际调用的模型名称
        format_name: 协议格式名称 (如 OpenAI, Anthropic, Gemini)
    """
    if not isinstance(usage_metadata, dict) or not usage_metadata:
        return

    tokens = extract_usage_tokens(usage_metadata)
    model_str = model if model else "未知模型"

    log.info(
        f"[UsageMetadata-{format_name}格式] 模型={model_str} | "
        f"输入Token(promptTokenCount)={tokens['prompt_token_count']}, "
        f"输出Token(candidatesTokenCount)={tokens['candidates_token_count']}, "
        f"总Token(totalTokenCount)={tokens['total_token_count']}, "
        f"缓存Token(cachedContentTokenCount)={tokens['cached_content_token_count']}, "
        f"思考Token(thoughtsTokenCount)={tokens['thoughts_token_count']}"
    )
