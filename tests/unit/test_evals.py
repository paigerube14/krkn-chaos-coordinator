"""Tests for eval infrastructure: EvalReport, sampler, and filter_eval."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from src.evals.eval_report import EvalReport
from src.evals.filter_eval import run_filter_eval
from src.evals.sampler import sample_bugs_for_eval
from src.models import Bug, FilterResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bug(key: str = "TEST-1", summary: str = "", component: str = "Etcd",
              agent: str = "control_plane") -> Bug:
    return Bug(
        key=key,
        summary=summary,
        description="",
        component=component,
        priority="",
        status="",
        created="2026-04-03",
        url=f"https://issues.redhat.com/browse/{key}",
    )


def _make_memory_json(bugs_by_agent: dict[str, list[str]]) -> dict:
    """Build a coordinator_memory.json-shaped dict from agent -> list of keys."""
    analyzed: dict[str, dict] = {}
    for agent, keys in bugs_by_agent.items():
        for key in keys:
            analyzed[key] = {
                "summary": f"Summary for {key}",
                "component": f"Component-{agent}",
                "analyzed_at": "2026-04-03T12:00:00",
                "agent": agent,
            }
    return {"analyzed_bugs": analyzed}


# ---------------------------------------------------------------------------
# EvalReport tests
# ---------------------------------------------------------------------------

class TestEvalReportPassCriteria:
    def test_eval_report_pass_criteria(self):
        """Agreement 0.95, false neg 0.02 -> passed=True."""
        report = EvalReport(
            eval_name="test",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=100,
            agreement_rate=0.95,
            false_negative_rate=0.02,
            false_positive_rate=0.03,
        )
        assert report.passed is True

    def test_eval_report_fail_on_low_agreement(self):
        """Agreement 0.85 -> passed=False."""
        report = EvalReport(
            eval_name="test",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=100,
            agreement_rate=0.85,
            false_negative_rate=0.02,
            false_positive_rate=0.13,
        )
        assert report.passed is False

    def test_eval_report_fail_on_high_false_negatives(self):
        """False neg 0.08 -> passed=False even with high agreement."""
        report = EvalReport(
            eval_name="test",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=100,
            agreement_rate=0.92,
            false_negative_rate=0.08,
            false_positive_rate=0.00,
        )
        assert report.passed is False

    def test_eval_report_boundary_pass(self):
        """Exactly at thresholds: agreement=0.90, false_neg=0.05 -> passed=True."""
        report = EvalReport(
            eval_name="boundary",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=100,
            agreement_rate=0.90,
            false_negative_rate=0.05,
            false_positive_rate=0.05,
        )
        assert report.passed is True

    def test_eval_report_boundary_fail_agreement(self):
        """Just below agreement threshold: 0.899 -> passed=False."""
        report = EvalReport(
            eval_name="boundary",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=100,
            agreement_rate=0.899,
            false_negative_rate=0.05,
            false_positive_rate=0.051,
        )
        assert report.passed is False


class TestEvalReportSummary:
    def test_eval_report_summary_format(self):
        """Verify summary() returns expected string with all fields."""
        report = EvalReport(
            eval_name="filter_chaos_relevance",
            baseline_model="claude-opus-4-6",
            candidate_model="claude-sonnet-4-6",
            sample_size=200,
            agreement_rate=0.93,
            false_negative_rate=0.04,
            false_positive_rate=0.03,
            disagreements=[
                {"bug_key": "BUG-1", "baseline": True, "candidate": False},
                {"bug_key": "BUG-2", "baseline": False, "candidate": True},
            ],
        )
        text = report.summary()

        assert "[PASS]" in text
        assert "filter_chaos_relevance" in text
        assert "claude-opus-4-6" in text
        assert "claude-sonnet-4-6" in text
        assert "200" in text
        assert "93.0%" in text
        assert "4.0%" in text
        assert "3.0%" in text
        assert "Disagreements: 2" in text

    def test_eval_report_summary_fail_status(self):
        """Verify summary shows FAIL for failing report."""
        report = EvalReport(
            eval_name="test",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=50,
            agreement_rate=0.80,
            false_negative_rate=0.10,
            false_positive_rate=0.10,
        )
        text = report.summary()
        assert "[FAIL]" in text

    def test_eval_report_is_immutable(self):
        """EvalReport is frozen and cannot be mutated."""
        report = EvalReport(
            eval_name="test",
            baseline_model="opus",
            candidate_model="sonnet",
            sample_size=10,
            agreement_rate=0.90,
            false_negative_rate=0.05,
            false_positive_rate=0.05,
        )
        with pytest.raises(AttributeError):
            report.agreement_rate = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Sampler tests
# ---------------------------------------------------------------------------

class TestSampler:
    def test_sampler_returns_correct_size(self, tmp_path):
        """Mock JSON file, verify sample size matches requested."""
        memory = _make_memory_json({
            "control_plane": [f"CP-{i}" for i in range(50)],
            "networking": [f"NET-{i}" for i in range(100)],
            "storage": [f"STG-{i}" for i in range(50)],
        })
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        bugs = sample_bugs_for_eval(
            memory_path=str(memory_path),
            sample_size=40,
            seed=42,
        )
        assert len(bugs) == 40

    def test_sampler_stratified_by_agent(self, tmp_path):
        """Verify proportional sampling across agents.

        With 60 control_plane and 40 networking bugs, a sample of 20
        should yield roughly 12 control_plane and 8 networking.
        """
        memory = _make_memory_json({
            "control_plane": [f"CP-{i}" for i in range(60)],
            "networking": [f"NET-{i}" for i in range(40)],
        })
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        bugs = sample_bugs_for_eval(
            memory_path=str(memory_path),
            sample_size=20,
            seed=42,
        )

        cp_count = sum(1 for b in bugs if b.key.startswith("CP-"))
        net_count = sum(1 for b in bugs if b.key.startswith("NET-"))

        assert cp_count + net_count == 20
        # 60% control_plane -> expect ~12, allow +/- 2 for rounding
        assert 10 <= cp_count <= 14
        # 40% networking -> expect ~8
        assert 6 <= net_count <= 10

    def test_sampler_reproducible(self, tmp_path):
        """Same seed produces same sample."""
        memory = _make_memory_json({
            "agent_a": [f"A-{i}" for i in range(30)],
            "agent_b": [f"B-{i}" for i in range(30)],
        })
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        sample1 = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=10, seed=99)
        sample2 = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=10, seed=99)

        keys1 = [b.key for b in sample1]
        keys2 = [b.key for b in sample2]
        assert keys1 == keys2

    def test_sampler_different_seed_different_sample(self, tmp_path):
        """Different seeds produce different samples."""
        memory = _make_memory_json({
            "agent_a": [f"A-{i}" for i in range(50)],
        })
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        sample1 = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=10, seed=1)
        sample2 = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=10, seed=2)

        keys1 = [b.key for b in sample1]
        keys2 = [b.key for b in sample2]
        assert keys1 != keys2

    def test_sampler_empty_memory(self, tmp_path):
        """Empty analyzed_bugs returns empty list."""
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps({"analyzed_bugs": {}}))

        bugs = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=0)
        assert bugs == []

    def test_sampler_raises_on_oversized_sample(self, tmp_path):
        """Requesting more bugs than available raises ValueError."""
        memory = _make_memory_json({"agent_a": ["A-1", "A-2"]})
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        with pytest.raises(ValueError, match="exceeds available"):
            sample_bugs_for_eval(memory_path=str(memory_path), sample_size=10)

    def test_sampler_creates_valid_bug_objects(self, tmp_path):
        """Verify Bug objects have correct fields from memory JSON."""
        memory = _make_memory_json({"control_plane": ["CP-1"]})
        memory_path = tmp_path / "memory.json"
        memory_path.write_text(json.dumps(memory))

        bugs = sample_bugs_for_eval(memory_path=str(memory_path), sample_size=1)

        assert len(bugs) == 1
        bug = bugs[0]
        assert bug.key == "CP-1"
        assert bug.summary == "Summary for CP-1"
        assert bug.component == "Component-control_plane"
        assert bug.description == ""
        assert "CP-1" in bug.url


# ---------------------------------------------------------------------------
# Filter eval tests
# ---------------------------------------------------------------------------

class TestFilterEval:
    def test_filter_eval_compares_models(self):
        """Mock llm_filter_bug, verify comparison logic."""
        bugs = [
            _make_bug(key="BUG-1", summary="etcd crash under load"),
            _make_bug(key="BUG-2", summary="CVE security issue"),
            _make_bug(key="BUG-3", summary="node drain failure"),
        ]

        # Define mock behavior: baseline and candidate agree on BUG-1 and BUG-2,
        # disagree on BUG-3 (false negative: baseline=True, candidate=False)
        call_count = 0

        def mock_llm_filter(bug: Bug, config: MagicMock) -> FilterResult:
            nonlocal call_count
            call_count += 1
            is_baseline = config.model == "opus"

            if bug.key == "BUG-1":
                # Both agree: relevant
                return FilterResult(
                    bug=bug, chaos_relevant=True,
                    failure_mode="crash", injection_method="pod_kill",
                )
            elif bug.key == "BUG-2":
                # Both agree: not relevant
                return FilterResult(
                    bug=bug, chaos_relevant=False,
                    skip_reason="CVE",
                )
            else:
                # BUG-3: baseline says yes, candidate says no
                if is_baseline:
                    return FilterResult(
                        bug=bug, chaos_relevant=True,
                        failure_mode="drain failure",
                        injection_method="node_drain",
                    )
                return FilterResult(
                    bug=bug, chaos_relevant=False,
                    skip_reason="not resilience",
                )

        with patch("src.evals.filter_eval.llm_filter_bug", side_effect=mock_llm_filter):
            report = run_filter_eval(
                bugs=bugs,
                baseline_model="opus",
                candidate_model="sonnet",
            )

        assert report.sample_size == 3
        # 2 agreements out of 3
        assert abs(report.agreement_rate - 2.0 / 3.0) < 0.001
        # 1 false negative out of 3
        assert abs(report.false_negative_rate - 1.0 / 3.0) < 0.001
        assert report.false_positive_rate == 0.0
        assert len(report.disagreements) == 1
        assert report.disagreements[0]["bug_key"] == "BUG-3"
        assert report.passed is False  # false negative rate > 5%
        # Each bug called twice (once per model)
        assert call_count == 6

    def test_filter_eval_perfect_agreement(self):
        """When both models agree on everything, report passes."""
        bugs = [_make_bug(key=f"BUG-{i}") for i in range(10)]

        def mock_llm_filter(bug: Bug, config: MagicMock) -> FilterResult:
            return FilterResult(
                bug=bug, chaos_relevant=True,
                failure_mode="crash", injection_method="pod_kill",
            )

        with patch("src.evals.filter_eval.llm_filter_bug", side_effect=mock_llm_filter):
            report = run_filter_eval(bugs=bugs, baseline_model="a", candidate_model="b")

        assert report.agreement_rate == 1.0
        assert report.false_negative_rate == 0.0
        assert report.false_positive_rate == 0.0
        assert report.passed is True
        assert report.disagreements == []

    def test_filter_eval_false_positive_tracking(self):
        """Verify false positives are tracked (candidate=yes, baseline=no)."""
        bugs = [_make_bug(key="FP-1")]

        def mock_llm_filter(bug: Bug, config: MagicMock) -> FilterResult:
            is_baseline = config.model == "baseline"
            if is_baseline:
                return FilterResult(bug=bug, chaos_relevant=False, skip_reason="not chaos")
            return FilterResult(
                bug=bug, chaos_relevant=True,
                failure_mode="false alarm", injection_method="none",
            )

        with patch("src.evals.filter_eval.llm_filter_bug", side_effect=mock_llm_filter):
            report = run_filter_eval(
                bugs=bugs, baseline_model="baseline", candidate_model="candidate",
            )

        assert report.false_positive_rate == 1.0
        assert report.false_negative_rate == 0.0
        assert len(report.disagreements) == 1

    def test_filter_eval_empty_bugs(self):
        """Empty bug list produces a valid report with zero rates."""
        report = run_filter_eval(bugs=[], baseline_model="a", candidate_model="b")

        assert report.sample_size == 0
        assert report.agreement_rate == 0.0
        assert report.false_negative_rate == 0.0
        assert report.false_positive_rate == 0.0
        assert report.disagreements == []
