# 小 T 多维表格 Agent（DEV）

小 T 与小 C 共用现有 `feishu2agents` DEV Worker、Queue、D1 数据库和 MCP App。小 T 使用自己的飞书机器人凭据、Agent API 通道与受限的多维表格工具；PRD 不包含小 T 路由或凭据。

## DEV 地址

- 小 T 飞书事件回调：`https://bot.boooe.com/xiaot/feishu/events`
- 小 C 与小 T 共用 MCP：`https://bot.boooe.com/mcp`
- MCP 授权：由现有 `workspace-agent-relay-mcp-dev` 连接器通过 `/oauth/*` 处理
- 小 T 状态：`https://bot.boooe.com/xiaot/health`

DEV 飞书应用需同时订阅 `im.message.receive_v1` 和 `im.message.reaction.created_v1`，事件回调使用小 T 地址。第二个事件用于把用户对待确认提案的表情发送给 Worker；未订阅时，消息文字确认仍可用，但表情确认不会触发。验证令牌通过 `XIAOT_FEISHU_VERIFY_TOKEN` 单独设置，不与小 C 共用。

## 小 T 专用密钥

将密钥保存在忽略 Git 的 `.env.xiaot.dev` 中；部署时仅写入现有 DEV Worker 的 Cloudflare Secrets：

- `XIAOT_FEISHU_APP_ID`
- `XIAOT_FEISHU_APP_SECRET`
- `XIAOT_FEISHU_BOT_OPEN_ID`
- `XIAOT_FEISHU_VERIFY_TOKEN`
- `XIAOT_AGENT_TRIGGER_URL`
- `XIAOT_AGENT_ACCESS_TOKEN`

不要将这些值写进源代码、Agent instructions 或版本库。Workspace Agent access token 需要在到期前轮换。MCP 登录令牌、OAuth scope 和客户端配置继续使用现有 DEV MCP 设置，不创建小 T 专属 OAuth 配置。

## 集成与工具隔离

入口为 `cloudflare_worker/src/entry.py`。小 T 通过 `/xiaot/feishu/events` 接收飞书消息，并使用共享 Queue；Queue 按事件来源和已存运行记录的 `xiaot:` conversation 前缀选择对应 Agent 与机器人凭据。

共享 MCP `/mcp` 暴露小 C 原有工具和小 T 的多维表格工具。刷新 `workspace-agent-relay-mcp-dev` 的 actions 后，分别在 Agent 配置中勾选需要的工具：小 C 保持原有日程工具选择；小 T 只勾选七张表的读取/提案/确认工具和必要的进度、回复工具。服务端也会拒绝小 T 会话调用日历等未启用工具，并要求所有小 T 多维表格操作携带当前小 T 会话上下文。

小 T 只能访问 `cloudflare_worker/src/xiaot_worker.py` 列出的七张表。新增、更新、删除必须先展示绑定当前会话和发起人的一次性提案；同一发起人明确确认后才执行，并写入 changelog。
