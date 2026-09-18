"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing."""


@dataclass(frozen=True)
class PlatformSettings:
    """Credentials and endpoints for one Open Platform tenant.

    Feishu and Lark expose the same API shapes but use different domains and
    application credentials.  Keeping this information together prevents an
    event received from one platform from accidentally using the other
    platform's tenant or user token.
    """

    name: str
    app_id: str
    app_secret: str
    domain: str
    api_base: str
    auth_base: str
    event_mode: str = "long_connection"
    verify_token: str = ""
    oauth_redirect_uri: str = ""
    oauth_scope: str = "bitable:app wiki:wiki:readonly offline_access"
    oauth_state_ttl_seconds: int = 600


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
    feishu_oauth_scope: str = "bitable:app wiki:wiki:readonly offline_access"
    feishu_oauth_state_ttl_seconds: int = 600
    # Lark is optional so existing Feishu-only deployments start unchanged.
    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_event_mode: str = "long_connection"
    lark_verify_token: str = ""
    lark_oauth_redirect_uri: str = ""
    lark_oauth_scope: str = "bitable:app wiki:wiki:readonly offline_access"
    lark_oauth_state_ttl_seconds: int = 600

    def platform(self, name: str) -> PlatformSettings:
        """Return the immutable configuration for ``feishu`` or ``lark``."""
        normalized = str(name or "").strip().lower()
        if normalized == "feishu":
            return PlatformSettings(
                name="feishu",
                app_id=self.feishu_app_id,
                app_secret=self.feishu_app_secret,
                domain="https://open.feishu.cn",
                api_base="https://open.feishu.cn",
                auth_base="https://accounts.feishu.cn",
                event_mode=self.feishu_event_mode,
                verify_token=self.feishu_verify_token,
                oauth_redirect_uri=self.feishu_oauth_redirect_uri,
                oauth_scope=self.feishu_oauth_scope,
                oauth_state_ttl_seconds=self.feishu_oauth_state_ttl_seconds,
            )
        if normalized == "lark":
            if not self.lark_app_id or not self.lark_app_secret:
                raise ConfigurationError(
                    "Lark platform is not configured: set LARK_APP_ID and LARK_APP_SECRET"
                )
            return PlatformSettings(
                name="lark",
                app_id=self.lark_app_id,
                app_secret=self.lark_app_secret,
                domain="https://open.larksuite.com",
                api_base="https://open.larksuite.com",
                auth_base="https://accounts.larksuite.com",
                event_mode=self.lark_event_mode,
                verify_token=self.lark_verify_token,
                oauth_redirect_uri=self.lark_oauth_redirect_uri,
                oauth_scope=self.lark_oauth_scope,
                oauth_state_ttl_seconds=self.lark_oauth_state_ttl_seconds,
            )
        raise ConfigurationError(f"Unsupported platform: {name!r}; expected feishu or lark")

    def configured_platforms(self) -> tuple[str, ...]:
        platforms = ["feishu"]
        if self.lark_app_id and self.lark_app_secret:
            platforms.append("lark")
        return tuple(platforms)

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
        lark_event_mode = os.getenv("LARK_EVENT_MODE", "long_connection").strip().lower()
        if lark_event_mode not in {"long_connection", "webhook"}:
            raise ConfigurationError(
                "Invalid LARK_EVENT_MODE: expected 'long_connection' or 'webhook', got "
                + lark_event_mode
            )
        return cls(
            feishu_app_id=values["FEISHU_APP_ID"],
            feishu_app_secret=values["FEISHU_APP_SECRET"],
            feishu_event_mode=event_mode,
            feishu_verify_token=os.getenv("FEISHU_VERIFY_TOKEN", "").strip(),
            classify_api_key=os.getenv("STEP_ONE_KEY", "").strip(),
            answer_api_key=os.getenv("STEP_TWO_KEY", "").strip(),
            feishu_oauth_redirect_uri=os.getenv("FEISHU_OAUTH_REDIRECT_URI", "").strip(),
            feishu_oauth_scope=os.getenv(
                "FEISHU_OAUTH_SCOPE",
                "bitable:app wiki:wiki:readonly offline_access",
            ).strip()
            or "bitable:app wiki:wiki:readonly offline_access",
            feishu_oauth_state_ttl_seconds=max(
                int(os.getenv("FEISHU_OAUTH_STATE_TTL_SECONDS", "600")), 60
            ),
            lark_app_id=os.getenv("LARK_APP_ID", "").strip(),
            lark_app_secret=os.getenv("LARK_APP_SECRET", "").strip(),
            lark_event_mode=lark_event_mode,
            lark_verify_token=os.getenv("LARK_VERIFY_TOKEN", "").strip(),
            lark_oauth_redirect_uri=os.getenv("LARK_OAUTH_REDIRECT_URI", "").strip(),
            lark_oauth_scope=os.getenv(
                "LARK_OAUTH_SCOPE",
                "bitable:app wiki:wiki:readonly offline_access",
            ).strip()
            or "bitable:app wiki:wiki:readonly offline_access",
            lark_oauth_state_ttl_seconds=max(
                int(os.getenv("LARK_OAUTH_STATE_TTL_SECONDS", "600")), 60
            ),
        )
