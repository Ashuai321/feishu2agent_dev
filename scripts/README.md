# 小 C 测试脚本

这些脚本是独立测试工具，不改动现有机器人启动流程、Cloudflare Worker 或 Agent 配置。
命令默认操作 Feishu；需要 Lark 时统一加 `--platform lark`，会使用 Lark 的 API 域名、
应用凭证和独立的用户 token 文件。

## 1. 在固定群聊中 @文帅

项目根目录 `.env` 中已有 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 即可；即使从其他目录用绝对路径启动，脚本也会自动读取该项目的 `.env`。运行：

```bash
python -m scripts.xiaoc_send_mention --text "小C测试：请确认收到。"
# Lark 测试：
python -m scripts.xiaoc_send_mention --platform lark --text "Lark 小C测试"
```

脚本固定使用群 `oc_5e9132f3638772d53d92d6fc5e953abc`，并 @`ou_a00b35e763c13d60eb411ca9a344e777`。
命令行仍允许临时覆盖 chat_id 和 open_id，便于测试，不会修改项目默认配置。

## 2. 获取当前登录账号的多维表格用户权限

在对应平台应用的重定向地址白名单中加入：

```text
https://bot.boooe.com/feishu/oauth/callback
```

Lark 应用对应使用：

```text
https://bot.boooe.com/lark/oauth/callback
```

然后运行：

```bash
python -m scripts.xiaoc_bitable_oauth
# Lark OAuth：
python -m scripts.xiaoc_bitable_oauth --platform lark
```

浏览器会打开对应平台的授权页，公开 Worker 回调会用同一平台的应用凭证把 `code`
换成 `user_access_token`，并在回调页返回 JSON。Feishu token 使用
`FEISHU_USER_ACCESS_TOKEN` 或 `.feishu-user-token.json`；Lark token 使用
`LARK_USER_ACCESS_TOKEN` 或 `.lark-user-token.json`。不要把 token 提交到 Git。

## 3. 查看「测试」表并创建一行记录

先只读检查字段和现有记录：

```bash
python -m scripts.xiaoc_bitable_create_row --inspect
# Lark 多维表格：
python -m scripts.xiaoc_bitable_create_row --platform lark --inspect
```

脚本使用你提供的 Wiki 节点、表和视图：

- Wiki 节点：`AYNDwkmOUiZtbgkZ3BAcLchUnnb`
- 表：`tblVyvH3RGHqwQBC`
- 视图：`vewXxBNTOK`（读取字段/写记录不需要把 view_id 传给 API）

创建测试记录：

```bash
python -m scripts.xiaoc_bitable_create_row \
  --task-description "小C测试任务" \
  --summary "验证用户 OAuth 写入" \
  --progress "待开始" \
  --start-date 2026-09-18
```

脚本只写入实际存在的字段，日期转换为多维表格 API 所需的毫秒时间戳；人员字段默认使用 OAuth 当前用户的
`open_id`，也可用 `--assignee-open-id` 明确指定。`--inspect` 只读，不会创建记录。
