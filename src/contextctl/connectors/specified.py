"""Connectors the paper *specifies* but does not build (Sec. 3).

They are declared here so the interface is explicit; calling one raises
:class:`NotImplementedError`. Each must reduce its source to one evidence
object per retained byte string, exactly like :mod:`.notes`.
"""

from __future__ import annotations

from typing import Any, NoReturn

SPECIFIED = ("repository", "document", "confluence", "slack", "email")


def connector(name: str, **_: Any) -> NoReturn:
    if name in SPECIFIED:
        raise NotImplementedError(
            f"the {name} connector is specified in the paper but not built in this release"
        )
    raise KeyError(name)
