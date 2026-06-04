"""Component-to-agent mapping — delegates to the agent registry.

Agent configs live in config/agents/*.yaml. This module provides
the same API as before (get_components_for_agent, get_all_agents)
but reads from the registry instead of a hardcoded dict.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from src.agents.registry import discover_agents

logger = logging.getLogger(__name__)

_registry_cache: dict[str, list[str]] | None = None
_registry_lock = threading.Lock()


def _load_registry() -> dict[str, list[str]]:
    """Load agent → components mapping from YAML registry, cached."""
    global _registry_cache
    with _registry_lock:
        if _registry_cache is None:
            agents = discover_agents()
            _registry_cache = {
                name: list(config.components)
                for name, config in agents.items()
            }
    return _registry_cache


def get_components_for_agent(agent_name: str) -> list[str]:
    """Get the OCPBUGS component names for a given agent."""
    registry = _load_registry()
    components = registry.get(agent_name)
    if components is None:
        raise ValueError(f"Unknown agent: {agent_name}. Valid: {list(registry.keys())}")
    return list(components)


def get_all_agents() -> list[str]:
    """Get all agent names."""
    return sorted(_load_registry().keys())


def load_team_component_map(path: Path) -> dict:
    """Load the full team component map from JSON file."""
    with open(path) as f:
        return json.load(f)
