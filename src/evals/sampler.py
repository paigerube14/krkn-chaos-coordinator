"""Stratified bug sampler for eval runs.

Supports two data sources:
- Neo4j (default): samples from the knowledge graph
- JSON (legacy): reads coordinator_memory.json
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
from collections import defaultdict

from src.models import Bug

logger = logging.getLogger(__name__)


def sample_bugs_for_eval(
    sample_size: int = 200,
    seed: int = 42,
    memory_path: str | None = None,
) -> list[Bug]:
    """Sample bugs stratified by agent domain for eval.

    Tries Neo4j first (if available), falls back to JSON file.
    """
    if memory_path:
        return _sample_from_json(memory_path, sample_size, seed)

    try:
        return _sample_from_neo4j(sample_size, seed)
    except Exception as e:
        logger.warning("Neo4j sampling failed (%s), trying JSON fallback", e)
        if os.path.exists("./coordinator_memory.json"):
            return _sample_from_json("./coordinator_memory.json", sample_size, seed)
        raise ValueError(
            "No data source available. Either connect Neo4j or provide --memory-path"
        ) from e


def _sample_from_neo4j(sample_size: int, seed: int) -> list[Bug]:
    """Sample bugs from Neo4j knowledge graph."""
    from src.knowledge.neo4j_store import Neo4jStore

    store = Neo4jStore(password=os.environ.get("NEO4J_PASSWORD", "password"))
    if not store.connect():
        raise ConnectionError("Cannot connect to Neo4j")

    try:
        with store._driver.session() as session:
            result = session.run(
                """
                MATCH (c:Component)-[:HAS_BUG]->(b:Bug)
                RETURN b.key AS key, b.summary AS summary,
                       c.name AS component, b.status AS status,
                       b.priority AS priority, b.url AS url
                """
            )
            all_bugs: dict[str, dict] = {}
            bug_agents: dict[str, str] = defaultdict(lambda: "unknown")

            for record in result:
                key = record["key"]
                if key not in all_bugs:
                    all_bugs[key] = dict(record)

            # Map components to agents
            from src.agents.registry import discover_agents
            agents = discover_agents()
            comp_to_agent: dict[str, str] = {}
            for agent_name, cfg in agents.items():
                for comp in cfg.components:
                    comp_to_agent[comp] = agent_name

            for key, data in all_bugs.items():
                comp = data.get("component", "")
                bug_agents[key] = comp_to_agent.get(comp, "unknown")

    finally:
        store.close()

    total_bugs = len(all_bugs)
    if total_bugs == 0:
        return []
    if sample_size > total_bugs:
        raise ValueError(f"sample_size ({sample_size}) exceeds available bugs ({total_bugs})")

    bugs_by_agent: dict[str, list[str]] = defaultdict(list)
    for key in all_bugs:
        bugs_by_agent[bug_agents[key]].append(key)

    rng = random.Random(seed)
    sampled = _stratified_sample(bugs_by_agent, all_bugs, sample_size, rng)

    logger.info("Sampled %d bugs from Neo4j (%d total)", len(sampled), total_bugs)
    return sampled


def _sample_from_json(
    memory_path: str, sample_size: int, seed: int,
) -> list[Bug]:
    """Sample bugs from coordinator_memory.json (legacy)."""
    with open(memory_path) as f:
        data = json.load(f)

    analyzed_bugs = data.get("analyzed_bugs", {})
    if not analyzed_bugs:
        return []

    total_bugs = len(analyzed_bugs)
    if sample_size <= 0:
        raise ValueError(f"sample_size must be positive, got {sample_size}")
    if sample_size > total_bugs:
        raise ValueError(f"sample_size ({sample_size}) exceeds available bugs ({total_bugs})")

    bugs_by_agent: dict[str, list[str]] = defaultdict(list)
    for key, entry in analyzed_bugs.items():
        agent = entry.get("agent", "unknown")
        bugs_by_agent[agent].append(key)

    all_bugs = {
        key: {
            "key": key,
            "summary": entry.get("summary", ""),
            "component": entry.get("component", ""),
            "url": f"https://issues.redhat.com/browse/{key}",
        }
        for key, entry in analyzed_bugs.items()
    }

    rng = random.Random(seed)
    sampled = _stratified_sample(bugs_by_agent, all_bugs, sample_size, rng)

    logger.info("Sampled %d bugs from JSON (%d total)", len(sampled), total_bugs)
    return sampled


def _stratified_sample(
    bugs_by_agent: dict[str, list[str]],
    all_bugs: dict[str, dict],
    sample_size: int,
    rng: random.Random,
) -> list[Bug]:
    """Stratified sampling proportional to bugs per agent."""
    total = sum(len(keys) for keys in bugs_by_agent.values())
    agents_sorted = sorted(bugs_by_agent.keys())

    allocation: dict[str, int] = {}
    remaining = sample_size
    for i, agent in enumerate(agents_sorted):
        agent_count = len(bugs_by_agent[agent])
        if i == len(agents_sorted) - 1:
            allocation[agent] = remaining
        else:
            share = math.floor(sample_size * agent_count / total)
            share = max(1, min(share, agent_count))
            allocation[agent] = share
            remaining -= share

    sampled_bugs: list[Bug] = []
    for agent in agents_sorted:
        keys = bugs_by_agent[agent]
        n = min(allocation[agent], len(keys))
        selected = rng.sample(keys, n)

        for key in selected:
            data = all_bugs[key]
            bug = Bug(
                key=key,
                summary=data.get("summary", ""),
                description="",
                component=data.get("component", ""),
                priority=data.get("priority", ""),
                status=data.get("status", ""),
                created="",
                url=data.get("url", f"https://issues.redhat.com/browse/{key}"),
            )
            sampled_bugs.append(bug)

    return sampled_bugs
