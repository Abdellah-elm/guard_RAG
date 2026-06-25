"""
GuardRAG v2 — hybrid indexing script.
Creates a Qdrant collection with both dense and sparse vectors,
then indexes all chunks from chunks_v2.jsonl.

Dense  : bge-small-en-v1.5 embeddings (384 dims) — semantic matching
Sparse : BM25 IDF weights — exact keyword matching

At query time, both vectors are used with RRF fusion for hybrid search.

Usage:
    python ingestion/embed_and_index_v2.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

# Add ingestion/ to path so we can import bm25_encoder
sys.path.insert(0, str(Path(__file__).parent))
from bm25_encoder import BM25Encoder

# ── Config ────────────────────────────────────────────────────────
CHUNKS_PATH = Path("data/chunks_v2.jsonl")
COLLECTION = "qiskit_docs_v2"
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
BM25_ENCODER_PATH = Path("data/bm25_encoder.pkl")
BATCH_SIZE = 32
# ─────────────────────────────────────────────────────────────────


def ensure_hybrid_collection(
    client: QdrantClient,
    collection_name: str,
    dense_size: int,
) -> None:
    if client.collection_exists(collection_name):
        client.delete_collection(collection_name)
        print(f"Deleted existing collection '{collection_name}'")

    client.create_collection(
        collection_name=collection_name,
        vectors_config={
            "dense": models.VectorParams(
                size=dense_size,
                distance=models.Distance.COSINE,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams()
        },
    )
    print(f"Created hybrid collection '{collection_name}' (dense={dense_size}, sparse=BM25)")


def main() -> None:
    # ── Load chunks ───────────────────────────────────────────────
    records = [json.loads(l) for l in CHUNKS_PATH.open(encoding="utf-8")]
    print(f"Loaded {len(records)} chunks from {CHUNKS_PATH}")

    # ── Fit BM25 on child texts (same texts that are embedded) ────
    print("Fitting BM25 encoder on corpus...")
    texts = [r["text"] for r in records]
    bm25 = BM25Encoder()
    bm25.fit(texts)
    bm25.save(BM25_ENCODER_PATH)
    print(f"BM25 encoder fitted: vocab_size={bm25.vocab_size} → saved to {BM25_ENCODER_PATH}")

    # ── Dense embeddings ──────────────────────────────────────────
    print(f"Loading embedding model: {EMBED_MODEL_NAME}")
    embedder = SentenceTransformer(EMBED_MODEL_NAME)
    dense_size = embedder.get_embedding_dimension()

    print("Encoding dense vectors...")
    dense_vectors = embedder.encode(
        texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,
    ).tolist()

    # ── Qdrant ────────────────────────────────────────────────────
    client = QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY"),
    )
    ensure_hybrid_collection(client, COLLECTION, dense_size)

    # ── Upsert in batches ─────────────────────────────────────────
    print(f"Indexing {len(records)} points into '{COLLECTION}'...")
    batch: list[models.PointStruct] = []

    for i, (record, dense_vec) in enumerate(zip(records, dense_vectors)):
        sparse_indices, sparse_values = bm25.encode_document(i)

        point = models.PointStruct(
            id=i,
            vector={
                "dense": dense_vec,
                "sparse": models.SparseVector(
                    indices=sparse_indices,
                    values=sparse_values,
                ),
            },
            payload={
                "text": record["text"],
                "parent_text": record.get("parent_text", record["text"]),
                "doc_title": record["metadata"]["doc_title"],
                "section": record["metadata"]["section"],
                "source_type": record["metadata"]["source_type"],
                "url": record["metadata"]["url"],
            },
        )
        batch.append(point)

        if len(batch) >= BATCH_SIZE or i == len(records) - 1:
            client.upsert(collection_name=COLLECTION, points=batch)
            batch = []

    print(f"Indexed {len(records)} chunks into '{COLLECTION}'")

    # ── Sanity check with hybrid query ────────────────────────────
    test_query = "dynamical decoupling qiskit runtime"
    test_dense = embedder.encode([test_query], normalize_embeddings=True).tolist()[0]
    sparse_idx, sparse_val = bm25.encode_query(test_query)

    results = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=test_dense, using="dense", limit=20),
            models.Prefetch(
                query=models.SparseVector(indices=sparse_idx, values=sparse_val),
                using="sparse",
                limit=20,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=5,
    )
    print(f"\nHybrid search sanity check — '{test_query}':")
    for pt in results.points:
        print(f"  {pt.payload['doc_title']} — {pt.payload['section']}")


if __name__ == "__main__":
    main()
