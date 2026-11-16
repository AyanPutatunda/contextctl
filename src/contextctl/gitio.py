"""Thin, deterministic wrapper around the ``git`` CLI.

Git is the write gate and the transaction-time record. All commits made by
contextctl use explicit identities and (optionally) explicit timestamps, so a
replay with the same inputs produces byte-identical commit SHAs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({code}): {stderr.strip()}")
        self.code = code
        self.stderr = stderr


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


DEFAULT_IDENTITY = Identity("contextctl", "contextctl@localhost")


class Git:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _env(self, when: str | None, who: Identity | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        ident = who or DEFAULT_IDENTITY
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = ident.name
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = ident.email
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        return env

    def run(
        self,
        *args: str,
        check: bool = True,
        when: str | None = None,
        who: Identity | None = None,
        input_bytes: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        env = self._env(when, who)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-c", "init.defaultBranch=main", *args],
            cwd=self.root,
            env=env,
            input=input_bytes,
            capture_output=True,
        )
        out = proc.stdout.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return out

    def run_bytes(self, *args: str) -> bytes:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, env=self._env(None, None), capture_output=True
        )
        if proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    # ---------------------------------------------------------- queries
    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def head(self, ref: str = "HEAD") -> str | None:
        out = self.run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip()
        return out or None

    def short(self, sha: str) -> str:
        return sha[:7]

    def show(self, rev: str, path: str) -> str | None:
        out = subprocess.run(
            ["git", "show", f"{rev}:{path}"], cwd=self.root, env=self._env(None, None),
            capture_output=True,
        )
        return out.stdout.decode("utf-8") if out.returncode == 0 else None

    def show_many(self, rev: str, paths: Sequence[str]) -> dict[str, str | None]:
        """Read many files at ``rev`` with one ``git cat-file --batch`` process."""
        if not paths:
            return {}
        listing = self.run("ls-tree", "-r", "--full-tree", rev, "--", *sorted(
            {p.split("/")[0] for p in paths}), check=False)
        blobs: dict[str, str] = {}
        for line in listing.splitlines():
            meta, _, name = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                blobs[name] = parts[2]
        req = "".join(f"{blobs.get(p, 'missing-' + p)}\n" for p in paths).encode()
        proc = subprocess.run(
            ["git", "cat-file", "--batch"], cwd=self.root, env=self._env(None, None),
            input=req, capture_output=True,
        )
        if proc.returncode != 0:  # pragma: no cover - defensive
            raise GitError(["cat-file", "--batch"], proc.returncode, proc.stderr.decode())
        out: dict[str, str | None] = {}
        buf, pos = proc.stdout, 0
        for p in paths:
            nl = buf.index(b"\n", pos)
            header = buf[pos:nl].split()
            pos = nl + 1
            if len(header) == 2 and header[1] == b"missing":
                out[p] = None
                continue
            size = int(header[2])
            out[p] = buf[pos:pos + size].decode("utf-8")
            pos += size + 1
        return out

    def ls_tree(self, rev: str, prefix: str) -> list[str]:
        out = self.run("ls-tree", "-r", "--name-only", rev, "--", prefix, check=False)
        return sorted(line for line in out.splitlines() if line)

    def first_parent_log(self, ref: str = "main") -> list[str]:
        """Oldest-first list of first-parent commits on ``ref``."""
        if not self.head(ref):
            return []
        out = self.run("rev-list", "--first-parent", "--reverse", ref)
        return [x for x in out.splitlines() if x]

    def commit_time(self, sha: str) -> str:
        """Committer time as ``YYYY-MM-DDTHH:MM:SSZ``."""
        out = self.run(
            "show", "-s", "--format=%cd", "--date=format-local:%Y-%m-%dT%H:%M:%SZ", sha,
            extra_env={"TZ": "UTC"},
        )
        return out.strip()

    def parents(self, sha: str) -> list[str]:
        out = self.run("rev-list", "--parents", "-n", "1", sha).split()
        return out[1:]

    def changed_paths(self, old: str | None, new: str, prefix: str) -> list[str]:
        if old is None:
            return self.ls_tree(new, prefix)
        out = self.run("diff", "--name-only", "--no-renames", old, new, "--", prefix)
        return sorted(x for x in out.splitlines() if x)

    def last_commit_touching(self, path: str, ref: str = "main") -> str | None:
        out = self.run(
            "log", "--first-parent", "-1", "--format=%H", ref, "--", path, check=False
        ).strip()
        return out or None

    def resolve_as_of(self, at: str, ref: str = "main") -> str | None:
        """Resolve a commit-ish or an ISO time to the last first-parent commit
        on ``ref`` at or before that time (transaction time)."""
        sha = self.head(at) if not _looks_like_time(at) else None
        if sha:
            return sha
        out = self.run(
            "rev-list", "--first-parent", "-1", f"--before={at}", ref, check=False
        ).strip()
        return out or None


def _looks_like_time(s: str) -> bool:
    return len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit()
