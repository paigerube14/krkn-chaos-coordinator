"""Run pipeline with pre-filtered bugs (filter done by Claude Code)."""

import json
import logging
import os
import sys
from pathlib import Path

from src.knowledge.chromadb_store import ChromaStore, DocChunk
from src.knowledge.neo4j_store import Neo4jStore
from src.knowledge.scenario_index import index_scenarios_from_repo
from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.agents.act import create_issues_for_gaps, build_issue_title, build_issue_body
from src.models import (
    ActionType, AgentResult, Bug, Confidence, FilterResult,
    GapAnalysis, MatchResult, ScenarioMatch,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def run_from_filtered(
    filtered_bugs: list[dict],
    krkn_repo_path: str | None = None,
) -> AgentResult:
    """Run MAP → ANALYZE on pre-filtered bugs.

    Args:
        filtered_bugs: List of dicts with keys:
            key, summary, description, component, priority, status, url,
            failure_mode, injection_method
        krkn_repo_path: Path to local krkn repo
    """
    if krkn_repo_path is None:
        krkn_repo_path = os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    # Convert to Bug objects
    bugs = []
    filter_results = []
    for fb in filtered_bugs:
        bug = Bug(
            key=fb["key"], summary=fb["summary"],
            description=fb.get("description", ""),
            component=fb["component"],
            priority=fb.get("priority", "Unknown"),
            status=fb.get("status", "Unknown"),
            created=fb.get("created", ""),
            url=fb.get("url", ""),
        )
        bugs.append(bug)
        filter_results.append(FilterResult(
            bug=bug, chaos_relevant=True,
            failure_mode=fb.get("failure_mode"),
            injection_method=fb.get("injection_method"),
        ))

    # Setup knowledge layer
    scenarios = index_scenarios_from_repo(Path(krkn_repo_path))
    chroma = ChromaStore(persist_dir="./chroma_data")

    # MAP
    logger.info("MAP: matching %d bugs against %d scenarios", len(bugs), len(scenarios))
    matched, unmatched = [], []
    for fr in filter_results:
        bug = fr.bug
        query = f"{bug.component} {bug.summary} {fr.failure_mode or ''}"
        scenario_hits = chroma.search_scenarios(query, n_results=5)
        doc_hits = chroma.search_krkn_docs(query, n_results=5)

        comp_lower = bug.component.lower()
        matching = [
            s for s in scenarios
            if comp_lower in s.name.lower()
            or comp_lower in s.scenario_type.lower()
            or any(kw in s.file_path.lower() for kw in comp_lower.split())
        ]

        best_dist = min(
            scenario_hits[0].get("distance", 1.0) if scenario_hits else 1.0,
            doc_hits[0].get("distance", 1.0) if doc_hits else 1.0,
        )

        best_path = matching[0].file_path if matching else None
        if not best_path and scenario_hits:
            text = scenario_hits[0].get("text", "")
            if "Scenario file:" in text:
                best_path = text.split("Scenario file:")[1].split("\n")[0].strip()

        if best_dist < 0.35 and best_path:
            matched.append(ScenarioMatch(bug=bug, match_result=MatchResult.FULL_MATCH,
                matched_scenario=best_path, matched_repo="krkn-chaos/krkn",
                similarity_score=1.0 - best_dist))
        elif best_dist < 0.65 or matching:
            unmatched.append(ScenarioMatch(bug=bug, match_result=MatchResult.PARTIAL_MATCH,
                matched_scenario=best_path, matched_repo="krkn-chaos/krkn",
                similarity_score=1.0 - best_dist))
        else:
            unmatched.append(ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH))

    # ANALYZE
    logger.info("ANALYZE: scoring %d unmatched bugs", len(unmatched))
    gaps = []
    for match in unmatched:
        bug = match.bug
        score, reasons = 0, []
        if bug.description and len(bug.description) > 200:
            score += 20; reasons.append("Clear repro (+20)")
        if match.match_result == MatchResult.PARTIAL_MATCH:
            score += 25; reasons.append(f"Partial: {match.matched_scenario} (+25)")
        failure_kws = ["timeout", "crash", "unavailable", "degraded", "unhealthy",
                       "not cleared", "failure", "failed", "outage", "disruption"]
        if any(kw in bug.summary.lower() for kw in failure_kws):
            score += 20; reasons.append("Known failure mode (+20)")
        doc_hits = chroma.search_krkn_docs(f"{bug.component} {bug.summary}", n_results=1)
        if doc_hits and doc_hits[0].get("distance", 1.0) < 0.5:
            score += 15; reasons.append("Similar pattern in krkn docs (+15)")
        score += 10; reasons.append("Domain match (+10)")

        confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
        action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
        modifications = [f"Extend {match.matched_scenario}"] if match.matched_scenario else []

        gaps.append(GapAnalysis(bug=bug, confidence_score=score, confidence_level=confidence,
            action_type=action, reasoning="; ".join(reasons),
            base_scenario=match.matched_scenario, modifications=modifications))

    result = AgentResult(
        agent_name="claude_code_filtered",
        bugs_discovered=bugs,
        bugs_matched=matched,
        gaps=gaps,
    )

    # REMEMBER (Neo4j)
    neo4j_store = Neo4jStore(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
    )
    if neo4j_store.connect():
        neo4j_store.remember_result(result)
        neo4j_store.close()
    else:
        logger.warning("Neo4j not available — results not persisted")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.run_filtered <filtered_bugs.json>")
        print("JSON format: [{\"key\": \"OCPBUGS-123\", \"summary\": \"...\", \"component\": \"Etcd\", "
              "\"description\": \"...\", \"failure_mode\": \"...\", \"injection_method\": \"...\"}]")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        filtered = json.load(f)

    result = run_from_filtered(filtered)
    gaps = deduplicate_gaps([result])

    print(format_summary([result]))
    print()
    if gaps:
        print(format_approval_queue(gaps))
        for gap in gaps:
            print(f"\n{'='*60}")
            print(build_issue_title(gap))
            print(f"{'='*60}")
            print(build_issue_body(gap, "claude_code_filtered"))
