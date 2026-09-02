"""
谷歌 OAuth 认证与客户端仿真常量
"""

# GeminiCLI 客户端仿真常量
_GEMINICLI_VERSION = "0.35.2"
_GEMINICLI_PLATFORM = "win32"
_GEMINICLI_ARCH = "x64"
_GEMINICLI_SURFACE = "cloud-shell"


def get_geminicli_user_agent(model: str = "") -> str:
    """生成动态 User-Agent: GeminiCLI/{version}/{model} ({platform}; {arch}; {surface})"""
    if model:
        return f"GeminiCLI/{_GEMINICLI_VERSION}/{model} ({_GEMINICLI_PLATFORM}; {_GEMINICLI_ARCH}; {_GEMINICLI_SURFACE})"
    return f"GeminiCLI/{_GEMINICLI_VERSION} ({_GEMINICLI_PLATFORM}; {_GEMINICLI_ARCH}; {_GEMINICLI_SURFACE})"


GEMINICLI_USER_AGENT = get_geminicli_user_agent()

# Antigravity CLI 客户端仿真常量
ANTIGRAVITY_CLI_VERSION = "1.1.9"
ANTIGRAVITY_CLI_PLATFORM = "windows/amd64"
ANTIGRAVITY_USER_AGENT = f"antigravity/cli/{ANTIGRAVITY_CLI_VERSION} {ANTIGRAVITY_CLI_PLATFORM}"

# OAuth Configuration - 标准模式
CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Antigravity OAuth Configuration
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

# 统一的 Token URL（两种模式相同）
TOKEN_URL = "https://oauth2.googleapis.com/token"

# 回调服务器配置
CALLBACK_HOST = "localhost"
