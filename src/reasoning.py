"""LLM reasoning for MAP and ANALYZE phases.

ChromaDB retrieves candidate docs/scenarios → LLM reasons over those results.
Same RAG pattern as the FILTER phase.
"""

from __future__ import annotations

import json
import logging

from src.filter.llm_config import LLMBackendConfig, detect_llm_backend
from src.filter.llm_filter import call_llm
from src.models import (
    Bug,
    Confidence,
    ActionType,
    FilterResult,
    GapAnalysis,
    MatchResult,
    ScenarioMatch,
)

logger = logging.getLogger(__name__)

MAP_SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes.

You are given:
1. A JIRA bug with its failure mode and injection method
2. Existing krkn chaos scenario configs (from ChromaDB search)
3. Relevant documentation context

Your job: determine if any existing krkn scenario ALREADY covers the exact failure mode in this bug.

Rules:
- FULL_MATCH: A scenario tests this EXACT failure mode. The bug's failure condition is already injected by the scenario.
- PARTIAL_MATCH: A scenario targets the same component or similar failure, but does NOT cover this specific failure mode. Example: scenario kills etcd pods, but the bug is about etcd under CPU stress.
- NO_MATCH: No scenario is related to this failure mode at all.

Be strict about FULL_MATCH — the scenario must inject the same type of disruption that triggers the bug. Same component is not enough.

Respond with ONLY a JSON object:
{
  "match": "FULL_MATCH" | "PARTIAL_MATCH" | "NO_MATCH",
  "matched_scenario": "path/to/scenario.yaml" or null,
  "explanation": "What the closest scenario tests vs what the bug describes"
}"""


def llm_map_match(
    bug: Bug,
    filter_result: FilterResult,
    scenario_hits: list[dict],
    doc_hits: list[dict],
    config: LLMBackendConfig | None = None,
) -> ScenarioMatch:
    """Use LLM to determine if existing scenarios cover a bug's failure mode.

    Args:
        bug: The JIRA bug to match.
        filter_result: FILTER output with failure_mode and injection_method.
        scenario_hits: ChromaDB scenario search results.
        doc_hits: ChromaDB doc search results for context.
        config: LLM backend config. Auto-detected if None.

    Returns:
        ScenarioMatch with LLM-reasoned match result.
    """
    if config is None:
        config = detect_llm_backend()

    scenario_context = "\n---\n".join(
        hit["text"][:500] for hit in scenario_hits[:5]
    ) or "No matching scenarios found."

    doc_context = "\n---\n".join(
        hit["text"][:300] for hit in doc_hits[:3]
    ) or "No relevant documentation found."

    prompt = f"""Bug: {bug.key}
Component: {bug.component}
Summary: {bug.summary}
Failure Mode: {filter_result.failure_mode or 'unknown'}
Injection Method: {filter_result.injection_method or 'unknown'}
Description: {bug.description[:800] if bug.description else 'No description'}

Existing krkn scenarios (from search):
{scenario_context}

Relevant OCP/krkn documentation:
{doc_context}

Does any existing scenario cover this bug's exact failure mode?"""

    try:
        text = call_llm(
            messages=[
                {"role": "system", "content": MAP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            config=config,
        )

        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        match_str = result.get("match", "NO_MATCH")
        matched_scenario = result.get("matched_scenario")

        match_result = {
            "FULL_MATCH": MatchResult.FULL_MATCH,
            "PARTIAL_MATCH": MatchResult.PARTIAL_MATCH,
        }.get(match_str, MatchResult.NO_MATCH)

        return ScenarioMatch(
            bug=bug,
            match_result=match_result,
            matched_scenario=matched_scenario,
            matched_repo="krkn-chaos/krkn" if matched_scenario else None,
            similarity_score=1.0 if match_result == MatchResult.FULL_MATCH else 0.5 if match_result == MatchResult.PARTIAL_MATCH else 0.0,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM MAP failed for %s (bad response), falling back: %s", bug.key, e)
        return _fallback_match(bug, scenario_hits)
    except Exception as e:
        logger.warning("LLM MAP failed for %s, falling back: %s", bug.key, e)
        return _fallback_match(bug, scenario_hits)


def _fallback_match(bug: Bug, scenario_hits: list[dict]) -> ScenarioMatch:
    """Threshold-based fallback when LLM is unavailable."""
    if not scenario_hits:
        return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

    best_dist = scenario_hits[0].get("distance", 1.0)
    best_text = scenario_hits[0].get("text", "")

    scenario_path = None
    if "Scenario file:" in best_text:
        scenario_path = best_text.split("Scenario file:")[1].split("\n")[0].strip()

    if best_dist < 0.35 and scenario_path:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.FULL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    if best_dist < 0.65:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.PARTIAL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
