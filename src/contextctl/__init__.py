"""contextctl: auditable, open-format context infrastructure for AI agents.

Four layers (paper, Fig. 1):

1. Generation (untrusted)  -- :mod:`contextctl.connectors`, :mod:`contextctl.evidence`
2. Curation (trust boundary) -- :mod:`contextctl.gate`, :mod:`contextctl.reconcile`
3. Metadata (derived)        -- :mod:`contextctl.sidecar`
4. Serving                    -- :mod:`contextctl.serving`
"""

__version__ = "0.1.0"
