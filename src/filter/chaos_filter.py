"""Chaos relevance filter — determines if a bug needs a chaos test.

Core rule: If a bug involves a component behaving incorrectly during, after,
or because of any disruption (restart, failure, load, resource pressure,
upgrade, scaling) — it's chaos-relevant. Even if the symptom appears in a
different component than the root cause.
"""

from __future__ import annotations

import logging

from src.models import Bug, FilterResult

logger = logging.getLogger(__name__)

# Keywords that indicate a bug is NOT chaos-relevant
NON_CHAOS_KEYWORDS = [
    "CVE-",
    "security tracking",
    "vulnerability",
    "flaky test",
    "test infrastructure",
    "ci failure",
    "test fix",
    "documentation",
    "typo",
    "backport",
    "cherry-pick",
    "cherry pick",
    "bump to",
    "bump ",
    "rebase",
    "vendor update",
    "dependency update",
    "[stub]",
]

# Keywords that indicate a bug IS chaos-relevant
CHAOS_KEYWORDS = [
    # Component crash/restart
    "crash",
    "panic",
    "oom",
    "out of memory",
    "oom kill",
    "deadlock",
    "crashloop",
    "restart loop",
    # Timeouts and degradation
    "timeout",
    "timed out",
    "context deadline",
    "unavailable",
    "degraded",
    "unhealthy",
    "not ready",
    # Performance degradation (NEW)
    "slow",
    "latency increased",
    "high latency",
    "p99",
    "p95",
    "response time",
    "throughput",
    "performance degradation",
    "performance regression",
    # Cluster health (NEW)
    "cluster operator",
    "clusteroperator",
    "co degraded",
    "co unavailable",
    "operator degraded",
    "operator unavailable",
    "cluster unhealthy",
    # Consensus and leadership
    "quorum",
    "leader election",
    "split brain",
    # Node failures
    "node drain",
    "node reboot",
    "node delete",
    "node replace",
    "node failure",
    "node not ready",
    # Network disruption
    "network partition",
    "latency",
    "packet loss",
    "dns failure",
    "connection refused",
    "connection reset",
    "connection timeout",
    # Service disruption (NEW)
    "service down",
    "endpoint not reachable",
    "service unavailable",
    "502",
    "503",
    "504",
    # Resource exhaustion (NEW)
    "high cpu",
    "memory leak",
    "resource quota",
    "limit reached",
    "resource exhaustion",
    "cpu spike",
    "memory spike",
    "disk full",
    "disk pressure",
    "memory pressure",
    "resource pressure",
    "throttl",
    # Scaling (NEW)
    "scale up failed",
    "autoscaler",
    "pending pods",
    "scheduling failed",
    "insufficient resources",
    "node pressure",
    "capacity",
    # Pod disruption
    "pod eviction",
    "pod kill",
    "pod disruption",
    # Upgrade/rollback (expanded)
    "upgrade fail",
    "upgrade from",
    "upgrade to",
    "rollback",
    "doesn't recover",
    "doesn't reconcile",
    "failed to reconcile",
    "not reconciling",
    "stale after restart",
    # Intermittent failures (NEW)
    "intermittent",
    "flapping",
    "under load",
    "under pressure",
    "under stress",
    # Certificates and time
    "certificate expired",
    "cert rotation",
    "clock skew",
    # Data integrity
    "data loss",
    "data corruption",
    "corrupt",
    "stale",
    "not cleared",
    "missing",
    # General failure indicators
    "failover",
    "recovery",
    "stuck",
    "outage",
    "disruption",
    "failure",
    "failed",
    "kill",
    "lost",
    "static pod",
    "member",
    "scale",
]

# krkn injection capabilities for Part 2 of the filter
KRKN_CAPABILITIES = [
    "pod failures (kill, restart, CPU/memory hog)",
    "node failures (drain, reboot, shutdown, network isolate)",
    "network chaos (partition, latency via tc netem, packet loss, DNS failure)",
    "resource stress (CPU, memory, disk fill, I/O pressure)",
    "time skew (NTP drift, clock jumps)",
    "container chaos (kill containers, corrupt mounts)",
    "cloud provider (detach volumes, stop VMs, AZ outage)",
    "cluster state (delete CRDs, corrupt configmaps, scale to 0)",
]


def filter_bug(bug: Bug) -> FilterResult:
    """Determine if a bug is chaos-relevant using keyword heuristics.

    Part 1: Is this a failure mode? (vs code bug, CVE, UI issue)
    Part 2: Can krkn inject this? (match against capabilities)
    """
    text = f"{bug.summary} {bug.description}".lower()

    # Check for clone/stub in first 200 chars
    if "clone of issue" in text[:200] or "[stub]" in bug.summary.lower():
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="Stub/clone ticket — not an original bug report",
        )

    # Part 1: Check for non-chaos indicators
    for keyword in NON_CHAOS_KEYWORDS:
        if keyword.lower() in text:
            return FilterResult(
                bug=bug,
                chaos_relevant=False,
                skip_reason=f"Not chaos-relevant: matches skip keyword '{keyword}'",
            )

    # Part 1: Check for chaos indicators
    matched_keywords = [kw for kw in CHAOS_KEYWORDS if kw.lower() in text]
    if not matched_keywords:
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="No chaos-relevant failure mode keywords found in bug description",
        )

    # Part 2: Determine injection method
    failure_mode = _extract_failure_mode(text, matched_keywords)
    injection_method = _match_injection_method(text)

    if injection_method is None:
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            failure_mode=failure_mode,
            skip_reason="Failure mode identified but no matching krkn injection capability",
        )

    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=failure_mode,
        injection_method=injection_method,
    )


def filter_bugs(bugs: list[Bug]) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter a list of bugs into chaos-relevant and non-relevant.

    Returns (relevant, skipped) tuples.
    """
    relevant = []
    skipped = []

    for bug in bugs:
        result = filter_bug(bug)
        if result.chaos_relevant:
            relevant.append(result)
            logger.info(
                "PASS %s: %s (injection: %s)",
                bug.key, result.failure_mode, result.injection_method,
            )
        else:
            skipped.append(result)
            logger.info("SKIP %s: %s", bug.key, result.skip_reason)

    logger.info(
        "Filter result: %d relevant, %d skipped out of %d total",
        len(relevant), len(skipped), len(bugs),
    )
    return relevant, skipped


def _extract_failure_mode(text: str, matched_keywords: list[str]) -> str:
    """Build a failure mode description from matched keywords."""
    return f"Failure indicators: {', '.join(matched_keywords[:5])}"


def _match_injection_method(text: str) -> str | None:
    """Match bug description against krkn's injection capabilities.

    Priority order matters — more specific matches first.
    """
    injection_rules: list[tuple[str, list[str]]] = [
        ("node", [
            "node delete", "node replace", "node drain", "node reboot",
            "node shutdown", "node fail", "node not ready", "kubelet",
            "machine api", "node outage", "nodestatuses", "node pressure",
        ]),
        ("network", [
            "network partition", "network chaos", "packet loss",
            "dns fail", "connection refused", "connection reset",
            "connection timeout", "ingress", "ovn",
            "network outage", "network disruption",
            "502", "503", "504",
        ]),
        ("resource_stress", [
            "cpu", "memory pressure", "memory leak", "memory spike",
            "disk full", "disk pressure", "resource exhaustion",
            "throttl", "resource pressure", "api server load",
            "resource stress", "hog", "i/o pressure", "cpu spike",
            "high cpu", "resource quota", "limit reached",
            "slow", "latency increased", "high latency",
            "p99", "p95", "response time", "throughput",
            "performance degradation", "performance regression",
            "under load", "under pressure", "under stress",
            "intermittent",
        ]),
        ("pod", [
            "pod kill", "pod delete", "pod disruption", "pod eviction",
            "container restart", "crashloop", "oom", "out of memory",
            "static pod", "pod fail", "pod outage", "oom kill",
        ]),
        ("cluster_state", [
            "crd", "configmap", "operator", "upgrade fail", "rollback",
            "scale", "quorum", "leader election", "member", "etcd",
            "split brain", "cluster state", "corrupt",
            "cluster operator", "co degraded", "co unavailable",
            "operator degraded", "upgrade from", "upgrade to",
            "doesn't reconcile", "failed to reconcile", "not reconciling",
            "autoscaler", "pending pods", "scheduling failed",
            "scale up failed", "insufficient resources",
            "flapping",
        ]),
        ("time_skew", [
            "clock", "ntp", "time skew", "certificate expired", "cert rotation",
        ]),
        ("cloud_provider", [
            "instance", "volume detach", "stop vm", "az outage",
            "availability zone",
        ]),
    ]

    for capability, keywords in injection_rules:
        for kw in keywords:
            if kw in text:
                return capability

    # Fallback: generic failure keywords
    generic = [
        "fail", "crash", "unavailable", "degraded", "unhealthy",
        "disruption", "outage", "panic", "deadlock", "stuck",
        "doesn't recover", "stale after restart", "data loss",
        "service down", "service unavailable", "endpoint not reachable",
    ]
    for kw in generic:
        if kw in text:
            return "cluster_state"

    return None
