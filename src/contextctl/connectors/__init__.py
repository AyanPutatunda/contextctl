"""Layer 1: generation (untrusted). Connectors fetch evidence and draft files.

Built: :mod:`.notes` (rule-based notes connector, paper Sec. 3) and
:mod:`.dbt` / :mod:`.dbt_metrics` (the scripted dbt-project harvesters used by
the replay harness; the latter is the paper's metric-level scope).
Specified only (paper Sec. 3): repository, document, Confluence, Slack and
email connectors -- see :mod:`.specified`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..okf import Concept


@dataclass
class Harvest:
    """One draft plus the raw evidence bytes it cites: ``(bytes, uri, fetched_at)``."""

    concept: Concept
    evidence: list[tuple[bytes, str, str]] = field(default_factory=list)
