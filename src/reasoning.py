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


ANALYZE_SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes using the krkn chaos testing framework.

You are given:
1. A JIRA bug with its failure mode
2. The closest existing krkn scenario (if any — partial match or no match)
3. Relevant OCP architecture documentation
4. Available krkn plugins and their capabilities
5. Previously resolved similar bugs (from Neo4j history)

Your job: analyze the gap and produce a SPECIFIC recommendation for how to fill it.

Scoring guide (0-100):
- Can you explain exact reproduction steps? (+20)
- Is there an existing scenario to extend? (+25)
- Do you understand HOW the component fails from the docs? (+20)
- Is there a krkn plugin that injects this exact failure? (+15)
- Does this match the agent's domain? (+10)
- Have we solved a similar bug before? (+10)

For modifications, be SPECIFIC:
- BAD: "extend the etcd scenario"
- GOOD: "Add a test case to scenarios/openshift/etcd.yml that deploys CPU hog pods on master nodes (use hog_scenarios plugin with cpu target 80%, duration 300s). While hog is running, check etcd operator status with: oc get co/etcd -o jsonpath='{.status.conditions}'. Assert: etcd should NOT report Degraded=True while members are actually healthy."

Respond with ONLY a JSON object:
{
  "confidence_score": 0-100,
  "reasoning": "Detailed explanation of the score breakdown and analysis",
  "modifications": ["specific step 1", "specific step 2", ...],
  "krkn_plugin": "exact plugin name" or null,
  "repos_to_update": ["krkn", "krkn-hub", "website"]
}"""


def llm_analyze_gap(
    bug: Bug,
    match: ScenarioMatch,
    ocp_docs: list[dict],
    krkn_docs: list[dict],
    neo4j_history: list[dict],
    config: LLMBackendConfig | None = None,
) -> GapAnalysis:
    """Use LLM to analyze a coverage gap and produce specific recommendations.

    Args:
        bug: The JIRA bug.
        match: ScenarioMatch from MAP phase (PARTIAL or NO_MATCH).
        ocp_docs: ChromaDB OCP doc search results for component context.
        krkn_docs: ChromaDB krkn doc search results for available plugins.
        neo4j_history: Similar resolved bugs from Neo4j.
        config: LLM backend config. Auto-detected if None.

    Returns:
        GapAnalysis with LLM-generated confidence score, reasoning, and modifications.
    """
    if config is None:
        config = detect_llm_backend()

    ocp_context = "\n---\n".join(
        hit["text"][:400] for hit in ocp_docs[:3]
    ) or "No OCP documentation found."

    krkn_context = "\n---\n".join(
        hit["text"][:400] for hit in krkn_docs[:3]
    ) or "No krkn plugin documentation found."

    history_context = "\n".join(
        f"- {h.get('bug_key', '?')}: {h.get('summary', '?')[:60]} → {h.get('issue_url', 'N/A')}"
        for h in neo4j_history[:5]
    ) or "No similar resolved bugs found."

    scenario_context = f"Closest scenario: {match.matched_scenario}" if match.matched_scenario else "No matching scenario found."

    prompt = f"""Bug: {bug.key}
Component: {bug.component}
Summary: {bug.summary}
Description: {bug.description[:1000] if bug.description else 'No description'}

Match result: {match.match_result.value}
{scenario_context}

OCP Architecture Documentation:
{ocp_context}

Available krkn Plugins:
{krkn_context}

Previously Resolved Similar Bugs:
{history_context}

Analyze this gap. Score confidence and provide SPECIFIC modifications."""

    try:
        text = call_llm(
            messages=[
                {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            config=config,
        )

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        score = min(100, max(0, int(result.get("confidence_score", 0))))

        reasoning_parts = []
        if result.get("reasoning"):
            reasoning_parts.append(result["reasoning"])
        if result.get("krkn_plugin"):
            reasoning_parts.append(f"krkn plugin: {result['krkn_plugin']}")
        if result.get("repos_to_update"):
            reasoning_parts.append(f"Repos: {', '.join(result['repos_to_update'])}")
        reasoning = "; ".join(reasoning_parts)

        modifications = result.get("modifications", [])
        if not isinstance(modifications, list):
            modifications = [str(modifications)]

        if score >= 70:
            confidence = Confidence.HIGH
            action = ActionType.DRAFT_PR
        elif score >= 40:
            confidence = Confidence.MEDIUM
            action = ActionType.GITHUB_ISSUE
        else:
            confidence = Confidence.LOW
            action = ActionType.GITHUB_ISSUE

        return GapAnalysis(
            bug=bug,
            confidence_score=score,
            confidence_level=confidence,
            action_type=action,
            reasoning=reasoning,
            base_scenario=match.matched_scenario,
            modifications=modifications,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM ANALYZE failed for %s (bad response), falling back: %s", bug.key, e)
        return _fallback_analyze(bug, match)
    except Exception as e:
        logger.warning("LLM ANALYZE failed for %s, falling back: %s", bug.key, e)
        return _fallback_analyze(bug, match)


def _fallback_analyze(bug: Bug, match: ScenarioMatch) -> GapAnalysis:
    """Keyword-based fallback when LLM is unavailable."""
    score = 0
    reasoning_parts = []

    if bug.description and len(bug.description) > 200:
        score += 20
        reasoning_parts.append("Clear repro steps (+20)")

    if match.match_result == MatchResult.PARTIAL_MATCH:
        score += 25
        reasoning_parts.append(f"Partial match: {match.matched_scenario} (+25)")

    failure_keywords = [
        "timeout", "crash", "unavailable", "degraded", "unhealthy",
        "not cleared", "failure", "failed", "outage", "disruption",
        "quorum", "leader election", "not ready", "eviction",
    ]
    if any(kw in bug.summary.lower() for kw in failure_keywords):
        score += 20
        reasoning_parts.append("Known failure mode (+20)")

    if score >= 70:
        confidence = Confidence.HIGH
        action = ActionType.DRAFT_PR
    elif score >= 40:
        confidence = Confidence.MEDIUM
        action = ActionType.GITHUB_ISSUE
    else:
        confidence = Confidence.LOW
        action = ActionType.GITHUB_ISSUE

    modifications = []
    if match.matched_scenario:
        modifications.append(f"Extend {match.matched_scenario}")

    return GapAnalysis(
        bug=bug,
        confidence_score=score,
        confidence_level=confidence,
        action_type=action,
        reasoning="; ".join(reasoning_parts),
        base_scenario=match.matched_scenario,
        modifications=modifications,
    )
