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

The corpus is the official [Qiskit/documentation](https://github.com/Qiskit/documentation) (CC BY-SA 4.0). It's also a domain I can personally verify — when the system reports a faithfulness rate, I can check that against my own quantum computing research background instead of trusting a metric blindly.

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

Measured against a 54-question golden set (12 in-scope, 20 out-of-scope, 3 ambiguous, 18 with embedded PII across 6 entity types, 1 prompt-injection probe) run through `eval/run_eval.py` against the live deployment, across multiple independent runs. Full methodology, raw results, and the question set are in [`eval/`](./eval).

| Metric | Result |
|---|---|
| Out-of-scope questions never resulting in a fabricated answer | **100%** (20/20) |
| Out-of-scope questions caught by the retrieval-confidence gate specifically | **85%** (17/20) — the remaining 3 were caught one layer later, see below |
| In-scope questions correctly answered (not falsely refused) | **100%** (12/12) |
| Faithfulness rate, genuinely-generated answers with a clear judge verdict | **84–89%**, consistent across two independent runs (16/19 and 16/18) |
| PII false-positive rate on clean questions | **0%** |
| PII true-positive rate, 5 of 6 entity types (email, phone, credit card, IBAN, IP) | **100%** (13/13) |
| PII true-positive rate, SSN specifically | **33%** (1/3) — an isolated, reproducible recognizer gap, not a general guardrail failure |
| Latency, full round trip under normal load (p50 / p90 / p95) | **~1.7s / ~3.0s / ~3.8s** |
| Fast-tier (20B) vs strong-tier (120B) routing split | **~65% / ~35%** under normal load |

**Two findings worth calling out specifically:**

*Defense in depth on refusal.* The retrieval-confidence gate (threshold 0.6, calibrated empirically — off-topic queries measured retrieving spurious matches around 0.50–0.54 cosine similarity, genuine matches consistently 0.65+) caught 17/20 out-of-scope questions outright. The 3 it missed ("chemical formula for table salt," "how do I make a paper airplane," "how do I file my taxes") scored just above threshold due to weak spurious retrieval matches — but in all 3 cases, the generation layer's own grounding instruction caught what the gate didn't, honestly declining rather than fabricating an answer. Zero hallucinated answers across 20 out-of-scope questions, even when the first line of defense let something through. The very first eval run, before any threshold calibration, did catch a real bug: an uncalibrated 0.5 threshold let 2/5 out-of-scope questions straight through to generation with no safety net at all.

*Faithfulness numbers were cross-validated against blind human labeling*, not just trusted at face value. After diagnosing and fixing the judge's initial over-strictness (it was flagging reasonable, hedged elaboration as "unsupported" — see Guardrails below), 18 answers were independently labeled faithful/unfaithful by hand, without seeing the judge's verdict first, and compared: 100% agreement (18/18), including both confabulation cases the judge caught.

## Guardrails

- **Faithfulness** — an LLM-as-judge call compares each answer against its retrieved context, with automatic fallback to the strong tier if the fast tier is unavailable (the judge is a single point of failure otherwise — discovered when a sustained Groq outage silently disabled it mid-eval). Honest refusals ("I don't have that information") are explicitly scored as faithful, not penalized, since declining to answer isn't a false claim. The judge's verdicts were validated against blind human labeling (100% agreement, 18/18) after an early version was found to be over-strict, flagging reasonable hedged elaboration as "unsupported" — prompt was rewritten to flag only claims that actually mislead, not claims that merely aren't verbatim in the context.
- **PII redaction** — restricted to pattern-based entities (email, phone, credit card, SSN, IBAN, IP address) rather than NER-based categories (PERSON, LOCATION). An earlier version using NER false-positived on domain vocabulary — "Qiskit" was misclassified as a person's name by the small NLP model, redacting it out of questions and breaking retrieval quality. Switching to structural-pattern entities only eliminated that failure class. Of the 6 pattern entities, 5 catch reliably (100%, 13/13 tested); SSN specifically is weak (1/3) — see Known limitations.
- **Refusal** — gated on retrieval confidence, calibrated against measured spurious-match scores, backed by a second line of defense in the generation prompt (see Evaluation above).

## Cost & resilience

- **Semantic cache** (Qdrant for similarity matching, Upstash Redis for TTL'd storage) — repeat and near-duplicate questions skip retrieval and generation entirely, returning in ~200ms instead of ~1-2s.
- Cache writes are skipped for degraded responses (API fallback failures, unverifiable faithfulness) to avoid permanently caching a bad answer.
- Every external call (generation, judge, cache) is wrapped to fail gracefully — a Redis or Groq hiccup degrades a single response rather than crashing the request.
- Model routing includes automatic fallback to the other tier on API error, and automatic escalation from fast to strong tier when the faithfulness judge flags a fast-tier answer.

## Known limitations

- **PII / SSN recognition**: Presidio's SSN recognizer is context-sensitive and caught only 1/3 synthetic SSNs in testing, while the other 5 entity types it checks caught 13/13. An isolated, reproducible gap — documented rather than papered over, not generalized into a vaguer "PII detection is unreliable" claim it doesn't deserve.
- **Latency under sustained load**: single-user interactive latency is consistently ~1.7-3.8s, but the free Groq tier throttles hard under rapid sequential load — the eval harness itself had to add deliberate pacing, and a 33-call run still hit a sustained outage (12/33 generations failing on both tiers at once) partway through this project. A production deployment would need a paid tier or request queuing.
- **No reranking stage**: retrieval is single-pass cosine similarity; a cross-encoder rerank step would likely help on ambiguous queries but wasn't built for this version — left out deliberately rather than added speculatively, since the eval never showed retrieval precision as the dominant failure mode.
- **No CI/CD pipeline or full A/B testing**: deliberately deprioritized — A/B testing isn't statistically meaningful without real production traffic. A lightweight CI step that reruns the eval harness on every push would have real signal here (catching faithfulness regressions) and is the one piece of this list worth reconsidering first.

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

- A cross-encoder reranking stage before generation, if a future eval shows retrieval precision (not generation or judge behavior) as the dominant failure mode
- A paid LLM tier to remove the rate-limit ceiling that constrained the evaluation harness itself
- Kubernetes deployment (the original target, scaled back to Hugging Face Spaces for a zero-cost path)
- A lightweight CI step rerunning the eval harness on every push, to catch faithfulness regressions automatically

---

Built by [Abdellah](https://github.com/Abdellah-elm) as a reference implementation of LLMOps practices — guardrails, observability, and cost control treated as first-class features, not afterthoughts.