"""Reconciliation rules, Table I, applied in order."""

from hypothesis import given, settings
from hypothesis import strategies as st

from contextctl.okf import Concept
from contextctl.reconcile import Decision, Reconciler, conflicting_fields, new_facts

BODY = "The nightly payments pipeline is owned by alex and runs at two in the morning."


def c(path, body=BODY, **h):
    return Concept(path, {"title": path.split("/")[-1], **h}, body)


def rec(trusted, **kw):
    return Reconciler({x.path: x for x in trusted}, **kw)


def test_conflict_same_path_only():
    c0 = c("concepts/p.md", owner="alex", actor="human")
    r = rec([c0]).reconcile(c("concepts/p.md", owner="blair", actor="agent"))
    assert r.decision is Decision.CONFLICT
    assert [(x.field, x.current, x.proposed) for x in r.conflicts] == [("owner", "alex", "blair")]
    # same differing value on a *different* path never raises Conflict (finding (a))
    r2 = rec([c0]).reconcile(c("concepts/q.md", owner="blair"))
    assert r2.decision is not Decision.CONFLICT


def test_provenance_only_update_is_duplicate():
    c0 = c("concepts/p.md", owner="alex", evidence=["a" * 64], confidence=0.5, actor="x")
    d = c("concepts/p.md", owner="alex", evidence=["b" * 64], confidence=0.9, actor="y",
          valid_from="2024-01-01", supersedes=[{"snapshot": "abc"}])
    r = rec([c0]).reconcile(d)
    assert r.decision is Decision.DUPLICATE and not r.held and r.best_sim > 0.999


def test_duplicate_with_new_fact_is_revision():
    c0 = c("concepts/p.md", owner="alex")
    r = rec([c0]).reconcile(c("concepts/p.md", owner="alex", tier="gold"))
    assert r.decision is Decision.REVISION and r.new_facts == ["tier"]


def test_revision_band_and_new():
    c0 = c("concepts/p.md", body=BODY)
    half = BODY + " It also exports ledgers to finance hourly."
    r = rec([c0]).reconcile(c("concepts/p.md", body=half))
    assert r.decision is Decision.REVISION and r.target_path == "concepts/p.md"
    r2 = rec([c0]).reconcile(c("concepts/z.md", body="Completely unrelated cafeteria menu."))
    assert r2.decision is Decision.NEW and r2.target_path == "concepts/z.md"
    assert rec([]).reconcile(c("concepts/z.md")).decision is Decision.NEW


def test_held_when_best_match_is_different_concept():
    c0 = c("concepts/old-name.md", owner="alex")
    r = rec([c0]).reconcile(c("concepts/new-name.md", owner="alex"))
    assert r.held and r.decision in (Decision.DUPLICATE, Decision.REVISION)
    assert r.best_match == "concepts/old-name.md"


def test_body_only_facts_affect_only_sim():
    c0 = c("concepts/p.md", body=BODY)
    d = c("concepts/p.md", body=BODY.replace("alex", "blair"))
    r = rec([c0]).reconcile(d)
    assert r.decision is not Decision.CONFLICT


def test_search_callable_and_fact_fields():
    c0 = c("concepts/p.md", owner="alex", tier="t1")
    other = c("concepts/o.md", body="other text entirely", owner="z")
    calls = []

    def search(q, k):
        calls.append(k)
        return ["concepts/o.md", "concepts/missing.md"]

    r = rec([c0, other], search=search, top_k=3, fact_fields=["owner"]).reconcile(
        c("concepts/p.md", owner="alex", tier="t2"))
    # tier is not a fact field here -> no conflict; c0 is added as same-path candidate
    assert calls == [3] and r.decision is Decision.DUPLICATE
    assert {p for p, _ in r.candidates} == {"concepts/o.md", "concepts/p.md"}
    assert r.as_dict()["decision"] == "Duplicate"


def test_field_helpers():
    a, b = c("concepts/a.md", x="1", y="2"), c("concepts/a.md", x="1", y="3", z="4")
    assert [f.field for f in conflicting_fields(b, a)] == ["y"]
    assert new_facts(b, a) == ["y", "z"]


values = st.sampled_from(["a", "b", "c"])


@settings(max_examples=60, deadline=None)
@given(values, values, st.sampled_from([BODY, BODY + " extra words here", "unrelated text"]))
def test_rule_order_property(v0, v1, body):
    """Conflict always wins when a same-path fact differs; otherwise the decision
    is a pure function of (max sim, new facts) per Table I."""
    c0 = c("concepts/p.md", owner=v0)
    r = rec([c0]).reconcile(c("concepts/p.md", body=body, owner=v1))
    if v0 != v1:
        assert r.decision is Decision.CONFLICT
    elif r.best_sim >= 0.9:
        assert r.decision is Decision.DUPLICATE
    elif r.best_sim >= 0.5:
        assert r.decision is Decision.REVISION
    else:
        assert r.decision is Decision.NEW
