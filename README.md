---
title: GuardRAG
emoji: 🛡️
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
---
# GuardRAG

**A production-style RAG assistant over the official Qiskit / IBM Quantum documentation — instrumented end-to-end with guardrails, multi-model routing, semantic caching, and observability, not just an answer-generation demo.**

🔗 **Live demo:** https://layvay-guard-rag.hf.space/
📊 **Traces & cost dashboard:** built on [Langfuse](https://langfuse.com)
💻 **Stack:** FastAPI · Qdrant · Groq · Langfuse · Presidio · Upstash

---

## Why this exists

Most "RAG demo" projects stop at retrieval + generation. The hard part of running an LLM application in production is everything around that: catching hallucinations before they reach a user, redacting PII before it hits a third-party API, controlling cost, knowing when something breaks, and proving any of it actually works. GuardRAG is built to show that layer explicitly — every answer ships with a visible readout of how it was produced, not just the answer itself.

The corpus is the official [Qiskit/documentation](https://github.com/Qiskit/documentation) (CC BY-SA 4.0). It's also a domain I can personally verify — when the system says it's 78% faithful, I can check that against my own quantum computing research background instead of trusting a metric blindly.

## Architecture

```
User question
   │
   ▼
PII detection (Presidio, pattern-based entities) — original question kept for retrieval,
   │                                                  redacted version sent to the 3rd-party LLM
   ▼
Embed locally (bge-small) ──► semantic cache lookup (Qdrant + Upstash)
   │                              │
   │ miss                         │ hit → return cached response (~200ms)
   ▼
Retrieval (Qdrant Cloud, top-k, cosine) ──► score < 0.6 → refuse, no LLM call made
   │
   ▼ score ≥ 0.6
Router: score ≥ 0.65 → gpt-oss-20b (fast)  |  else → gpt-oss-120b (strong)
   │
   ▼
Generation (Groq) ──► API error → fallback to the other tier
   │
   ▼
Faithfulness judge (LLM-as-judge) ──► flagged unfaithful on fast tier → escalate + retry
   │
   ▼
Response + Langfuse trace (tokens, cost, latency, scores) + cache write (confident answers only)
```

## Evaluation

Measured against a 24-question golden set (12 in-scope, 5 out-of-scope, 3 ambiguous, 3 with embedded PII, 1 prompt-injection probe) run through `eval/run_eval.py` against the live deployment. Full methodology, raw results, and the question set are in [`eval/`](./eval).

| Metric | Result |
|---|---|
| Out-of-scope questions correctly refused | **100%** (5/5) |
| In-scope questions correctly answered (not falsely refused) | **100%** (12/12) |
| Faithfulness rate (answered, non-refused questions) | **78%** |
| PII false-positive rate on clean questions | **0%** |
| PII true-positive rate on questions containing real PII | **67%** (2/3 — see limitations) |
| Latency, full round trip (p50 / p90 / p95) | **~1.7s / ~3.0s / ~3.8s** |
| Fast-tier (20B) vs strong-tier (120B) routing split | **~65% / ~35%** |

The refusal threshold (0.6) was calibrated empirically from this eval: off-topic questions ("best pizza topping") were measured retrieving spurious matches around 0.50–0.54 cosine similarity, while genuine in-scope matches consistently scored 0.65+. The first eval run, before this calibration, caught a real bug — the threshold was too permissive and let 2/5 out-of-scope questions through to generation.

## Guardrails

- **Faithfulness** — an LLM-as-judge call compares each answer against its retrieved context. Honest refusals ("I don't have that information") are explicitly scored as faithful, not penalized, since declining to answer isn't a false claim.
- **PII redaction** — restricted to pattern-based entities (email, phone, credit card, SSN, IBAN, IP address) rather than NER-based categories (PERSON, LOCATION). An earlier version using NER false-positived on domain vocabulary — "Qiskit" was misclassified as a person's name by the small NLP model, redacting it out of questions and breaking retrieval quality. Switching to structural-pattern entities only eliminated that failure class.
- **Refusal** — gated on retrieval confidence, calibrated against measured spurious-match scores (see Evaluation above).

## Cost & resilience

- **Semantic cache** (Qdrant for similarity matching, Upstash Redis for TTL'd storage) — repeat and near-duplicate questions skip retrieval and generation entirely, returning in ~200ms instead of ~1-2s.
- Cache writes are skipped for degraded responses (API fallback failures, unverifiable faithfulness) to avoid permanently caching a bad answer.
- Every external call (generation, judge, cache) is wrapped to fail gracefully — a Redis or Groq hiccup degrades a single response rather than crashing the request.
- Model routing includes automatic fallback to the other tier on API error, and automatic escalation from fast to strong tier when the faithfulness judge flags a fast-tier answer.

## Known limitations

- **PII / SSN recognition**: Presidio's SSN recognizer is context-sensitive and missed a synthetic SSN in testing (1/3 false negative in the PII eval set). Documented rather than papered over.
- **Faithfulness judge resilience**: the judge call always runs on the fast tier. Under sustained load against Groq's free-tier rate limits, judge calls can fail, returning an "unknown" rather than a real verdict — a constraint of the free-tier infrastructure choice, not the evaluation logic itself.
- **Latency under sustained load**: single-user interactive latency is consistently ~1-2s, but the free Groq tier throttles hard under rapid sequential load (the eval harness itself had to add deliberate pacing to get clean measurements). A production deployment would need a paid tier or request queuing.
- **No reranking stage**: retrieval is single-pass cosine similarity; a cross-encoder rerank step would likely improve precision on ambiguous queries but wasn't built for this version.
- **No CI/CD pipeline or A/B testing**: deliberately deprioritized — CI/CD adds limited signal for a single-developer project at this stage, and A/B testing isn't statistically meaningful without real production traffic.

## Stack

FastAPI · Qdrant Cloud · Groq (`openai/gpt-oss-20b` / `openai/gpt-oss-120b`) · Langfuse · Presidio · Upstash Redis · sentence-transformers (`bge-small-en-v1.5`) · Hugging Face Spaces (Docker)

## Run it locally

```bash
git clone https://github.com/Abdellah-elm/guard_RAG.git
cd guard_RAG
docker compose up -d          # Qdrant + Redis for local dev
pip install -r requirements.txt
python -m spacy download en_core_web_sm
cp .env.example .env          # fill in your API keys
python ingestion/parse_qiskit_docs.py --docs-dir data/qiskit-docs/docs/guides --out data/chunks.jsonl
python ingestion/embed_and_index.py
uvicorn app.main:app --reload
```

Full setup details, including the Qdrant Cloud / Upstash migration path used for the live deployment, are in [`eval/`](./eval) and inline comments in `app/main.py`.

## What I'd build next with more time/budget

- A cross-encoder reranking stage before generation
- A paid LLM tier to remove the rate-limit ceiling on the evaluation harness itself
- Kubernetes deployment (the original target, scaled back to Hugging Face Spaces for a zero-cost path)
- A larger, stratified eval set with statistical confidence intervals rather than a single 24-question pass

---

Built by [Abdellah](https://github.com/Abdellah-elm) as a reference implementation of LLMOps practices — guardrails, observability, and cost control treated as first-class features, not afterthoughts.