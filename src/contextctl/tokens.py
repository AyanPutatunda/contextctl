"""Token counting for the disclosure-cost measurements.

Uses tiktoken ``cl100k_base`` (as in the paper) when installed and its
encoding is available; otherwise falls back to a ``ceil(chars / 4)`` estimate
and says so (``exact`` is False). Never mix the two in one report.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _encoder() -> Any:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - depends on environment
        return None


def exact() -> bool:
    return _encoder() is not None


def count(text: str) -> int:
    enc = _encoder()
    if enc is None:  # pragma: no cover - depends on environment
        return math.ceil(len(text) / 4)
    return len(enc.encode(text, disallowed_special=()))
