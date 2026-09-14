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
Do not set it to the Worker public origin (`https://bot.boooe.com`) either:
that would make the Worker proxy to itself.

Example:

```text
PYTHON_ORIGIN=https://origin.bot.boooe.com
```

The origin must be reachable from the public Internet and must keep the Python
service running with `FEISHU_EVENT_MODE=webhook`.

`origin.bot.boooe.com` is intended to be a permanent backend hostname. It must
resolve to the independent Python deployment (for example, a VPS or a managed
container service) before the Worker secret is changed. A Quick Tunnel URL is
not suitable here because it changes or disappears when the tunnel process
stops.

The public addresses remain stable and separate:

```text
Feishu event URL: https://bot.boooe.com/feishu/events
MCP URL:          https://bot.boooe.com/mcp
Python origin:    https://origin.bot.boooe.com
```
