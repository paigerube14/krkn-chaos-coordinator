"""Main entry point for krkn-chaos-coordinator."""

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.apis.jira_client import JiraClient, JiraConfig
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.agents.control_plane_agent import ControlPlaneAgent
from src.agents.upgrade_lifecycle_agent import UpgradeLifecycleAgent
from src.agents.node_machine_agent import NodeMachineAgent
from src.agents.networking_agent import NetworkingAgent
from src.agents.storage_agent import StorageAgent
from src.agents.operators_platform_agent import OperatorsPlatformAgent
from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.scenario_index import index_scenarios_from_repo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

AGENT_CLASSES = {
    "control_plane": ControlPlaneAgent,
    "upgrade_lifecycle": UpgradeLifecycleAgent,
    "node_machine": NodeMachineAgent,
    "networking": NetworkingAgent,
    "storage": StorageAgent,
    "operators_platform": OperatorsPlatformAgent,
}


def main():
    parser = argparse.ArgumentParser(description="krkn-chaos-coordinator")
    parser.add_argument("--release", default="4.21", help="OCP release to analyze")
    parser.add_argument(
        "--agent", default=None,
        help=f"Run a single agent ({', '.join(AGENT_CLASSES.keys())}). Default: all.",
    )
    parser.add_argument(
        "--max-bugs", type=int, default=200, help="Max bugs per agent from JIRA (default: 200)"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Look back N days for bugs (default: 14)"
    )
    parser.add_argument(
        "--krkn-repo", default=str(Path.home() / "krkn"), help="Path to local krkn repo"
    )
    args = parser.parse_args()

    load_dotenv()

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
    logger.info("Target release: %s", args.release)

    # Connect Neo4j (optional — falls back to JSON if unavailable)
    from src.knowledge.neo4j_store import Neo4jStore
    neo4j_store = Neo4jStore(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", "password"),
    )
    neo4j_connected = neo4j_store.connect()
    if neo4j_connected:
        logger.info("Neo4j connected — REMEMBER phase will use knowledge graph")
    else:
        logger.info("Neo4j not available — using JSON memory fallback")
        neo4j_store = None

    # Build agents
    agent_kwargs = {
        "jira": jira,
        "sippy": sippy,
        "github": github,
        "chroma": chroma,
        "scenarios": scenarios,
        "release": args.release,
        "neo4j_store": neo4j_store,
    }

    if args.agent:
        if args.agent not in AGENT_CLASSES:
            print(f"Unknown agent: {args.agent}. Available: {', '.join(AGENT_CLASSES.keys())}")
            return
        agents_to_run = [AGENT_CLASSES[args.agent](**agent_kwargs)]
    else:
        agents_to_run = [cls(**agent_kwargs) for cls in AGENT_CLASSES.values()]

    # Run agents
    results = []
    for agent in agents_to_run:
        result = agent.run()
        results.append(result)

    # Orchestrator: deduplicate and format
    gaps = deduplicate_gaps(results)

    print(format_summary(results))
    print()
    if gaps:
        print(format_approval_queue(gaps))
    else:
        print("No chaos test coverage gaps identified.")


if __name__ == "__main__":
    main()
