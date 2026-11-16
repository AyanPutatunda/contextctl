from hypothesis import given
from hypothesis import strategies as st

from contextctl.similarity import TfIdf, cosine, tokenize


def test_basic():
    m = TfIdf(["the cat sat", "the dog ran"])
    assert abs(m.cosine("the cat sat", "the cat sat") - 1.0) < 1e-9
    assert m.cosine("cat", "dog") == 0.0
    assert m.vector("") == {}
    assert tokenize("Food_Revenue = 1") == ["food", "revenue", "1"]
    assert cosine({}, {"a": 1.0}) == 0.0


words = st.lists(st.sampled_from(["alpha", "beta", "gamma", "delta", "eps", "zeta"]), min_size=1,
                 max_size=12).map(" ".join)


@given(words, words)
def test_cosine_symmetric_bounded(a, b):
    m = TfIdf([a, b, "alpha beta"])
    x, y = m.cosine(a, b), m.cosine(b, a)
    assert abs(x - y) < 1e-9 and 0.0 <= x <= 1.0
    assert abs(m.cosine(a, a) - 1.0) < 1e-9
