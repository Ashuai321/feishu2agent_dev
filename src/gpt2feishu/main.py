"""Application entry point."""

from __future__ import annotations

import logging

from .bot_handler import EchoMessageHandler
from .config import ConfigurationError, Settings
from .feishu import FeishuBot


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    try:
        settings = Settings.from_environment()
        FeishuBot(settings, EchoMessageHandler()).start()
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Feishu bot stopped by user")
        return 0
    except Exception as exc:
        # Avoid rendering arbitrary SDK exception details: connection errors can
        # contain URLs or headers. API errors produced by our adapter are sanitized.
        logger.error("Feishu bot stopped unexpectedly error_type=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
