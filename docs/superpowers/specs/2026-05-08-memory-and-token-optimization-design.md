# Memory Architecture & Token Optimization

**Date:** 2026-05-08
**Status:** Draft
**Scope:** Consolidate memory layers, add tiered model routing, prompt caching, batch API, and pre-filtering

---

## Problem

The coordinator has three overlapping memory layers (Graphiti, Neo4j Direct, JSON file) and sends every LLM call through a single model. Running all 6 agents on Opus costs ~$7.55/run ($225/mo daily). Graphiti is dead code. JSON memory duplicates Neo4j.

## Goals

1. Single memory backend (Neo4j Direct) with lightweight fallback
2. Reduce LLM cost by ~91% without losing accuracy on critical decisions
3. Zero tokens spent in the REMEMBER phase

## Non-Goals

- Replacing ChromaDB (stays as-is for doc retrieval)
- Changing the pipeline phases (DISCOVER/FILTER/MAP/ANALYZE/ACT/REMEMBER stay)
- Modifying JIRA, Sippy, or GitHub API clients

---

## Part 1: Memory Consolidation

### What gets removed

| File | Lines | Reason |
|------|-------|--------|
| `src/knowledge/graphiti_store.py` | 252 | Dead code. Not imported anywhere outside its own file. LLM-powered entity extraction on already-structured pipeline data. |
| `src/knowledge/ollama_llm_client.py` | 217 | Only exists to support Graphiti. Custom Ollama adapter with fake OpenAI response objects. |

### What gets promoted

**`src/knowledge/neo4j_store.py`** becomes the single memory backend.

Currently Neo4j is optional — `main.py` tries to connect, passes `None` if it fails. Change:

- Neo4j becomes the expected backend
- If Neo4j is down at startup, the pipeline exits with a clear error message (fail-fast). Neo4j must be running for the pipeline to operate — this avoids silent data loss where analyzed bugs aren't persisted and get re-analyzed next run.
- No dual-write to JSON

### What gets demoted

**`src/knowledge/memory.py`** (JSON file store) becomes a migration-only utility.

- One-time migration script reads `coordinator_memory.json` and writes all 3,346 analyzed bugs + 576 gaps into Neo4j
- After migration, `coordinator_memory.json` is archived (not deleted — kept as backup)
- `MemoryStore` class removed from the pipeline import chain

### Changes to `base_agent.py`

Before:
```python
from src.knowledge.memory import MemoryStore
from src.knowledge.neo4j_store import Neo4jStore

self.memory = memory or MemoryStore()
self.neo4j = neo4j_store

def _get_known_bugs(self):
    if self.neo4j:
        return self.neo4j.get_analyzed_bug_keys_sync()
    return self.memory.get_analyzed_bug_keys()

def _remember(self, result):
    self.memory.remember_result(result)  # always JSON
    if self.neo4j:
        self.neo4j.remember_result_sync(result)  # optional Neo4j
```

After:
```python
from src.knowledge.neo4j_store import Neo4jStore

self.neo4j = neo4j_store  # required

def _get_known_bugs(self):
    return self.neo4j.get_analyzed_bug_keys_sync()

def _remember(self, result):
    self.neo4j.remember_result_sync(result)
```

### Docker Compose

No changes. Neo4j container stays as-is:
```yaml
services:
  neo4j:
    image: neo4j:5-community
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: neo4j/password
    volumes:
      - neo4j_data:/data
```

---

## Part 2: Token Optimization

### 2.1 Keyword Pre-Filter (Layer 1 — zero tokens)

Run the existing `chaos_filter.py` keyword filter **before** the LLM. It classifies bugs into three buckets:

| Bucket | Action | Estimated % |
|--------|--------|-------------|
| **Obvious no** — summary contains UI, console, docs, CVE, typo patterns | Skip. No LLM call. | ~38% |
| **Obvious yes** — summary contains crash, OOM, timeout, network failure patterns | Pass to MAP. No LLM call. | ~17% |
| **Ambiguous** — keyword filter returns low confidence | Send to LLM for classification | ~45% |

Implementation: Add a `confidence` field to the keyword filter's `FilterResult`. High-confidence results (positive or negative) bypass the LLM. Low-confidence results proceed to LLM classification.

Current `chaos_filter.py` returns a binary yes/no. Change it to return a score:
- Score > 0.8 → obvious yes (pass to MAP)
- Score < 0.2 → obvious no (skip)
- 0.2 to 0.8 → ambiguous (send to LLM)

### 2.2 Semantic Cache (Layer 2 — zero tokens)

Before calling the LLM, check if a semantically similar bug was already classified in a previous run.

Use ChromaDB (already available) with a new collection `filter_cache`:
- Document: bug summary text (ChromaDB generates the embedding)
- Metadata dict: `{"chaos_relevant": true, "failure_mode": "...", "injection_method": "...", "cached_at": "ISO timestamp"}`
- Lookup: query the collection with the new bug's summary, check if best hit has cosine distance < 0.15
- If hit: return a `FilterResult` built from the cached metadata
- If miss: proceed to LLM, then upsert the result into the cache collection

Example:
- Bug A: "etcd pod OOMKilled under memory pressure" → LLM classifies → cache result
- Bug B: "coredns pod OOMKilled under memory pressure" → cache hit → reuse classification, swap component

Cache invalidation: entries expire after 30 days or when the system prompt changes.

### 2.3 Model Routing (Layer 3 — tiered models)

Each pipeline phase uses the cheapest model that produces reliable output for that task type.

| Phase | Model | Rationale |
|-------|-------|-----------|
| FILTER | Sonnet 4.6 | Binary classification with structured JSON output. Needs to understand OCP component architecture from ChromaDB context, but decision is narrow. |
| MAP | Sonnet 4.6 | 3-way classification (FULL/PARTIAL/NO match). Compares bug description against scenario configs. Moderate reasoning. |
| ANALYZE | Opus 4.6 | Deep reasoning required. Synthesizes OCP docs, krkn plugin capabilities, past resolved bugs. Produces specific step-by-step modifications and confidence scores. |

Configuration via environment variables:
```
LLM_FILTER_PROVIDER=anthropic
LLM_FILTER_MODEL=claude-sonnet-4-6

LLM_MAP_PROVIDER=anthropic
LLM_MAP_MODEL=claude-sonnet-4-6

LLM_ANALYZE_PROVIDER=anthropic
LLM_ANALYZE_MODEL=claude-opus-4-6
```

Fallback: if per-phase config is not set, use the global `LLM_MODEL` for all phases (backwards compatible).

Implementation: `detect_llm_backend()` in `llm_config.py` gains a `phase` parameter:
```python
def detect_llm_backend(phase: str = "default") -> LLMBackendConfig:
```

### 2.4 Confidence-Based Escalation (Layer 4 — Sonnet → Opus)

For FILTER only. When Sonnet returns a low confidence score, escalate to Opus for a second opinion.

The FILTER prompt already returns structured JSON. Add a `confidence` field (0-100):
```json
{
  "chaos_relevant": true,
  "confidence": 55,
  "failure_mode": "...",
  "injection_method": "...",
  "skip_reason": null
}
```

Routing logic:
- confidence >= 80 → trust Sonnet's answer
- confidence < 80 → re-send the same prompt to Opus
- If Opus also returns low confidence → use Opus's answer (final authority)

Expected escalation rate: ~10-15% of LLM-filtered bugs.

### 2.5 Prompt Caching (Layer 5 — 90% off cached input)

System prompts are identical across all bugs in a run. Mark them as cacheable using Anthropic's `cache_control` parameter.

Before:
```python
response = client.messages.create(
    model=model,
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ],
)
```

After:
```python
response = client.messages.create(
    model=model,
    system=[
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ],
    messages=[
        {"role": "user", "content": prompt},
    ],
)
```

Cached content and pricing:

| Phase | Cached content | Size | Calls | Cache write (1.25x, once) | Cache read (0.1x, rest) |
|-------|---------------|------|-------|--------------------------|------------------------|
| FILTER | `FILTER_SYSTEM_PROMPT` | ~800 tokens | ~85 | 1 call at 1.25x | 84 calls at 0.1x |
| MAP | `MAP_SYSTEM_PROMPT` | ~600 tokens | ~60 | 1 call at 1.25x | 59 calls at 0.1x |
| ANALYZE | `ANALYZE_SYSTEM_PROMPT` | ~900 tokens | ~32 | 1 call at 1.25x | 31 calls at 0.1x |

Cache TTL is 5 minutes. Since bugs are processed in a tight loop, the cache stays warm for the entire run.

Note: prompt caching requires the Anthropic SDK. For Ollama/OpenAI providers, this layer is skipped (no-op).

### 2.6 Batch API (Layer 6 — 50% off, stacks with caching)

Submit all requests per phase as a single batch instead of sequential API calls. Anthropic Batch API processes requests within 24 hours (typically minutes for small batches) at 50% off.

Pipeline flow becomes:
```
FILTER: collect 85 requests → submit batch → poll → parse results
MAP:    collect 60 requests → submit batch → poll → parse results  
ANALYZE: collect 32 requests → submit batch → poll → parse results
```

Combined discount: cached input in a batch costs 0.1x × 0.5x = 0.05x (95% off base input price).

Configuration:
```
LLM_USE_BATCH=true   # default: false (real-time mode)
```

When `LLM_USE_BATCH=false`, the pipeline works as today — sequential real-time calls. This preserves interactive use (dashboard, debugging).

Implementation: new `src/filter/llm_batch.py` module:
- `batch_filter_bugs(bugs, config)` → submits batch, returns results
- `batch_map_match(bugs, config)` → submits batch, returns results
- `batch_analyze_gaps(gaps, config)` → submits batch, returns results
- Each function falls back to sequential `call_llm` if batch submission fails
- Poll interval: 10 seconds. Timeout: 30 minutes (small batches typically complete in under 2 minutes)
- Results are returned as a list in the same order as inputs

---

## Full Pipeline Flow (After)

```
DISCOVER
  JIRA returns ~235 bugs (14-day window, all 6 agents combined)

DEDUP (Neo4j, 0 tokens)
  3,346 known bugs filtered out → only new bugs proceed
  Known bugs get status update in Neo4j (Cypher query, 0 tokens)

FILTER Layer 1: Keyword pre-filter (0 tokens)
  ~90 obvious "no" → stored as not-chaos-relevant in Neo4j
  ~40 obvious "yes" → proceed to MAP
  ~105 ambiguous → proceed to Layer 2

FILTER Layer 2: Semantic cache (0 tokens)
  ~20 cache hits → reuse cached classification
  ~85 cache misses → proceed to Layer 3

FILTER Layer 3: Sonnet LLM (85 calls, cached system prompt, batched)
  ~75 high-confidence → trust Sonnet
  ~10 low-confidence → proceed to Layer 4

FILTER Layer 4: Opus escalation (10 calls)
  Final classification for ambiguous bugs

MAP: Sonnet (60 bugs, cached + batched)
  FULL_MATCH → covered, skip
  PARTIAL/NO_MATCH → proceed to ANALYZE

ANALYZE: Opus (32 gaps, cached + batched)
  Score confidence, produce specific krkn modifications

ACT: Create GitHub issues/PRs (0 tokens)

REMEMBER: Neo4j (0 tokens)
  Store all results for dedup in future runs
```

---

## Cost Model

### Per-run cost (all 6 agents)

| Step | Calls | Model | Input tokens | Output tokens | Cost |
|------|-------|-------|-------------|--------------|------|
| Keyword pre-filter | 235 | none | 0 | 0 | $0.00 |
| Semantic cache | 105 | none | 0 | 0 | $0.00 |
| FILTER (Sonnet, batch+cache) | 85 | Sonnet 4.6 | ~170K | ~17K | $0.06 |
| FILTER escalation | 10 | Opus 4.6 | ~20K | ~2K | $0.04 |
| MAP (Sonnet, batch+cache) | 60 | Sonnet 4.6 | ~150K | ~12K | $0.12 |
| ANALYZE (Opus, batch+cache) | 32 | Opus 4.6 | ~100K | ~16K | $0.45 |
| Neo4j REMEMBER | — | none | 0 | 0 | $0.00 |
| **Total** | | | | | **~$0.67** |

### Monthly cost comparison

| Configuration | Per run | Daily (30 days) |
|--------------|---------|-----------------|
| Current (all Opus, no optimization) | $7.55 | $225/mo |
| Model routing only | $2.65 | $80/mo |
| + prompt caching | $0.85 | $25/mo |
| + batch API | $0.70 | $21/mo |
| + keyword pre-filter + semantic cache | $0.67 | **$20/mo** |

### Accuracy safeguards

| Risk | Mitigation |
|------|-----------|
| Sonnet misclassifies a FILTER bug | Confidence-based escalation to Opus for uncertain cases (confidence < 80) |
| Keyword pre-filter false negative | Conservative thresholds: only skip bugs matching strong negative patterns (UI, docs, CVE). Ambiguous bugs always go to LLM. |
| Semantic cache returns stale result | 30-day TTL. Cache keyed on summary embedding, not exact text. System prompt changes invalidate cache. |
| Batch API too slow for interactive use | `LLM_USE_BATCH=false` flag for real-time mode. Batch is opt-in for scheduled runs. |

---

## Files Changed

### Deleted
- `src/knowledge/graphiti_store.py`
- `src/knowledge/ollama_llm_client.py`

### New
- `src/filter/llm_batch.py` — Batch API wrapper for all three LLM phases
- `src/filter/llm_tools.py` — Typed tool functions (`filter_bug_llm`, `map_match_llm`, `analyze_gap_llm`) with context objects, observation returns, and error recovery contracts
- `src/evals/filter_eval.py` — FILTER capability and regression eval
- `src/evals/map_eval.py` — MAP capability eval
- `src/evals/eval_report.py` — `EvalReport` dataclass and comparison logic
- `src/scripts/migrate_json_to_neo4j.py` — One-time migration from JSON memory to Neo4j

### Modified
- `src/knowledge/neo4j_store.py` — No schema changes. Remove `_sync` aliases (they're identical to the sync methods). Add filter cache collection for semantic cache. Add `RunMetrics` node storage linked to `Run` nodes.
- `src/knowledge/memory.py` — Remove from pipeline imports. Keep file for migration script reference.
- `src/agents/base_agent.py` — Remove `MemoryStore` import, remove JSON fallback, remove dual-write. Replace inline LLM calls with typed tool functions. Pipeline reads `Observation.next_actions` for flow control instead of if/else chains. Add keyword pre-filter call before LLM filter. Add semantic cache check. Collect and store `RunMetrics` at end of run.
- `src/filter/llm_config.py` — Add `phase` parameter to `detect_llm_backend()`. Add per-phase env vars (`LLM_FILTER_MODEL`, `LLM_MAP_MODEL`, `LLM_ANALYZE_MODEL`).
- `src/filter/llm_filter.py` — Refactor to use typed tool functions from `llm_tools.py`. Add `cache_control` to system message. Add confidence field to FILTER prompt. Add escalation logic. Support batch mode. Context budget enforcement (truncation limits per phase).
- `src/filter/chaos_filter.py` — Return confidence score (0.0-1.0) instead of binary yes/no. Scores > 0.8 or < 0.2 bypass LLM.
- `src/reasoning.py` — Refactor to use typed tool functions from `llm_tools.py`. Add `cache_control` to system messages. Support batch mode. Context budget enforcement.
- `src/main.py` — Remove JSON memory initialization. Make Neo4j required (fail-fast at startup).
- `docker-compose.yaml` — No changes.
- `.env.example` — Add per-phase model config vars and `LLM_USE_BATCH`.

---

## Migration Plan

### Phase 0: Eval baseline (before any code changes)
1. Sample 200 bugs from `coordinator_memory.json` (stratified by agent domain)
2. Run Opus FILTER on all 200 → save as baseline labels
3. Run Sonnet FILTER on same 200 → compare against Opus
4. If agreement >= 90% and false negative rate <= 5% → proceed
5. If not → adjust escalation threshold or keep Opus for FILTER

### Phase 1: Memory consolidation
6. Run `migrate_json_to_neo4j.py` to import 3,346 bugs + 576 gaps from `coordinator_memory.json`
7. Verify Neo4j contains all data: `MATCH (b:Bug) RETURN count(b)` should return 3346
8. Rename `coordinator_memory.json` to `coordinator_memory.json.bak`
9. Delete `graphiti_store.py` and `ollama_llm_client.py`
10. Update `base_agent.py` — remove MemoryStore, make Neo4j required

### Phase 2: Token optimization
11. Add per-phase model routing to `llm_config.py`
12. Add prompt caching (`cache_control`) to `llm_filter.py` and `reasoning.py`
13. Add keyword pre-filter confidence scoring to `chaos_filter.py`
14. Add semantic cache collection to ChromaDB
15. Add confidence-based escalation to FILTER
16. Add batch API support (`llm_batch.py`)

### Phase 3: Harness improvements
17. Create typed tool functions (`llm_tools.py`)
18. Add `Observation` and context dataclasses to `models.py`
19. Refactor `base_agent.py` to use typed tools and observation-driven flow
20. Add error recovery contracts
21. Add `RunMetrics` tracking

### Phase 4: Smoke test
22. Run one agent (control_plane) as smoke test
23. Compare against eval baseline — verify no regression
24. If successful, run all 6 agents

---

## Part 2.7: Eval-First Validation

Before switching models (Opus → Sonnet for FILTER/MAP), we need to prove Sonnet produces equivalent results. The agentic engineering principle: define evals before execution, run baseline, compare deltas.

### Building the eval dataset

We have 3,346 analyzed bugs and 576 gaps with confidence scores. But the JSON memory doesn't store FILTER decisions (chaos_relevant / skip_reason). To build a labeled eval set:

1. **Sample 200 bugs** from the 3,346 known bugs — stratified across all 6 agent domains
2. **Run Opus FILTER on all 200** — this produces the "ground truth" labels (chaos_relevant, failure_mode, injection_method, confidence)
3. **Run Sonnet FILTER on the same 200** — compare against Opus labels
4. **Run keyword filter on the same 200** — baseline for the pre-filter

This costs ~$1.60 one-time (200 bugs × Opus + 200 × Sonnet) and produces a reusable eval set.

### Capability evals

**FILTER eval** — measures classification accuracy:
```
Metrics:
- Agreement rate: % of bugs where Sonnet matches Opus (chaos_relevant field)
- False negative rate: % of bugs Sonnet says "no" but Opus says "yes" (the dangerous case)
- False positive rate: % of bugs Sonnet says "yes" but Opus says "no" (costs tokens, not dangerous)
- Confidence calibration: do Sonnet confidence scores correlate with Opus agreement?

Pass criteria:
- Agreement rate >= 90%
- False negative rate <= 5%
- Confidence calibration: bugs where Sonnet confidence < 80 should have > 30% Opus disagreement
  (validates that our escalation threshold catches the right cases)
```

**MAP eval** — measures scenario matching accuracy:
```
Metrics:
- Match agreement: % where Sonnet and Opus agree on FULL/PARTIAL/NO
- Scenario agreement: when both say PARTIAL or FULL, do they pick the same scenario?

Pass criteria:
- Match agreement >= 85%
- FULL_MATCH must never become NO_MATCH (one grade of drift is acceptable)
```

**ANALYZE eval** — Opus stays as the model, so no model-switch eval needed. Instead, measure output quality:
```
Metrics:
- Confidence score distribution (should match historical: ~7% HIGH, ~43% MEDIUM, ~50% LOW)
- Modification specificity: % of gaps with concrete file paths and plugin names (vs vague "extend the scenario")

Pass criteria:
- Score distribution within 10% of historical baseline
- >= 80% of HIGH confidence gaps have specific modifications
```

### Regression evals

Run after every change to prompts, model versions, or context budget:
- Re-run FILTER eval on the 200-bug set
- Compare against last baseline
- Alert if false negative rate increases by > 2%

### Eval infrastructure

```python
# src/evals/filter_eval.py
def run_filter_eval(
    bugs: list[Bug],
    baseline_model: str = "claude-opus-4-6",
    candidate_model: str = "claude-sonnet-4-6",
) -> EvalReport:
    """Run FILTER on same bugs with two models, compare results."""

# src/evals/map_eval.py
def run_map_eval(bugs: list[Bug], ...) -> EvalReport:

# src/evals/eval_report.py
@dataclass
class EvalReport:
    eval_name: str
    baseline_model: str
    candidate_model: str
    agreement_rate: float
    false_negative_rate: float
    false_positive_rate: float
    sample_size: int
    disagreements: list[dict]  # bugs where models disagreed, for review
```

### Eval-first implementation order

1. Build eval dataset (sample 200 bugs, run Opus baseline) — **do this before any code changes**
2. Run Sonnet against same dataset, measure agreement
3. If pass criteria met → proceed with model routing implementation
4. If not met → adjust escalation threshold or keep Opus for FILTER

This ensures we never ship a model downgrade without proof it works.

---

## Part 3: Agent Harness Improvements

The pipeline is an agent harness — 6 domain agents each calling tools (JIRA, ChromaDB, LLM, GitHub, Neo4j) in a fixed sequence. Applying harness construction principles to improve action space, observation quality, error recovery, and context budgeting.

### 3.1 Typed Tool Functions (Action Space)

Replace the catch-all `call_llm(messages, config)` with typed, phase-specific functions that accept domain objects and return domain objects.

Current:
```python
# Everything goes through one function with raw dicts
text = call_llm(
    messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": prompt}],
    config=config,
)
result = json.loads(text)  # hope it's valid JSON
parsed = FilterResult(bug=bug, chaos_relevant=result.get("chaos_relevant", False), ...)
```

After:
```python
# Phase-specific functions with typed inputs and outputs
def filter_bug_llm(bug: Bug, context: FilterContext) -> FilterResult:
    """FILTER phase tool. Returns typed FilterResult, never raw JSON."""

def map_match_llm(bug: Bug, filter_result: FilterResult, context: MapContext) -> ScenarioMatch:
    """MAP phase tool. Returns typed ScenarioMatch."""

def analyze_gap_llm(bug: Bug, match: ScenarioMatch, context: AnalyzeContext) -> GapAnalysis:
    """ANALYZE phase tool. Returns typed GapAnalysis."""
```

Context objects bundle the per-call inputs that vary:

```python
@dataclass(frozen=True)
class FilterContext:
    ocp_docs: list[dict]
    krkn_docs: list[dict]
    config: LLMBackendConfig

@dataclass(frozen=True)
class MapContext:
    scenario_hits: list[dict]
    doc_hits: list[dict]
    kb_context: dict | None
    config: LLMBackendConfig

@dataclass(frozen=True)
class AnalyzeContext:
    ocp_docs: list[dict]
    krkn_docs: list[dict]
    neo4j_history: list[dict]
    config: LLMBackendConfig
```

Benefits:
- Each tool has a narrow, schema-first interface
- Type checker catches misuse at import time, not runtime
- JSON parsing and validation happen inside the tool, not in the caller
- Batch mode wraps the same typed functions (collect inputs → batch call → return typed outputs)

### 3.2 Structured Observations

Every tool response should include status, summary, and next actions — not just the result data. Add an `Observation` wrapper that each phase returns alongside its domain object.

```python
@dataclass(frozen=True)
class Observation:
    status: str          # "success" | "warning" | "error"
    summary: str         # One-line human-readable result
    next_actions: list[str]  # What the pipeline should do next
    artifacts: dict      # File paths, IDs, counts
```

Examples of what each phase returns:

**FILTER:**
```python
Observation(
    status="success",
    summary="OCPBUGS-1234 is chaos-relevant: etcd leader election failure under network partition",
    next_actions=["proceed_to_map"],
    artifacts={"bug_key": "OCPBUGS-1234", "injection_method": "network_chaos"},
)
```

**FILTER (low confidence):**
```python
Observation(
    status="warning",
    summary="OCPBUGS-5678 classified with low confidence (55%). Escalating to Opus.",
    next_actions=["escalate_to_opus"],
    artifacts={"bug_key": "OCPBUGS-5678", "confidence": 55},
)
```

**MAP (error):**
```python
Observation(
    status="error",
    summary="LLM returned invalid JSON for OCPBUGS-9012. Falling back to distance-based matching.",
    next_actions=["use_fallback_match"],
    artifacts={"bug_key": "OCPBUGS-9012", "error": "JSONDecodeError"},
)
```

The pipeline reads `observation.next_actions` to decide flow instead of inline if/else chains. This makes the pipeline self-documenting — you can log observations and see exactly what happened and why.

### 3.3 Error Recovery Contracts

Every tool function defines explicit recovery behavior. Currently errors are caught with broad `except Exception` and fallbacks are ad-hoc. Define a contract per error path.

```python
FILTER_RECOVERY = {
    "llm_timeout": {
        "root_cause": "LLM provider did not respond within timeout",
        "retry": "Retry once with 2x timeout. If still fails, use keyword filter.",
        "stop_condition": "After 1 retry, fall back permanently for this bug.",
    },
    "json_parse_error": {
        "root_cause": "LLM returned non-JSON or malformed JSON",
        "retry": "Retry once with explicit 'respond with ONLY JSON' appended.",
        "stop_condition": "After 1 retry, use keyword filter for this bug.",
    },
    "confidence_below_threshold": {
        "root_cause": "Sonnet uncertain about classification",
        "retry": "Escalate to Opus (different model, same prompt).",
        "stop_condition": "Accept Opus answer regardless of confidence.",
    },
    "batch_submission_failed": {
        "root_cause": "Anthropic Batch API rejected the request",
        "retry": "Fall back to sequential real-time calls for entire phase.",
        "stop_condition": "If sequential also fails, skip LLM and use keyword filter.",
    },
}

MAP_RECOVERY = {
    "llm_timeout": {
        "root_cause": "LLM provider timeout during MAP",
        "retry": "Retry once. If fails, use ChromaDB distance-based fallback.",
        "stop_condition": "Distance < 0.35 → FULL_MATCH, < 0.65 → PARTIAL, else NO_MATCH.",
    },
    "json_parse_error": {
        "root_cause": "LLM returned non-JSON",
        "retry": "Retry once with stricter prompt.",
        "stop_condition": "Fall back to distance-based matching.",
    },
    "no_scenario_hits": {
        "root_cause": "ChromaDB returned zero scenario matches",
        "retry": "No retry. This is a valid NO_MATCH.",
        "stop_condition": "Proceed directly to ANALYZE.",
    },
}

ANALYZE_RECOVERY = {
    "llm_timeout": {
        "root_cause": "Opus timeout during ANALYZE (most expensive call)",
        "retry": "Retry once with 3x timeout (Opus needs more time for deep reasoning).",
        "stop_condition": "After 1 retry, create gap with confidence=0 and reasoning='LLM analysis failed'.",
    },
    "json_parse_error": {
        "root_cause": "Opus returned non-JSON analysis",
        "retry": "Retry once.",
        "stop_condition": "Create gap with confidence=20 (LOW) and raw text as reasoning.",
    },
    "neo4j_history_unavailable": {
        "root_cause": "Neo4j query for similar resolved bugs failed",
        "retry": "No retry. Proceed with empty history context.",
        "stop_condition": "ANALYZE runs without historical comparison. Note in observation.",
    },
}
```

Implementation: Each typed tool function uses its recovery contract internally. The pipeline never catches raw exceptions — it receives `Observation` objects with `status="error"` and `next_actions` telling it what to do.

### 3.4 Context Budget Rules

Formalize how much context each phase is allowed to consume. Prevents "context explosion" where ChromaDB returns too many docs and inflates every call.

| Phase | System prompt | Bug content | ChromaDB docs | Neo4j history | Total budget |
|-------|--------------|-------------|---------------|---------------|-------------|
| FILTER | 800 tokens (cached) | 1,500 tokens (summary + desc truncated) | 900 tokens (3 OCP × 300 chars) | 0 | ~3,200 tokens |
| MAP | 600 tokens (cached) | 800 tokens (summary + desc truncated) | 2,500 tokens (5 scenarios × 500 chars) + 900 (3 docs × 300) | 0 | ~4,800 tokens |
| ANALYZE | 900 tokens (cached) | 1,000 tokens (summary + desc) | 1,200 (3 OCP × 400) + 1,200 (3 krkn × 400) | 500 tokens (5 bugs × 100 chars) | ~4,800 tokens |

Rules:
- ChromaDB `n_results` is fixed per phase, not configurable at runtime
- Bug descriptions are truncated to phase-specific limits (1500 for FILTER, 800 for MAP, 1000 for ANALYZE)
- System prompts are invariant and cached — never modified per-bug
- Neo4j history is only loaded in ANALYZE (the only phase that uses it)
- If ChromaDB returns fewer results than requested, no padding — send what you have

These budgets are enforced in the typed tool functions, not in the caller. The caller passes the full bug; the tool truncates internally.

### 3.5 Pipeline Architecture Pattern

The current pipeline is a function-calling chain (fixed sequence). The harness skill recommends **Hybrid: ReAct planning + typed tool execution** for complex flows.

For this project, we keep function-calling as the primary pattern because the sequence is deterministic (DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER). But we add ReAct-style decision points at the junctions:

```
FILTER result → Observation.next_actions decides:
  ["proceed_to_map"]           → normal flow
  ["escalate_to_opus"]         → re-filter with Opus
  ["skip_already_cached"]      → semantic cache hit
  ["skip_keyword_negative"]    → keyword pre-filter caught it

MAP result → Observation.next_actions decides:
  ["skip_full_match"]          → bug already covered
  ["proceed_to_analyze"]       → gap found
  ["use_fallback_match"]       → LLM failed, using distance

ANALYZE result → Observation.next_actions decides:
  ["create_pr"]                → high confidence
  ["create_issue"]             → medium confidence
  ["log_gap_only"]             → low confidence
  ["skip_analysis_failed"]     → Opus failed after retry
```

This keeps the pipeline deterministic while making flow decisions explicit and loggable.

### 3.6 Benchmarking

Track per-run metrics to measure harness quality over time:

```python
@dataclass
class RunMetrics:
    # Completion
    bugs_processed: int
    bugs_succeeded: int
    completion_rate: float     # succeeded / processed

    # Retries
    filter_retries: int        # JSON parse retries
    filter_escalations: int    # Sonnet → Opus escalations
    map_fallbacks: int         # LLM → distance fallbacks
    analyze_retries: int       # Opus retries

    # Cost
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    cost_per_gap: float        # total_cost / gaps_found

    # Cache
    keyword_filter_hits: int   # bugs caught by keyword pre-filter
    semantic_cache_hits: int   # bugs caught by semantic cache
    prompt_cache_hit_rate: float

    # Timing
    filter_duration_sec: float
    map_duration_sec: float
    analyze_duration_sec: float
    total_duration_sec: float
```

Stored in Neo4j as a `RunMetrics` node linked to each `Run` node. Enables trend analysis: "are retries increasing? is cost per gap going up?"

---

## Part 4: Testing Strategy

Existing test coverage: 656 lines across 7 test files. Tests cover keyword filter and JSON memory. No tests for LLM calls, Neo4j, batch API, or the pipeline flow. The refactor touches all critical paths — testing bar must go up.

### 4.1 What exists today

| Test file | Lines | Covers |
|-----------|-------|--------|
| `test_chaos_filter.py` | 110 | Keyword filter: CVE skip, crash detection, UI skip |
| `test_memory.py` | 82 | JSON memory: store, dedup, persist, gaps, resolve |
| `test_reasoning.py` | 217 | LLM MAP/ANALYZE with mocked `call_llm` |
| `test_orchestrator.py` | 82 | Orchestrator dedup and formatting |
| `test_models.py` | 60 | Dataclass construction and enums |
| `test_component_map.py` | 33 | Agent → component mapping |
| `test_scenario_index.py` | 72 | YAML scenario indexing |

### 4.2 Tests to add per refactor phase

**Phase 1 — Memory consolidation:**

```
tests/unit/test_neo4j_store.py (NEW)
  - test_connect_and_create_schema
  - test_remember_result_stores_bugs_and_gaps
  - test_get_analyzed_bug_keys_returns_stored_keys
  - test_is_bug_analyzed_true_for_known_bug
  - test_update_bug_statuses_closes_resolved_gaps
  - test_get_open_gaps_returns_only_open
  - test_get_similar_resolved_bugs_by_component
  - test_run_history_ordered_by_timestamp

tests/integration/test_neo4j_integration.py (NEW)
  - test_full_remember_and_query_cycle (requires Neo4j container)
  - test_migration_from_json_matches_neo4j_data

tests/unit/test_memory.py (UPDATE)
  - Keep existing tests but mark as "migration reference"
  - Add: test_migration_script_imports_all_bugs
  - Add: test_migration_script_imports_all_gaps
```

**Phase 2 — Token optimization:**

```
tests/unit/test_chaos_filter.py (UPDATE — confidence scoring)
  - test_obvious_negative_returns_score_below_0_2
  - test_obvious_positive_returns_score_above_0_8
  - test_ambiguous_bug_returns_score_between_0_2_and_0_8
  - test_cve_scored_as_obvious_negative
  - test_crash_keyword_scored_as_obvious_positive
  - test_borderline_bug_scored_as_ambiguous

tests/unit/test_llm_config.py (NEW)
  - test_detect_backend_default_uses_global_model
  - test_detect_backend_filter_phase_uses_filter_model
  - test_detect_backend_map_phase_uses_map_model
  - test_detect_backend_analyze_phase_uses_analyze_model
  - test_fallback_to_global_when_phase_config_missing

tests/unit/test_semantic_cache.py (NEW)
  - test_cache_miss_returns_none
  - test_cache_hit_returns_filter_result
  - test_cache_respects_distance_threshold
  - test_cache_expires_after_30_days
  - test_different_component_same_pattern_hits_cache

tests/unit/test_llm_batch.py (NEW — mocked API)
  - test_batch_filter_collects_and_submits_requests
  - test_batch_returns_results_in_input_order
  - test_batch_fallback_to_sequential_on_failure
  - test_batch_poll_timeout_triggers_fallback
  - test_batch_disabled_uses_sequential
```

**Phase 3 — Harness improvements:**

```
tests/unit/test_llm_tools.py (NEW)
  - test_filter_bug_llm_returns_typed_filter_result
  - test_filter_bug_llm_returns_observation_with_status
  - test_filter_bug_llm_escalates_on_low_confidence
  - test_filter_bug_llm_truncates_description_to_budget
  - test_map_match_llm_returns_typed_scenario_match
  - test_analyze_gap_llm_returns_typed_gap_analysis
  - test_json_parse_error_triggers_retry_then_fallback
  - test_timeout_triggers_retry_then_fallback
  - test_context_budget_enforced_per_phase

tests/unit/test_observation.py (NEW)
  - test_observation_next_actions_drives_pipeline_flow
  - test_error_observation_contains_recovery_hint
  - test_warning_observation_triggers_escalation

tests/unit/test_run_metrics.py (NEW)
  - test_metrics_track_retries
  - test_metrics_track_cache_hits
  - test_metrics_compute_cost_per_gap
  - test_metrics_stored_in_neo4j
```

### 4.3 Testing rules for the refactor

1. **Regression coverage required** — Every modified file must have its existing tests pass after changes. No "fix later" exceptions.

2. **Edge cases must be explicit** — Each error recovery contract (Section 3.3) gets a dedicated test. If the contract says "retry once then fallback," the test verifies: (a) first call fails, (b) retry fires, (c) retry fails, (d) fallback activates, (e) correct result returned.

3. **Interface boundary tests** — Every typed tool function gets a test that validates: input types accepted, output type returned, observation shape correct. These are the contract tests.

4. **LLM calls are always mocked in unit tests** — Use `unittest.mock.patch` on `call_llm`. Never hit a real API in unit tests. Integration tests with real APIs go in `tests/integration/` and are marked `@pytest.mark.integration`.

5. **Neo4j tests use testcontainers or skip** — Unit tests for Neo4j mock the driver. Integration tests use `testcontainers-python` to spin up a real Neo4j container, or skip with `@pytest.mark.skipif(not neo4j_available)`.

6. **Target: 80%+ coverage on modified files** — Run `pytest --cov=src --cov-report=term-missing` after each phase.

### 4.4 Test execution

```bash
# Unit tests (no external deps, fast)
PYTHONPATH=. pytest tests/unit/ -v

# Integration tests (requires Neo4j container)
docker compose up -d neo4j
PYTHONPATH=. pytest tests/integration/ -v -m integration

# Coverage report
PYTHONPATH=. pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Part 5: Backend Patterns

### 5.1 Repository Protocol for Memory

`Neo4jStore` is the single backend, but its interface is implicit. Define a protocol so the contract is explicit and testable with fakes.

```python
from typing import Protocol

class MemoryRepository(Protocol):
    def connect(self) -> bool: ...
    def remember_result(self, result: AgentResult) -> dict: ...
    def get_analyzed_bug_keys(self) -> set[str]: ...
    def is_bug_analyzed(self, bug_key: str) -> bool: ...
    def update_bug_statuses(self, bugs: list[Bug]) -> dict: ...
    def get_open_gaps(self) -> list[dict]: ...
    def get_similar_resolved_bugs(self, component: str) -> list[dict]: ...
    def mark_gap_resolved(self, bug_key: str, issue_url: str) -> None: ...
    def get_run_history(self, limit: int = 20) -> list[dict]: ...
    def store_run_metrics(self, metrics: RunMetrics) -> None: ...
    def close(self) -> None: ...
```

`Neo4jStore` implements this protocol. `base_agent.py` depends on the protocol, not the concrete class. Tests can use a `FakeMemoryRepository` that stores in-memory dicts — no Neo4j container needed for unit tests.

### 5.2 Retry with Exponential Backoff

The error recovery contracts (Part 3.3) specify "retry once" but don't define timing. Add backoff for LLM calls and batch polling.

```python
import time

def call_with_retry(
    fn,
    max_retries: int = 2,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
) -> tuple[any, int]:
    """Call fn with exponential backoff. Returns (result, retries_used)."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return fn(), attempt
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                time.sleep(delay)
    raise last_error
```

Applied per phase:

| Phase | Max retries | Base delay | Rationale |
|-------|-------------|------------|-----------|
| FILTER (Sonnet) | 1 | 1s | Fast model, quick retry |
| FILTER escalation (Opus) | 0 | — | Final authority, no retry |
| MAP (Sonnet) | 1 | 1s | Has distance fallback if both fail |
| ANALYZE (Opus) | 1 | 2s | Expensive call, longer delay |
| Batch API poll | 180 | 10s | 30-min timeout, steady polling |

### 5.3 Cache-Aside for Semantic Cache

Formalize the semantic cache as the Cache-Aside pattern:

```python
class SemanticFilterCache:
    """Cache-Aside pattern over ChromaDB for FILTER results."""

    def __init__(self, chroma: ChromaStore, max_distance: float = 0.15, ttl_days: int = 30):
        self._collection = chroma._client.get_or_create_collection(
            name="filter_cache",
            metadata={"hnsw:space": "cosine"},
        )
        self._max_distance = max_distance
        self._ttl_days = ttl_days

    def get(self, bug_summary: str) -> FilterResult | None:
        """Check cache. Returns FilterResult on hit, None on miss."""
        if self._collection.count() == 0:
            return None
        results = self._collection.query(
            query_texts=[bug_summary], n_results=1,
        )
        if not results["documents"][0]:
            return None
        distance = results["distances"][0][0]
        if distance > self._max_distance:
            return None
        metadata = results["metadatas"][0][0]
        if self._is_expired(metadata.get("cached_at", "")):
            return None
        return self._to_filter_result(metadata)

    def put(self, bug_summary: str, result: FilterResult) -> None:
        """Store classification in cache after LLM call."""
        self._collection.upsert(
            ids=[f"cache_{hash(bug_summary)}"],
            documents=[bug_summary],
            metadatas=[{
                "chaos_relevant": result.chaos_relevant,
                "failure_mode": result.failure_mode or "",
                "injection_method": result.injection_method or "",
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }],
        )
```

### 5.4 Structured Logging for Run Metrics

Replace ad-hoc `logger.info()` calls with structured log entries that can be parsed by monitoring tools.

```python
import json
import logging

class StructuredLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def log_phase(self, phase: str, observation: Observation, **context) -> None:
        entry = {
            "phase": phase,
            "status": observation.status,
            "summary": observation.summary,
            **context,
        }
        self._logger.info(json.dumps(entry))
```

Usage in the pipeline:
```python
slog = StructuredLogger("coordinator")

# After FILTER
slog.log_phase("filter", observation, bug_key="OCPBUGS-1234", model="sonnet", tokens=2700)

# After ANALYZE
slog.log_phase("analyze", observation, bug_key="OCPBUGS-1234", model="opus", confidence=75)
```

This makes `RunMetrics` trivially constructable from the structured log — count entries by phase, sum tokens, count retries.

---

## Dependencies

No new Python packages required. All optimizations use existing Anthropic SDK features:
- `cache_control` — already supported in `anthropic` SDK
- `MessageBatch` — already supported in `anthropic` SDK
- ChromaDB — already installed, add one new collection for semantic cache

Neo4j, ChromaDB, and the Anthropic SDK versions stay as-is.
