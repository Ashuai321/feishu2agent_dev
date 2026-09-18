# Feishu2Agents

当前生产入口是 Cloudflare Python Worker：Feishu/Lark 事件、MCP/OAuth 和 Agent 回调都在
Cloudflare 内完成。Worker 使用 D1 保存最小状态，Queue 处理后台任务，平台 API
负责重新同步可恢复信息，R2 只保存必要文件。稳定公网域名保持为
`https://bot.boooe.com`，不使用 `PYTHON_ORIGIN` 或 tunnel。

## 架构

```mermaid
flowchart TD
    U[Feishu/Lark 用户] -->|@机器人| F[对应平台群聊]
    F -->|平台 Webhook| W[Cloudflare Python Worker]
    W --> D1[(D1 最小状态)]
    W --> Q[Cloudflare Queue]
    Q --> A[Workspace Agent Trigger]
    A -->|MCP prd 回调| W
    W --> API[对应平台 API]
    W -.必要文件.-> R2[(R2)]
    API --> F
```

队列让 Webhook 先快速确认，再异步发送占位消息、触发 Agent 和覆盖原占位消息。
D1 的去重、会话、发起人和 OAuth 状态在 Worker 重启后仍可恢复。

## 环境要求

- Python 3.11 or newer（Cloudflare Python Workers / `pywrangler` requirement）
- 一个已启用机器人能力的 Feishu 企业自建应用；如需 Lark，再准备一个 Lark 应用
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
# 可选：配置后本地进程会同时监听 Lark；凭证只放本地环境，不提交仓库。
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=your_lark_app_secret
```

`.env` 已被 Git 忽略。不要将 App Secret、tenant access token、请求头或真实 `.env`
提交到仓库。如果 Secret 泄漏，请立即在飞书开放平台重新生成并更新本地配置。

进程环境变量优先于 `.env`，因此部署环境可以直接注入同名变量。

### Feishu 测试时切换 Workspace Agent

在 `.env` 末尾追加 `activated_agents` 即可切换目标：填一个 Workspace Agent
触发 URL 表示单选；多个 URL 用英文逗号分隔表示多选；填写 `all` 表示启用
relay 数据库中已登记的全部 Agent。未设置该字段时，程序继续使用原来的当前
Agent。新 URL 需要同时配置对应的
`WORKSPACE_AGENT_RELAY_AGENT_TOKEN_<NAME>` 与
`WORKSPACE_AGENT_RELAY_AGENT_<NAME>_TRIGGER_URL`，令牌只保存在本地环境变量中。

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

1. 校验 Feishu 凭证；若同时配置 `LARK_APP_ID`/`LARK_APP_SECRET`，再初始化 Lark 客户端。
2. 分别通过对应平台 API 获取机器人的 `open_id`，用于准确识别多人 mention 中的 Bot。
3. 各平台独立建立长连接，或分别挂载 `POST /feishu/events`、`POST /lark/events`（均保留
   单数 `/event` 兼容别名）。
4. 处理群聊中明确 @当前 Bot 的文本消息。
5. 使用消息回复 API 回复原消息。

日志只输出 message、chat、sender 等诊断标识和错误码，不输出 Secret、token、完整消息正文
或原始事件。

### 平台判定与用户权限

平台不会通过 `open_id` 的字符串格式猜测，因为 Feishu 与 Lark 的 ID 形状可能相同。
程序以事件进入的 Webhook 路径或长连接客户端作为可信来源：Feishu 事件进入
`/feishu/events`，Lark 事件进入 `/lark/events`；本地长连接也分别绑定对应应用凭证。
随后会话键带有 `feishu:` 或 `lark:` 前缀，Agent 回调、占位消息、最终回复、群聊工具和
去重记录都沿用这个前缀，因此不会把 Lark 请求发到 Feishu，反之亦然。

涉及用户权限的多维表格操作同样按平台分开：先进入对应平台的 OAuth 地址取得当前用户的
`user_access_token`，再使用同一平台 API 域名读取该用户对目标多维表格的权限并执行读写。
本地脚本用 `--platform feishu` 或 `--platform lark` 选择平台，token 文件和环境变量也彼此
独立；不提供 Lark 凭证时，原 Feishu 流程保持不变。

### 指定测试群：以 @ 发起人的身份写入多维表格

`BITABLE_WORKFLOW_GROUP_CHAT_ID` 默认是
`oc_5e9132f3638772d53d92d6fc5e953abc`。这个群只接受两种模式指令：`[飞书文档]` 和
`[lark文档]`（也兼容不带方括号的写法）。输入模式后，机器人会提示同一用户继续
`@` 机器人发送要写入的文字；也可以在指令后直接附带文字。飞书文档模式沿用原来的
Feishu OAuth 和「测试」表，将文字写入「任务描述」、用户写入「任务执行人」。Lark
文档模式使用 Lark OAuth，将文字写入 Lark Wiki 节点
`UmGRwFFDQiegHckOVh0j0DIrpRc` 下的表 `tblmd8DAQwM00t7B` 的「文本」，并将用户写入
「测试3」。不同平台的 token、API 域名和用户身份严格隔离。

机器人事件本身只能提供发送人的 `open_id`，不会携带该用户的 `user_access_token`，所以
第一次操作必须点击授权。成功授权的 token 会按 `平台 + open_id` 保存在 Worker 的 D1
中，后续同一用户可直接复用；撤销或过期后会再次要求授权。若 Lark 用户无权访问这个
Feishu 租户中的 Wiki/多维表格，API 会返回权限错误，系统不会降级使用 Feishu 机器人
或其他人的 token。

需要只在本地验证当前已授权用户时，可运行：

```bash
python -m scripts.xiaoc_bitable_current_user --list-users
python -m scripts.xiaoc_bitable_current_user --text "当前用户测试任务"
```

另有逐人 @ 测试脚本：

```bash
python -m scripts.xiaoc_group_mention_all --dry-run
python -m scripts.xiaoc_group_mention_all --platform feishu --text "请确认收到"
```

指定群之外的消息仍交给原来的 Workspace Agent Relay；该 Agent 路径已单独封装，避免与
授权写表流程互相影响。

## Cloudflare Python Worker 部署

生产入口是 `cloudflare_worker/src/entry.py`。它把飞书 Webhook、MCP/OAuth 和 Agent 回调都运行在 Cloudflare Python Worker 内部，不再使用 Python 源站，也不需要 `PYTHON_ORIGIN`。现有稳定域名继续使用 `https://bot.boooe.com`。

Worker 使用四类 Cloudflare 绑定：

- D1（`DB`）保存去重键、Relay 运行记录、发起人映射和 OAuth 状态。
- Queue（`AGENT_QUEUE`）在飞书 Webhook 请求之外处理占位回复、Agent 触发和最终结果回写，避免超过飞书的响应时限。
- R2（`AVATARS`）只为确实需要跨重启保留的文件预留；当前部署暂不绑定 R2，避免开通需要付款方式的订阅。
- Worker Secrets 保存 Feishu/Lark 凭证和 Workspace Agent 触发凭证。Feishu 与 Lark 的
  App ID、App Secret、Bot open_id 和 verify token 必须分别配置；事件的平台前缀会贯穿
  会话、回复和用户授权，因而不会交叉使用另一平台的 token。

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
npx wrangler secret put LARK_APP_ID
npx wrangler secret put LARK_APP_SECRET
npx wrangler secret put LARK_BOT_OPEN_ID
npx wrangler secret put LARK_VERIFY_TOKEN
npx wrangler secret put WORKSPACE_AGENT_RELAY_TRIGGER_URL
npx wrangler secret put WORKSPACE_AGENT_RELAY_AGENT_TOKEN
npx wrangler secret put WORKSPACE_AGENT_RELAY_OAUTH_LOGIN_TOKEN
```

飞书用户 OAuth 默认回调地址为 `https://bot.boooe.com/feishu/oauth/callback`，请把它加入飞书应用的重定向地址白名单。若使用其他地址，再配置 `FEISHU_OAUTH_REDIRECT_URI`；授权入口为
`https://bot.boooe.com/feishu/oauth/authorize`，回调会用 `code` 换取
`user_access_token`。当前使用飞书 OAuth v3 令牌端点
`https://accounts.feishu.cn/oauth/v3/token`，`state` 会保存在 D1 中并且只能使用一次。

`WORKSPACE_AGENT_RELAY_PUBLIC_BASE_URL` 可不设置，代码默认使用 `https://bot.boooe.com`。
`WORKSPACE_AGENT_RELAY_TRIGGER_URL` 是已发布 Workspace Agent 的触发地址，不是 Python 服务地址。
`FEISHU_BOT_OPEN_ID` 是机器人自身的 `open_id`；部署前通过飞书的
`GET /open-apis/bot/v3/info` 查询一次并保存。Worker 不会在事件请求内临时查询它，
这样 URL 验证和消息确认不会因冷启动或飞书 API 延迟超过 3 秒。

飞书“开发者服务器”事件请求地址填写：

```text
https://bot.boooe.com/feishu/events
```

Lark 应用填写：

```text
https://bot.boooe.com/lark/events
```

用户多维表格授权也按平台区分：Feishu 使用 `/feishu/oauth/authorize`，Lark 使用
`/lark/oauth/authorize`；本地脚本可通过 `--platform lark` 取得 Lark 用户 token，写入
脚本会使用对应的 Lark API 域名和 `.lark-user-token.json`，不会复用 Feishu token。

指定测试群是 Feishu 外部群时，不要求 Lark 机器人加入群。Feishu 机器人接收消息后会
根据事件中的发送方租户判断授权平台：同租户走 Feishu OAuth，外部租户走 Lark OAuth，
授权链接仍由 Feishu 机器人回复；回调再使用 Lark 用户令牌写入多维表格。若部署环境能
明确列出 Lark 租户，可用逗号分隔的 `LARK_EXTERNAL_TENANT_KEYS` 覆盖自动判断结果。
由于 Feishu/Lark 的用户 `open_id` 属于不同命名空间，跨平台回调不会再比较两边的
`open_id`，而是使用一次性、短时有效的 OAuth state 绑定原始群消息；同平台仍保持
严格的用户身份匹配。

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
