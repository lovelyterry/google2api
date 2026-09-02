"""
Converter Utilities - 统一的转换工具函数模块
包含 thoughtSignature 处理、系统消息合并、内容/思维链提取等公共基础功能
"""
from typing import Any, Dict, List, Mapping, Optional, Tuple
from src.log import log

# ==============================================================================
# 1. Thought Signature 处理
# ==============================================================================

# 在工具调用ID中嵌入thoughtSignature的分隔符
THOUGHT_SIGNATURE_SEPARATOR = "__thought__"
SKIP_THOUGHT_SIGNATURE_VALIDATOR = "skip_thought_signature_validator"
SKIP_THOUGHT_SIGNATURE_PLACEHOLDER_TEXT = "..."


def is_internal_placeholder_text(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return text.strip() in (SKIP_THOUGHT_SIGNATURE_PLACEHOLDER_TEXT, "…")


def is_skip_thought_signature_placeholder(part: Mapping[str, Any]) -> bool:
    """Return True for the internal placeholder that should not reach clients."""
    if not isinstance(part, Mapping):
        return False
    if part.get("thoughtSignature") != SKIP_THOUGHT_SIGNATURE_VALIDATOR:
        return False
    if "functionCall" in part or "function_call" in part or "functionResponse" in part:
        return False
    return is_internal_placeholder_text(part.get("text"))


def encode_tool_id_with_signature(tool_id: str, signature: Optional[str]) -> str:
    """
    将 thoughtSignature 编码到工具调用ID中，以便往返保留。
    """
    if not signature:
        return tool_id
    return f"{tool_id}{THOUGHT_SIGNATURE_SEPARATOR}{signature}"


def decode_tool_id_and_signature(encoded_id: str) -> Tuple[str, Optional[str]]:
    """
    从编码的ID中提取原始工具ID和thoughtSignature。
    """
    if not encoded_id or THOUGHT_SIGNATURE_SEPARATOR not in encoded_id:
        return encoded_id, None
    parts = encoded_id.split(THOUGHT_SIGNATURE_SEPARATOR, 1)
    return parts[0], parts[1] if len(parts) == 2 else None


# ==============================================================================
# 2. 内容提取与消息处理
# ==============================================================================

def extract_content_and_reasoning(parts: list) -> tuple:
    """从Gemini响应部件中提取内容和推理内容"""
    content = ""
    reasoning_content = ""
    images = []

    for part in parts:
        if is_skip_thought_signature_placeholder(part):
            continue

        # 提取文本内容
        text = part.get("text", "")
        if is_internal_placeholder_text(text):
            continue
        if text:
            if part.get("thought", False):
                reasoning_content += text
            else:
                content += text

        # 提取图片数据
        if "inlineData" in part:
            inline_data = part["inlineData"]
            mime_type = inline_data.get("mimeType", "image/png")
            base64_data = inline_data.get("data", "")
            images.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_data}"
                }
            })

    return content, reasoning_content, images


async def merge_system_messages(request_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    处理请求体中的 system 消息并将其规范提取为 systemInstruction
    """
    system_parts = []

    # 1. 提取顶层 system 字段
    system_content = request_body.get("system")
    if system_content:
        if isinstance(system_content, str):
            if system_content.strip():
                system_parts.append({"text": system_content})
        elif isinstance(system_content, list):
            for item in system_content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text", "").strip():
                        system_parts.append({"text": item["text"]})
                elif isinstance(item, str) and item.strip():
                    system_parts.append({"text": item})

    # 2. 提取 messages 中的 system 角色消息
    messages = request_body.get("messages", [])
    non_system_messages = []

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                system_parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text.strip():
                            system_parts.append({"text": text})
                    elif isinstance(item, str) and item.strip():
                        system_parts.append({"text": item})
        else:
            non_system_messages.append(msg)

    if not system_parts:
        return request_body

    result = request_body.copy()
    result.pop("system", None)
    result["systemInstruction"] = {
        "role": "system",
        "parts": system_parts
    }
    result["messages"] = non_system_messages

    return result

