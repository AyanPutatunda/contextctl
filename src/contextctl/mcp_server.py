"""MCP server -- *specified* in the paper (Sec. 2 "Serving"), not built.

The intended tool surface mirrors :class:`contextctl.serving.Server`:
``orient``, ``search``, ``read``, ``history``, ``provenance`` and a
staging-only ``propose``. Use the CLI or the Python API in this release.
"""

from __future__ import annotations

from typing import NoReturn

TOOLS = ("orient", "search", "read", "history", "provenance", "propose")


def serve() -> NoReturn:
    raise NotImplementedError(
        "The contextctl MCP server is specified in the paper but not built in this release. "
        "Use the `contextctl` CLI or contextctl.serving.Server."
    )
