#!/usr/bin/env python3
"""Build the static contextctl site into site/dist.

Config: site/site.config.json, overridable by environment variables
SITE_DOMAIN, REPO_URL, REPO_PUBLIC (0/1) and INDEXABLE (0/1).

While INDEXABLE is false (the default until go-live) every page carries
``<meta name="robots" content="noindex,nofollow">``, robots.txt disallows all
crawlers, and an ``X-Robots-Tag: noindex`` header is emitted via ``_headers``.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC, DIST = HERE / "src", HERE / "dist"
PDF = ROOT / "paper" / "contextctl_AAIBD2026.pdf"
RESULTS = ROOT / "examples" / "results" / "results.json"

BIBTEX = """@inproceedings{putatunda2026contextctl,
  title     = {contextctl: Auditable, Open-Format Context Infrastructure for AI Agents},
  author    = {Putatunda, Ayan and Jangoan, Suhas},
  booktitle = {2026 IEEE International Conference on Big Data (BigData),
               AAI-BD Workshop},
  year      = {2026},
  note      = {Demo paper},
  url       = {%(url)s}
}"""


def flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes"}


def config() -> dict:
    cfg = json.loads((HERE / "site.config.json").read_text())
    cfg["SITE_DOMAIN"] = os.environ.get("SITE_DOMAIN", cfg["SITE_DOMAIN"]).strip().lower()
    cfg["REPO_URL"] = os.environ.get("REPO_URL", cfg["REPO_URL"]).rstrip("/")
    cfg["REPO_PUBLIC"] = flag("REPO_PUBLIC", bool(cfg.get("REPO_PUBLIC")))
    cfg["INDEXABLE"] = flag("INDEXABLE", bool(cfg.get("INDEXABLE")))
    cfg["SITE_URL"] = f"https://{cfg['SITE_DOMAIN']}"
    return cfg


def results_rows() -> str:
    if not RESULTS.exists():
        return ('        <tr><td colspan="5">Run examples/reproduce_paper.py to fill this '
                "table.</td></tr>")
    r = json.loads(RESULTS.read_text())
    syn, pub = r["replay_synthetic"], r.get("replay_public_dbt")
    pap = r.get("replay_public_dbt_paper")

    def cell(d: dict | None, fn) -> str:
        return html.escape(str(fn(d))) if d else "not run"

    rows = [
        ("Source steps", "25", lambda d: d["steps"]),
        ("Drafts", "373", lambda d: d["drafts"]),
        ("New / Duplicate / Revision / Conflict", "16 / 322 / 24 / 11",
         lambda d: " / ".join(str(d["counts"][k]) for k in ("New", "Duplicate", "Revision",
                                                            "Conflict"))),
        ("Final concepts", "22", lambda d: d["final_concepts"]),
        ("As-of reads matching git show (field level)", "665/665",
         lambda d: f"{d['asof_field_matches']}/{d['asof_field_checks']}"),
        ("Rebuilt sidecar = incremental", "yes",
         lambda d: "yes" if d["rebuild_matches"] else "no"),
        ("Identical repeats", "7/7", lambda d: f"{d['repeats']}/{d['repeats']}"
         if d["repeat_digests_identical"] else "no"),
        ("Full bundle / raw tokens", "4.6×", lambda d: f"{d['tokens']['full_bundle_over_raw']}×"),
        ("Appendix B food_revenue rows", "3 rows: 2023-08-30 → 2024-09-12 → 2025-07-01",
         lambda d: _food(d)),
    ]
    out = []
    for label, paper, fn in rows:
        out.append(f"        <tr><th scope=\"row\">{html.escape(label)}</th><td>{paper}</td>"
                   f"<td>{cell(syn, fn)}</td><td>{cell(pub, fn)}</td>"
                   f"<td>{cell(pap, fn)}</td></tr>")
    return "\n".join(out)


def _food(d: dict) -> str:
    for h in d.get("example_history", []):
        if h["concept"].endswith("food_revenue.md") and h["field"] == "expr":
            dates = [x["valid_from"][:10] for x in h["rows"]]
            return f"{len(dates)} rows: " + " → ".join(dates)
    return "n/a"


def build() -> Path:
    cfg = config()
    if DIST.exists():
        shutil.rmtree(DIST)
    (DIST / "paper").mkdir(parents=True)
    robots_meta = ('<meta name="robots" content="index,follow">' if cfg["INDEXABLE"]
                   else '<meta name="robots" content="noindex,nofollow">')
    repo_note = "" if cfg["REPO_PUBLIC"] else " (access on request)"
    repo_access = ("The repository is public." if cfg["REPO_PUBLIC"] else
                   "The repository is private for now; access on request.")
    page = (SRC / "index.html").read_text()
    subs = {
        "{{ROBOTS_META}}": robots_meta,
        "{{SITE_URL}}": cfg["SITE_URL"],
        "{{REPO_URL}}": cfg["REPO_URL"],
        "{{REPO_NOTE}}": repo_note,
        "{{REPO_ACCESS}}": repo_access,
        "{{BIBTEX}}": html.escape(BIBTEX % {"url": cfg["SITE_URL"]}),
        "{{RESULTS_ROWS}}": results_rows(),
        "{{ASSET_VERSION}}": hashlib.sha256((SRC / "styles.css").read_bytes()).hexdigest()[:10],
    }
    for k, v in subs.items():
        page = page.replace(k, v)
    assert "{{" not in page, "unreplaced placeholder"
    (DIST / "index.html").write_text(page)
    for name in ("styles.css", "architecture.svg", "favicon.svg", "og.png"):
        if (SRC / name).exists():
            shutil.copyfile(SRC / name, DIST / name)
    shutil.copyfile(PDF, DIST / "paper" / PDF.name)
    (DIST / "404.html").write_text(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">{robots_meta}"
        "<title>Not found · contextctl</title><link rel=\"stylesheet\" href=\"/styles.css\">"
        "</head><body><main class=\"sec\"><div class=\"wrap\"><h1>Not found</h1>"
        "<p><a href=\"/\">Back to contextctl</a></p></div></main></body></html>\n")
    if cfg["INDEXABLE"]:
        robots = f"User-agent: *\nAllow: /\n\nSitemap: {cfg['SITE_URL']}/sitemap.xml\n"
    else:
        robots = "User-agent: *\nDisallow: /\n"
    (DIST / "robots.txt").write_text(robots)
    today = date.today().isoformat()
    (DIST / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{cfg['SITE_URL']}/</loc><lastmod>{today}</lastmod></url>\n"
        f"  <url><loc>{cfg['SITE_URL']}/paper/{PDF.name}</loc><lastmod>{today}</lastmod></url>\n"
        "</urlset>\n")
    headers = ["/*", "  X-Content-Type-Options: nosniff",
               "  Referrer-Policy: strict-origin-when-cross-origin", "  X-Frame-Options: DENY",
               "  Permissions-Policy: camera=(), microphone=(), geolocation=()"]
    if not cfg["INDEXABLE"]:
        headers.append("  X-Robots-Tag: noindex, nofollow")
    headers += ["/styles.css", "  Cache-Control: public, max-age=86400",
                "/paper/*", "  Content-Type: application/pdf"]
    (DIST / "_headers").write_text("\n".join(headers) + "\n")
    print(f"built {DIST} for {cfg['SITE_URL']} (indexable={cfg['INDEXABLE']}, "
          f"repo_public={cfg['REPO_PUBLIC']})")
    return DIST


if __name__ == "__main__":
    build()
