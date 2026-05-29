"""Tests for the claude_code LLM provider (Claude Code CLI backend)."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.filter.llm_config import (
    LLMBackendConfig,
    LLMProvider,
    _claude_code_available,
    detect_llm_backend,
)


def _clean_llm_env() -> dict[str, str]:
    """Return a copy of os.environ with all LLM-related vars removed."""
    keys_to_strip = {
        "LLM_PROVIDER", "LLM_MODEL",
        "LLM_FILTER_PROVIDER", "LLM_FILTER_MODEL",
        "LLM_MAP_PROVIDER", "LLM_MAP_MODEL",
        "LLM_ANALYZE_PROVIDER", "LLM_ANALYZE_MODEL",
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
        "OLLAMA_BASE_URL",
    }
    return {k: v for k, v in os.environ.items() if k not in keys_to_strip}


# ---------------------------------------------------------------------------
# _claude_code_available()
# ---------------------------------------------------------------------------


class TestClaudeCodeAvailable:

    def test_returns_true_when_claude_binary_found(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert _claude_code_available() is True

    def test_returns_false_when_claude_binary_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _claude_code_available() is False

    def test_returns_false_on_exception(self) -> None:
        with patch("shutil.which", side_effect=OSError("permission denied")):
            assert _claude_code_available() is False


# ---------------------------------------------------------------------------
# detect_llm_backend() — claude_code auto-detection
# ---------------------------------------------------------------------------


class TestClaudeCodeAutoDetection:

    def test_explicit_claude_code_provider(self) -> None:
        """LLM_PROVIDER=claude_code should select CLAUDE_CODE."""
        env = {**_clean_llm_env(), "LLM_PROVIDER": "claude_code"}
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.CLAUDE_CODE
        assert cfg.model == "claude-sonnet-4-6"

    def test_explicit_claude_code_with_custom_model(self) -> None:
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "claude_code",
            "LLM_MODEL": "claude-opus-4-6",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="default")

        assert cfg.provider == LLMProvider.CLAUDE_CODE
        assert cfg.model == "claude-opus-4-6"

    def test_auto_detects_when_cli_available_and_no_api_keys(self) -> None:
        """When no provider is set and claude CLI is on PATH, auto-select it."""
        with patch.dict(os.environ, _clean_llm_env(), clear=True), \
             patch("src.filter.llm_config._claude_code_available", return_value=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.CLAUDE_CODE

    def test_explicit_anthropic_overrides_cli_autodetect(self) -> None:
        """Explicit LLM_PROVIDER=anthropic should win over CLI auto-detection."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, env, clear=True), \
             patch("src.filter.llm_config._claude_code_available", return_value=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.ANTHROPIC

    def test_per_phase_claude_code_provider(self) -> None:
        """Per-phase LLM_FILTER_PROVIDER=claude_code should work."""
        env = {
            **_clean_llm_env(),
            "LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "LLM_FILTER_PROVIDER": "claude_code",
            "LLM_FILTER_MODEL": "claude-haiku-4-5",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.provider == LLMProvider.CLAUDE_CODE
        assert cfg.model == "claude-haiku-4-5"

    def test_claude_code_config_has_no_api_key(self) -> None:
        """CLAUDE_CODE provider should not require an API key."""
        env = {**_clean_llm_env(), "LLM_PROVIDER": "claude_code"}
        with patch.dict(os.environ, env, clear=True):
            cfg = detect_llm_backend(phase="filter")

        assert cfg.api_key is None
        assert cfg.base_url is None


# ---------------------------------------------------------------------------
# call_llm() — claude_code subprocess path
# ---------------------------------------------------------------------------


class TestClaudeCodeCallLlm:

    def _claude_code_config(self) -> LLMBackendConfig:
        return LLMBackendConfig(
            provider=LLMProvider.CLAUDE_CODE,
            model="claude-sonnet-4-6",
        )

    @patch("subprocess.run")
    def test_calls_claude_cli_with_correct_args(self, mock_run: MagicMock) -> None:
        import json
        cli_output = json.dumps({
            "result": '{"relevant": true}',
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "total_cost_usd": 0.001,
        })
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=cli_output, stderr="",
        )
        from src.filter.llm_filter import call_llm

        result = call_llm(
            messages=[
                {"role": "system", "content": "You are an expert."},
                {"role": "user", "content": "Analyze this bug."},
            ],
            config=self._claude_code_config(),
        )

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args.args[0] if call_args.args else call_args.kwargs.get("args", [])
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--model" in cmd
        assert "claude-sonnet-4-6" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "--bare" in cmd
        assert "--exclude-dynamic-system-prompt-sections" in cmd
        assert "--system-prompt" in cmd
        assert result == '{"relevant": true}'

    @patch("subprocess.run")
    def test_separates_system_and_user_messages(self, mock_run: MagicMock) -> None:
        import json
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0}),
            stderr="",
        )
        from src.filter.llm_filter import call_llm

        call_llm(
            messages=[
                {"role": "system", "content": "System prompt here."},
                {"role": "user", "content": "User question here."},
            ],
            config=self._claude_code_config(),
        )

        cmd = mock_run.call_args.args[0]
        prompt_arg = cmd[cmd.index("-p") + 1]
        assert "User question here." in prompt_arg
        assert "System prompt here." not in prompt_arg
        sys_idx = cmd.index("--system-prompt")
        assert cmd[sys_idx + 1] == "System prompt here."

    @patch("subprocess.run")
    def test_raises_on_nonzero_exit(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: invalid model",
        )
        from src.filter.llm_filter import call_llm

        with pytest.raises(RuntimeError, match="Claude Code CLI failed"):
            call_llm(
                messages=[{"role": "user", "content": "test"}],
                config=self._claude_code_config(),
            )

    @patch("subprocess.run")
    def test_strips_whitespace_from_output(self, mock_run: MagicMock) -> None:
        import json
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "  trimmed response  ", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0}),
            stderr="",
        )
        from src.filter.llm_filter import call_llm

        result = call_llm(
            messages=[{"role": "user", "content": "test"}],
            config=self._claude_code_config(),
        )
        assert result == "trimmed response"

    @patch("subprocess.run")
    def test_timeout_set_to_120_seconds(self, mock_run: MagicMock) -> None:
        import json
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"result": "ok", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0}),
            stderr="",
        )
        from src.filter.llm_filter import call_llm

        call_llm(
            messages=[{"role": "user", "content": "test"}],
            config=self._claude_code_config(),
        )

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("timeout") == 120

    @patch("subprocess.run")
    def test_accumulates_token_usage(self, mock_run: MagicMock) -> None:
        import json
        from src.filter.llm_filter import call_llm, get_token_usage, reset_token_usage

        reset_token_usage()
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({
                "result": "ok",
                "usage": {"input_tokens": 150, "output_tokens": 30,
                          "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0},
                "total_cost_usd": 0.0023,
            }),
            stderr="",
        )

        call_llm(messages=[{"role": "user", "content": "test"}], config=self._claude_code_config())
        usage = get_token_usage()

        assert usage["input_tokens"] == 650
        assert usage["output_tokens"] == 30
        assert usage["total_tokens"] == 680
        assert usage["cost_usd"] == 0.0023
        assert usage["call_count"] == 1
