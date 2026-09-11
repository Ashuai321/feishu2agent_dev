# Cloudflare Worker edge entrypoint

This directory contains the Cloudflare Worker that fronts the existing Python
Feishu2Agents service. The Worker owns the stable public endpoint; the Python
service keeps the Feishu Webhook, Relay/MCP, OAuth, storage, Agent dispatch and
group workflow logic.

## Routes

- `GET /health` is served by the Worker itself.
- Feishu URL-verification `POST /feishu/events` requests containing a `challenge`
  are answered directly at the edge, without waiting for the Python origin.
- `POST /feishu/events` is mapped to the Python service's existing
  `POST /feishu/event` route.
- All other paths, including `/mcp`, `/oauth/*`, `/.well-known/*`, and `/api/*`,
  are forwarded unchanged.

## Cloudflare secret

Add a Worker secret named `PYTHON_ORIGIN` containing the absolute public origin
of the Python service, without a trailing slash. Do not use `127.0.0.1` or
`localhost`; a Cloudflare Worker cannot reach the developer machine directly.

Example:

```text
PYTHON_ORIGIN=https://your-python-origin.example.com
```

The origin must be reachable from the public Internet and must keep the Python
service running with `FEISHU_EVENT_MODE=webhook`.
