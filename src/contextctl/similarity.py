"""Dependency-free TF-IDF cosine similarity (paper, Sec. 2 "Reconciliation")."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class TfIdf:
    """Smoothed IDF (``ln((1+N)/(1+df)) + 1``) with L2-normalized vectors."""

    def __init__(self, corpus: Iterable[str]) -> None:
        docs = [set(tokenize(d)) for d in corpus]
        self.n = len(docs)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(d)
        self.df = df

    def idf(self, term: str) -> float:
        return math.log((1 + self.n) / (1 + self.df.get(term, 0))) + 1.0

    def vector(self, text: str) -> dict[str, float]:
        tf = Counter(tokenize(text))
        vec = {t: c * self.idf(t) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {t: v / norm for t, v in vec.items()} if norm else {}

    def cosine(self, a: str, b: str) -> float:
        return cosine(self.vector(a), self.vector(b))


def cosine(va: Mapping[str, float], vb: Mapping[str, float]) -> float:
    if len(va) > len(vb):
        va, vb = vb, va
    s = sum(v * vb.get(t, 0.0) for t, v in va.items())
    return max(0.0, min(1.0, s))
