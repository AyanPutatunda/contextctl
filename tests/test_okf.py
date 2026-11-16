import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contextctl.okf import (
    Concept,
    OKFError,
    normalize_time,
    validate_header,
)


def test_parse_roundtrip():
    text = "---\nid: a\ntitle: A\nowner: x\nvalid_from: 2024-01-02\n---\nHello [[concepts/b.md]]\n"
    c = Concept.parse("concepts/a.md", text)
    assert c.header["valid_from"] == "2024-01-02"
    assert Concept.parse(c.path, c.dumps()) == c
    assert c.links() == ["concepts/b.md"]
    assert c.title == "A" and c.concept_id == "a"


def test_parse_errors():
    with pytest.raises(OKFError):
        Concept.parse("x.md", "no header")
    with pytest.raises(OKFError):
        Concept.parse("x.md", "---\n- a\n---\n")
    with pytest.raises(OKFError):
        Concept.parse("x.md", "---\na: [\n---\n")
    assert Concept.parse("x.md", "---\n\n---\nbody").header == {}


def test_facts_exclude_provenance_and_identity():
    c = Concept("concepts/a.md", {"id": "a", "title": "t", "actor": "x", "evidence": [],
                                  "confidence": 1, "owner": "o", "valid_from": "2024-01-01"})
    assert c.facts() == {"owner": "o"}
    assert c.facts(["owner", "actor"]) == {"owner": "o"}
    tf = c.tracked_fields()
    assert "valid_from" not in tf and "body_digest" in tf and "actor" in tf


def test_sources_and_digests_shapes():
    h = "a" * 64
    c = Concept("concepts/a.md", {"sources": {"uri": "u", "evidence": h}})
    assert c.sources() == [{"uri": "u", "evidence": h}]
    assert c.evidence_digests() == [h]
    assert Concept("concepts/a.md", {"source": "u"}).sources() == [{"uri": "u"}]
    assert Concept("concepts/a.md", {"evidence": h}).evidence_digests() == [h]
    assert Concept("concepts/a.md", {"sources": ["u"]}).sources() == [{"uri": "u"}]


def test_normalize_time():
    assert normalize_time("2024-01-02") == "2024-01-02T00:00:00Z"
    assert normalize_time("2024-01-02T03:04:05+02:00") == "2024-01-02T01:04:05Z"
    assert normalize_time(dt.date(2024, 1, 2)) == "2024-01-02T00:00:00Z"
    assert normalize_time(dt.datetime(2024, 1, 2, 3)) == "2024-01-02T03:00:00Z"
    with pytest.raises(OKFError):
        normalize_time("yesterday")
    with pytest.raises(OKFError):
        normalize_time(5)


def test_validate_header():
    ok = Concept("concepts/a.md", {"confidence": 0.5, "valid_from": "2024-01-01"})
    assert validate_header(ok) == []
    bad = Concept("other/../a.txt", {"confidence": 2, "valid_from": "nope",
                                     "evidence": ["xyz"]})
    probs = validate_header(bad)
    assert len(probs) == 5
    rev = Concept("concepts/a.md", {"valid_from": "2024-02-01", "valid_to": "2024-01-01"})
    assert validate_header(rev) == ["valid_to precedes valid_from"]


safe_text = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=40)


@given(st.dictionaries(st.sampled_from(["owner", "tier", "expr", "label"]),
                       safe_text.filter(lambda x: x.strip() == x and x != "")),
       st.text(alphabet=st.characters(blacklist_categories=("Cs",),
                                      blacklist_characters="\r\x85\u2028\u2029"),
               max_size=200))
def test_roundtrip_property(header, body):
    c = Concept("concepts/x.md", {"id": "x", **header}, body)
    back = Concept.parse(c.path, c.dumps())
    assert back.header == c.header
    assert back.body == (body if body.endswith("\n") or not body else body + "\n")
