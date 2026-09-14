# Feishu2Agents

当前生产入口是 Cloudflare Python Worker：飞书事件、MCP/OAuth 和 Agent 回调都在
Cloudflare 内完成。Worker 使用 D1 保存最小状态，Queue 处理后台任务，Feishu API
负责重新同步可恢复信息，R2 只保存必要文件。稳定公网域名保持为
`https://bot.boooe.com`，不使用 `PYTHON_ORIGIN` 或 tunnel。

## 架构

```mermaid
flowchart TD
    U[飞书用户] -->|@机器人| F[飞书群聊]
    F -->|开发者服务器 Webhook| W[Cloudflare Python Worker]
    W --> D1[(D1 最小状态)]
    W --> Q[Cloudflare Queue]
    Q --> A[Workspace Agent Trigger]
    A -->|MCP v3 回调| W
    W --> API[Feishu API]
    W -.必要文件.-> R2[(R2)]
    API --> F
```

队列让 Webhook 先快速确认，再异步发送占位消息、触发 Agent 和覆盖原占位消息。
D1 的去重、会话、发起人和 OAuth 状态在 Worker 重启后仍可恢复。

## 环境要求

- Python 3.11 or newer（Cloudflare Python Workers / `pywrangler` requirement）
- 一个已启用机器人能力的飞书企业自建应用
- 可以访问飞书开放平台的本地网络

本地 ASGI 入口仍可用于回归测试和故障排查；生产部署使用下面的 Cloudflare Python Worker，
不再把请求转发到另一个 Python 源站。

## 飞书开放平台配置

在运行程序前确认：

1. 在应用的“添加应用能力”中启用机器人。
2. 在“事件与回调”中选择“将事件发送至开发者服务器”，或在本地调试时选择“使用长连接接收事件”。
3. 添加事件 `im.message.receive_v1`。
4. 开通该事件页面要求的群聊 @机器人消息读取权限。
5. 开通回复消息 API 要求的权限。常见 scope 为
   `im:message:send_as_bot`，请以当前开放平台或 API Explorer 显示为准。
6. 创建并发布包含上述权限和事件的新应用版本，并完成管理员审批。
7. 确保应用可用范围包含测试用户。
8. 将机器人添加到测试群。

外部群还会受到租户安全策略、群管理员设置和应用可用范围限制。外部用户可能不提供
内部 `user_id`；本项目优先保存 `open_id`，不会假设发送者是本企业员工。

## 安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## 环境变量

复制模板并填写企业自建应用的凭证：

```bash
cp .env.example .env
```

`.env` 内容：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=your_app_secret
```

`.env` 已被 Git 忽略。不要将 App Secret、tenant access token、请求头或真实 `.env`
提交到仓库。如果 Secret 泄漏，请立即在飞书开放平台重新生成并更新本地配置。

进程环境变量优先于 `.env`，因此部署环境可以直接注入同名变量。

## 本地运行

激活虚拟环境后运行：

```bash
feishu2agents
```

也可以运行模块：

```bash
python -m feishu2agents.main
```

启动时程序会：

1. 校验 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。
2. 通过飞书 API 获取当前机器人的 `open_id`，用于准确识别多人 mention 中的 Bot。
3. 在长连接模式建立连接并订阅 `im.message.receive_v1`；在 Webhook 模式挂载
   `POST /feishu/events`（并保留 `/feishu/event` 兼容别名）。
4. 处理群聊中明确 @当前 Bot 的文本消息。
5. 使用消息回复 API 回复原消息。

日志只输出 message、chat、sender 等诊断标识和错误码，不输出 Secret、token、完整消息正文
或原始事件。

## Cloudflare Python Worker 部署

生产入口是 `cloudflare_worker/src/entry.py`。它把飞书 Webhook、MCP/OAuth 和 Agent 回调都运行在 Cloudflare Python Worker 内部，不再使用 Python 源站，也不需要 `PYTHON_ORIGIN`。现有稳定域名继续使用 `https://bot.boooe.com`。

Worker 使用四类 Cloudflare 绑定：

- D1（`DB`）保存去重键、Relay 运行记录、发起人映射和 OAuth 状态。
- Queue（`AGENT_QUEUE`）在飞书 Webhook 请求之外处理占位回复、Agent 触发和最终结果回写，避免超过飞书的响应时限。
- R2（`AVATARS`）只为确实需要跨重启保留的文件预留；当前部署暂不绑定 R2，避免开通需要付款方式的订阅。
- Worker Secrets 保存飞书凭证和 Workspace Agent 触发凭证。

首次部署时，在仓库根目录执行以下命令创建资源（先执行 `npx wrangler login`）：

```bash
npx wrangler d1 create feishu2agents-state
npx wrangler queues create feishu2agents-agent-jobs
# R2 需要先在 Cloudflare 账户中激活订阅并绑定付款方式，当前部署可跳过。
```

把 D1 命令输出的 `database_id` 写入 `wrangler.jsonc`，替换 `REPLACE_WITH_D1_DATABASE_ID`，然后执行：

```bash
npx wrangler d1 migrations apply feishu2agents-state --remote
npm install
uv run pywrangler deploy
```

再设置 Worker Secrets。下面的命令会逐项提示输入真实值，凭证不要提交到 Git：

```bash
npx wrangler secret put FEISHU_APP_ID
npx wrangler secret put FEISHU_APP_SECRET
npx wrangler secret put FEISHU_BOT_OPEN_ID
npx wrangler secret put FEISHU_VERIFY_TOKEN
npx wrangler secret put WORKSPACE_AGENT_RELAY_TRIGGER_URL
npx wrangler secret put WORKSPACE_AGENT_RELAY_AGENT_TOKEN
npx wrangler secret put WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN
```

`WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL` 可不设置，代码默认使用 `https://bot.boooe.com`。
`WORKSPACE_AGENT_RELAY_TRIGGER_URL` 是已发布 Workspace Agent 的触发地址，不是 Python 服务地址。
`FEISHU_BOT_OPEN_ID` 是机器人自身的 `open_id`；部署前通过飞书的
`GET /open-apis/bot/v3/info` 查询一次并保存。Worker 不会在事件请求内临时查询它，
这样 URL 验证和消息确认不会因冷启动或飞书 API 延迟超过 3 秒。

飞书“开发者服务器”事件请求地址填写：

```text
https://bot.boooe.com/feishu/events
```

ChatGPT 连接器的 MCP 地址填写：

```text
https://bot.boooe.com/mcp
```

如果使用 Cloudflare 的 GitHub 自动部署，仓库根目录保持 `/`，生产分支使用 `main`，构建命令留空，部署命令填写 `uv run pywrangler deploy`。D1、Queue 资源和 Worker Secrets 仍需在同一个 Cloudflare 账户中准备好；以后激活 R2 后再把 `AVATARS` 绑定加入配置。

## 测试

自动化检查：

```bash
ruff check .
ruff format --check .
pytest
```

群聊人工测试：

1. 启动程序并确认没有连接或认证错误。
2. 在已添加机器人的内部群发送 `@Bot hello`。
3. 确认机器人回复原消息 `收到：hello`。
4. 连续发送多条消息，确认每条只回复一次。
5. 发送不 @Bot 的消息，确认没有回复。
6. 由不同用户分别 @Bot，确认都可以正常回复。
7. 发送 `@Bot @其他用户 hello`，确认只移除 Bot mention。
8. 发送图片、文件或只发送 `@Bot`，确认程序忽略消息且继续运行。

## 当前行为与限制

- 只处理群聊 `text` 消息。
- 私聊、图片、文件、富文本和卡片暂不处理。
- Bot 或应用身份发送的事件会被忽略，避免消息循环。
- 使用 `message_id` 做进程内 TTL 去重：默认保留 10 分钟，最多 10,000 项。
- 处理失败会释放去重记录，以便飞书重推后再次处理。
- 去重状态不会跨进程或重启保留，也不在多个实例之间共享。
- 长连接由官方 SDK 管理和重连；多个实例不会广播收到同一事件。

## 常见问题

### 缺少环境变量

错误会列出缺失的变量名。确认 `.env` 位于仓库根目录，或在进程环境中设置变量。

### 认证失败或启动时无法获取 Bot identity

确认 App ID/Secret 正确、机器人能力已启用、应用版本已发布。日志会保留飞书错误码和
request log ID，但不会输出凭证。

### 能连接但收不到事件

确认使用的是长连接订阅模式、已经添加 `im.message.receive_v1`、消息读取权限已批准，
并且机器人已加入测试群。权限或事件变更后需要重新发布/安装应用版本。

### 收到事件但无法回复

检查回复消息 API 对应的发送权限、应用版本审批状态和飞书返回的错误码。不要将日志级别
改成会输出请求头或 token 的模式。

### Bot 无法加入外部群

检查企业管理员安全策略、应用可用范围、外部群类型和群管理员设置。这通常是飞书侧策略，
不是 WebSocket 代码问题。

### 长连接断开

SDK 会执行自动重连。若进程退出，检查网络、credentials 和脱敏后的异常日志，并由部署环境
的进程管理器重新启动程序。

## 后续 Agent 接入点

当前业务边界为：

```text
MessageContext -> EchoMessageHandler.handle() -> reply text
```

第二阶段只需替换为：

```text
MessageContext -> AgentGateway.process() -> reply text
```

`MessageContext` 已保留 `chat_id`、sender identifiers、tenant keys、bot app ID、message ID、
消息类型、文本和 mentions，可用于后续 Agent 路由、Memory 和权限判断。第一阶段不实现这些系统。
