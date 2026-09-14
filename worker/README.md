# Cloudflare Python Worker

原来的 TypeScript 代理不再是生产入口。当前 Worker 入口是
[`cloudflare_worker/src/entry.py`](../cloudflare_worker/src/entry.py)，配置文件是
[`wrangler.jsonc`](../wrangler.jsonc)。

Worker 直接在 `https://bot.boooe.com` 提供飞书 Webhook、MCP 和 OAuth，不使用
`PYTHON_ORIGIN` 或 tunnel。运行状态放在 D1，后台 Agent 工作放入 Queue，只有必须跨
重启保留的文件才放到 R2。资源创建、Secret 配置和部署命令见根目录
[README](../README.md)。
