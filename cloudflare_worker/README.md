# Cloudflare Python Worker implementation

`src/entry.py` is the Python Worker entrypoint. The root `wrangler.jsonc` binds it to
D1 (`DB`) and Queue (`AGENT_QUEUE`). R2 (`AVATARS`) remains optional until the account's
R2 billing subscription is activated. Use the deployment instructions in
[`../README.md`](../README.md); this directory is not a separate Worker project.

The configured test group (`BITABLE_WORKFLOW_GROUP_CHAT_ID`, default
`oc_5e9132f3638772d53d92d6fc5e953abc`) has a separate path: an @ message is
bound to the platform that delivered it, the sender's `open_id` is recorded, and
the sender must authorize that same Feishu/Lark platform before the text is
written to the Bitable. All other groups continue through the Agent queue.
