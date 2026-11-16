"""Rule-based notes connector (no language model).

Each note file (``.md`` / ``.txt``) becomes one draft concept:

* the first ``# Heading`` (or the file stem) becomes ``title``;
* leading ``Key: value`` lines become fact header fields (keys snake_cased);
* ``[[links]]`` and the remaining text become the body;
* the raw file bytes are the evidence object, cited by SHA-256.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..evidence import sha256_hex
from ..okf import Concept
from . import Harvest

_KV = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]{0,40}):\s*(.+?)\s*$")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def harvest_note(
    data: bytes, uri: str, fetched_at: str, name: str, actor: str = "connector:notes",
    area: str = "notes",
) -> Harvest:
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lines = text.split("\n")
    title = Path(name).stem
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].startswith("# "):
        title = lines[i][2:].strip()
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    facts: dict[str, str] = {}
    while i < len(lines):
        m = _KV.match(lines[i])
        if not m:
            break
        facts[re.sub(r"[ -]+", "_", m.group(1).strip().lower())] = m.group(2)
        i += 1
    body = "\n".join(lines[i:]).strip() + "\n"
    h = sha256_hex(data)
    header: dict[str, object] = {
        "id": f"{area}/{slugify(title)}",
        "title": title,
        "actor": actor,
        "confidence": 0.5,
        "evidence": [h],
        "sources": [{"uri": uri, "fetched_at": fetched_at, "evidence": h}],
        **facts,
    }
    c = Concept(f"concepts/{area}/{slugify(title)}.md", header, body)
    return Harvest(c, [(data, uri, fetched_at)])


def harvest_dir(root: Path, fetched_at: str, actor: str = "connector:notes") -> list[Harvest]:
    out = []
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.suffix in {".md", ".txt"}:
            rel = p.relative_to(root).as_posix()
            out.append(harvest_note(p.read_bytes(), f"file:{rel}", fetched_at, p.name, actor))
    return out
