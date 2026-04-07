"""Base domain agent with the DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER pipeline."""

import logging
from abc import ABC

from src.apis.jira_client import JiraClient
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.filter.chaos_filter import filter_bugs
from src.filter.llm_filter import llm_filter_bugs
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.component_map import get_components_for_agent
from src.knowledge.memory import MemoryStore
from src.knowledge.neo4j_store import Neo4jStore
from src.knowledge.scenario_index import ScenarioInfo
from src.models import (
    AgentResult,
    Bug,
    FilterResult,
    GapAnalysis,
    MatchResult,
    ScenarioMatch,
)

logger = logging.getLogger(__name__)


class BaseDomainAgent(ABC):
    """Base class for domain-specific chaos coverage agents."""

    def __init__(
        self,
        agent_name: str,
        jira: JiraClient,
        sippy: SippyClient,
        github: GitHubClient,
        chroma: ChromaStore,
        scenarios: list[ScenarioInfo],
        release: str,
        memory: MemoryStore | None = None,
        neo4j_store: Neo4jStore | None = None,
        use_llm: bool = False,
        max_bugs: int = 200,
        days: int = 14,
    ):
        self.agent_name = agent_name
        self.jira = jira
        self.sippy = sippy
        self.github = github
        self.chroma = chroma
        self.scenarios = scenarios
        self.release = release
        self.memory = memory or MemoryStore()
        self.neo4j = neo4j_store
        self.use_llm = use_llm
        self.max_bugs = max_bugs
        self.days = days
        self.components = get_components_for_agent(agent_name)

    def run(self) -> AgentResult:
        """Execute the full pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER."""
        import asyncio
        logger.info("=== %s agent starting ===", self.agent_name)

        # DISCOVER
        bugs = self._discover()
        logger.info("DISCOVER: found %d bugs", len(bugs))

        # Split into new vs known bugs
        known_keys = self._get_known_bugs()
        new_bugs = [b for b in bugs if b.key not in known_keys]
        known_bugs = [b for b in bugs if b.key in known_keys]

        if known_bugs:
            logger.info("DISCOVER: %d known bugs — updating status in Neo4j", len(known_bugs))
            self._update_known_bugs(known_bugs)

        if new_bugs:
            logger.info("DISCOVER: %d new bugs to analyze", len(new_bugs))

        # FILTER
        relevant, skipped = self._filter(new_bugs)
        logger.info("FILTER: %d relevant, %d skipped", len(relevant), len(skipped))

        # MAP
        matched, unmatched = self._map(relevant)
        logger.info("MAP: %d matched, %d unmatched", len(matched), len(unmatched))

        # ANALYZE
        gaps = self._analyze(unmatched)
        logger.info("ANALYZE: %d gaps identified", len(gaps))

        result = AgentResult(
            agent_name=self.agent_name,
            bugs_discovered=bugs,
            bugs_filtered_out=skipped,
            bugs_matched=matched,
            gaps=gaps,
        )

        # REMEMBER (Neo4j + JSON fallback)
        self._remember(result)

        logger.info("=== %s agent complete ===", self.agent_name)
        return result

    def _update_known_bugs(self, known_bugs: list[Bug]) -> None:
        """Update status/priority for already-analyzed bugs. Closes gaps for resolved bugs."""
        if self.neo4j:
            try:
                self.neo4j.update_bug_statuses(known_bugs)
            except Exception as e:
                logger.warning("Failed to update known bug statuses: %s", e)

    def _get_known_bugs(self) -> set[str]:
        """Get already-analyzed bug keys from Neo4j or JSON memory."""
        if self.neo4j:
            try:
                return self.neo4j.get_analyzed_bug_keys_sync()
            except Exception as e:
                logger.warning("Neo4j lookup failed, using JSON fallback: %s", e)
        return self.memory.get_analyzed_bug_keys()

    def _remember(self, result: AgentResult) -> None:
        """Store results in Neo4j and JSON memory."""
        # Always store in JSON (fast, reliable fallback)
        self.memory.remember_result(result)

        # Also store in Neo4j if available
        if self.neo4j:
            try:
                self.neo4j.remember_result_sync(result)
                logger.info("REMEMBER: stored in Neo4j")
            except Exception as e:
                logger.warning("Neo4j remember failed (JSON fallback used): %s", e)

    def _discover(self) -> list[Bug]:
        """DISCOVER: Query JIRA and Sippy for bugs and regressions."""
        return self.jira.get_bugs_by_components(
            self.components, days=self.days, max_results=self.max_bugs
        )

    def _filter(self, bugs: list[Bug]) -> tuple[list[FilterResult], list[FilterResult]]:
        """FILTER: Determine chaos relevance of each bug."""
        if self.use_llm:
            logger.info("Using LLM-enhanced filter (Ollama)")
            return llm_filter_bugs(bugs)
        return filter_bugs(bugs)

    def _map(self, relevant: list[FilterResult]) -> tuple[list[ScenarioMatch], list[ScenarioMatch]]:
        """MAP: Match bugs against existing krkn scenarios using ChromaDB + local index."""
        matched = []
        unmatched = []

        for filter_result in relevant:
            bug = filter_result.bug
            match = self._find_scenario_match(bug, filter_result)
            if match.match_result == MatchResult.FULL_MATCH:
                matched.append(match)
            else:
                unmatched.append(match)

        return matched, unmatched

    def _find_scenario_match(self, bug: Bug, filter_result: FilterResult) -> ScenarioMatch:
        """Search for existing krkn scenarios that match a bug.

        Uses ChromaDB for retrieval — searches per component separately for
        multi-component bugs so results aren't diluted. If use_llm is True,
        LLM reasons over the retrieved results.
        """
        summary_parts = [bug.summary]
        if filter_result.failure_mode:
            summary_parts.append(filter_result.failure_mode)
        if filter_result.injection_method:
            summary_parts.append(filter_result.injection_method)
        summary = " ".join(summary_parts)

        components = bug.all_components or (bug.component,)

        # ChromaDB retrieval — per component, merged
        scenario_hits = self.chroma.search_per_component(
            components, summary, collection="scenarios", n_results=5,
        )
        doc_hits = self.chroma.search_per_component(
            components, summary, collection="krkn_docs", n_results=5,
        )

        if self.use_llm:
            from src.reasoning import llm_map_match
            return llm_map_match(bug, filter_result, scenario_hits, doc_hits)

        # Fallback: threshold-based matching
        from src.reasoning import _fallback_match
        return _fallback_match(bug, scenario_hits)

    def _analyze(self, unmatched: list[ScenarioMatch]) -> list[GapAnalysis]:
        """ANALYZE: Score confidence and determine action for each gap.

        If use_llm is True, LLM reasons over ChromaDB context + Neo4j history
        to produce specific recommendations. Otherwise, uses keyword scoring.
        """
        gaps = []

        for match in unmatched:
            bug = match.bug

            if self.use_llm:
                # Gather context for LLM — search per component
                components = bug.all_components or (bug.component,)
                ocp_docs = self.chroma.search_per_component(
                    components, bug.summary, collection="all", n_results=5,
                )
                krkn_docs = self.chroma.search_per_component(
                    components, bug.summary, collection="krkn_docs", n_results=3,
                )

                neo4j_history = []
                if self.neo4j:
                    try:
                        # Check history for ALL components
                        for comp in components:
                            neo4j_history.extend(
                                self.neo4j.get_similar_resolved_bugs(comp)
                            )
                    except Exception as e:
                        logger.warning("Neo4j history lookup failed: %s", e)

                from src.reasoning import llm_analyze_gap
                gap = llm_analyze_gap(
                    bug=bug,
                    match=match,
                    ocp_docs=ocp_docs,
                    krkn_docs=krkn_docs,
                    neo4j_history=neo4j_history,
                )
            else:
                from src.reasoning import _fallback_analyze
                gap = _fallback_analyze(bug, match)

            gaps.append(gap)

        return gaps
