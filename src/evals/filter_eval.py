"""Filter eval: compare chaos relevance classification across two LLM models.

Runs the same set of bugs through both a baseline and candidate model,
then computes agreement metrics to validate model routing decisions.

Usage:
    python -m src.evals.filter_eval [--sample-size 200] \
        [--baseline claude-opus-4-6] [--candidate claude-sonnet-4-6]
"""

import argparse
import logging
import sys

from src.evals.eval_report import EvalReport
from src.evals.sampler import sample_bugs_for_eval
from src.filter.llm_config import LLMBackendConfig, LLMProvider
from src.filter.llm_filter import llm_filter_bug
from src.models import Bug

logger = logging.getLogger(__name__)


def run_filter_eval(
    bugs: list[Bug],
    baseline_model: str = "claude-opus-4-6",
    candidate_model: str = "claude-sonnet-4-6",
    provider: str = "claude_code",
) -> EvalReport:
    """Run FILTER on same bugs with two models, compare results.

    For each bug:
    1. Run llm_filter_bug with baseline_model -> baseline result
    2. Run llm_filter_bug with candidate_model -> candidate result
    3. Compare chaos_relevant field

    Args:
        bugs: List of Bug objects to classify.
        baseline_model: Model name for the baseline (ground truth).
        candidate_model: Model name for the candidate being evaluated.
        provider: LLM provider name (claude_code, anthropic, openai, google, ollama).

    Returns:
        EvalReport with agreement metrics and disagreement details.
    """
    llm_provider = LLMProvider(provider)

    baseline_config = LLMBackendConfig(
        provider=llm_provider,
        model=baseline_model,
    )
    candidate_config = LLMBackendConfig(
        provider=llm_provider,
        model=candidate_model,
    )

    agreements = 0
    false_negatives = 0
    false_positives = 0
    disagreements: list[dict] = []

    skipped = 0
    for i, bug in enumerate(bugs):
        logger.info("Eval %d/%d: %s", i + 1, len(bugs), bug.key)

        try:
            baseline_result = llm_filter_bug(bug, baseline_config)
            candidate_result = llm_filter_bug(bug, candidate_config)
        except Exception as e:
            logger.warning("Eval skipped for %s (LLM error): %s", bug.key, e)
            skipped += 1
            continue

        baseline_relevant = baseline_result.chaos_relevant
        candidate_relevant = candidate_result.chaos_relevant

        if baseline_relevant == candidate_relevant:
            agreements += 1
        else:
            disagreement = {
                "bug_key": bug.key,
                "summary": bug.summary,
                "component": bug.component,
                "baseline": baseline_relevant,
                "candidate": candidate_relevant,
                "baseline_reason": (
                    baseline_result.failure_mode
                    if baseline_relevant
                    else baseline_result.skip_reason
                ),
                "candidate_reason": (
                    candidate_result.failure_mode
                    if candidate_relevant
                    else candidate_result.skip_reason
                ),
            }
            disagreements = [*disagreements, disagreement]

            if baseline_relevant and not candidate_relevant:
                false_negatives += 1
            elif not baseline_relevant and candidate_relevant:
                false_positives += 1

    evaluated = len(bugs) - skipped
    if skipped:
        logger.warning("Eval: %d/%d bugs skipped due to LLM errors", skipped, len(bugs))
    agreement_rate = agreements / evaluated if evaluated > 0 else 0.0
    false_negative_rate = false_negatives / evaluated if evaluated > 0 else 0.0
    false_positive_rate = false_positives / evaluated if evaluated > 0 else 0.0

    return EvalReport(
        eval_name="filter_chaos_relevance",
        baseline_model=baseline_model,
        candidate_model=candidate_model,
        sample_size=evaluated,
        agreement_rate=agreement_rate,
        false_negative_rate=false_negative_rate,
        false_positive_rate=false_positive_rate,
        disagreements=disagreements,
    )


def main() -> None:
    """CLI entry point for filter eval."""
    parser = argparse.ArgumentParser(
        description="Evaluate chaos filter accuracy across two LLM models",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=200,
        help="Number of bugs to sample (default: 200)",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default="claude-opus-4-6",
        help="Baseline model name (default: claude-opus-4-6)",
    )
    parser.add_argument(
        "--candidate",
        type=str,
        default="claude-sonnet-4-6",
        help="Candidate model name (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="claude_code",
        help="LLM provider (default: claude_code)",
    )
    parser.add_argument(
        "--memory-path",
        type=str,
        default=None,
        help="Path to coordinator_memory.json (omit to use Neo4j)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for sampling (default: 42)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    source = args.memory_path or "Neo4j"
    logger.info("Sampling %d bugs from %s", args.sample_size, source)
    bugs = sample_bugs_for_eval(
        sample_size=args.sample_size,
        memory_path=args.memory_path,
        seed=args.seed,
    )

    logger.info(
        "Running filter eval: %s vs %s on %d bugs",
        args.baseline,
        args.candidate,
        len(bugs),
    )
    report = run_filter_eval(
        bugs=bugs,
        baseline_model=args.baseline,
        candidate_model=args.candidate,
        provider=args.provider,
    )

    print(report.summary())

    if report.disagreements:
        print("\nDisagreements:")
        for d in report.disagreements:
            direction = (
                "FALSE NEG" if d["baseline"] and not d["candidate"] else "FALSE POS"
            )
            print(f"  [{direction}] {d['bug_key']}: {d['summary'][:80]}")

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
