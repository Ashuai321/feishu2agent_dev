"""Backward-compatible entrypoint. Prefer feishu2agents.relay.app.build_app."""

from .app import build_app

__all__ = ["build_app"]
