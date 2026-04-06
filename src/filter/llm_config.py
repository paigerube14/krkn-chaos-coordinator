"""Pluggable LLM configuration for the chaos relevance filter.

Supports multiple LLM backends. Set via environment variable or .env file.
Falls back to keyword filter if no LLM is configured.

Environment variables:
  LLM_PROVIDER=ollama|anthropic|openai|google|none
  LLM_MODEL=qwen2.5-coder:14b  (or claude-sonnet-4-6, gpt-4o, gemini-2.5-pro, etc.)

  # Provider-specific keys (only need the one matching LLM_PROVIDER):
  OLLAMA_BASE_URL=http://localhost:11434  (default)
  ANTHROPIC_API_KEY=sk-ant-...
  OPENAI_API_KEY=sk-...
  GOOGLE_API_KEY=...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class LLMProvider(Enum):
    NONE = "none"          # Keyword filter only
    OLLAMA = "ollama"      # Local Ollama (default when available)
    ANTHROPIC = "anthropic"  # Claude API
    OPENAI = "openai"      # OpenAI API
    GOOGLE = "google"      # Google Gemini API


@dataclass(frozen=True)
class LLMBackendConfig:
    provider: LLMProvider
    model: str
    api_key: str | None = None
    base_url: str | None = None


def detect_llm_backend() -> LLMBackendConfig:
    """Auto-detect the best available LLM backend from environment.

    Priority: explicit LLM_PROVIDER > Anthropic key > OpenAI key > Google key > Ollama > none
    """
    explicit = os.environ.get("LLM_PROVIDER", "").lower()

    if explicit == "anthropic" or (not explicit and os.environ.get("ANTHROPIC_API_KEY")):
        model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            logger.info("LLM backend: Anthropic (%s)", model)
            return LLMBackendConfig(
                provider=LLMProvider.ANTHROPIC, model=model, api_key=key,
            )

    if explicit == "openai" or (not explicit and os.environ.get("OPENAI_API_KEY")):
        model = os.environ.get("LLM_MODEL", "gpt-4o")
        key = os.environ.get("OPENAI_API_KEY")
        if key and key != "not-needed":
            logger.info("LLM backend: OpenAI (%s)", model)
            return LLMBackendConfig(
                provider=LLMProvider.OPENAI, model=model, api_key=key,
            )

    if explicit == "google" or (not explicit and os.environ.get("GOOGLE_API_KEY")):
        model = os.environ.get("LLM_MODEL", "gemini-2.5-pro")
        key = os.environ.get("GOOGLE_API_KEY")
        if key:
            logger.info("LLM backend: Google (%s)", model)
            return LLMBackendConfig(
                provider=LLMProvider.GOOGLE, model=model, api_key=key,
            )

    if explicit == "ollama" or not explicit:
        # Check if Ollama is running
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
            model = os.environ.get("LLM_MODEL", "qwen2.5-coder:14b")
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            logger.info("LLM backend: Ollama (%s)", model)
            return LLMBackendConfig(
                provider=LLMProvider.OLLAMA, model=model, base_url=base_url,
            )
        except Exception:
            pass

    if explicit == "none":
        logger.info("LLM backend: none (keyword filter only)")
    else:
        logger.info("No LLM backend available — using keyword filter")

    return LLMBackendConfig(provider=LLMProvider.NONE, model="")
