"""Content-addressed evidence store (paper, Sec. 2 "Evidence object and digest scope").

At fetch time a connector stores the retained raw bytes at ``evidence/<h>`` where
``h`` is the SHA-256 digest of *those bytes only*. The source URI and retrieval
time are kept outside ``h``: in the concept's ``sources`` header and in a
metadata record ``evidence/<h>.meta.json`` written beside the blob **only when
the blob is first stored**. A retried fetch is therefore idempotent, and an
edited source becomes a new object while the old one is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .okf import is_digest


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    digest: str
    size: int
    first_uri: str
    fetched_at: str
    created: bool  # True if this call stored a new blob


class EvidenceStore:
    """A folder of content-addressed blobs."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, digest: str) -> Path:
        if not is_digest(digest):
            raise ValueError(f"not a SHA-256 hex digest: {digest!r}")
        return self.root / digest

    def meta_path(self, digest: str) -> Path:
        return self.root / f"{digest}.meta.json"

    def put(self, data: bytes, uri: str, fetched_at: str) -> EvidenceRecord:
        h = sha256_hex(data)
        blob = self.path(h)
        if blob.exists():
            meta = self.meta(h)
            return EvidenceRecord(
                h, len(data), str(meta.get("first_uri", uri)), str(meta.get("fetched_at", "")),
                False,
            )
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(blob, data)
        meta = {"sha256": h, "bytes": len(data), "first_uri": uri, "fetched_at": fetched_at}
        meta_text = json.dumps(meta, sort_keys=True, indent=2) + "\n"
        _atomic_write(self.meta_path(h), meta_text.encode())
        return EvidenceRecord(h, len(data), uri, fetched_at, True)

    def get(self, digest: str) -> bytes:
        return self.path(digest).read_bytes()

    def exists(self, digest: str) -> bool:
        return is_digest(digest) and self.path(digest).is_file()

    def meta(self, digest: str) -> dict[str, Any]:
        p = self.meta_path(digest)
        if not p.exists():
            return {}
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}

    def verify(self, digest: str) -> bool:
        """True iff the blob exists and its bytes hash to its name."""
        return self.exists(digest) and sha256_hex(self.get(digest)) == digest

    def digests(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if is_digest(p.name))

    def verify_all(self) -> dict[str, bool]:
        return {d: self.verify(d) for d in self.digests()}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
