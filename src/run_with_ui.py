"""Run krkn-chaos-coordinator with Rich terminal UI."""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from rich.live import Live

from src.filter.chaos_filter import filter_bugs
from src.knowledge.chromadb_store import ChromaStore, DocChunk
from src.knowledge.scenario_index import index_scenarios_from_repo
from src.coordinator.orchestrator import deduplicate_gaps
from src.agents.act import create_issues_for_gaps
from src.models import (
    ActionType, AgentResult, Bug, Confidence,
    GapAnalysis, MatchResult, ScenarioMatch,
)
from src.ui.terminal_ui import AgentStatus, TerminalUI


def bugs_from_jira_json(jira_data: dict) -> list[Bug]:
    """Parse JIRA MCP response into Bug objects."""
    bugs = []
    for issue in jira_data.get("issues", {}).get("nodes", []):
        fields = issue["fields"]
        components = fields.get("components", [])
        comp_name = components[0]["name"] if components else "Unknown"
        desc = fields.get("description", "") or ""
        bugs.append(Bug(
            key=issue["key"], summary=fields.get("summary", ""),
            description=desc, component=comp_name,
            priority=fields.get("priority", {}).get("name", "Unknown"),
            status=fields.get("status", {}).get("name", "Unknown"),
            created=fields.get("created", ""),
            url=f"https://redhat.atlassian.net/browse/{issue['key']}",
        ))
    return bugs


def run_with_ui(jira_json_path: str, krkn_repo_path: str | None = None):
    """Run the full pipeline with Rich live dashboard."""
    if krkn_repo_path is None:
        krkn_repo_path = os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    ui = TerminalUI(release="4.21")
    ui.init_agents([
        "control_plane", "networking", "node_machine",
        "storage", "upgrade_lifecycle", "operators_platform",
    ])

    with Live(ui.build_layout(), refresh_per_second=4, console=ui.console) as live:

        # === CONTROL PLANE AGENT ===
        agent_name = "control_plane"
        ui.update_agent(agent_name, AgentStatus.DISCOVERING, "querying JIRA...")
        live.update(ui.build_layout())
        time.sleep(0.5)

        # DISCOVER
        with open(jira_json_path) as f:
            jira_data = json.load(f)
        bugs = bugs_from_jira_json(jira_data)
        ui.update_agent(agent_name, AgentStatus.DISCOVERING, f"{len(bugs)} bugs found")
        ui.add_feed(f"[cyan]DISCOVER:[/cyan] Found {len(bugs)} bugs for Control Plane")
        ui.set_counts(total_bugs=len(bugs))
        live.update(ui.build_layout())
        time.sleep(0.3)

        # FILTER
        ui.update_agent(agent_name, AgentStatus.FILTERING, "checking chaos relevance...")
        live.update(ui.build_layout())
        time.sleep(0.3)

        relevant, skipped = filter_bugs(bugs)

        for s in skipped:
            if "CVE" in (s.skip_reason or ""):
                ui.add_feed(f"  [red]SKIP[/red] {s.bug.key}: CVE (security patch)")
            else:
                ui.add_feed(f"  [dim]SKIP[/dim] {s.bug.key}: not chaos-relevant")
            live.update(ui.build_layout())
            time.sleep(0.1)

        for r in relevant:
            ui.add_feed(f"  [green]PASS[/green] {r.bug.key}: {r.injection_method}")
            live.update(ui.build_layout())
            time.sleep(0.1)

        ui.update_agent(agent_name, AgentStatus.FILTERING,
                        f"{len(relevant)} relevant, {len(skipped)} skipped")
        ui.set_counts(total_bugs=len(bugs), relevant=len(relevant), skipped=len(skipped))
        live.update(ui.build_layout())
        time.sleep(0.3)

        # MAP
        ui.update_agent(agent_name, AgentStatus.MAPPING, "indexing krkn scenarios...")
        live.update(ui.build_layout())

        scenarios = index_scenarios_from_repo(Path(krkn_repo_path))
        chroma = ChromaStore(persist_dir="/tmp/krkn_chroma_ui")
        chunks = [
            DocChunk(
                text=f"{s.scenario_type}: {s.file_path} ({s.description})",
                component=s.plugin_name, doc_type="scenario", source="krkn",
            )
            for s in scenarios
        ]
        chroma.add_scenario_docs(chunks)

        ui.update_agent(agent_name, AgentStatus.MAPPING,
                        f"{len(scenarios)} scenarios, matching bugs...")
        ui.add_feed(f"[cyan]MAP:[/cyan] Indexed {len(scenarios)} krkn scenarios")
        live.update(ui.build_layout())
        time.sleep(0.3)

        matched = []
        unmatched = []
        for fr in relevant:
            bug = fr.bug
            query = f"{bug.component} {bug.summary}"
            chroma_results = chroma.search_scenarios(query, n_results=5)
            comp_lower = bug.component.lower()
            matching = [
                s for s in scenarios
                if comp_lower in s.name.lower()
                or comp_lower in s.scenario_type.lower()
                or any(kw in s.file_path.lower() for kw in comp_lower.split())
            ]

            if matching and chroma_results and chroma_results[0]["distance"] < 0.3:
                sm = ScenarioMatch(bug=bug, match_result=MatchResult.FULL_MATCH,
                                   matched_scenario=matching[0].file_path,
                                   matched_repo="krkn-chaos/krkn",
                                   similarity_score=1.0 - chroma_results[0]["distance"])
                matched.append(sm)
                ui.add_feed(f"  [green]MATCH[/green] {bug.key} -> {matching[0].file_path}")
            elif matching:
                sm = ScenarioMatch(bug=bug, match_result=MatchResult.PARTIAL_MATCH,
                                   matched_scenario=matching[0].file_path,
                                   matched_repo="krkn-chaos/krkn")
                unmatched.append(sm)
                ui.add_feed(f"  [yellow]PARTIAL[/yellow] {bug.key} ~ {matching[0].file_path}")
            else:
                sm = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
                unmatched.append(sm)
                ui.add_feed(f"  [red]NO MATCH[/red] {bug.key}")
            live.update(ui.build_layout())
            time.sleep(0.2)

        # ANALYZE
        ui.update_agent(agent_name, AgentStatus.ANALYZING, "scoring confidence...")
        live.update(ui.build_layout())
        time.sleep(0.3)

        gaps = []
        for match in unmatched:
            bug = match.bug
            score = 0
            reasons = []
            if bug.description and len(bug.description) > 200:
                score += 20; reasons.append("Clear repro (+20)")
            if match.match_result == MatchResult.PARTIAL_MATCH:
                score += 25; reasons.append(f"Partial: {match.matched_scenario} (+25)")
            if any(kw in bug.summary.lower() for kw in [
                "timeout", "crash", "unavailable", "degraded", "unhealthy",
                "not cleared", "failure", "failed",
            ]):
                score += 20; reasons.append("Known failure mode (+20)")
            score += 10; reasons.append("Domain match (+10)")

            confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
            action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
            modifications = [f"Extend {match.matched_scenario}"] if match.matched_scenario else []

            gaps.append(GapAnalysis(
                bug=bug, confidence_score=score, confidence_level=confidence,
                action_type=action, reasoning="; ".join(reasons),
                base_scenario=match.matched_scenario, modifications=modifications,
            ))

            level = confidence.value.upper()
            ui.add_feed(f"  [bold]{level}[/bold] {bug.key}: score {score}/100")
            live.update(ui.build_layout())
            time.sleep(0.2)

        # ACT (dry run)
        ui.update_agent(agent_name, AgentStatus.ACTING, "preparing issues...")
        prs = sum(1 for g in gaps if g.action_type == ActionType.DRAFT_PR)
        issues = sum(1 for g in gaps if g.action_type == ActionType.GITHUB_ISSUE)
        ui.set_counts(
            total_bugs=len(bugs), relevant=len(relevant), skipped=len(skipped),
            gaps=len(gaps), issues=issues, prs=prs,
        )
        live.update(ui.build_layout())
        time.sleep(0.5)

        # Mark done
        ui.update_agent(agent_name, AgentStatus.DONE,
                        f"{len(gaps)} gaps, {prs} PRs, {issues} issues")
        ui.add_feed(f"[green]COMPLETE:[/green] Control Plane agent finished")

        # Mark other agents as not run
        for other in ["networking", "node_machine", "storage", "upgrade_lifecycle", "operators_platform"]:
            ui.update_agent(other, AgentStatus.WAITING, "not configured yet")

        live.update(ui.build_layout())
        time.sleep(1)

    # Build result
    result = AgentResult(
        agent_name="control_plane",
        bugs_discovered=bugs,
        bugs_filtered_out=skipped,
        bugs_matched=matched,
        gaps=gaps,
    )

    # Render final results
    all_gaps = deduplicate_gaps([result])
    ui.render_final_results([result], all_gaps)


if __name__ == "__main__":
    jira_path = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/jira_etcd_bugs.json"
    krkn_path = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    run_with_ui(jira_path, krkn_path)
