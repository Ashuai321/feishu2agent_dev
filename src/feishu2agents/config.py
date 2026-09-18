"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing."""


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_event_mode: str = "long_connection"
    feishu_verify_token: str = ""
    classify_api_key: str = ""
    answer_api_key: str = ""
    # Optional user-identity (OAuth) settings for operating Bitable with a
    # user's own permissions. The callback uri is the url Feishu redirects back
    # to after the user authorizes; it MUST be registered in the developer
    # console's redirect-url allowlist.
    feishu_oauth_redirect_uri: str = ""
    feishu_oauth_scope: str = "bitable:app"
    feishu_oauth_state_ttl_seconds: int = 600

    @classmethod
    def from_environment(cls) -> Settings:
        # load_dotenv never overrides values already supplied by the process.
        load_dotenv(override=False)
        names = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
        values = {name: os.getenv(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )
        event_mode = os.getenv("FEISHU_EVENT_MODE", "long_connection").strip().lower()
        if event_mode not in {"long_connection", "webhook"}:
            raise ConfigurationError(
                "Invalid FEISHU_EVENT_MODE: expected 'long_connection' or 'webhook', got "
                + event_mode
            )
        return cls(
            feishu_app_id=values["FEISHU_APP_ID"],
            feishu_app_secret=values["FEISHU_APP_SECRET"],
            feishu_event_mode=event_mode,
            feishu_verify_token=os.getenv("FEISHU_VERIFY_TOKEN", "").strip(),
            classify_api_key=os.getenv("STEP_ONE_KEY", "").strip(),
            answer_api_key=os.getenv("STEP_TWO_KEY", "").strip(),
            feishu_oauth_redirect_uri=os.getenv("FEISHU_OAUTH_REDIRECT_URI", "").strip(),
            feishu_oauth_scope=os.getenv("FEISHU_OAUTH_SCOPE", "bitable:app").strip()
            or "bitable:app",
            feishu_oauth_state_ttl_seconds=max(
                int(os.getenv("FEISHU_OAUTH_STATE_TTL_SECONDS", "600")), 60
            ),
        )
