# Cloudflare Python Worker implementation

`src/entry.py` is the Python Worker entrypoint. The root `wrangler.jsonc` binds it to
D1 (`DB`) and Queue (`AGENT_QUEUE`). R2 (`AVATARS`) remains optional until the account's
R2 billing subscription is activated. Use the deployment instructions in
[`../README.md`](../README.md); this directory is not a separate Worker project.

The configured test group (`BITABLE_WORKFLOW_GROUP_CHAT_ID`, default
`oc_5e9132f3638772d53d92d6fc5e953abc`) has a separate path. It accepts the
`[飞书文档]` and `[lark文档]` mode commands, then writes the requester's
follow-up text to the corresponding target table. The command only selects the
target document; OAuth is always selected from the requester's actual Feishu or
Lark account, so `[lark文档]` cannot force a Feishu user into Lark OAuth. The
authorization card is sent as a user-targeted ephemeral card, so it
is visible only to the requester in the group. All other groups continue
through the Agent queue.
