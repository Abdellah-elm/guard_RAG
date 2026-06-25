"""
BM25 sparse encoder for GuardRAG hybrid search.

Fits on a corpus of texts, then encodes both documents and queries
into sparse vectors (indices + values) compatible with Qdrant's
SparseVector format.

The fitted encoder is saved to disk so the same vocabulary is used
at both indexing time (embed_and_index.py) and query time (app/main.py).

Usage:
    # Fit and save
    encoder = BM25Encoder()
    encoder.fit(texts)
    encoder.save("data/bm25_encoder.pkl")

    # Load and encode a query
    encoder = BM25Encoder.load("data/bm25_encoder.pkl")
    indices, values = encoder.encode_query("dynamical decoupling qiskit")
"""

from __future__ import annotations

import pickle
import re
from collections import Counter
from pathlib import Path

from rank_bm25 import BM25Okapi


def _tokenize(text: str) -> list[str]:
    """Lower-case, keep alphanumeric and underscores, split on whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return [t for t in text.split() if len(t) > 1]


class BM25Encoder:
    """
    Wraps BM25Okapi to produce Qdrant-compatible sparse vectors.

    Sparse vector format: two parallel lists `indices` and `values`
    where each (index, value) pair encodes one vocabulary term and its weight.
    """

    def __init__(self) -> None:
        self._bm25: BM25Okapi | None = None
        self._vocab: dict[str, int] = {}       # term → index in the sparse dimension
        self._tokenized_corpus: list[list[str]] = []

    def fit(self, texts: list[str]) -> "BM25Encoder":
        """Fit BM25 on a list of raw texts."""
        self._tokenized_corpus = [_tokenize(t) for t in texts]
        self._bm25 = BM25Okapi(self._tokenized_corpus)

        # Build a stable vocabulary: sorted so indices are deterministic
        all_terms = sorted(self._bm25.idf.keys())
        self._vocab = {term: idx for idx, term in enumerate(all_terms)}

        return self

    @property
    def vocab_size(self) -> int:
        return len(self._vocab)

    def encode_document(self, doc_idx: int) -> tuple[list[int], list[float]]:
        """
        Encode document doc_idx as a sparse vector using TF * IDF weights.
        doc_idx must be the same position as in the list passed to fit().
        """
        if self._bm25 is None:
            raise RuntimeError("Call fit() before encode_document()")
        tf = Counter(self._tokenized_corpus[doc_idx])
        indices, values = [], []
        for term, freq in tf.items():
            if term in self._vocab:
                idf = self._bm25.idf.get(term, 0.0)
                weight = freq * max(idf, 0.0)   # clamp negative IDF to 0
                if weight > 0:
                    indices.append(self._vocab[term])
                    values.append(float(weight))
        return indices, values

    def encode_query(self, query: str) -> tuple[list[int], list[float]]:
        """
        Encode a query as a sparse vector using IDF weights only
        (standard BM25 query encoding — no TF for queries).
        """
        if self._bm25 is None:
            raise RuntimeError("Call fit() before encode_query()")
        tokens = set(_tokenize(query))
        indices, values = [], []
        for token in tokens:
            if token in self._vocab:
                idf = self._bm25.idf.get(token, 0.0)
                weight = max(idf, 0.0)
                if weight > 0:
                    indices.append(self._vocab[token])
                    values.append(float(weight))
        return indices, values

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "bm25": self._bm25,
                    "vocab": self._vocab,
                    "tokenized_corpus": self._tokenized_corpus,
                },
                f,
            )

    @classmethod
    def load(cls, path: str | Path) -> "BM25Encoder":
        with open(path, "rb") as f:
            state = pickle.load(f)
        encoder = cls()
        encoder._bm25 = state["bm25"]
        encoder._vocab = state["vocab"]
        encoder._tokenized_corpus = state["tokenized_corpus"]
        return encoder
