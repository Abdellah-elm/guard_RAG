# GuardRAG

A production-style RAG assistant over the official Qiskit / IBM Quantum
documentation, instrumented with full LLMOps: online evaluation, guardrails,
observability, cost optimization, prompt versioning and A/B testing.

## Why Qiskit docs

[Qiskit/documentation](https://github.com/Qiskit/documentation) is dual
licensed — code under Apache 2.0, content under CC BY-SA 4.0 — so indexing it
for a personal RAG project is clean. It's also a domain I can evaluate myself:
when the hallucination-rate dashboard says "3%", I can actually check whether
that's true instead of trusting an LLM-judge blindly.

v1 indexes `docs/guides` (89 pages, the practical how-to content — closest to
a real support knowledge base). `docs/tutorials` and the auto-generated
`docs/api` reference (900+ pages) are candidates for a later expansion.

## Architecture

```
query -> semantic cache (Redis) -> retrieval + rerank (Qdrant) ->
router (vLLM local -> Claude Haiku -> Claude Sonnet) -> guardrails
(PII / faithfulness / refusal) -> response + Langfuse trace
```

## Phase 0 — local setup

1. Pull the docs corpus (sparse checkout, guides only):

   ```bash
   git clone --depth 1 --filter=blob:none --sparse \
       https://github.com/Qiskit/documentation.git data/qiskit-docs
   cd data/qiskit-docs && git sparse-checkout set docs/guides && cd ../..
   ```

2. Start the local infra:

   ```bash
   docker compose up -d
   ```

3. Parse the docs into clean, chunked JSONL (zero extra dependencies):

   ```bash
   python ingestion/parse_qiskit_docs.py \
       --docs-dir data/qiskit-docs/docs/guides \
       --out data/chunks.jsonl
   ```

   Sanity check on the real corpus: 89 files -> 834 chunks, ~104 words/chunk
   average.

4. Copy `.env.example` to `.env` and fill in your keys (Anthropic API key,
   Langfuse cloud keys — free tier is enough for now).

5. (Phase 2) Start vLLM locally — needs GPU passthrough, kept out of
   docker-compose for simplicity:

   ```bash
   pip install vllm --break-system-packages
   python -m vllm.entrypoints.openai.api_server \
       --model Qwen/Qwen2.5-3B-Instruct-AWQ \
       --quantization awq \
       --max-model-len 4096 \
       --gpu-memory-utilization 0.85
   ```

   6 GB VRAM is tight: stick to a 1.5B-3B model in AWQ/GPTQ 4-bit. If vLLM
   chokes on your CUDA/driver combo, fall back to Ollama for local serving
   and revisit vLLM once the rest of the pipeline works — the routing layer
   doesn't care which server is behind the "local" endpoint.

## Status

- [x] Phase 0 — repo scaffold, local infra, doc parsing (tested against the
      real corpus)
- [ ] Phase 1 — embeddings + retrieval + reranking
- [ ] Phase 2 — multi-model serving + routing (vLLM -> Haiku -> Sonnet)
- [ ] Phase 3 — guardrails (PII, faithfulness, refusal)
- [ ] Phase 4 — observability (Langfuse + Prometheus/Grafana)
- [ ] Phase 5 — cost optimization (semantic cache)
- [ ] Phase 6 — prompt versioning + A/B testing + feedback loop
- [ ] Phase 7 — CI/CD + Kubernetes deployment
