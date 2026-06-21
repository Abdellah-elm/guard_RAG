import json
import os
import uuid
from dotenv import load_dotenv
load_dotenv()

import redis
from fastapi import FastAPI
from pydantic import BaseModel
from groq import Groq
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from langfuse import get_client, observe
from pathlib import Path
from fastapi.responses import HTMLResponse

COLLECTION = "qiskit_docs"
CACHE_COLLECTION = "query_cache"
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
FAST_MODEL = "openai/gpt-oss-20b"
STRONG_MODEL = "openai/gpt-oss-120b"
COMPLEXITY_THRESHOLD = 0.65
REFUSAL_THRESHOLD = 0.5
CACHE_SIMILARITY_THRESHOLD = 0.95
CACHE_TTL_SECONDS = 86400
REFUSAL_MESSAGE = "I don't have enough information in the documentation to answer that confidently."

app = FastAPI()
qdrant = QdrantClient(
    url=os.getenv("QDRANT_CLOUD_URL","http://localhost:6333"),
    api_key=os.getenv("QDRANT_CLOUD_API_KEY"),  # None en local, requis en prod
)
embedder = SentenceTransformer(EMBED_MODEL_NAME)
groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
nlp_engine = NlpEngineProvider(nlp_configuration={
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}).create_engine()
pii_analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
pii_anonymizer = AnonymizerEngine()
langfuse = get_client()
redis_client = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

if not qdrant.collection_exists(CACHE_COLLECTION):
    qdrant.create_collection(
        collection_name=CACHE_COLLECTION,
        vectors_config=models.VectorParams(
            size=embedder.get_embedding_dimension(), distance=models.Distance.COSINE
        ),
    )


class QueryRequest(BaseModel):
    question: str
    top_k: int = 5


class FeedbackRequest(BaseModel):
    trace_id: str
    rating: int  # 1 = thumbs up, -1 = thumbs down
    comment: str | None = None


SYSTEM_PROMPT = (
    "You are a technical support assistant for Qiskit / IBM Quantum. "
    "Answer ONLY using the provided context. If the context doesn't contain "
    "the answer, say you don't have that information — never invent details."
)


def redact_pii(text: str) -> tuple[str, list[str]]:
    findings = pii_analyzer.analyze(text=text, language="en")
    entity_types = sorted({f.entity_type for f in findings})
    anonymized = pii_anonymizer.anonymize(text=text, analyzer_results=findings)
    return anonymized.text, entity_types

def get_cached_response(query_vector: list[float]) -> dict | None:
    try:
        hits = qdrant.query_points(
            collection_name=CACHE_COLLECTION, query=query_vector, limit=1,
            score_threshold=CACHE_SIMILARITY_THRESHOLD,
        ).points
        if not hits:
            return None
        cached = redis_client.get(str(hits[0].id))
        return json.loads(cached) if cached else None
    except Exception:
        return None  # cache indisponible -> traité comme un miss, pas de crash


def store_cached_response(query_vector: list[float], response_payload: dict) -> None:
    try:
        cache_key = str(uuid.uuid4())
        redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(response_payload))
        qdrant.upsert(
            collection_name=CACHE_COLLECTION,
            points=[models.PointStruct(id=cache_key, vector=query_vector, payload={})],
        )
    except Exception:
        pass  # le cache est une optimisation, jamais un point de défaillance critique

@observe(as_type="generation", name="answer_generation")
def generate_answer(context: str, question: str, model: str) -> str:
    response = groq.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
        ],
    )
    langfuse.update_current_generation(
        model=model,
        usage_details={
            "input": response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
            "total": response.usage.total_tokens,
        },
    )
    return response.choices[0].message.content


@observe(as_type="evaluator", name="faithfulness_check")
def check_faithfulness(context: str, answer: str) -> dict:
    judge_prompt = f"""You are a strict fact-checker. Given the CONTEXT and an ANSWER, \
determine if every claim in the ANSWER is directly supported by the CONTEXT.

CONTEXT:
{context}

ANSWER:
{answer}

If the ANSWER states that it doesn't have enough information to answer (a refusal or \
non-answer), that is always faithful — respond with faithful=true and an empty \
unsupported_claims list, since declining to answer is not a false claim.

Respond with JSON only, no other text, no markdown fences:
{{"faithful": true/false, "unsupported_claims": ["..."], "confidence": 0.0}}"""
    try:
        judge_response = groq.chat.completions.create(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": judge_prompt}],
            temperature=0,
        )
        raw = judge_response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception:
        return {"faithful": None, "unsupported_claims": [], "confidence": 0.0, "raw": None}


def generate_with_routing(context: str, question: str, top_score: float) -> tuple[str, dict, str]:
    model = FAST_MODEL if top_score >= COMPLEXITY_THRESHOLD else STRONG_MODEL

    try:
        answer = generate_answer(context, question, model)
    except Exception:
        model = STRONG_MODEL if model == FAST_MODEL else FAST_MODEL
        try:
            answer = generate_answer(context, question, model)
        except Exception:
            return (
                "I'm temporarily unable to generate an answer — please try again in a few seconds.",
                {"faithful": None, "unsupported_claims": [], "confidence": 0.0},
                model,
            )

    faithfulness = check_faithfulness(context, answer)  # ne plante plus jamais, retourne un dict même en cas d'erreur

    if faithfulness.get("faithful") is False and model == FAST_MODEL:
        try:
            model = STRONG_MODEL
            escalated_answer = generate_answer(context, question, model)
            escalated_faithfulness = check_faithfulness(context, escalated_answer)
            answer, faithfulness = escalated_answer, escalated_faithfulness
        except Exception:
            pass  # garde la réponse du modèle rapide si l'escalade elle-même échoue

    return answer, faithfulness, model
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")
@app.post("/query")
@observe(name="rag_query")
def query(req: QueryRequest):
    trace_id = langfuse.get_current_trace_id()
    safe_question, pii_types = redact_pii(req.question)
    if pii_types:
        langfuse.score_current_trace(name="pii_detected", value=1.0, data_type="BOOLEAN")

    query_vector = embedder.encode(req.question).tolist()

    cached = get_cached_response(query_vector)
    if cached:
        langfuse.score_current_trace(name="cache_hit", value=1.0, data_type="BOOLEAN")
        return {**cached, "pii_detected": pii_types, "cached": True, "trace_id": trace_id}

    results = qdrant.query_points(
        collection_name=COLLECTION, query=query_vector, limit=req.top_k
    ).points

    if not results or results[0].score < REFUSAL_THRESHOLD:
        langfuse.score_current_trace(name="refused", value=1.0, data_type="BOOLEAN")
        return {
            "answer": REFUSAL_MESSAGE,
            "faithfulness": {"faithful": None, "unsupported_claims": [], "confidence": 0.0},
            "sources": [], "pii_detected": pii_types, "refused": True, "cached": False,
            "trace_id": trace_id,
        }

    context = "\n\n".join(
        f"[{p.payload['doc_title']} — {p.payload['section']}]\n{p.payload['text']}"
        for p in results
    )

    answer, faithfulness, model_used = generate_with_routing(context, safe_question, results[0].score)
    langfuse.score_current_trace(
        name="faithfulness", value=1.0 if faithfulness.get("faithful") else 0.0, data_type="BOOLEAN"
    )
    langfuse.score_current_trace(name="cache_hit", value=0.0, data_type="BOOLEAN")

    response_payload = {
        "answer": answer, "faithfulness": faithfulness, "model_used": model_used,
        "sources": [
            {"doc_title": p.payload["doc_title"], "section": p.payload["section"],
             "url": p.payload["url"], "score": p.score}
            for p in results
        ],
        "refused": False,
    }
    store_cached_response(query_vector, response_payload)
    return {**response_payload, "pii_detected": pii_types, "cached": False, "trace_id": trace_id}


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    langfuse.create_score(
        trace_id=req.trace_id,
        name="user_feedback",
        value=req.rating,
        data_type="NUMERIC",
        comment=req.comment,
    )
    return {"status": "recorded"}