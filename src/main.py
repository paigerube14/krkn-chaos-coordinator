"""Main entry point for krkn-chaos-coordinator."""

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.agents.base_agent import BaseDomainAgent
from src.agents.registry import discover_agents
from src.apis.jira_client import JiraClient, JiraConfig
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.scenario_index import index_scenarios_from_repo

LOG_FILE = "krkn-chaos-coordinator.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    filename=LOG_FILE,
    filemode="w",
)
logger = logging.getLogger(__name__)


def main():
    load_dotenv()

    registered = discover_agents()
    agent_names_str = ", ".join(sorted(registered.keys()))

    parser = argparse.ArgumentParser(description="krkn-chaos-coordinator")
    parser.add_argument(
        "--release", default="4.21",
        help="OCP release(s) to analyze. Comma-separated for multiple (e.g. '4.20,4.21'). Default: 4.21",
    )
    parser.add_argument(
        "--agent", default=None,
        help=(
            f"Agent(s) to run. Comma-separated for multiple (e.g. 'control_plane,networking'). "
            f"'all' or omit for all agents. Available: {agent_names_str}"
        ),
    )
    parser.add_argument(
        "--max-bugs", type=int, default=2000, help="Max bugs per agent from JIRA (default: 2000)"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Look back N days for bugs (default: 14)"
    )
    parser.add_argument(
        "--use-llm", action="store_true", default=False,
        help="Enable LLM-enhanced filter/map/analyze (uses tiered model routing)",
    )
    parser.add_argument(
        "--krkn-repo",
        default=os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn")),
        help="Path to local krkn repo (env: KRKN_REPO_PATH)",
    )
    args = parser.parse_args()

    # Initialize API clients
    jira = JiraClient(
        JiraConfig(
            url=os.environ.get("JIRA_URL", "https://redhat.atlassian.net"),
            username=os.environ.get("JIRA_USERNAME", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
        )
    )
    sippy = SippyClient()
    github = GitHubClient(token=os.environ.get("GITHUB_TOKEN", ""))

    # Initialize knowledge layer
    chroma = ChromaStore(persist_dir="./chroma_data")
    scenarios = index_scenarios_from_repo(Path(args.krkn_repo))

    logger.info("Indexed %d scenarios from %s", len(scenarios), args.krkn_repo)

    # Parse releases and agents
    releases = [r.strip() for r in args.release.split(",") if r.strip()]
    logger.info("Target release(s): %s", ", ".join(releases))

    if args.agent and args.agent.lower() != "all":
        agent_names = [a.strip() for a in args.agent.split(",") if a.strip()]
        unknown = [a for a in agent_names if a not in registered]
        if unknown:
            print(f"Unknown agent(s): {', '.join(unknown)}. Available: {agent_names_str}")
            return
    else:
        agent_names = sorted(registered.keys())

    logger.info("Agent(s): %s", ", ".join(agent_names))

    # Connect Neo4j (required — no JSON fallback)
    from src.knowledge.neo4j_store import Neo4jStore
    neo4j_store = Neo4jStore(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
    )
    if not neo4j_store.connect():
        logger.error(
            "Neo4j is required. Start it with: podman start neo4j-coordinator"
        )
        return
    logger.info("Neo4j connected — REMEMBER phase will use knowledge graph")

    # Run each agent × release combination
    all_results = []
    for release in releases:
        agent_kwargs = {
            "jira": jira,
            "sippy": sippy,
            "github": github,
            "chroma": chroma,
            "scenarios": scenarios,
            "release": release,
            "neo4j_store": neo4j_store,
            "use_llm": args.use_llm,
        }

        for agent_name in agent_names:
            agent = BaseDomainAgent(agent_name=agent_name, **agent_kwargs)
            result = agent.run()
            all_results.append(result)

    # Orchestrator: deduplicate and format
    gaps = deduplicate_gaps(all_results)

    print(format_summary(all_results))
    print()
    if gaps:
        print(format_approval_queue(gaps))
    else:
        print("No chaos test coverage gaps identified.")


if __name__ == "__main__":
    main()
