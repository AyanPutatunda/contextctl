"""A contextctl bundle: OKF concept files + Git + evidence folder (trusted
state, layer 2) and a rebuildable sidecar (derived, layer 3).

Layout::

    <bundle>/
      contextctl.yaml           bundle config (committed)
      concepts/**.md            OKF-MEM concept files (trusted)
      evidence/<sha256>         content-addressed evidence blobs (trusted)
      evidence/<sha256>.meta.json
      .contextctl/              local, git-ignored
        sidecar.sqlite          derived metadata (delete and rebuild at will)
        staging/                untrusted drafts and their fetched evidence
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .evidence import EvidenceStore
from .gitio import Git, Identity
from .okf import Concept

CONFIG_FILE = "contextctl.yaml"
LOCAL_DIR = ".contextctl"


@dataclass
class BundleConfig:
    """Bundle configuration. Defaults follow the paper (Table I)."""

    theta_dup: float = 0.9
    theta_rev: float = 0.5
    top_k: int = 10
    fact_fields: list[str] | None = None  # None: every non-provenance header field
    main_branch: str = "main"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> BundleConfig:
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        known = {"theta_dup", "theta_rev", "top_k", "fact_fields", "main_branch"}
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        cfg.extra = {k: v for k, v in raw.items() if k not in known}
        cfg.check()
        return cfg

    def check(self) -> None:
        if not 0 <= self.theta_rev <= self.theta_dup <= 1:
            raise ValueError("require 0 <= theta_rev <= theta_dup <= 1")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")

    def dump(self) -> str:
        data: dict[str, Any] = {
            "theta_dup": self.theta_dup,
            "theta_rev": self.theta_rev,
            "top_k": self.top_k,
            "main_branch": self.main_branch,
        }
        if self.fact_fields is not None:
            data["fact_fields"] = list(self.fact_fields)
        data.update(self.extra)
        return yaml.safe_dump(data, sort_keys=True)


class Bundle:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.git = Git(self.root)
        self.config = BundleConfig.load(self.root / CONFIG_FILE)
        self.evidence = EvidenceStore(self.root / "evidence")
        self.local = self.root / LOCAL_DIR
        self.staging_dir = self.local / "staging"
        self.staging_evidence = EvidenceStore(self.staging_dir / "evidence")
        self.sidecar_path = self.local / "sidecar.sqlite"

    # ------------------------------------------------------------ setup
    @classmethod
    def init(
        cls,
        root: Path | str,
        config: BundleConfig | None = None,
        when: str | None = None,
        who: Identity | None = None,
        install_hook: bool = True,
    ) -> Bundle:
        rootp = Path(root).resolve()
        rootp.mkdir(parents=True, exist_ok=True)
        git = Git(rootp)
        if not git.is_repo():
            git.run("init", "-q", "-b", (config or BundleConfig()).main_branch)
        cfg = config or BundleConfig()
        (rootp / CONFIG_FILE).write_text(cfg.dump())
        (rootp / "concepts").mkdir(exist_ok=True)
        (rootp / "evidence").mkdir(exist_ok=True)
        (rootp / "concepts" / ".gitkeep").touch()
        (rootp / "evidence" / ".gitkeep").touch()
        (rootp / ".gitignore").write_text(f"{LOCAL_DIR}/\n")
        if not git.head():
            git.run("add", "-A")
            git.run("commit", "-q", "-m", "contextctl: init bundle", when=when, who=who)
        b = cls(rootp)
        if install_hook:
            b.install_post_merge_hook()
        return b

    def install_post_merge_hook(self) -> None:
        """Install a Git post-merge hook that keeps the sidecar in sync when
        humans merge with plain ``git merge``. ``sync`` is idempotent."""
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "post-merge"
        hook.write_text(
            "#!/bin/sh\n# installed by contextctl: keep the derived sidecar in sync\n"
            f'"{sys.executable}" -m contextctl -C "{self.root}" sidecar sync'
            " >/dev/null 2>&1 || true\n"
        )
        hook.chmod(0o755)

    # ---------------------------------------------------------- reading
    @property
    def main(self) -> str:
        return self.config.main_branch

    def head(self) -> str | None:
        return self.git.head(self.main)

    def concept_paths(self, rev: str | None = None) -> list[str]:
        rev = rev or self.main
        return [p for p in self.git.ls_tree(rev, "concepts") if p.endswith(".md")]

    def read(self, path: str, rev: str | None = None) -> Concept | None:
        text = self.git.show(rev or self.main, path)
        return None if text is None else Concept.parse(path, text)

    def concepts(self, rev: str | None = None) -> list[Concept]:
        texts = self.git.show_many(rev or self.main, self.concept_paths(rev))
        return [Concept.parse(p, t) for p, t in texts.items() if t is not None]
