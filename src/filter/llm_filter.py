"""LLM-enhanced chaos relevance filter — pluggable backend.

Supports: Ollama (local), Anthropic (Claude API), OpenAI, Google Gemini.
Auto-detects the best available backend from environment variables.
"""

from __future__ import annotations

import json
import logging

from dataclasses import dataclass, field

from src.filter.llm_config import LLMBackendConfig, LLMProvider, detect_llm_backend
from src.models import Bug, FilterResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token usage accumulator (module-level, reset per run)
# ---------------------------------------------------------------------------

@dataclass
class _UsageAccumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    call_count: int = 0

_usage = _UsageAccumulator()


def _accumulate_usage(input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    _usage.call_count += 1
    _usage.input_tokens += input_tokens
    _usage.output_tokens += output_tokens
    _usage.cost_usd += cost_usd
    logger.info(
        "LLM CALL #%d: %d in + %d out = %d tokens, $%.4f",
        _usage.call_count, input_tokens, output_tokens,
        input_tokens + output_tokens, cost_usd,
    )


def get_token_usage() -> dict:
    """Return accumulated token usage since last reset."""
    return {
        "input_tokens": _usage.input_tokens,
        "output_tokens": _usage.output_tokens,
        "total_tokens": _usage.input_tokens + _usage.output_tokens,
        "cost_usd": round(_usage.cost_usd, 4),
        "call_count": _usage.call_count,
    }


def reset_token_usage() -> None:
    """Reset the accumulator (call at start of each run)."""
    _usage.input_tokens = 0
    _usage.output_tokens = 0
    _usage.cost_usd = 0.0
    _usage.call_count = 0

SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes clusters.
Your job is to determine if a JIRA bug describes a failure mode that can be tested with chaos engineering tools (krkn).

A bug IS chaos-relevant if:
- A component fails under stress, load, or resource pressure
- A component fails when another component dies or becomes unavailable
- Recovery doesn't work after a disruption (node reboot, pod kill, network partition)
- Race condition during upgrade or rollout
- Data corruption or loss under failure conditions

A bug is NOT chaos-relevant if:
- It's a code logic bug (wrong output from correct inputs, nil pointer, bad parsing)
- It's a CVE or security vulnerability (needs a patch, not a resilience test)
- It's a UI/console rendering issue
- It's a flaky test or test infrastructure problem
- It's a documentation or configuration error
- It's a version-specific migration issue that can't be reproduced via chaos injection

krkn can inject these failure types:
- Pod failures (kill, restart, CPU/memory hog)
- Node failures (drain, reboot, shutdown, network isolate)
- Network chaos (partition, latency, packet loss, DNS failure)
- Resource stress (CPU, memory, disk fill, I/O pressure)
- Time skew (NTP drift, clock jumps)
- Cloud provider chaos (stop VMs, detach volumes, AZ outage)
- Cluster state chaos (delete CRDs, corrupt configmaps, scale to 0)

Respond with ONLY a JSON object, no other text:
{
  "chaos_relevant": true/false,
  "confidence": 0-100,
  "failure_mode": "brief description of the failure mode" or null,
  "injection_method": "which krkn injection type would test this" or null,
  "skip_reason": "why this is not chaos-relevant" or null
}"""


def call_llm(
    messages: list[dict],
    config: LLMBackendConfig,
    system_prompt: str | None = None,
) -> str:
    """Call the configured LLM backend and return the response text.

    Args:
        messages: Conversation messages (role/content dicts).
        config: LLM backend configuration.
        system_prompt: When provided AND provider is Anthropic, passed via
            the ``system`` parameter with ``cache_control`` for prompt caching.
            Non-Anthropic providers fall back to including the system message
            in the messages list.
    """
    if config.provider == LLMProvider.CLAUDE_CODE:
        import json as _json
        import subprocess

        effective_messages = _prepend_system_message(messages, system_prompt)
        sys_parts = []
        user_parts = []
        for msg in effective_messages:
            if msg["role"] == "system":
                sys_parts.append(msg["content"])
            elif msg["role"] == "user":
                user_parts.append(msg["content"])
        full_prompt = "\n\n".join(user_parts)

        cmd = [
            "claude", "-p", full_prompt,
            "--model", config.model,
            "--output-format", "json",
            "--bare",
            "--exclude-dynamic-system-prompt-sections",
        ]
        if sys_parts:
            cmd.extend(["--system-prompt", "\n\n".join(sys_parts)])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Claude Code CLI failed: {result.stderr[:200]}")

        try:
            parsed = _json.loads(result.stdout)
            text = parsed.get("result", "").strip()
            usage = parsed.get("usage", {})
            _accumulate_usage(
                input_tokens=usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cost_usd=parsed.get("total_cost_usd", 0.0),
            )
        except (_json.JSONDecodeError, KeyError):
            text = result.stdout.strip()

        return text

    elif config.provider == LLMProvider.OLLAMA:
        import ollama

        # For non-Anthropic providers, prepend system_prompt as a message
        effective_messages = _prepend_system_message(messages, system_prompt)
        response = ollama.chat(
            model=config.model,
            messages=effective_messages,
            options={"temperature": 0.1, "num_predict": 300},
        )
        return response["message"]["content"].strip()

    elif config.provider == LLMProvider.ANTHROPIC:
        import anthropic

        client = anthropic.Anthropic(api_key=config.api_key)
        user_msgs = [m for m in messages if m["role"] != "system"]

        if system_prompt is not None:
            # Use cache_control for Anthropic prompt caching
            system_param: str | list[dict] = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            # Legacy path: extract system from messages
            system_param = (
                messages[0]["content"]
                if messages and messages[0]["role"] == "system"
                else ""
            )

        response = client.messages.create(
            model=config.model,
            max_tokens=1024,
            system=system_param,
            messages=user_msgs,
            temperature=0.1,
        )
        return response.content[0].text.strip()

    elif config.provider == LLMProvider.OPENAI:
        import openai

        effective_messages = _prepend_system_message(messages, system_prompt)
        client = openai.OpenAI(api_key=config.api_key)
        response = client.chat.completions.create(
            model=config.model,
            messages=effective_messages,
            max_tokens=300,
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()

    elif config.provider == LLMProvider.GOOGLE:
        import google.genai as genai

        effective_messages = _prepend_system_message(messages, system_prompt)
        client = genai.Client(api_key=config.api_key)
        system = (
            effective_messages[0]["content"]
            if effective_messages and effective_messages[0]["role"] == "system"
            else ""
        )
        user_msg = effective_messages[-1]["content"]
        response = client.models.generate_content(
            model=config.model,
            contents=f"{system}\n\n{user_msg}",
        )
        return response.text.strip()

    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def _prepend_system_message(
    messages: list[dict],
    system_prompt: str | None,
) -> list[dict]:
    """Return messages with system_prompt prepended (for non-Anthropic providers).

    If ``system_prompt`` is provided, any existing system messages are removed
    and the new system prompt is prepended.  If ``system_prompt`` is None the
    original messages are returned unchanged.
    """
    if system_prompt is None:
        return messages

    user_msgs = [m for m in messages if m["role"] != "system"]
    return [{"role": "system", "content": system_prompt}, *user_msgs]


def llm_filter_bug(
    bug: Bug,
    config: LLMBackendConfig | None = None,
    ocp_docs: list[dict] | None = None,
    krkn_docs: list[dict] | None = None,
) -> FilterResult:
    """Use LLM to determine if a bug is chaos-relevant.

    Args:
        bug: The bug to filter.
        config: LLM backend config. Auto-detected if None.
        ocp_docs: OCP architecture docs from ChromaDB (per-component search).
            When provided, the LLM understands component internals.
        krkn_docs: krkn plugin/scenario docs from ChromaDB.
            When provided, the LLM knows what krkn can actually inject.

    Falls back to the keyword filter if LLM fails.
    """
    if config is None:
        config = detect_llm_backend(phase="filter")

    if config.provider == LLMProvider.NONE:
        from src.filter.chaos_filter import filter_bug
        return filter_bug(bug)

    if bug.fixed_in_release:
        commit_detail = ""
        if bug.fix_commits:
            commit_lines = "\n".join(f"  - {c}" for c in bug.fix_commits[:5])
            commit_detail = f"\nFix commits ({bug.fix_image or 'unknown'}):\n{commit_lines}"
        fix_info = f"\nFixed in release: {bug.fixed_in_release} (chaos test still valuable for regression prevention and older z-streams){commit_detail}"
    else:
        fix_info = "\nNot yet fixed in any z-stream release."

    # Build context sections from ChromaDB
    if ocp_docs:
        doc_text = "\n---\n".join(hit["text"][:300] for hit in ocp_docs[:3])
        ocp_section = f"\nOpenShift component architecture (from docs):\n{doc_text}\n"
    else:
        ocp_section = ""

    if krkn_docs:
        krkn_text = "\n---\n".join(hit["text"][:300] for hit in krkn_docs[:3])
        krkn_section = f"\nAvailable krkn chaos scenarios for this component:\n{krkn_text}\n"
    else:
        krkn_section = ""

    prompt = f"""Analyze this OpenShift bug for chaos test relevance:

Bug Key: {bug.key}
Component: {bug.component}
Priority: {bug.priority}
Summary: {bug.summary}
Description: {bug.description[:1500] if bug.description else 'No description'}
{fix_info}
{ocp_section}{krkn_section}
Is this bug chaos-relevant? Respond with JSON only."""

    messages = [
        {"role": "user", "content": prompt},
    ]

    try:
        text = call_llm(messages, config, system_prompt=SYSTEM_PROMPT)

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        # Confidence-based escalation: re-run with Opus when confidence is low
        raw_confidence = result.get("confidence", 1.0)
        # Normalize to 0-100 scale (handle both 0-1 and 0-100 formats)
        confidence = int(raw_confidence * 100) if raw_confidence <= 1.0 else int(raw_confidence)
        confidence = max(0, min(100, confidence))

        sonnet_result = result

        if confidence < 80 and "opus" not in config.model.lower():
            logger.info(
                "FILTER escalation: %s confidence=%d, re-running with Opus",
                bug.key,
                confidence,
            )
            try:
                opus_config = detect_llm_backend(phase="analyze")
                text = call_llm(messages, opus_config, system_prompt=SYSTEM_PROMPT)

                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip()

                result = json.loads(text)
                raw_confidence = result.get("confidence", 1.0)
                confidence = int(raw_confidence * 100) if raw_confidence <= 1.0 else int(raw_confidence)
                confidence = max(0, min(100, confidence))
            except Exception as e:
                logger.warning("Opus escalation failed for %s, using Sonnet result: %s", bug.key, e)
                result = sonnet_result

        return FilterResult(
            bug=bug,
            chaos_relevant=result.get("chaos_relevant", False),
            failure_mode=result.get("failure_mode"),
            injection_method=result.get("injection_method"),
            skip_reason=result.get("skip_reason"),
            confidence=confidence / 100.0,
        )

    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning("LLM filter failed for %s, falling back to keyword: %s", bug.key, e)
        from src.filter.chaos_filter import filter_bug
        return filter_bug(bug)


def llm_filter_bugs(
    bugs: list[Bug], config: LLMBackendConfig | None = None,
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter bugs using LLM with fallback to keyword filter.

    Auto-detects the best available LLM backend if config not provided.
    """
    if config is None:
        config = detect_llm_backend()

    logger.info("LLM filter using %s (%s)", config.provider.value, config.model)

    relevant = []
    skipped = []

    for i, bug in enumerate(bugs):
        logger.info("LLM filtering %d/%d: %s", i + 1, len(bugs), bug.key)
        result = llm_filter_bug(bug, config)
        if result.chaos_relevant:
            relevant.append(result)
            logger.info("  PASS: %s (%s)", result.failure_mode, result.injection_method)
        else:
            skipped.append(result)
            logger.info("  SKIP: %s", result.skip_reason)

    logger.info("LLM filter: %d relevant, %d skipped", len(relevant), len(skipped))
    return relevant, skipped
