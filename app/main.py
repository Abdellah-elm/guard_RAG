import json
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from upstash_redis import Redis as UpstashRedis
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── L8 Velox toggle ─────────────────────────────────────────────────────────
USE_VELOX = os.getenv("USE_VELOX", "false").lower() == "true"
if USE_VELOX:
    from openai import OpenAI as Groq
    groq = Groq(
        base_url=os.getenv("VELOX_BASE_URL", "http://localhost:8000/v1"),
        api_key=os.getenv("VELOX_API_KEY", "velox-local"),
    )
    FAST_MODEL   = os.getenv("VELOX_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
    STRONG_MODEL = os.getenv("VELOX_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
else:
    from groq import Groq
# ────────────────────────────────────────────────────────────────────────────

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from langfuse import get_client, observe

# ── Phase 2 : BM25 sparse encoder ───────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "ingestion"))
try:
    from bm25_encoder import BM25Encoder
    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
# ────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("guardrag")

COLLECTION = "qiskit_docs_v2"
CACHE_COLLECTION = "query_cache"
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
BM25_ENCODER_PATH = Path("data/bm25_encoder.pkl")

if not USE_VELOX:
    FAST_MODEL   = "openai/gpt-oss-20b"
    STRONG_MODEL = "openai/gpt-oss-120b"

COMPLEXITY_THRESHOLD     = 0.65
REFUSAL_THRESHOLD        = 0.6
CACHE_SIMILARITY_THRESHOLD = 0.95
CACHE_TTL_SECONDS        = 86400
REFUSAL_MESSAGE = "I don't have enough information in the documentation to answer that confidently."
DOMAIN_ALLOW_LIST = ["Qiskit", "IBM", "MCP", "Python", "Runtime"]

app = FastAPI()

qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL", "http://localhost:6333"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

embedder = SentenceTransformer(EMBED_MODEL_NAME)

# ── Charger le BM25 encoder ─────────────────────────────────────────────────
if _BM25_AVAILABLE and BM25_ENCODER_PATH.exists():
    bm25_encoder = BM25Encoder.load(BM25_ENCODER_PATH)
    USE_HYBRID = True
    logger.info(f"BM25 encoder loaded: vocab_size={bm25_encoder.vocab_size}")
else:
    bm25_encoder = None
    USE_HYBRID = False
    logger.warning("BM25 encoder not found — using dense-only search (run embed_and_index_v2.py)")

# ── Phase 3 : Cross-encoder reranker ────────────────────────────────────────
RERANKER_MODEL   = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_TOP_N     = 5      # final top-k after reranking
RETRIEVE_TOP_N   = 10     # candidates fetched per query variant
USE_RERANKER_ENV = os.getenv("USE_RERANKER", "true").lower() == "true"

try:
    from sentence_transformers import CrossEncoder
    if USE_RERANKER_ENV:
        reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
        USE_RERANKER = True
        logger.info(f"Cross-encoder reranker loaded: {RERANKER_MODEL}")
    else:
        reranker = None
        USE_RERANKER = False
        logger.info("Reranker disabled via USE_RERANKER=false")
except Exception as e:
    reranker = None
    USE_RERANKER = False
    logger.warning(f"Cross-encoder not available ({e}) — skipping reranking")
# ────────────────────────────────────────────────────────────────────────────

# ── Phase 4 : Query Expansion ────────────────────────────────────────────────
USE_QUERY_EXPANSION = os.getenv("USE_QUERY_EXPANSION", "true").lower() == "true"
EXPAND_MAX_VARIANTS = 2    # number of additional phrasings to generate
# ────────────────────────────────────────────────────────────────────────────


if not USE_VELOX:
    groq = Groq(api_key=os.getenv("GROQ_API_KEY"))

nlp_engine = NlpEngineProvider(nlp_configuration={
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}).create_engine()
pii_analyzer  = AnalyzerEngine(nlp_engine=nlp_engine)
from presidio_analyzer import PatternRecognizer, Pattern as PPattern
_ssn_recognizer = PatternRecognizer(
    supported_entity="US_SSN",
    patterns=[PPattern(name="SSN_REGEX", regex=r"\b\d{3}-\d{2}-\d{4}\b", score=0.85)]
)
pii_analyzer.registry.add_recognizer(_ssn_recognizer)
pii_anonymizer = AnonymizerEngine()

langfuse = get_client()
redis_client = UpstashRedis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)

if not qdrant.collection_exists(CACHE_COLLECTION):
    qdrant.create_collection(
        collection_name=CACHE_COLLECTION,
        vectors_config=models.VectorParams(
            size=embedder.get_embedding_dimension(),
            distance=models.Distance.COSINE,
        ),
    )


class QueryRequest(BaseModel):
    question: str
    top_k: int = 5


class FeedbackRequest(BaseModel):
    trace_id: str
    rating: int          # 1 = thumbs up, -1 = thumbs down
    comment: str | None = None


SYSTEM_PROMPT = (
    "You are a technical support assistant for Qiskit / IBM Quantum. "
    "Answer ONLY using the provided context. If the context doesn't contain "
    "the answer, say you don't have that information — never invent details. "
    "If the context establishes a general procedure or topic but does not give "
    "an exact value (a specific file path, config key name, parameter name, or "
    "version number), say so explicitly rather than inventing a plausible-looking "
    "value — name what's missing instead of filling the gap."
)

PII_ENTITIES = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
    "US_SSN", "IBAN_CODE", "IP_ADDRESS",
]


def redact_pii(text: str) -> tuple[str, list[str]]:
    findings = pii_analyzer.analyze(text=text, language="en", entities=PII_ENTITIES)
    entity_types = sorted({f.entity_type for f in findings})
    anonymized = pii_anonymizer.anonymize(text=text, analyzer_results=findings)
    return anonymized.text, entity_types


def get_cached_response(query_vector: list[float]) -> dict | None:
    try:
        hits = qdrant.query_points(
            collection_name=CACHE_COLLECTION,
            query=query_vector,
            limit=1,
            score_threshold=CACHE_SIMILARITY_THRESHOLD,
        ).points
        if not hits:
            return None
        cached = redis_client.get(str(hits[0].id))
        return json.loads(cached) if cached else None
    except Exception as e:
        logger.warning(f"cache read failed: {e}")
        return None


def store_cached_response(query_vector: list[float], response_payload: dict) -> None:
    try:
        cache_key = str(uuid.uuid4())
        redis_client.set(cache_key, json.dumps(response_payload), ex=CACHE_TTL_SECONDS)
        qdrant.upsert(
            collection_name=CACHE_COLLECTION,
            points=[models.PointStruct(id=cache_key, vector=query_vector, payload={})],
        )
    except Exception as e:
        logger.warning(f"cache write failed: {e}")


def hybrid_retrieve(
    query_vector: list[float],
    safe_question: str,
    top_k: int,
) -> tuple[list, float]:
    """
    Hybrid retrieval: dense + sparse BM25 with RRF fusion.
    Always fetches RETRIEVE_TOP_N candidates (20) for the reranker.
    Returns (results, top_dense_score).

    top_dense_score : cosine similarity of best dense match
                      → used for refusal gate (calibrated at 0.6)
                      RRF scores (0.01–0.05) cannot be used for this.
    """
    # Dense results — always needed for refusal gate score
    dense_results = qdrant.query_points(
        collection_name=COLLECTION,
        query=query_vector,
        using="dense",
        limit=RETRIEVE_TOP_N,
    ).points
    top_dense_score = dense_results[0].score if dense_results else 0.0

    if USE_HYBRID and bm25_encoder is not None:
        sparse_indices, sparse_values = bm25_encoder.encode_query(safe_question)
        if sparse_indices:
            hybrid_results = qdrant.query_points(
                collection_name=COLLECTION,
                prefetch=[
                    models.Prefetch(
                        query=query_vector,
                        using="dense",
                        limit=RETRIEVE_TOP_N,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_indices,
                            values=sparse_values,
                        ),
                        using="sparse",
                        limit=RETRIEVE_TOP_N,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=RETRIEVE_TOP_N,
            ).points
            return hybrid_results, top_dense_score

    return dense_results[:RETRIEVE_TOP_N], top_dense_score


def expand_query(question: str) -> list[str]:
    """
    Generate EXPAND_MAX_VARIANTS alternative phrasings of the question.
    Returns [original_question, variant_1, variant_2, ...].
    Falls back to [original_question] on any error — query expansion is
    a best-effort optimisation, never a hard dependency.
    """
    if not USE_QUERY_EXPANSION:
        return [question]
    try:
        response = groq.chat.completions.create(
            model=FAST_MODEL,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate {EXPAND_MAX_VARIANTS} alternative phrasings of this question "
                    f"about Qiskit / IBM Quantum documentation.\n"
                    f"Return only the phrasings, one per line, no numbering, no explanation.\n"
                    f"Question: {question}"
                ),
            }],
            temperature=0.3,
            max_tokens=120,
        )
        variants = [
            v.strip()
            for v in response.choices[0].message.content.strip().split("\n")
            if v.strip() and v.strip() != question
        ]
        result = [question] + variants[:EXPAND_MAX_VARIANTS]
        logger.info(f"Query expansion: {len(result)} variants for '{question[:60]}'")
        return result
    except Exception as e:
        logger.warning(f"query expansion failed: {e} — using original query only")
        return [question]


def multi_query_retrieve(
    original_vector: list[float],
    safe_question: str,
    top_k: int,
) -> tuple[list, float]:
    """
    Phase 4 : Query Expansion + Hybrid Retrieval.

    1. Generate query variants via expand_query().
    2. Embed each variant and call hybrid_retrieve().
    3. Merge results: deduplicate by point id, keep union of all candidates.
    4. The refusal gate score comes from the ORIGINAL query's dense score only
       (calibrated at 0.6 — variant scores would be noisier).

    Returns (merged_candidates, top_dense_score_of_original_query).
    """
    # Original query dense score for the refusal gate (always from original)
    dense_results = qdrant.query_points(
        collection_name=COLLECTION,
        query=original_vector,
        using="dense",
        limit=RETRIEVE_TOP_N,
    ).points
    top_dense_score = dense_results[0].score if dense_results else 0.0

    # Query variants
    if USE_QUERY_EXPANSION:
        variants = expand_query(safe_question)
    else:
        variants = [safe_question]

    # Retrieve for each variant and merge (deduplicate by point id)
    seen_ids: set = set()
    merged: list = []

    for i, variant in enumerate(variants):
        if i == 0:
            # Original query — reuse already-computed dense results
            v_vector = original_vector
        else:
            v_vector = embedder.encode(variant).tolist()

        # Hybrid retrieve for this variant
        candidates, _ = hybrid_retrieve(v_vector, variant, top_k)

        for c in candidates:
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                merged.append(c)

    logger.info(
        f"multi_query_retrieve: {len(variants)} variants → "
        f"{len(merged)} unique candidates (top_dense={top_dense_score:.3f})"
    )
    return merged, top_dense_score


def rerank(question: str, candidates: list, top_k: int) -> list:
    """
    Cross-encoder reranking: scores each (question, chunk) pair jointly.
    Returns top_k results sorted by reranker score descending.
    Falls back to the original order if the reranker is unavailable.
    """
    if not USE_RERANKER or reranker is None or not candidates:
        return candidates[:top_k]

    try:
        pairs = [(question, r.payload.get("text", "")) for r in candidates]
        scores = reranker.predict(pairs, show_progress_bar=False)
        ranked = sorted(zip(scores, candidates), key=lambda x: float(x[0]), reverse=True)
        return [r for _, r in ranked[:top_k]]
    except Exception as e:
        logger.warning(f"reranking failed: {e} — using original order")
        return candidates[:top_k]


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
    judge_prompt = f"""You are a fact-checker evaluating whether an ANSWER is faithful to its CONTEXT.

CONTEXT:
{context}

ANSWER:
{answer}

Flag a claim as unsupported ONLY if it is factually contradicted by the context, or if it states a
specific technical detail (an exact file path, function name, config key, command, or similar) with
confident, authoritative phrasing that is not actually present in the context.

Do NOT flag: reasonable generalizations, common-sense elaboration, claims the answer itself hedges
("typically", "the exact details may vary"), or restating what a well-known term/acronym means. The
bar is "does this claim mislead the reader," not "does this exact phrase appear verbatim in the context."

If the ANSWER states that it doesn't have enough information to answer (a refusal or non-answer),
that is always faithful — respond with faithful=true and an empty unsupported_claims list, since
declining to answer is not a false claim. This rule has no exceptions.

Respond with JSON only, no other text, no markdown fences:
{{"faithful": true/false, "unsupported_claims": ["..."], "confidence": 0.0}}"""

    for judge_model in (FAST_MODEL, STRONG_MODEL):
        try:
            judge_response = groq.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": judge_prompt}],
                temperature=0,
            )
            raw = judge_response.choices[0].message.content.strip()
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"faithfulness check failed on {judge_model}: {e}")
            continue

    return {"faithful": None, "unsupported_claims": [], "confidence": 0.0, "raw": None}


def generate_with_routing(
    context: str, question: str, top_score: float
) -> tuple[str, dict, str, str]:
    if top_score >= COMPLEXITY_THRESHOLD:
        model, reason = FAST_MODEL, "routed_fast_high_confidence"
    else:
        model, reason = STRONG_MODEL, "routed_strong_low_confidence"

    try:
        answer = generate_answer(context, question, model)
    except Exception as e:
        logger.warning(f"generate_answer failed on {model}: {e}")
        model  = STRONG_MODEL if model == FAST_MODEL else FAST_MODEL
        reason = "fallback_on_api_error"
        try:
            answer = generate_answer(context, question, model)
        except Exception as e2:
            logger.error(f"generate_answer failed on fallback {model}: {e2}")
            return (
                "I'm temporarily unable to generate an answer — please try again in a few seconds.",
                {"faithful": None, "unsupported_claims": [], "confidence": 0.0},
                model,
                "fallback_failed",
            )

    faithfulness = check_faithfulness(context, answer)

    if faithfulness.get("faithful") in (False, None) and model == FAST_MODEL:
        try:
            model  = STRONG_MODEL
            reason = "faithfulness_escalation"
            escalated_answer      = generate_answer(context, question, model)
            escalated_faithfulness = check_faithfulness(context, escalated_answer)
            answer, faithfulness  = escalated_answer, escalated_faithfulness
        except Exception as e:
            logger.warning(f"escalation to {model} failed: {e}")

    return answer, faithfulness, model, reason


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

    # ── Cache lookup ────────────────────────────────────────────────
    cached = get_cached_response(query_vector)
    if cached:
        langfuse.score_current_trace(name="cache_hit", value=1.0, data_type="BOOLEAN")
        return {**cached, "pii_detected": pii_types, "cached": True, "trace_id": trace_id}

    # ── Hybrid retrieval + Query Expansion (Phase 2+4) ─────────────
    candidates, top_dense_score = multi_query_retrieve(query_vector, safe_question, req.top_k)

    if not candidates or top_dense_score < REFUSAL_THRESHOLD:
        langfuse.score_current_trace(name="refused", value=1.0, data_type="BOOLEAN")
        return {
            "answer": REFUSAL_MESSAGE,
            "faithfulness": {"faithful": None, "unsupported_claims": [], "confidence": 0.0},
            "sources": [], "pii_detected": pii_types, "refused": True,
            "cached": False, "trace_id": trace_id,
        }

    # ── Cross-encoder reranking (Phase 3) ───────────────────────────
    results = rerank(safe_question, candidates, top_k=RERANK_TOP_N)

    # ── Build context with parent_text (Phase 1) ────────────────────
    context = "\n\n".join(
        f"[{p.payload['doc_title']} — {p.payload['section']}]\n"
        f"{p.payload.get('parent_text', p.payload['text'])}"
        for p in results
    )

    answer, faithfulness, model_used, routing_reason = generate_with_routing(
        context, safe_question, top_dense_score
    )

    langfuse.score_current_trace(
        name="faithfulness",
        value=1.0 if faithfulness.get("faithful") else 0.0,
        data_type="BOOLEAN",
    )
    langfuse.score_current_trace(name="cache_hit", value=0.0, data_type="BOOLEAN")

    response_payload = {
        "answer": answer,
        "faithfulness": faithfulness,
        "model_used": model_used,
        "routing_reason": routing_reason,
        "sources": [
            {
                "doc_title": p.payload["doc_title"],
                "section":   p.payload["section"],
                "url":       p.payload["url"],
                "score":     p.score,
            }
            for p in results
        ],
        "refused": False,
    }

    should_cache = (
        routing_reason != "fallback_failed"
        and faithfulness.get("faithful") is not None
    )
    if should_cache:
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