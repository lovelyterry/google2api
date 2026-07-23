# google2api

<p align="center">
  <b>高性能 Google Gemini Code Assist & Antigravity 代理网关服务</b><br>
  将 Google 凭证无缝转换为标准 OpenAI、Anthropic (Claude) 以及 Gemini 格式 API 接口。
</p>

---

## 🌟 核心特性

- ⚡ **多协议 API 转换**：完整兼容 Anthropic (`/v1/messages`)、OpenAI (`/v1/chat/completions`) 及 Native Gemini 格式。
- 🤖 **Claude Code 完美整合**：支持作为 Claude Code 后端代理，零成本畅享高性能 Claude 协议格式体验。
- 🎯 **智能周额度优先调度**：内置精细化周额度阶梯算法，优先调用周限额剩余最多且重置日期最近的账号。
- 📌 **粘性账号与零冗余调度**：健康账号请求自动粘性复用，避免无意义的频繁切号，提升并发响应速度。
- ⏰ **429 自动冷却与故障转移**：遇到 429 限流或 503 服务异常时，自动施加模型级冷却并无缝切换备用账号重试。
- 📊 **Token 消耗可视化**：精确统计输入、输出、缓存及思考 Token，计算规则与前端仪表盘实时同步。
- 🖥️ **现代化 Web 控制面板**：提供可视化账号管理、一键【手动调度】、冒烟测试、检验 Project ID、额度刷新及日志监控。

---

## 🚀 快速开始

### 1. 环境要求与安装

确保安装了 **Python 3.10+**。推荐使用 `uv` 或 `pipenv` / `pip` 安装依赖：

```bash
# 克隆仓库
git clone https://github.com/lovelyterry/google2api.git
cd google2api

# 安装依赖 (推荐使用 uv)
uv sync

# 或使用标准 pip 安装
pip install -r requirements.txt
```

### 2. 启动服务

运行主程序启动网关服务：

```bash
# 直接运行主服务（默认端口 8051）
python main.py
```

终端输出如下提示即代表启动成功：

```text
INFO: 启动 google2api 主服务
INFO: Uvicorn running on http://0.0.0.0:8051 (Press CTRL+C to quit)
```

访问 Web 控制面板：**http://localhost:8051/** 

---

## 🤖 配合 Claude Code 使用配置

`google2api` 提供了与 Anthropic `/v1/messages` 完全兼容的代理端点。在 Claude Code 中配置环境变量即可无缝衔接：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8051/antigravity",
    "ANTHROPIC_API_KEY": "admin"
  }
}
```

### 配置参数解析：

- **`ANTHROPIC_BASE_URL`**: 设为 `http://127.0.0.1:8051/antigravity`（也可以使用 `http://127.0.0.1:8051/geminicli` 模式）。
- **`ANTHROPIC_API_KEY`**: 项目设定的 API 访问密码，默认值为 `admin`（可在环境变量 `API_PASSWORD` 中进行个性化配置）。

---

## 📡 API 端点一览

网关支持统一接入各种客户端（如 Claude Code, NextChat, OneAPI, Cherry Studio, LobeChat 等）：

### 1. Anthropic 格式 (Messages API)
- **Base URL**: `http://localhost:8051/antigravity` (Antigravity 模式)
- **Base URL**: `http://localhost:8051/geminicli` (GeminiCLI 模式)
- **接口路径**: `/v1/messages`

### 2. OpenAI 格式 (Chat Completions)
- **Antigravity 模式**: `http://localhost:8051/antigravity/v1/chat/completions`
- **GeminiCLI 模式**: `http://localhost:8051/v1/chat/completions`

### 3. 模型列表接口
- `GET http://localhost:8051/antigravity/v1/models`
- `GET http://localhost:8051/v1/models`

---

## ⚙️ 环境变量与高级配置

可通过设置系统环境变量或在 Web 面板【系统设置】中修改配置：

| 环境变量 | 默认值 | 作用说明 |
| :--- | :--- | :--- |
| `PORT` | `8051` | 网关服务监听端口 |
| `HOST` | `0.0.0.0` | 网关服务监听地址 |
| `API_PASSWORD` | `admin` | API 请求访问校验密钥 (`Authorization: Bearer <key>`) |
| `PANEL_PASSWORD` | `admin` | Web 控制面板登录密码 |
| `RETRY_429_ENABLED` | `true` | 是否开启 429 限流自动重试与切号 |
| `RETRY_429_MAX_RETRIES` | `5` | 429 / 错误触发的最大重试切号次数 |
| `AUTO_BAN_ERROR_CODES` | `[403]` | 触发自动禁用的错误码列表 |

---

## 🎛️ Web 控制面板功能

1. **账号调度高亮**：实时显示当前处于激活调度状态的账号（标记 `🎯 当前调度选中` 勋章）。
2. **手动调度**：点击卡片或表格行中的 **【手动调度】** 按钮，可手动指定特定账号作为当前调度的激活账号。
3. **冒烟测试 & 检验**：支持单账号或批量一键检验 Project ID 与进行即时请求冒烟测试。
4. **额度与重置时间**：直观展示账号 Weekly Limit 剩余比例、5小时限额及临近重置倒计时。
5. **Token 日志监控**：后台流式打印净输入 Token、输出 Token、缓存 Token 与总计消耗 Token。

---

## 📜 开源协议

本项目基于 [MIT License](LICENSE) 协议开源。
