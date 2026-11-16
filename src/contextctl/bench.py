"""Microbenchmarks (paper, Appendix C): snapshot commit, as-of read, revert and
sidecar rebuild on synthetic single-template bundles. Medians of ``repeats``.
Absolute numbers depend on the machine; compare shapes, not values."""

from __future__ import annotations

import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bundle import Bundle
from .gitio import Identity
from .serving import Server
from .sidecar import Sidecar
from .synthetic import synthetic_concepts

WHO = Identity("bench", "bench@contextctl.invalid")


def _median_ms(fn: Callable[[], Any], repeats: int) -> float:
    xs = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(xs), 2)


def run(sizes: list[int], commits: int = 5, touch: float = 0.01, repeats: int = 5
        ) -> list[dict[str, Any]]:
    out = []
    for n in sizes:
        with tempfile.TemporaryDirectory() as td:
            out.append(_one(Path(td) / "b", n, commits, touch, repeats))
    return out


def _one(root: Path, n: int, commits: int, touch: float, repeats: int) -> dict[str, Any]:
    b = Bundle.init(root, who=WHO, install_hook=False)
    concepts = synthetic_concepts(n)
    for c in concepts:
        p = root / c.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c.dumps())
    b.git.run("add", "-A")
    b.git.run("commit", "-q", "-m", "load", who=WHO)
    k = max(1, int(n * touch))
    commit_ms = []
    for j in range(commits):
        for c in concepts[j * k:(j + 1) * k] or concepts[:k]:
            c.header["tier"] = f"rev{j}"
            (root / c.path).write_text(c.dumps())
        t = time.perf_counter()
        b.git.run("add", "-A", "concepts")
        b.git.run("commit", "-q", "-m", f"snapshot {j}", who=WHO)
        commit_ms.append((time.perf_counter() - t) * 1000)
    sc = Sidecar(b)
    rebuild_ms = _median_ms(sc.rebuild, max(1, repeats // 2))
    srv = Server(b, sc)
    first = b.git.first_parent_log()[1]
    asof_ms = _median_ms(lambda: srv.read(concepts[0].path, as_of=first), repeats)
    valid_ms = _median_ms(
        lambda: sc.value_at(concepts[0].path, "tier", "2030-01-01"), repeats)
    t = time.perf_counter()
    b.git.run("revert", "--no-edit", "HEAD", who=WHO)
    revert_ms = (time.perf_counter() - t) * 1000
    sc.close()
    return {
        "concepts": n,
        "snapshot_commit_ms": round(statistics.median(commit_ms), 2),
        "asof_read_ms": asof_ms,
        "valid_time_query_ms": valid_ms,
        "revert_ms": round(revert_ms, 2),
        "sidecar_rebuild_ms": rebuild_ms,
    }
