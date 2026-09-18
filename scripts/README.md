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

## 4. 按当前授权用户自动选择 Feishu/Lark 并写入

`xiaoc_bitable_current_user.py` 会读取已保存的 Feishu/Lark 用户 OAuth token，
调用对应平台的 `user_info` 获取当前用户的 `open_id`，再用同一平台的 API 检查机器人
身份并写入「测试」表。它不会根据 `open_id` 的字符串形状猜平台；平台来自 token
配置和对应 API 域名。

```bash
# 列出所有可用的已授权用户（不会写入）
python -m scripts.xiaoc_bitable_current_user --list-users

# 只有一个授权用户时自动选择；多个时在终端选择
python -m scripts.xiaoc_bitable_current_user --text "来自当前用户的测试任务"

# 非交互环境明确指定平台或 open_id
python -m scripts.xiaoc_bitable_current_user --platform lark --user-open-id ou_xxx \
  --text "Lark 用户测试任务"

# 只读查看目标表，验证用户实际权限
python -m scripts.xiaoc_bitable_current_user --inspect
```

App ID/Secret 只代表机器人，不能凭空得到群成员的用户 token。群里的 @ 事件能可靠提供
`platform`（事件进入 `/feishu/events` 或 `/lark/events`）和发送人的 `open_id`；若要以该
用户身份访问多维表格，仍必须让该用户完成一次对应平台 OAuth。回调会校验 OAuth 返回的
用户 `open_id` 与发起 @ 的人一致，避免把一个人的授权用于另一个人。

## 5. 获取群成员并逐人 @

```bash
# 自动尝试 Feishu/Lark；若两个平台都能看到同一 chat_id，会要求选择
python -m scripts.xiaoc_group_mention_all --dry-run
python -m scripts.xiaoc_group_mention_all --platform feishu --text "请确认收到"
```

脚本会排除机器人自身，每个成员单独发送一条只包含一个 `<at>` 的消息；`--dry-run` 只
读取成员 `open_id`，不会发送。
