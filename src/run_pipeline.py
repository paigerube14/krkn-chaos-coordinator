"""End-to-end pipeline runner — standalone script to test the full flow."""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.filter.chaos_filter import filter_bugs
from src.knowledge.chromadb_store import ChromaStore, DocChunk
from src.knowledge.scenario_index import index_scenarios_from_repo
from src.models import (
    ActionType,
    AgentResult,
    Bug,
    Confidence,
    GapAnalysis,
    MatchResult,
    ScenarioMatch,
)
from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.agents.act import create_issues_for_gaps, build_issue_title, build_issue_body

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def bugs_from_jira_json(jira_data: dict) -> list[Bug]:
    """Parse JIRA MCP response into Bug objects."""
    bugs = []
    nodes = jira_data.get("issues", {}).get("nodes", [])
    for issue in nodes:
        fields = issue["fields"]
        components = fields.get("components", [])
        comp_name = components[0]["name"] if components else "Unknown"
        desc = fields.get("description", "") or ""

        bugs.append(Bug(
            key=issue["key"],
            summary=fields.get("summary", ""),
            description=desc,
            component=comp_name,
            priority=fields.get("priority", {}).get("name", "Unknown"),
            status=fields.get("status", {}).get("name", "Unknown"),
            created=fields.get("created", ""),
            url=f"https://redhat.atlassian.net/browse/{issue['key']}",
        ))
    return bugs


def run_pipeline(jira_json_path: str, krkn_repo_path: str | None = None) -> AgentResult:
    """Run the full DISCOVER → FILTER → MAP → ANALYZE pipeline.

    Args:
        jira_json_path: Path to saved JIRA MCP JSON response
        krkn_repo_path: Path to local krkn repo
    """
    if krkn_repo_path is None:
        krkn_repo_path = os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    # DISCOVER
    logger.info("=== DISCOVER ===")
    with open(jira_json_path) as f:
        jira_data = json.load(f)
    bugs = bugs_from_jira_json(jira_data)
    logger.info("Found %d bugs", len(bugs))

    # FILTER
    logger.info("=== FILTER ===")
    relevant, skipped = filter_bugs(bugs)

    # INDEX scenarios
    logger.info("=== INDEX ===")
    scenarios = index_scenarios_from_repo(Path(krkn_repo_path))
    chroma = ChromaStore(persist_dir="/tmp/krkn_chroma_pipeline")
    chunks = [
        DocChunk(
            text=f"{s.scenario_type}: {s.file_path} ({s.description})",
            component=s.plugin_name,
            doc_type="scenario",
            source="krkn",
        )
        for s in scenarios
    ]
    chroma.add_scenario_docs(chunks)

    # MAP
    logger.info("=== MAP ===")
    matched = []
    unmatched = []

    for filter_result in relevant:
        bug = filter_result.bug
        query = f"{bug.component} {bug.summary}"
        chroma_results = chroma.search_scenarios(query, n_results=5)

        comp_lower = bug.component.lower()
        matching_scenarios = [
            s for s in scenarios
            if comp_lower in s.name.lower()
            or comp_lower in s.scenario_type.lower()
            or any(kw in s.file_path.lower() for kw in comp_lower.split())
        ]

        if matching_scenarios and chroma_results and chroma_results[0]["distance"] < 0.3:
            sm = ScenarioMatch(
                bug=bug, match_result=MatchResult.FULL_MATCH,
                matched_scenario=matching_scenarios[0].file_path,
                matched_repo="krkn-chaos/krkn",
                similarity_score=1.0 - chroma_results[0]["distance"],
            )
            matched.append(sm)
        elif matching_scenarios:
            sm = ScenarioMatch(
                bug=bug, match_result=MatchResult.PARTIAL_MATCH,
                matched_scenario=matching_scenarios[0].file_path,
                matched_repo="krkn-chaos/krkn",
            )
            unmatched.append(sm)
        else:
            sm = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
            unmatched.append(sm)

    # ANALYZE
    logger.info("=== ANALYZE ===")
    gaps = []
    for match in unmatched:
        bug = match.bug
        score = 0
        reasons = []

        if bug.description and len(bug.description) > 200:
            score += 20
            reasons.append("Clear repro steps (+20)")
        if match.match_result == MatchResult.PARTIAL_MATCH:
            score += 25
            reasons.append(f"Partial match: {match.matched_scenario} (+25)")
        if any(kw in bug.summary.lower() for kw in [
            "timeout", "crash", "unavailable", "degraded", "unhealthy",
            "not cleared", "failure", "failed",
        ]):
            score += 20
            reasons.append("Known failure mode (+20)")
        score += 10
        reasons.append("Domain match (+10)")

        confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
        action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE

        modifications = []
        if match.matched_scenario:
            modifications.append(f"Extend {match.matched_scenario}")

        gaps.append(GapAnalysis(
            bug=bug,
            confidence_score=score,
            confidence_level=confidence,
            action_type=action,
            reasoning="; ".join(reasons),
            base_scenario=match.matched_scenario,
            modifications=modifications,
        ))

    result = AgentResult(
        agent_name="control_plane",
        bugs_discovered=bugs,
        bugs_filtered_out=skipped,
        bugs_matched=matched,
        gaps=gaps,
    )

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m src.run_pipeline <jira_json_path> [krkn_repo_path]")
        sys.exit(1)

    jira_path = sys.argv[1]
    krkn_path = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))

    result = run_pipeline(jira_path, krkn_path)

    print()
    print(format_summary([result]))
    print()

    gaps = deduplicate_gaps([result])
    if gaps:
        print(format_approval_queue(gaps))
        print()
        # Dry run: show what issues would be created
        create_issues_for_gaps(
            github=None,
            gaps=gaps,
            agent_name="control_plane",
            dry_run=True,
        )
