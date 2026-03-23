from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import ConfigError, load_config


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_config_requires_enabled_provider_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: false
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_blocks_disabled_critique_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: critique
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: false
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_allows_zero_enabled_providers_for_ui_bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: false
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: false
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
    )
    config = load_config(str(cfg))
    assert all(not provider.enabled for provider in config.providers.values())


def test_config_allows_consensus_default_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: consensus
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: true
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
    )
    config = load_config(str(cfg))
    assert config.default_mode == "consensus"


def test_config_encryption_requires_passphrase_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("MMO_MASTER_PASSPHRASE", raising=False)
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: true
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
  data_protection:
    encrypt_at_rest: true
    key_provider: passphrase
    passphrase_env: MMO_MASTER_PASSPHRASE
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_defaults_to_encryption_with_os_keyring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
""",
    )
    config = load_config(str(cfg))
    assert config.security.data_protection.encrypt_at_rest is True
    assert config.security.data_protection.key_provider == "os_keyring"


def test_config_blocks_invalid_local_quality_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
  anthropic:
    enabled: true
    api_key_env: ANTHROPIC_API_KEY
    models: { fast: a, deep: b }
    pricing_usd_per_1m_tokens:
      a: { input: 0.1, output: 0.2 }
      b: { input: 0.3, output: 0.4 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: anthropic
    refiner_provider: openai
local_routing:
  enabled: true
  local_provider_name: local
  quality_threshold: 1.5
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_blocks_invalid_rate_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    rate_limits:
      rpm: 0
      tpm: 1000
      max_wait_seconds: 1
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_allows_auto_default_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: auto
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
""",
    )
    config = load_config(str(cfg))
    assert config.default_mode == "auto"


def test_config_blocks_invalid_router_weights_learning_rate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
router_weights:
  learning_rate: 0
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_blocks_invalid_artifact_retention_days(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
artifacts:
  retention_days: 0
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_blocks_invalid_discord_daily_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    models: { fast: gpt-4o-mini, deep: gpt-4.1 }
    pricing_usd_per_1m_tokens:
      gpt-4o-mini: { input: 0.1, output: 0.2 }
      gpt-4.1: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
integrations:
  discord:
    per_user_daily_cap: 0
""",
    )
    with pytest.raises(ConfigError):
        load_config(str(cfg))


def test_config_parses_temperature_unsupported_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        """
default_mode: single
providers:
  openai:
    enabled: true
    api_key_env: OPENAI_API_KEY
    temperature_unsupported_models:
      - gpt-5-mini
      - gpt-5
    models: { fast: gpt-5-mini, deep: gpt-5 }
    pricing_usd_per_1m_tokens:
      gpt-5-mini: { input: 0.1, output: 0.2 }
      gpt-5: { input: 1.0, output: 2.0 }
budgets:
  session_usd_cap: 1
  daily_usd_cap: 2
  monthly_usd_cap: 3
  usage_file: ~/.mmo/usage.json
security:
  block_on_secrets: true
  redact_logs: true
  tool_allowlist: []
  retrieval_domain_allowlist: []
  retrieval_domain_denylist: []
routing:
  critique:
    drafter_provider: openai
    critic_provider: openai
    refiner_provider: openai
""",
    )
    config = load_config(str(cfg))
    assert config.providers["openai"].temperature_unsupported_models == ["gpt-5-mini", "gpt-5"]
