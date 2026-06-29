"""Tiny BM25 scorer for small project-scoped corpora."""

from __future__ import annotations

import math
from collections import Counter


class BM25Index:
    """In-memory BM25 index optimized for <1000 short project documents."""

    def __init__(self, documents: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.documents = documents
        self.k1 = k1
        self.b = b
        self.doc_count = len(documents)
        self.doc_lengths = [len(doc) for doc in documents]
        self.avgdl = (sum(self.doc_lengths) / self.doc_count) if self.doc_count else 0.0
        self.term_freqs = [Counter(doc) for doc in documents]
        doc_freq: Counter[str] = Counter()
        for doc in documents:
            doc_freq.update(set(doc))
        self.idf = {
            term: math.log(1 + (self.doc_count - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    def score(self, query_tokens: list[str], index: int) -> float:
        if not query_tokens or index >= self.doc_count:
            return 0.0
        freqs = self.term_freqs[index]
        doc_len = self.doc_lengths[index] or 1
        denom_norm = self.k1 * (1 - self.b + self.b * doc_len / (self.avgdl or 1))
        score = 0.0
        for token in query_tokens:
            tf = freqs.get(token, 0)
            if tf <= 0:
                continue
            score += self.idf.get(token, 0.0) * (tf * (self.k1 + 1)) / (tf + denom_norm)
        return score

    def scores(self, query_tokens: list[str]) -> list[float]:
        return [self.score(query_tokens, idx) for idx in range(self.doc_count)]
