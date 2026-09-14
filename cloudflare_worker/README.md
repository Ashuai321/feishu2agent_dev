# Cloudflare Python Worker implementation

`src/entry.py` is the Python Worker entrypoint. The root `wrangler.jsonc` binds it to
D1 (`DB`), Queue (`AGENT_QUEUE`) and R2 (`AVATARS`). Use the deployment instructions in
[`../README.md`](../README.md); this directory is not a separate Worker project.
