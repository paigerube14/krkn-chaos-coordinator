"""Domain models for krkn-chaos-coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ChaosRelevance(Enum):
    RELEVANT = "relevant"
    NOT_RELEVANT = "not_relevant"
    UNCERTAIN = "uncertain"


class Confidence(Enum):
    HIGH = "high"      # 70-100: draft PR
    MEDIUM = "medium"  # 40-69: GitHub issue with recommendation
    LOW = "low"        # 0-39: GitHub issue describing gap


class ActionType(Enum):
    DRAFT_PR = "draft_pr"
    GITHUB_ISSUE = "github_issue"
    SKIP = "skip"


class MatchResult(Enum):
    FULL_MATCH = "full_match"
    PARTIAL_MATCH = "partial_match"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class Bug:
    key: str
    summary: str
    description: str
    component: str
    priority: str
    status: str
    created: str
    url: str


@dataclass(frozen=True)
class Regression:
    regression_id: int
    test_name: str
    component: str
    opened: str
    closed: str | None
    triaged: bool


@dataclass(frozen=True)
class FilterResult:
    bug: Bug
    chaos_relevant: bool
    failure_mode: str | None = None
    injection_method: str | None = None
    skip_reason: str | None = None


@dataclass(frozen=True)
class ScenarioMatch:
    bug: Bug
    match_result: MatchResult
    matched_scenario: str | None = None
    matched_repo: str | None = None
    similarity_score: float = 0.0


@dataclass(frozen=True)
class GapAnalysis:
    bug: Bug
    reuse_plan: str | None = None
    confidence_score: int = 0
    confidence_level: Confidence = Confidence.LOW
    action_type: ActionType = ActionType.GITHUB_ISSUE
    reasoning: str = ""
    base_scenario: str | None = None
    modifications: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentResult:
    agent_name: str
    bugs_discovered: list[Bug] = field(default_factory=list)
    bugs_filtered_out: list[FilterResult] = field(default_factory=list)
    bugs_matched: list[ScenarioMatch] = field(default_factory=list)
    gaps: list[GapAnalysis] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
