"""Application entry point.

Boots the Feishu long-connection bot and the built-in relay server
(``feishu2agents.relay``) in the same process. The relay server owns the
MCP ``/mcp`` endpoint (agent callback channel) plus the dashboard/HTTP API and
its ``RelayStore``; this process adds the Feishu listener and a worker that
posts completed agent runs back into Feishu threads.
"""

from __future__ import annotations

import logging
import os
import threading

import uvicorn

from .config import ConfigurationError, Settings
from .feishu import FeishuBot
from .feishu_bridge import FeishuBridge
from .feishu_mcp import register_feishu_tools
from .relay_worker import RelayWorker
from .requester_registry import RequesterRegistry
from .trigger_dispatch import FeishuTriggerDispatcher
from .workspace_agent import WorkspaceAgentMessageHandler, WorkspaceAgentSettings

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _trust_certifi() -> None:
    """Point the default TLS trust store at certifi.

    Some libraries (e.g. lark-oapi's websockets client) build an SSL context from
    the interpreter's default verify paths, which on this Python 3.14 layout is a
    framework bundle that lacks the proxy CA certifi already trusts.
    """
    try:
        import certifi
    except ImportError:
        return
    if not os.environ.get("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = certifi.where()


def main() -> int:
    configure_logging()
    _trust_certifi()
    worker = None
    http_server = None
    bot_thread = None
    dispatcher = FeishuTriggerDispatcher()
    try:
        settings = Settings.from_environment()

        # The built-in relay owns config, store, MCP and the dashboard.
        from feishu2agents.relay.server import build_http_app
        from feishu2agents.relay.server import config as relay_config
        from feishu2agents.relay.server import mcp as relay_mcp
        from feishu2agents.relay.server import store as relay_store

        relay_config.ensure_runtime_directories()
        bridge = FeishuBridge(relay_config.state_dir / "feishu-bridge.sqlite")
        requester_registry = RequesterRegistry()
        agent_settings = WorkspaceAgentSettings(
            store=relay_store,
            bridge=bridge,
            dispatcher=dispatcher,
            relay_config=relay_config,
        )
        handler = WorkspaceAgentMessageHandler(agent_settings)
        bot = FeishuBot(settings, handler)
        handler.post_placeholder = bot._reply
        handler.on_dispatch = lambda ctx, ck: requester_registry.register(
            ck,
            open_id=ctx.sender_ids.open_id or ctx.sender_id,
            name=ctx.sender_ids.open_id or "",
            source_chat_id=ctx.chat_id,
        )
        worker = RelayWorker(
            relay_store,
            bridge,
            bot._reply,
            update_message=bot._update,
            interval=float(os.getenv("AGENT_WORKER_INTERVAL", "2.0")),
            freshness_ttl_seconds=int(os.getenv("AGENT_WORKER_TTL_SECONDS", "3600")),
        )

        # Add the Feishu group-creation tools to the shared relay MCP server
        register_feishu_tools(relay_mcp, bot, requester_registry)
        app = build_http_app()

        def serve_http() -> None:
            uvicorn.run(app, host=relay_config.host, port=relay_config.port, log_config=None)

        http_server = threading.Thread(target=serve_http, name="relay-http", daemon=True)
        http_server.start()
        logger.info("Relay server listening on %s:%s", relay_config.host, relay_config.port)

        worker.start()
        bot_thread = threading.Thread(target=bot.start, name="feishu-bot", daemon=True)
        bot_thread.start()

        # Keep the main process alive; signal (Ctrl+C) exits the try/finally.
        threading.Event().wait()
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Feishu bot stopped by user")
        return 0
    except Exception as exc:
        logger.error(
            "Feishu bot stopped unexpectedly error_type=%s",
            type(exc).__name__,
            exc_info=True,
        )
        return 1
    finally:
        if worker is not None:
            worker.stop()
        dispatcher.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())