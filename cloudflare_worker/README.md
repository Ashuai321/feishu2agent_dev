# Cloudflare Python Worker implementation

`src/entry.py` is the Python Worker entrypoint. The root `wrangler.jsonc` binds it to
D1 (`DB`) and Queue (`AGENT_QUEUE`). R2 (`AVATARS`) remains optional until the account's
R2 billing subscription is activated. Use the deployment instructions in
[`../README.md`](../README.md); this directory is not a separate Worker project.

The configured test group (`BITABLE_WORKFLOW_GROUP_CHAT_ID`, default
`oc_35d72b82857ee2f639d24190c0d6ca2d`) has a separate path. It accepts the
`[飞书文档]` and `[lark文档]` mode commands, then writes the requester's
follow-up text to the corresponding target table. The command only selects the
target document; OAuth is always selected from the requester's actual Feishu or
Lark account, so `[lark文档]` cannot force a Feishu user into Lark OAuth. The
authorization card is sent as a user-targeted ephemeral card, so it
is visible only to the requester in the group. All other groups continue
through the Agent queue.

## Bitable AI/workflow HTTP callback

The Worker also exposes a separate callback for a Bitable automation's
`发送 HTTP 请求` action:

```text
POST https://bot.boooe.com/bitable/automation/webhook
```

Configure the HTTP action after the table's `AI 分析` step. Set the request
body to the AI step's result/response-body variable (Feishu's `+` picker can
insert it into the JSON body). The callback accepts either that raw value or a
JSON object and forwards the result as a text message to the configured 小 C
group (`BITABLE_WORKFLOW_GROUP_CHAT_ID`). It uses the Feishu bot's tenant token
and does not use or change any user's OAuth state.

For a protected callback, set the Worker secret
`BITABLE_AUTOMATION_WEBHOOK_TOKEN` and add the same value as the
`X-Bitable-Webhook-Token` header in the HTTP action. The endpoint returns the
outbound Feishu `message_id` after the group message is sent.
