"""Cloudflare Python Worker entrypoint.

The Worker deliberately keeps the public paths used by the existing
installation (``/feishu/events``, ``/lark/events``, ``/mcp`` and ``/oauth/*``), but does not
proxy to a Python origin.  Durable state is stored in D1, queued work is
handled by a Cloudflare Queue, and avatar bytes use the optional R2 binding.
"""

from worker_app import Default

__all__ = ["Default"]
