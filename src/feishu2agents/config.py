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
        return cls(
            feishu_app_id=values["FEISHU_APP_ID"],
            feishu_app_secret=values["FEISHU_APP_SECRET"],
        )
