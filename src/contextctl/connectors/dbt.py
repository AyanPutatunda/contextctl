"""Scripted dbt-project harvester used by the replay harness (no model).

Every semantic model, every measure inside a semantic model, and every metric in
a dbt YAML file becomes one draft concept. The fact fields are the 14 header
fields below (the paper's replay used 14 dbt metric fields such as ``expr`` and ``dimensions``); the
entry's ``description`` is the body. The raw YAML file bytes are the evidence.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..evidence import sha256_hex
from ..okf import Concept
from . import Harvest

FACT_FIELDS: list[str] = [
    "model", "semantic_model", "agg_time_dimension", "entities", "dimensions", "measures",
    "type", "label", "measure", "expr", "agg", "filter", "numerator", "denominator",
]


def _names(items: Any) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            name = str(it.get("name", ""))
            extra = [f"{k}={it[k]}" for k in ("type", "expr", "agg") if k in it]
            out.append(" ".join([name, *extra]).strip())
        else:
            out.append(str(it))
    return out


def _semantic_model(sm: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if "model" in sm:
        facts["model"] = str(sm["model"])
    atd = (sm.get("defaults") or {}).get("agg_time_dimension")
    if atd:
        facts["agg_time_dimension"] = str(atd)
    for key in ("entities", "dimensions", "measures"):
        if sm.get(key):
            facts[key] = _names(sm[key])
    return facts


def _measure(me: dict[str, Any], sm: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {"semantic_model": str(sm.get("name", ""))}
    for key in ("agg", "expr"):
        if me.get(key) is not None:
            facts[key] = str(me[key]).strip()
    atd = me.get("agg_time_dimension") or (sm.get("defaults") or {}).get("agg_time_dimension")
    if atd:
        facts["agg_time_dimension"] = str(atd)
    return facts


def _metric(m: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for key in ("type", "label"):
        if key in m:
            facts[key] = str(m[key])
    tp = m.get("type_params") or {}
    meas = tp.get("measure")
    if isinstance(meas, dict):
        if "name" in meas:
            facts["measure"] = str(meas["name"])
        if meas.get("filter"):
            facts["filter"] = str(meas["filter"]).strip()
    elif meas:
        facts["measure"] = str(meas)
    for key in ("expr", "numerator", "denominator", "window"):
        if tp.get(key) is not None:
            v = tp[key]
            facts[key] = str(v["name"]) if isinstance(v, dict) and "name" in v else str(v)
    if m.get("filter"):
        facts["filter"] = str(m["filter"]).strip()
    return facts


def harvest_yaml(
    data: bytes, path: str, source: str, rev: str, fetched_at: str,
    actor: str = "connector:dbt",
) -> list[Harvest]:
    try:
        doc = yaml.safe_load(data.decode("utf-8", "replace"))
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    h = sha256_hex(data)
    uri = f"{source}#{path}@{rev[:7]}"
    items: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for sm in doc.get("semantic_models") or []:
        if isinstance(sm, dict) and sm.get("name"):
            items.append(("semantic_models", sm, _semantic_model(sm)))
            for me in sm.get("measures") or []:
                if isinstance(me, dict) and me.get("name"):
                    items.append(("measures", me, _measure(me, sm)))
    for m in doc.get("metrics") or []:
        if isinstance(m, dict) and m.get("name"):
            items.append(("metrics", m, _metric(m)))
    out: list[Harvest] = []
    for kind, item, facts in items:
        name = str(item["name"])
        header: dict[str, Any] = {
            "id": f"dbt/{kind}/{name}",
            "title": f"{name} ({kind[:-1].replace('_', ' ')})",
            "actor": actor,
            "confidence": 1.0,
            "evidence": [h],
            "sources": [{"uri": uri, "fetched_at": fetched_at, "evidence": h}],
            **facts,
        }
        body = str(item.get("description") or "").strip() + "\n"
        out.append(Harvest(Concept(f"concepts/{kind}/{name}.md", header, body),
                           [(data, uri, fetched_at)]))
    return out


def harvest_tree(
    files: dict[str, bytes], source: str, rev: str, fetched_at: str
) -> list[Harvest]:
    out: list[Harvest] = []
    for path in sorted(files):
        if path.endswith((".yml", ".yaml")):
            out.extend(harvest_yaml(files[path], path, source, rev, fetched_at))
    return out
