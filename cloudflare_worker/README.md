# Cloudflare Python Worker implementation

`src/entry.py` is the Python Worker entrypoint. The root `wrangler.jsonc` binds it to
D1 (`DB`) and Queue (`AGENT_QUEUE`). R2 (`AVATARS`) remains optional until the account's
R2 billing subscription is activated. Use the deployment instructions in
[`../README.md`](../README.md); this directory is not a separate Worker project.
