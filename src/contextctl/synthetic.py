"""Synthetic data generators (no real or proprietary data anywhere).

* :func:`build_dbt_history` writes a small, fully synthetic dbt project for a
  fictional coffee chain ("Synthetic Beans") as a Git repository with a
  scripted history of 25 model-touching first-parent steps, including side
  branches merged back (as real projects do). It is shaped like the public
  dbt Jaffle Shop project the paper replays, and it scripts the worked
  example of the paper's Appendix B on a ``food_revenue`` measure:
  ``= 1`` (2023-08-30) -> ``is true`` (authored 2024-07-04 on a branch, merged
  2024-09-12) -> bare boolean (merged 2025-07-01).
* :func:`synthetic_concepts` generates N single-template concepts for the
  microbenchmarks / scale check.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .gitio import Git, Identity
from .okf import Concept

AUTHOR = Identity("Synthetic Analyst", "analyst@synthetic-beans.invalid")

Doc = dict[str, Any]
Project = dict[str, Doc]


def _sm(name: str, model: str, entities: list[Doc], dims: list[Doc], measures: list[Doc],
        desc: str) -> Doc:
    return {
        "name": name, "description": desc, "model": f"ref('{model}')",
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": entities, "dimensions": dims, "measures": measures,
    }


def _metric(name: str, label: str, measure: str, desc: str, mtype: str = "simple") -> Doc:
    return {"name": name, "label": label, "description": desc, "type": mtype,
            "type_params": {"measure": measure}}


REV_DESC = "Revenue for each order line, the sum of product prices on the line."


def initial_project() -> Project:
    items = _sm(
        "order_items", "order_items",
        [{"name": "order_item", "type": "primary", "expr": "order_item_id"},
         {"name": "order_id", "type": "foreign", "expr": "order_id"}],
        [{"name": "ordered_at", "type": "time"}, {"name": "is_food_item", "type": "categorical"},
         {"name": "is_drink_item", "type": "categorical"}],
        [{"name": "revenue", "agg": "sum", "expr": "product_price", "description": REV_DESC},
         {"name": "food_revenue", "agg": "sum",
          "expr": "case when is_food_item = 1 then product_price else 0 end",
          "description": "Food share of line revenue; zero for drink lines."},
         {"name": "drink_revenue", "agg": "sum",
          "expr": "case when is_drink_item = 1 then product_price else 0 end",
          "description": "Drink share of line revenue; zero for food lines."}],
        "Order line fact table at the grain of one row per item sold.",
    )
    orders = _sm(
        "orders", "orders",
        [{"name": "order_id", "type": "primary"},
         {"name": "customer", "type": "foreign", "expr": "customer_id"}],
        [{"name": "ordered_at", "type": "time"}, {"name": "order_total_dim", "type": "categorical",
                                                    "expr": "order_total"}],
        [{"name": "order_total", "agg": "sum", "description": "Order total including tax."},
         {"name": "order_count", "agg": "sum", "expr": "1",
          "description": "Count of orders placed."}],
        "Order fact table with one row per order placed at a synthetic store.",
    )
    customers = _sm(
        "customers", "customers",
        [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        [{"name": "first_ordered_at", "type": "time"}],
        [{"name": "lifetime_spend", "agg": "sum", "description": "Total spend per customer."}],
        "Customer dimension with one row per synthetic customer.",
    )
    return {
        "models/marts/order_items.yml": {
            "semantic_models": [items],
            "metrics": [
                _metric("revenue", "Revenue", "revenue", "Sum of line revenue."),
                _metric("food_revenue", "Food Revenue", "food_revenue", "Revenue from food."),
                _metric("drink_revenue", "Drink Revenue", "drink_revenue", "Revenue from drinks."),
            ],
        },
        "models/marts/orders.yml": {
            "semantic_models": [orders],
            "metrics": [_metric("order_total", "Order Total", "order_total", "Sum of totals."),
                        _metric("order_count", "Orders", "order_count", "Number of orders.")],
        },
        "models/marts/customers.yml": {"semantic_models": [customers], "metrics": []},
    }


def _find(doc: Doc, kind: str, name: str) -> Doc:
    for it in doc.get(kind, []):
        if it["name"] == name:
            return it  # type: ignore[no-any-return]
    raise KeyError(name)


def _measure(p: Project, file: str, sm: str, name: str) -> Doc:
    for me in _find(p[file], "semantic_models", sm)["measures"]:
        if me["name"] == name:
            return me  # type: ignore[no-any-return]
    raise KeyError(name)


OI = "models/marts/order_items.yml"
ORD = "models/marts/orders.yml"
CUS = "models/marts/customers.yml"
LOC, SUP = "models/marts/locations.yml", "models/marts/supplies.yml"


def _write(root: Path, p: Project) -> None:
    for rel, doc in p.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump(doc, sort_keys=False, width=100))


# Each step mutates the project. Steps marked branch=True are authored on a
# side branch at ``authored`` and merged into main at ``date``.
Mut = Callable[[Project], None]


def _nothing(p: Project) -> None:
    return None


def _steps() -> list[tuple[str, str, Mut, str | None]]:
    def noop_comment(p: Project) -> None:  # whitespace-only change: re-harvest, all duplicates
        p.setdefault("_touch", {})

    def s_add_supplies_cost(p: Project) -> None:
        _find(p[ORD], "semantic_models", "orders")["measures"].append(
            {"name": "order_cost", "agg": "sum", "description": "Supply cost to fulfil the order."})
        p[ORD]["metrics"].append(_metric("order_cost", "Order Cost", "order_cost",
                                         "Sum of supply costs."))

    def s_customers_desc(p: Project) -> None:
        _find(p[CUS], "semantic_models", "customers")["description"] = (
            "Customer dimension with one row per synthetic customer, including the first "
            "order date and lifetime spend used for cohort analysis.")

    def s_locations(p: Project) -> None:
        p[LOC] = {"semantic_models": [_sm(
            "locations", "locations", [{"name": "location", "type": "primary",
                                        "expr": "location_id"}],
            [{"name": "opened_at", "type": "time"}],
            [{"name": "average_tax_rate", "agg": "average", "expr": "tax_rate",
              "description": "Average sales tax rate across synthetic stores."}],
            "Store dimension, one row per synthetic store location.")],
            "metrics": [_metric("average_tax_rate", "Average Tax Rate", "average_tax_rate",
                                "Mean tax rate across stores.")]}

    def s_orders_dims(p: Project) -> None:
        _find(p[ORD], "semantic_models", "orders")["dimensions"].append(
            {"name": "is_food_order", "type": "categorical"})

    def s_food_is_true(p: Project) -> None:
        _measure(p, OI, "order_items", "food_revenue")["expr"] = (
            "case when is_food_item is true then product_price else 0 end")

    def s_drink_is_true(p: Project) -> None:
        _measure(p, OI, "order_items", "drink_revenue")["expr"] = (
            "case when is_drink_item is true then product_price else 0 end")

    def s_ratio_metrics(p: Project) -> None:
        p[OI]["metrics"] += [
            {"name": "food_revenue_pct", "label": "Food Revenue %", "type": "ratio",
             "description": "Share of order revenue from food.",
             "type_params": {"numerator": "food_revenue", "denominator": "revenue"}},
            {"name": "drink_revenue_pct", "label": "Drink Revenue %", "type": "ratio",
             "description": "Share of order revenue from drinks.",
             "type_params": {"numerator": "drink_revenue", "denominator": "revenue"}},
        ]

    def s_supplies(p: Project) -> None:
        p[SUP] = {"semantic_models": [_sm(
            "supplies", "supplies", [{"name": "supply", "type": "primary", "expr": "supply_id"}],
            [{"name": "is_perishable", "type": "categorical"}],
            [{"name": "supply_cost", "agg": "sum", "expr": "supply_cost",
              "description": "Cost of supplies consumed."}],
            "Supply dimension, one row per synthetic supply item and product.")],
            "metrics": [_metric("supply_cost", "Supply Cost", "supply_cost",
                                "Total supply spend.")]}

    def s_revenue_desc(p: Project) -> None:
        p[OI]["metrics"][0]["description"] = (
            "Sum of line revenue across all synthetic stores, excluding tax.")

    def s_order_items_desc(p: Project) -> None:
        _find(p[OI], "semantic_models", "order_items")["description"] = (
            "Order line fact table at the grain of one row per item sold, joined to product "
            "attributes such as food and drink flags.")

    def s_customers_entities(p: Project) -> None:
        _find(p[CUS], "semantic_models", "customers")["dimensions"].append(
            {"name": "customer_type", "type": "categorical"})

    def s_rename_tax(p: Project) -> None:  # sibling/rename: held for review
        loc = p[LOC]["metrics"][0]
        loc["name"] = "mean_tax_rate"

    def s_food_bare(p: Project) -> None:
        _measure(p, OI, "order_items", "food_revenue")["expr"] = (
            "case when is_food_item then product_price else 0 end")

    def s_drink_bare(p: Project) -> None:
        _measure(p, OI, "order_items", "drink_revenue")["expr"] = (
            "case when is_drink_item then product_price else 0 end")

    def s_lifetime_desc(p: Project) -> None:
        _measure(p, CUS, "customers", "lifetime_spend")["description"] = (
            "Total pre-tax spend per customer over all synthetic orders to date.")

    def s_growth(p: Project) -> None:
        p[OI]["metrics"].append({
            "name": "revenue_growth_mom", "label": "Revenue Growth % M/M", "type": "derived",
            "description": "Percentage growth of revenue compared to one month earlier.",
            "type_params": {"expr": "(current_revenue - revenue_prev_month) * 100 / "
                                    "revenue_prev_month"}})

    def s_order_total_dims(p: Project) -> None:
        dims = _find(p[ORD], "semantic_models", "orders")["dimensions"]
        dims.append({"name": "is_drink_order", "type": "categorical"})

    def s_cost_agg(p: Project) -> None:
        _measure(p, SUP, "supplies", "supply_cost")["expr"] = "supply_cost * quantity"

    def s_label(p: Project) -> None:
        p[ORD]["metrics"][1]["label"] = "Order Count"

    def s_last(p: Project) -> None:
        s_cost_agg(p)
        s_label(p)

    return [
        ("2023-08-30T16:00:00Z", "Initial synthetic semantic layer", _nothing, None),
        ("2023-09-14T17:20:00Z", "Reformat YAML", noop_comment, None),
        ("2023-10-02T15:05:00Z", "Add order cost", s_add_supplies_cost, None),
        ("2023-11-20T18:40:00Z", "Expand customer docs", s_customers_desc, None),
        ("2024-01-08T16:10:00Z", "Add locations", s_locations, None),
        ("2024-02-15T19:00:00Z", "Reformat YAML", noop_comment, None),
        ("2024-03-30T15:30:00Z", "Add is_food_order dimension", s_orders_dims, None),
        ("2024-05-02T16:45:00Z", "Reformat YAML", noop_comment, None),
        ("2024-09-12T20:00:00Z", "Merge branch 'fix-bool-filters'", s_food_is_true,
         "2024-07-04T17:00:00Z"),
        ("2024-09-30T16:00:00Z", "Drink filter uses is true", s_drink_is_true, None),
        ("2024-10-21T18:00:00Z", "Add ratio metrics", s_ratio_metrics, None),
        ("2024-11-05T17:00:00Z", "Reformat YAML", noop_comment, None),
        ("2024-12-02T16:00:00Z", "Merge branch 'supplies'", s_supplies, "2024-11-18T16:00:00Z"),
        ("2025-01-14T15:00:00Z", "Clarify revenue metric", s_revenue_desc, None),
        ("2025-02-03T15:00:00Z", "Reformat YAML", noop_comment, None),
        ("2025-02-25T17:30:00Z", "Clarify order_items docs", s_order_items_desc, None),
        ("2025-03-18T18:00:00Z", "Add customer_type dimension", s_customers_entities, None),
        ("2025-04-09T16:00:00Z", "Rename tax metric", s_rename_tax, None),
        ("2025-05-01T16:00:00Z", "Reformat YAML", noop_comment, None),
        ("2025-07-01T19:00:00Z", "Merge branch 'bare-booleans'", s_food_bare,
         "2025-06-20T16:00:00Z"),
        ("2025-07-15T16:00:00Z", "Drink filter bare boolean", s_drink_bare, None),
        ("2025-08-12T16:00:00Z", "Lifetime spend docs", s_lifetime_desc, None),
        ("2025-09-09T16:00:00Z", "Add revenue growth metric", s_growth, None),
        ("2025-10-07T16:00:00Z", "Merge branch 'orders-dims'", s_order_total_dims,
         "2025-09-30T16:00:00Z"),
        ("2025-11-04T16:00:00Z", "Supply cost uses quantity; order count label",
         s_last, None),
    ]


def build_dbt_history(root: Path | str) -> Path:
    """Create the synthetic dbt project repository at ``root``. Deterministic."""
    rootp = Path(root)
    rootp.mkdir(parents=True, exist_ok=True)
    git = Git(rootp)
    git.run("init", "-q", "-b", "main")
    (rootp / "README.md").write_text(
        "# Synthetic Beans semantic layer\n\nFully synthetic dbt-style project used to "
        "exercise the contextctl replay harness. No real data.\n")
    project = initial_project()
    reformat = 0
    for date, msg, mut, authored in _steps():
        mut(project)
        out = {k: v for k, v in project.items() if not k.startswith("_")}
        if "_touch" in project:
            project.pop("_touch")
            reformat += 1
            # whitespace-only change that keeps semantics: a trailing comment
            _write(rootp, out)
            f = rootp / OI
            f.write_text(f.read_text() + f"# reformatted ({reformat})\n")
        else:
            _write(rootp, out)
        if authored:
            git.run("checkout", "-q", "-b", "side")
            git.run("add", "-A")
            git.run("commit", "-q", "-m", msg.replace("Merge branch", "Work on"),
                    when=authored, who=AUTHOR)
            git.run("checkout", "-q", "main")
            git.run("merge", "-q", "--no-ff", "-m", msg, "side", when=date, who=AUTHOR)
            git.run("branch", "-q", "-D", "side")
        else:
            git.run("add", "-A")
            git.run("commit", "-q", "-m", msg, when=date, who=AUTHOR)
    return rootp


def synthetic_concepts(n: int, seed: int = 0) -> list[Concept]:
    """N single-template concepts (as in the paper's scale check)."""
    out = []
    for i in range(n):
        k = (i * 2654435761 + seed) % 1000003
        out.append(Concept(
            f"concepts/synthetic/c{i:06d}.md",
            {"id": f"synthetic/c{i:06d}", "title": f"Synthetic concept {i}",
             "actor": "generator", "confidence": 0.9, "owner": f"team-{k % 97}",
             "tier": f"t{k % 5}", "valid_from": "2024-01-01"},
            f"Synthetic concept {i} links to [[concepts/synthetic/c{(i + 1) % n:06d}.md]].\n"
            f"Token {k} describes a fictional dataset with {k % 13} columns.\n",
        ))
    return out


def deep_copy(p: Project) -> Project:
    return copy.deepcopy(p)
