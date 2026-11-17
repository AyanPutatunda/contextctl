"""Metric-level dbt harvester: the paper's replay scope (``--harvest metrics``).

The default :mod:`.dbt` harvester turns every semantic model, measure and metric
into its own concept. The paper's Jaffle Shop replay used a narrower scope: one
concept per *metric* (``concepts/metrics/<name>.md``), with the metric resolved
through its measure to the semantic model that defines it, so each concept
carries the 14 fact fields listed in :data:`FACT_FIELDS` (``expr`` and
``dimensions`` among them). The body is rendered deterministically from those
facts and is what TF-IDF similarity compares (bundle ``similarity_text: body``).

Evidence for one metric is the raw bytes of the YAML file declaring it, the YAML
file of its semantic model, the SQL of every upstream dbt model (transitive
``ref()`` closure) and, for derived and ratio metrics, the YAML declaring each
input metric.

:func:`enrich` adds a final step's observed values (``observed_value``), read
from a directory of MetricFlow query results (``mf_values.json`` plus one
``<metric>.csv`` / ``<metric>.sql`` per metric). No model is involved anywhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ..evidence import sha256_hex
from ..okf import Concept
from . import Harvest

FACT_FIELDS: list[str] = [
    "metric_type", "type_params", "filter", "measure", "agg", "expr", "semantic_model",
    "time_dimension", "entities", "dimensions", "input_metrics", "upstream_models",
    "upstream_sources", "observed_value",
]

ACTOR = "connector:dbt-metrics"
ENRICH_ACTOR = "connector:dbt-metricflow"

_REF = re.compile(r"""ref\(\s*['"]([A-Za-z0-9_]+)['"]\s*\)""")
_SRC = re.compile(r"""source\(\s*['"]([A-Za-z0-9_]+)['"]\s*,\s*['"]([A-Za-z0-9_]+)['"]\s*\)""")


def slug(name: object) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(name).strip()).strip("_").lower()


def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _items(doc: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    return [x for x in (doc.get(key) or []) if isinstance(x, dict) and "name" in x]


def build_index(files: dict[str, bytes]) -> dict[str, Any]:
    """Parse every YAML file under the prefix; index metrics, semantic models
    and model SQL files."""
    metrics: dict[str, dict[str, Any]] = {}
    sems: dict[str, dict[str, Any]] = {}
    for p in sorted(files):
        if not p.endswith((".yml", ".yaml")):
            continue
        try:
            doc = yaml.safe_load(files[p].decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        for m in _items(doc, "metrics"):
            metrics[slug(m["name"])] = dict(m, _file=p)
        for s in _items(doc, "semantic_models"):
            sems[str(s["name"])] = dict(s, _file=p)
    sql = {p.rsplit("/", 1)[-1][:-4]: p for p in sorted(files) if p.endswith(".sql")}
    return {"files": files, "metrics": metrics, "sems": sems, "sql": sql}


def _upstream(idx: dict[str, Any], model: str) -> tuple[list[str], list[str]]:
    seen: list[str] = []
    srcs: list[str] = []
    stack = [model]
    while stack:
        name = stack.pop()
        if name in seen or name not in idx["sql"]:
            continue
        seen.append(name)
        txt = idx["files"][idx["sql"][name]].decode("utf-8", "replace")
        stack.extend(_REF.findall(txt))
        for a, b in _SRC.findall(txt):
            if f"{a}.{b}" not in srcs:
                srcs.append(f"{a}.{b}")
    return sorted(seen), sorted(srcs)


def _sem_model_ref(sem: dict[str, Any]) -> str | None:
    m = _REF.search(str(sem.get("model", "")))
    return m.group(1) if m else None


def _measures(idx: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for sname, s in idx["sems"].items():
        for ms in s.get("measures") or []:
            if isinstance(ms, dict) and "name" in ms:
                out.setdefault(str(ms["name"]), []).append((sname, ms))
    return out


def input_metrics(m: dict[str, Any]) -> list[str]:
    tp = m.get("type_params") or {}

    def nm(x: Any) -> str:
        return slug(x["name"] if isinstance(x, dict) else x)

    out: list[str] = []
    if m.get("type") == "ratio":
        for k in ("numerator", "denominator"):
            if tp.get(k) is not None:
                out.append(nm(tp[k]))
    for x in tp.get("metrics") or []:
        out.append(nm(x))
    for x in tp.get("input_metrics") or []:
        out.append(nm(x))
    return list(dict.fromkeys(out))


def resolve(idx: dict[str, Any], name: str, depth: int = 0) -> dict[str, Any]:
    """Resolve a metric through its measure to a semantic model (following
    input metrics for derived and ratio metrics)."""
    m = idx["metrics"][name]
    tp = m.get("type_params") or {}
    meas = tp.get("measure")
    if isinstance(meas, dict):
        meas = meas.get("name")
    res: dict[str, Any] = {
        "measure": meas, "agg": None, "expr": None, "semantic_model": None, "model": None,
        "time_dimension": None, "entities": [], "dimensions": [], "sem_file": None,
    }
    if meas:
        cands = _measures(idx).get(str(meas), [])
        if cands:
            sname, ms = cands[0]
            s = idx["sems"][sname]
            res.update(agg=ms.get("agg"), expr=str(ms.get("expr", meas)).strip(),
                       semantic_model=sname, model=_sem_model_ref(s), sem_file=s["_file"])
            res["time_dimension"] = (ms.get("agg_time_dimension")
                                     or (s.get("defaults") or {}).get("agg_time_dimension"))
            res["entities"] = sorted(f"{e['name']}:{e.get('type')}"
                                     for e in (s.get("entities") or []))
            res["dimensions"] = sorted(f"{d['name']}:{d.get('type')}"
                                       for d in (s.get("dimensions") or []))
        return res
    models: list[str] = []
    sems: list[str] = []
    tds: list[str] = []
    ents: set[str] = set()
    dims: set[str] = set()
    for i in input_metrics(m):
        if i in idx["metrics"] and depth < 6:
            r = resolve(idx, i, depth + 1)
            if r["model"]:
                models.append(r["model"])
            if r["semantic_model"]:
                sems.append(r["semantic_model"])
            if r["time_dimension"]:
                tds.append(r["time_dimension"])
            ents.update(r["entities"])
            dims.update(r["dimensions"])
    res["model"] = ",".join(sorted(set(models))) or None
    res["semantic_model"] = ",".join(sorted(set(sems))) or None
    res["time_dimension"] = ",".join(sorted(set(tds))) or None
    res["entities"], res["dimensions"] = sorted(ents), sorted(dims)
    return res


def _ref_name(v: Any) -> Any:
    return v.get("name") if isinstance(v, dict) else v


def render_body(fm: dict[str, Any], m: dict[str, Any]) -> str:
    lines = [f"# {fm['title']} (`{m['name']}`)", ""]
    if fm["description"]:
        lines += [fm["description"], ""]
    tp = m.get("type_params") or {}
    d = f"Metric type: {fm['metric_type']}."
    if fm["metric_type"] in ("simple", "cumulative") and fm["measure"]:
        d += (f" It aggregates measure `{fm['measure']}` as {fm['agg']}({fm['expr']}) "
              f"in semantic model `{fm['semantic_model']}`.")
    elif fm["metric_type"] == "ratio":
        d += (f" It is the ratio of metric `{_ref_name(tp.get('numerator'))}` "
              f"to metric `{_ref_name(tp.get('denominator'))}`.")
    elif fm["metric_type"] == "derived":
        d += (f" It is derived as expression `{tp.get('expr')}` over metrics "
              + ", ".join(f"`{i}`" for i in fm["input_metrics"]) + ".")
    if fm["filter"]:
        d += f" Filter: {fm['filter']}."
    for k in ("window", "grain_to_date"):
        if tp.get(k):
            d += f" {k}: {tp[k]}."
    lines += [d, ""]
    if fm["time_dimension"]:
        lines += [f"Time dimension: `{fm['time_dimension']}`.", ""]
    for key, label in (("entities", "Entities"), ("dimensions", "Dimensions"),
                       ("upstream_models", "Upstream dbt models"),
                       ("upstream_sources", "Upstream sources")):
        if fm[key]:
            lines += [f"{label}: " + ", ".join(f"`{e}`" for e in fm[key]) + ".", ""]
    if fm["input_metrics"]:
        lines += ["Related metrics: "
                  + ", ".join(f"[{i}]({i}.md)" for i in fm["input_metrics"]) + ".", ""]
    return "\n".join(lines).rstrip() + "\n"


def concept_for(
    idx: dict[str, Any], name: str, source: str, rev: str, fetched_at: str, actor: str = ACTOR,
) -> Harvest:
    m = idx["metrics"][name]
    r = resolve(idx, name)
    ins = input_metrics(m)
    up_models: list[str] = []
    up_sources: list[str] = []
    for mod in (r["model"] or "").split(","):
        if mod:
            a, b = _upstream(idx, mod)
            up_models += a
            up_sources += b
    up_models, up_sources = sorted(set(up_models)), sorted(set(up_sources))
    ev_paths = {m["_file"]}
    if r["sem_file"]:
        ev_paths.add(r["sem_file"])
    ev_paths.update(idx["sql"][u] for u in up_models)
    ev_paths.update(idx["metrics"][i]["_file"] for i in ins if i in idx["metrics"])
    evidence: list[tuple[bytes, str, str]] = []
    sources: list[dict[str, Any]] = []
    for p in sorted(ev_paths):
        data = idx["files"][p]
        uri = f"{source}#{p}@{rev[:7]}"
        evidence.append((data, uri, fetched_at))
        sources.append({"uri": uri, "fetched_at": fetched_at, "evidence": sha256_hex(data)})
    tp = m.get("type_params") or {}
    filt = m.get("filter")
    fm: dict[str, Any] = {
        "id": f"metric/{name}",
        "type": "dbt-metric",
        "title": str(m.get("label") or m["name"]),
        "description": " ".join(str(m.get("description") or "").split()),
        "metric_type": m.get("type"),
        "type_params": _canon(tp),
        "filter": " ".join(str(filt).split()) if filt else None,
        "measure": r["measure"], "agg": r["agg"], "expr": r["expr"],
        "semantic_model": r["semantic_model"], "time_dimension": r["time_dimension"],
        "entities": r["entities"], "dimensions": r["dimensions"],
        "input_metrics": ins, "upstream_models": up_models, "upstream_sources": up_sources,
    }
    body = render_body(fm, m)
    header = {k: v for k, v in fm.items() if v is not None}  # unasserted facts are omitted
    header.update({
        "actor": actor, "confidence": 1.0,
        "evidence": [s["evidence"] for s in sources], "sources": sources,
        "valid_from": fetched_at[:10],
    })
    return Harvest(Concept(f"concepts/metrics/{name}.md", header, body), evidence)


def harvest_tree(files: dict[str, bytes], source: str, rev: str, fetched_at: str) -> list[Harvest]:
    """One draft per metric declared anywhere under the prefix (YAML and SQL
    files are both needed: SQL gives the upstream ``ref()`` closure)."""
    idx = build_index(files)
    return [concept_for(idx, n, source, rev, fetched_at) for n in sorted(idx["metrics"])]


def load_observed(values_dir: Path | str) -> dict[str, Any]:
    d = Path(values_dir)
    data: dict[str, Any] = json.loads((d / "mf_values.json").read_text())
    return data


def enrich(
    harvests: list[Harvest], values_dir: Path | str, source: str, fetched_at: str,
    tool: str = "MetricFlow",
) -> list[Harvest]:
    """Re-issue each metric draft with its observed overall value (fact field
    ``observed_value``) and the query result + query SQL as extra evidence."""
    d = Path(values_dir)
    values = load_observed(d).get("metrics", {})
    out: list[Harvest] = []
    for h in harvests:
        c = h.concept
        name = c.path.rsplit("/", 1)[-1][:-3]
        header = json.loads(json.dumps(c.header))
        body = c.body
        evidence = list(h.evidence)
        m = values.get(name)
        if m:
            for ext in ("csv", "sql"):
                f = d / f"{name}.{ext}"
                data = f.read_bytes() if f.exists() else b""
                uri = f"{source}#metricflow/{name}.{ext}"
                evidence.append((data, uri, fetched_at))
                header["sources"].append(
                    {"uri": uri, "fetched_at": fetched_at, "evidence": sha256_hex(data)})
                if sha256_hex(data) not in header["evidence"]:
                    header["evidence"].append(sha256_hex(data))
            if m.get("status") == "ok":
                header["observed_value"] = m["value"]
                body += (f"\nObserved overall value ({tool} `mf query --metrics {name}`, "
                         f"no grouping or filter): {m['value']}.\n")
            else:
                err = str(m.get("error_message", ""))[:90]
                body += (f"\nOverall value: not available; `mf query --metrics {name}` "
                         f"returned an error ({err}).\n")
        header["actor"] = ENRICH_ACTOR
        out.append(Harvest(Concept(c.path, header, body), evidence))
    return out
