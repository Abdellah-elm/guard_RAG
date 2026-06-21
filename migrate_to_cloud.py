# migrate_to_cloud.py
import json
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

CHUNKS_PATH = Path("data/chunks.jsonl")
COLLECTION = "qiskit_docs"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
UPLOAD_BATCH_SIZE = 100

client = QdrantClient(
    url=os.getenv("QDRANT_CLOUD_URL"),
    api_key=os.getenv("QDRANT_CLOUD_API_KEY"),
    timeout=60,
)
model = SentenceTransformer(MODEL_NAME)

with CHUNKS_PATH.open(encoding="utf-8") as f:
    chunks = [json.loads(line) for line in f]

if not client.collection_exists(COLLECTION):
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=models.VectorParams(
            size=model.get_embedding_dimension(), distance=models.Distance.COSINE
        ),
    )

texts = [c["text"] for c in chunks]
vectors = model.encode(texts, batch_size=32, show_progress_bar=True)

points = [
    models.PointStruct(
        id=i, vector=vectors[i].tolist(),
        payload={**chunks[i]["metadata"], "text": chunks[i]["text"], "chunk_id": chunks[i]["id"]},
    )
    for i in range(len(chunks))
]

for i in range(0, len(points), UPLOAD_BATCH_SIZE):
    batch = points[i : i + UPLOAD_BATCH_SIZE]
    client.upsert(collection_name=COLLECTION, points=batch)
    print(f"Uploaded {min(i + UPLOAD_BATCH_SIZE, len(points))}/{len(points)}")

print(f"Migrated {len(chunks)} chunks to Qdrant Cloud collection '{COLLECTION}'")