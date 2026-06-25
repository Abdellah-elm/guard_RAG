# ingestion/embed_and_index.py
import json
import os
from pathlib import Path

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

CHUNKS_PATH = Path("data/chunks_v2.jsonl")
COLLECTION = "qiskit_docs_v2"
MODEL_NAME = "BAAI/bge-small-en-v1.5"


def load_chunks(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def ensure_collection(client: QdrantClient, name: str, vector_size: int) -> None:
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        )


def index_chunks(client: QdrantClient, model: SentenceTransformer, chunks: list[dict]) -> None:
    texts = [c["text"] for c in chunks]
    vectors = model.encode(texts, batch_size=32, show_progress_bar=True)

    points = [
        models.PointStruct(
            id=i,  # Qdrant veut un int ou un UUID — pas de string libre, donc enumerate()
            vector=vectors[i].tolist(),
            payload={**chunks[i]["metadata"], "text": chunks[i]["text"], "chunk_id": chunks[i]["id"]},
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=COLLECTION, points=points)


def search(client: QdrantClient, model: SentenceTransformer, query: str, top_k: int = 5):
    query_vector = model.encode(query).tolist()
    result = client.query_points(collection_name=COLLECTION, query=query_vector, limit=top_k)
    return result.points  # liste de ScoredPoint : .id, .score, .payload


def main() -> None:
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    model = SentenceTransformer(MODEL_NAME)

    chunks = load_chunks(CHUNKS_PATH)
    ensure_collection(client, COLLECTION, model.get_sentence_embedding_dimension())
    index_chunks(client, model, chunks)
    print(f"Indexed {len(chunks)} chunks into '{COLLECTION}'")

    # test rapide
    for point in search(client, model, "How does dynamical decoupling work?"):
        print(f"{point.score:.3f}  {point.payload['doc_title']} — {point.payload['section']}")


if __name__ == "__main__":
    main()