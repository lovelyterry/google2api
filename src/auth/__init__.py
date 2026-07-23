"""
统一认证与凭证管理模块 (src.auth)

下属子组件：
1. api_auth.py: API 密码鉴权与 Panel 访问令牌校验 (authenticate_flexible, verify_panel_token)
2. constants.py: 谷歌 OAuth 客户端常量与 User-Agent
3. credential_manager.py: 统一凭证生命周期调度器 (CredentialManager, credential_manager)
4. google_oauth.py: 谷歌底层 OAuth API 交互 (Credentials, TokenError, get_user_projects 等)
5. oauth_flow.py: OAuth 登录流程与回调处理 (create_auth_url, complete_auth_flow_from_callback_url 等)
"""

from src.auth.api_auth import (
    authenticate_bearer,
    authenticate_flexible,
    authenticate_gemini_flexible,
    security,
    verify_panel_token,
)
from src.auth.constants import (
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
    get_geminicli_user_agent,
)
from src.auth.credential_manager import CredentialManager, credential_manager
from src.auth.google_oauth import (
    Credentials,
    TokenError,
    enable_required_apis,
    fetch_project_id_and_tier,
    get_user_email,
    get_user_projects,
    select_default_project,
)
from src.auth.oauth_flow import (
    asyncio_complete_auth_flow,
    complete_auth_flow_from_callback_url,
    create_auth_url,
    get_auth_status,
    verify_password,
)

__all__ = [
    # api_auth
    "authenticate_flexible",
    "authenticate_bearer",
    "authenticate_gemini_flexible",
    "verify_panel_token",
    "security",
    # constants
    "CLIENT_ID",
    "CLIENT_SECRET",
    "SCOPES",
    "ANTIGRAVITY_CLIENT_ID",
    "ANTIGRAVITY_CLIENT_SECRET",
    "ANTIGRAVITY_SCOPES",
    "TOKEN_URL",
    "CALLBACK_HOST",
    "GEMINICLI_USER_AGENT",
    "ANTIGRAVITY_USER_AGENT",
    "get_geminicli_user_agent",
    # credential_manager
    "CredentialManager",
    "credential_manager",
    # google_oauth
    "Credentials",
    "TokenError",
    "enable_required_apis",
    "fetch_project_id_and_tier",
    "get_user_email",
    "get_user_projects",
    "select_default_project",
    # oauth_flow
    "asyncio_complete_auth_flow",
    "complete_auth_flow_from_callback_url",
    "create_auth_url",
    "get_auth_status",
    "verify_password",
]
