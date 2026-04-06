"""Tests for LLM-enhanced MAP and ANALYZE reasoning."""

import json
from unittest.mock import patch

from src.models import Bug, FilterResult, MatchResult, ScenarioMatch
from src.reasoning import llm_map_match


def _make_bug(key="TEST-1", summary="", description="", component="Etcd"):
    return Bug(
        key=key,
        summary=summary,
        description=description,
        component=component,
        priority="Major",
        status="New",
        created="2026-04-06",
        url=f"https://redhat.atlassian.net/browse/{key}",
    )


def _make_filter_result(bug, failure_mode="pod crash", injection_method="pod kill"):
    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=failure_mode,
        injection_method=injection_method,
    )


class TestLlmMapMatch:
    """Test LLM-enhanced scenario matching."""

    @patch("src.reasoning.call_llm")
    def test_full_match_when_llm_says_exact_coverage(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "FULL_MATCH",
            "matched_scenario": "scenarios/openshift/etcd.yml",
            "explanation": "This scenario kills etcd containers which directly tests the failure mode described in the bug.",
        })

        bug = _make_bug(
            summary="etcd crash under network partition",
            description="etcd members crash when network is partitioned",
        )
        filter_result = _make_filter_result(bug)
        scenario_hits = [
            {"text": "Scenario file: scenarios/openshift/etcd.yml\nkill etcd container", "distance": 0.3},
        ]
        doc_hits = [
            {"text": "etcd is the key-value store for Kubernetes", "distance": 0.4},
        ]

        result = llm_map_match(bug, filter_result, scenario_hits, doc_hits)

        assert result.match_result == MatchResult.FULL_MATCH
        assert result.matched_scenario == "scenarios/openshift/etcd.yml"
        assert result.similarity_score > 0.0
        mock_llm.assert_called_once()

    @patch("src.reasoning.call_llm")
    def test_partial_match_when_same_component_different_failure(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "PARTIAL_MATCH",
            "matched_scenario": "scenarios/openshift/network_chaos.yaml",
            "explanation": "Scenario tests network latency but not CPU-induced OVN state desync.",
        })

        bug = _make_bug(
            summary="OVN state desync under CPU load",
            component="Networking / ovn-kubernetes",
        )
        filter_result = _make_filter_result(bug, "state desync", "CPU hog")
        scenario_hits = [
            {"text": "Scenario file: scenarios/openshift/network_chaos.yaml\nnetwork_chaos: latency: 50ms", "distance": 0.5},
        ]

        result = llm_map_match(bug, filter_result, scenario_hits, [])

        assert result.match_result == MatchResult.PARTIAL_MATCH
        assert result.matched_scenario == "scenarios/openshift/network_chaos.yaml"

    @patch("src.reasoning.call_llm")
    def test_no_match_when_nothing_covers_failure(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "NO_MATCH",
            "matched_scenario": None,
            "explanation": "No existing scenario tests admission webhook pressure on CVO.",
        })

        bug = _make_bug(
            summary="CVO bootstrap deadlock due to ClusterResourceQuota",
            component="Cluster Version Operator",
        )
        filter_result = _make_filter_result(bug, "deadlock", "resource pressure")

        result = llm_map_match(bug, filter_result, [], [])

        assert result.match_result == MatchResult.NO_MATCH
        assert result.matched_scenario is None

    @patch("src.reasoning.call_llm")
    def test_falls_back_on_invalid_json(self, mock_llm):
        mock_llm.return_value = "I don't understand the question"

        bug = _make_bug(summary="some bug")
        filter_result = _make_filter_result(bug)

        result = llm_map_match(bug, filter_result, [], [])

        assert result.match_result == MatchResult.NO_MATCH

    @patch("src.reasoning.call_llm")
    def test_falls_back_on_llm_exception(self, mock_llm):
        mock_llm.side_effect = Exception("Ollama connection refused")

        bug = _make_bug(summary="some bug")
        filter_result = _make_filter_result(bug)

        result = llm_map_match(bug, filter_result, [{"text": "scenario", "distance": 0.4}], [])

        assert isinstance(result, ScenarioMatch)
