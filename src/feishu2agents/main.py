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
from .group_draft_store import GroupDraftStore
from .relay_worker import RelayWorker
from .requester_registry import RequesterRegistry
from .trigger_dispatch import FeishuTriggerDispatcher
from .workspace_agent import WorkspaceAgentMessageHandler, WorkspaceAgentSettings

logger = logging.getLogger(__name__)


def _bootstrap_env_agents(relay_config: object, relay_store: object) -> None:
    """Register optional Agent definitions declared only in ``.env``.

    Existing database agents and settings are left untouched. Each definition
    uses a dedicated env-backed token reference, so switching test targets is
    a matter of changing the appended .env selection values and restarting.
    """
    prefix = "WORKSPACE_AGENT_RELAY_AGENT_"
    token_prefix = "WORKSPACE_AGENT_RELAY_AGENT_TOKEN_"
    for key, trigger_url in os.environ.items():
        if not key.startswith(prefix) or not key.endswith("_TRIGGER_URL"):
            continue
        suffix = key[len(prefix) : -len("_TRIGGER_URL")]
        if not suffix or suffix in {"TOKEN", "SELECTION_MODE"}:
            continue
        trigger_url = str(trigger_url or "").strip()
        token_var = f"{token_prefix}{suffix}"
        token_ref = f"env:{token_var}"
        if not trigger_url or not os.environ.get(token_var, "").strip():
            logger.warning("Skipping env Agent %s: trigger URL or token is missing", suffix)
            continue
        name = os.environ.get(f"{prefix}NAME_{suffix}", "").strip()
        if not name:
            name = suffix.replace("_", " ").title()
        try:
            relay_store.upsert_agent(name=name, trigger_url=trigger_url, token_ref=token_ref)
        except Exception:
            logger.exception("Could not register env Agent name=%s", name)

    activated_urls = tuple(getattr(relay_config, "activated_agent_urls", ()) or ())
    if activated_urls:
        if len(activated_urls) == 1 and activated_urls[0].strip().lower() == "all":
            try:
                relay_store.update_settings(agent_selection_mode="all")
            except (KeyError, ValueError):
                logger.exception("Could not activate all Agents from activated_agents")
            return
        selected_ids: list[int] = []
        for trigger_url in activated_urls:
            match = next(
                (agent for agent in relay_store.list_agents() if agent.get("trigger_url") == trigger_url),
                None,
            )
            if match is None:
                # A URL not yet in the database is accepted only when its
                # companion env definition supplies the token. This keeps old
                # DB agents untouched while allowing a new test Agent to be
                # selected with one .env field.
                trigger_id = trigger_url.rstrip("/").rsplit("/", 2)[-2]
                suffix = ""
                for env_key, env_url in os.environ.items():
                    if env_key.startswith(prefix) and env_key.endswith("_TRIGGER_URL") and env_url.strip() == trigger_url:
                        suffix = env_key[len(prefix) : -len("_TRIGGER_URL")]
                        break
                token_var = f"{token_prefix}{suffix}" if suffix else ""
                if not token_var or not os.environ.get(token_var, "").strip():
                    logger.warning("activated_agents URL is not configured with a token: %s", trigger_id)
                    continue
                name = os.environ.get(f"{prefix}NAME_{suffix}", "").strip() or f"Activated {trigger_id}"
                try:
                    match = relay_store.upsert_agent(
                        name=name,
                        trigger_url=trigger_url,
                        token_ref=f"env:{token_var}",
                    )
                except Exception:
                    logger.exception("Could not register activated Agent name=%s", name)
                    continue
            selected_ids.append(int(match["id"]))
        if selected_ids:
            try:
                relay_store.update_settings(
                    agent_selection_mode="single" if len(selected_ids) == 1 else "multi",
                    enabled_agent_ids=selected_ids,
                )
            except (KeyError, ValueError) as exc:
                logger.warning("Could not apply activated_agents: %s", exc)
        return

    mode = str(getattr(relay_config, "agent_selection_mode", "") or "").strip().lower()
    names = tuple(getattr(relay_config, "enabled_agent_names", ()) or ())
    if not mode and not names:
        return
    if mode not in {"single", "multi", "all"}:
        logger.warning("Ignoring invalid WORKSPACE_AGENT_RELAY_AGENT_SELECTION_MODE=%s", mode)
        return
    try:
        if mode == "all":
            relay_store.update_settings(agent_selection_mode="all")
            return
        if not names:
            logger.warning("Agent selection names are required for mode=%s", mode)
            return
        ids = [int(relay_store.get_agent_by_name(name)["id"]) for name in names]
        if mode == "single" and len(ids) != 1:
            logger.warning("Single-agent mode requires exactly one enabled agent")
            return
        relay_store.update_settings(
            agent_selection_mode=mode,
            enabled_agent_ids=ids,
        )
    except (KeyError, ValueError) as exc:
        logger.warning("Could not apply env Agent selection: %s", exc)


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
    dispatcher = FeishuTriggerDispatcher()
    try:
        settings = Settings.from_environment()

        # The built-in relay owns config, store, MCP and the dashboard.
        from feishu2agents.relay.server import build_http_app
        from feishu2agents.relay.server import config as relay_config
        from feishu2agents.relay.server import mcp as relay_mcp
        from feishu2agents.relay.server import store as relay_store

        relay_config.ensure_runtime_directories()
        _bootstrap_env_agents(relay_config, relay_store)
        bridge = FeishuBridge(relay_config.state_dir / "feishu-bridge.sqlite")
        group_draft_store = GroupDraftStore(relay_config.state_dir / "group-draft.sqlite")
        requester_registry = RequesterRegistry()
        agent_settings = WorkspaceAgentSettings(
            store=relay_store,
            bridge=bridge,
            dispatcher=dispatcher,
            relay_config=relay_config,
            group_draft_store=group_draft_store,
        )
        handler = WorkspaceAgentMessageHandler(agent_settings)
        # A single business handler is shared by both platforms, while each
        # platform has its own SDK client, bot identity and credentials.
        bots = {
            platform: FeishuBot(settings, handler, platform=platform)
            for platform in settings.configured_platforms()
        }

        def bot_for_context(context):
            return bots.get(context.platform) or bots["feishu"]

        handler.post_placeholder = bots["feishu"]._reply
        handler.post_placeholder_for_context = lambda context, text: bot_for_context(
            context
        )._reply(context.message_id, text)
        handler.download_image = bots["feishu"].download_message_image
        handler.download_image_for_context = lambda context, image_key: bot_for_context(
            context
        ).download_message_image(context.message_id, image_key)
        handler.on_dispatch = lambda ctx, ck: requester_registry.register(
            ck,
            open_id=ctx.sender_ids.open_id or ctx.sender_id,
            name=ctx.sender_ids.open_id or "",
            source_chat_id=ctx.chat_id,
            platform=ctx.platform,
        )

        def bot_for_run(run):
            key = str(run.get("conversation_key") or "")
            platform = key.split(":", 1)[0].lower() if ":" in key else "feishu"
            return bots.get(platform) or bots["feishu"]

        worker = RelayWorker(
            relay_store,
            bridge,
            bots["feishu"]._reply,
            update_message=bots["feishu"]._update,
            reply_for_run=lambda message_id, text, run: bot_for_run(run)._reply(
                message_id, text
            ),
            update_for_run=lambda message_id, text, run: bot_for_run(run)._update(
                message_id, text
            ),
            interval=float(os.getenv("AGENT_WORKER_INTERVAL", "2.0")),
            freshness_ttl_seconds=int(os.getenv("AGENT_WORKER_TTL_SECONDS", "3600")),
        )

        # Add the Feishu group-creation tools to the shared relay MCP server
        register_feishu_tools(relay_mcp, bots, requester_registry, group_draft_store)

        # In webhook mode, receive Feishu events over HTTP instead of the long
        # connection: mount the event callback on the same relay HTTP app. The
        # user-identity OAuth callbacks are mounted regardless of event mode.
        extra_routes = []
        for platform_bot in bots.values():
            extra_routes.extend(platform_bot.oauth_routes())
            if platform_bot._platform_config.event_mode == "webhook":
                extra_routes.extend(platform_bot.webhook_routes())
        app = build_http_app(extra_routes=extra_routes)

        def serve_http() -> None:
            uvicorn.run(app, host=relay_config.host, port=relay_config.port, log_config=None)

        http_server = threading.Thread(target=serve_http, name="relay-http", daemon=True)
        http_server.start()
        logger.info("Relay server listening on %s:%s", relay_config.host, relay_config.port)

        worker.start()
        if any(bot._platform_config.event_mode != "webhook" for bot in bots.values()):
            for platform, platform_bot in bots.items():
                if platform_bot._platform_config.event_mode == "webhook":
                    continue
                thread = threading.Thread(
                    target=platform_bot.start, name=f"{platform}-bot", daemon=True
                )
                thread.start()

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
