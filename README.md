# Feishu2Agents

一个使用飞书官方 Python SDK 和长连接接收事件的企业自建应用 Bot。

第一阶段实现最小 Echo Bot：在群里发送 `@Bot hello`，机器人回复原消息
`收到：hello`。代码将飞书事件转换为独立的 `MessageContext`，以后可以将 Echo
handler 替换成 ChatGPT/Agent Gateway，而不重写飞书接入层。

## 环境要求

- Python 3.11（项目支持 `>=3.11,<3.13`）
- 一个已启用机器人能力的飞书企业自建应用
- 可以访问飞书开放平台的本地网络

长连接模式不需要公网服务器或 webhook URL，但程序必须保持运行。

## 飞书开放平台配置

在运行程序前确认：

1. 在应用的“添加应用能力”中启用机器人。
2. 在“事件与回调”中选择“使用长连接接收事件”。
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
python3.11 -m venv .venv
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
3. 建立长连接并订阅 `im.message.receive_v1`。
4. 处理群聊中明确 @当前 Bot 的文本消息。
5. 使用消息回复 API 回复原消息。

日志只输出 message、chat、sender 等诊断标识和错误码，不输出 Secret、token、完整消息正文
或原始事件。

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
