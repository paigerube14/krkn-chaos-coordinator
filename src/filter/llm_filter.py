"""LLM-enhanced chaos relevance filter — pluggable backend.

Supports: Ollama (local), Anthropic (Claude API), OpenAI, Google Gemini.
Auto-detects the best available backend from environment variables.
"""

from __future__ import annotations

import json
import logging

from src.filter.llm_config import LLMBackendConfig, LLMProvider, detect_llm_backend
from src.models import Bug, FilterResult

logger = logging.getLogger(__name__)

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
  "failure_mode": "brief description of the failure mode" or null,
  "injection_method": "which krkn injection type would test this" or null,
  "skip_reason": "why this is not chaos-relevant" or null,
  "confidence": 0.0-1.0
}"""


def call_llm(messages: list[dict], config: LLMBackendConfig) -> str:
    """Call the configured LLM backend and return the response text."""
    if config.provider == LLMProvider.OLLAMA:
        import ollama
        response = ollama.chat(
            model=config.model,
            messages=messages,
            options={"temperature": 0.1, "num_predict": 300},
        )
        return response["message"]["content"].strip()

    elif config.provider == LLMProvider.ANTHROPIC:
        import anthropic
        client = anthropic.Anthropic(api_key=config.api_key)
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        user_msgs = [m for m in messages if m["role"] != "system"]
        response = client.messages.create(
            model=config.model,
            max_tokens=300,
            system=system,
            messages=user_msgs,
            temperature=0.1,
        )
        return response.content[0].text.strip()

    elif config.provider == LLMProvider.OPENAI:
        import openai
        client = openai.OpenAI(api_key=config.api_key)
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            max_tokens=300,
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()

    elif config.provider == LLMProvider.GOOGLE:
        import google.genai as genai
        client = genai.Client(api_key=config.api_key)
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        user_msg = messages[-1]["content"]
        response = client.models.generate_content(
            model=config.model,
            contents=f"{system}\n\n{user_msg}",
        )
        return response.text.strip()

    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def llm_filter_bug(bug: Bug, config: LLMBackendConfig | None = None) -> FilterResult:
    """Use LLM to determine if a bug is chaos-relevant.

    Auto-detects the best available LLM backend if config not provided.
    Falls back to the keyword filter if LLM fails.
    """
    if config is None:
        config = detect_llm_backend()

    if config.provider == LLMProvider.NONE:
        from src.filter.chaos_filter import filter_bug
        return filter_bug(bug)

    prompt = f"""Analyze this OpenShift bug for chaos test relevance:

Bug Key: {bug.key}
Component: {bug.component}
Priority: {bug.priority}
Summary: {bug.summary}
Description: {bug.description[:1500] if bug.description else 'No description'}

Is this bug chaos-relevant? Respond with JSON only."""

    try:
        text = call_llm(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            config=config,
        )

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        return FilterResult(
            bug=bug,
            chaos_relevant=result.get("chaos_relevant", False),
            failure_mode=result.get("failure_mode"),
            injection_method=result.get("injection_method"),
            skip_reason=result.get("skip_reason"),
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
