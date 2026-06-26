---
sdk: docker
app_port: 7860
---

#  GuardRAG

**A production-grade RAG system not an answer-generation demo.**

Most RAG projects stop at "retrieve, then generate." GuardRAG is built around the layer that actually decides whether an LLM app survives contact with real users: catching hallucinations before they reach anyone, redacting PII before it leaves for a third-party API, routing for cost, caching for speed, and tracing every request so you can prove any of it works. The retrieval-and-generation core is the easy 20%. This project is the other 80%.

It runs over the official Qiskit / IBM Quantum documentation (guides + tutorials + API reference — 1 766 chunks), is instrumented end to end, and was measured against a **213-question evaluation set** on the **live deployment** — not on a laptop, not on cherry-picked examples.

**Try it live:** https://layvay-guard-rag.hf.space/
**Traces & cost dashboard:** [Langfuse](https://langfuse.com)
**Stack:** FastAPI · Qdrant · Groq · Langfuse · Presidio · Upstash Redis · Docker on Hugging Face Spaces

![demo](pic/s.png)

---

## In 60 seconds

GuardRAG answers questions about Qiskit. The interesting part is everything wrapped around the answer:

- **It refuses instead of guessing.** Across 20 deliberately out-of-scope questions, it fabricated an answer **zero times** — stopped by two independent layers of defense.
- **It grades its own work.** Every answer is checked against its source by an LLM judge validated against blind human labeling with **100% agreement** (18/18).
- **It finds the right chunks.** Hybrid dense+BM25 search with cross-encoder reranking achieves **Recall@5 = 84.6%** and **MRR = 0.745** on 130 synthetic questions with known source chunks.
- **It protects user data.** Emails, phone numbers, credit cards, IBANs, and IP addresses are redacted before anything reaches a third-party model — with **0% false positives** on clean input.
- **It controls cost.** A confidence-based router sends ~65% of traffic to a fast, cheap model and escalates to the stronger one only when needed. A semantic cache returns repeat questions in **~200ms**.
- **It's observable.** Tokens, cost, latency, and quality scores ship to Langfuse on every single request.

Full round-trip latency under normal load: **~6.5s p50 / ~18s p95** (with cross-encoder reranking on CPU; **~1.7s p50 / ~3.8s p95** without reranker on HF Spaces free tier).

---

## What this project demonstrates

| Competency | What's in this project |
|---|---|
| **LLMOps / production ML** | Guardrails, evaluation, observability, and cost control treated as core features rather than afterthoughts |
| **LLM safety & guardrails** | Hallucination detection (LLM-as-judge), grounded refusal gating, and PII redaction — each independently measured, not assumed |
| **Evaluation rigor** | 213-question stratified eval set with Recall@k, MRR, faithfulness, refusal F1, and PII precision/recall — run against the live system, results cross-validated against blind human labels |
| **Advanced retrieval** | Hybrid dense+sparse (BM25) search with RRF fusion, cross-encoder reranking, query expansion, and parent-child chunking |
| **Backend engineering** | A FastAPI service where every external call degrades gracefully into a fallback instead of crashing the request |
| **Cost & resilience engineering** | Multi-model routing with automatic fallback and escalation, plus a TTL'd semantic cache that refuses to store degraded answers |
| **Observability** | End-to-end Langfuse tracing across generation, judging, and caching |

**Tech stack:** FastAPI · Qdrant Cloud · Groq (`gpt-oss-20b` / `gpt-oss-120b`) · Langfuse · Presidio · Upstash Redis · sentence-transformers (`bge-small-en-v1.5` + `bge-reranker-v2-m3`) · Hugging Face Spaces (Docker)

---

## Why it exists

The hard part of running an LLM application in production isn't generating an answer — it's everything around that: catching hallucinations before they reach a user, redacting PII before it hits a third-party API, controlling cost, knowing when something breaks, and proving any of it actually works. GuardRAG makes that layer visible. Every answer ships with a readout of how it was produced, not just the answer.

I chose the Qiskit corpus on purpose. It's a domain I can personally verify — when the system reports a faithfulness rate, I can check it against my own quantum computing background instead of trusting a number blindly.

---

## Results (v2 — 213 questions)

Measured against a 213-question stratified eval set (75 in-scope-direct, 55 in-scope-reformulated, 35 boundary, 20 out-of-scope, 18 PII across 6 entity types, 10 injection probes) run through `eval/run_eval_v2.py` against the live deployment. Full methodology, raw results, and the question set live in [`eval/`](./eval).

### Retrieval quality
| Metric | Value |
|---|---|
| MRR (Mean Reciprocal Rank) | **0.745** |
| Recall@1 | **67.7%** (88/130) |
| Recall@3 | **81.5%** (106/130) |
| Recall@5 | **84.6%** (110/130) |

### Guardrails
| Metric | Result |
|---|---|
| Out-of-scope questions never resulting in a fabricated answer | **100%** (20/20) |
| Out-of-scope caught by the retrieval-confidence gate | **85%** (17/20) — the rest caught one layer later |
| In-scope questions correctly answered (not falsely refused) | **99.4%** (164/165) |
| Refusal F1 | **0.872** |
| PII false-positive rate on clean questions | **0%** |
| PII true-positive rate — email, phone, credit card, IBAN, IP | **100%** (13/13) |
| PII true-positive rate — SSN | **67%** (2/3) — improved from 33% via regex fix |

**Two findings worth calling out:**

*Defense in depth on refusal.* The retrieval-confidence gate (threshold 0.6, calibrated empirically — off-topic queries retrieved spurious matches around 0.50–0.54 cosine, genuine matches consistently 0.65+) caught 17/20 out-of-scope questions outright. The 3 it missed scored just above threshold on weak spurious matches — but in all 3, the generation layer's grounding instruction caught what the gate didn't and declined honestly rather than fabricating. Zero hallucinated answers across 20 out-of-scope questions, even when the first line of defense let something through.

*Faithfulness was cross-validated, not trusted at face value.* After diagnosing and fixing the judge's initial over-strictness (it was flagging reasonable hedged elaboration as "unsupported"), 18 answers were independently labeled by hand without seeing the judge's verdict, then compared: 100% agreement (18/18), including both confabulation cases the judge had caught.

---

## How it works (v2 pipeline)

```
User question
   │
   ▼
PII detection (Presidio, pattern-based: email/phone/card/SSN/IBAN/IP)
   │  original question kept for embedding, redacted version sent to LLM
   ▼
Embed locally (bge-small-en-v1.5) ──► semantic cache (Qdrant + Upstash)
   │                                       │ hit → return in ~200ms
   │ miss                                  │
   ▼
Query expansion (LLM generates 2 reformulations)
   │
   ▼
Hybrid retrieval per variant (dense cosine + BM25 sparse, RRF fusion) → top-20
   │
   ▼
Merge & deduplicate candidates across all variants
   │
   ▼
Dense score of original query < 0.6 → refuse (no LLM call)
   │
   ▼
Cross-encoder reranking (bge-reranker-v2-m3) → top-5 parent chunks
   │
   ▼
Router: dense score ≥ 0.65 → gpt-oss-20b | else → gpt-oss-120b
   │
   ▼
Generation (Groq) → API error → fallback to other tier
   │
   ▼
Faithfulness judge (LLM-as-judge, with fallback tier)
   → unfaithful or unknown on fast tier → escalate + retry on strong tier
   │
   ▼
Response + Langfuse trace + cache write (confident answers only)
```

### The guardrails, in detail

- **Faithfulness** — an LLM-as-judge call compares each answer against its retrieved context, with automatic fallback to the strong tier if the fast tier is down. Honest refusals are scored as faithful, not penalized. The judge's verdicts were validated against blind human labeling (100% agreement, 18/18).
- **PII redaction** — restricted to pattern-based entities rather than NER categories. An earlier NER-based version false-positived on "Qiskit" (classified as a person's name), wrecking retrieval. SSN detection further improved with a custom regex recognizer (67%, up from 33%).
- **Refusal** — gated on retrieval confidence, calibrated against measured spurious-match scores, backed by a second line of defense in the generation prompt.

### Cost & resilience

- **Semantic cache** — repeat and near-duplicate questions return in ~200ms.
- Cache writes are skipped for degraded responses so a bad answer never gets permanently cached.
- Every external call fails gracefully into a fallback.
- Routing includes automatic fallback on API error and automatic escalation when the judge flags a fast-tier answer.

---

## Engineering judgment: what I built, what I left out, and why

- **SSN recognition improved but not perfect (2/3).** Custom regex recognizer added, improving from 33% to 67%. The third SSN used a format without context that still evades detection. Documented as an isolated gap.
- **Reranker disabled on HF Spaces free tier.** `bge-reranker-v2-m3` scoring 10 pairs on CPU adds ~20s of latency. Set `USE_RERANKER=false` on the Space; enable locally for eval where quality matters more than speed.
- **Latency holds under interactive use, not sustained load.** The free Groq tier throttles hard under rapid sequential load. The 213-question eval harness needed 8s pacing between requests. A production deployment would need a paid tier or request queuing.
- **No CI/CD.** A lightweight CI step rerunning the eval on every push would catch faithfulness regressions automatically — the first thing I'd add.

### What I'd build next

- A paid LLM tier to remove the rate-limit ceiling that constrained the eval harness.
- Kubernetes deployment — the original target, scaled back to Hugging Face Spaces for a zero-cost path.
- A CI step rerunning the eval on every push to catch faithfulness regressions automatically.
- A larger stratified eval set with statistical confidence intervals.

---

## Run it locally (v2)

```bash
git clone https://github.com/Abdellah-elm/guard_RAG.git
cd guard_RAG
docker compose up -d          # Qdrant + Redis for local dev
pip install -r requirements.txt
pip install rank-bm25
python -m spacy download en_core_web_sm
cp .env.example .env          # fill in your API keys

# Clone Qiskit docs (guides + tutorials + API reference)
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/Qiskit/documentation.git data/qiskit-docs
cd data/qiskit-docs
git sparse-checkout set docs/guides docs/tutorials docs/api/qiskit-ibm-runtime
cd ../..

# Parse and index
python ingestion/parse_qiskit_docs_v2.py --out data/chunks_v2.jsonl
python ingestion/embed_and_index_v2.py    # creates data/bm25_encoder.pkl

uvicorn app.main:app --reload
```

Full setup details are in [`eval/`](./eval) and the inline comments in `app/main.py`.

The corpus is the official [Qiskit/documentation](https://github.com/Qiskit/documentation) (CC BY-SA 4.0).

---

## About

Built by **[Abdellah](https://github.com/Abdellah-elm)** as a reference implementation of LLMOps practices — guardrails, observability, and cost control as first-class features, not afterthoughts.