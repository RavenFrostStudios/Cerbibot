from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ProviderModelConfig:
    fast: str
    deep: str


@dataclass(slots=True)
class ProviderPricing:
    input: float
    output: float


@dataclass(slots=True)
class ProviderTimeoutConfig:
    standard_seconds: float = 60.0
    deep_seconds: float = 120.0


@dataclass(slots=True)
class ProviderRateLimitConfig:
    rpm: int = 60
    tpm: int = 120_000
    max_wait_seconds: float = 5.0


@dataclass(slots=True)
class ProviderConfig:
    enabled: bool
    api_key_env: str
    models: ProviderModelConfig
    pricing_usd_per_1m_tokens: dict[str, ProviderPricing]
    timeouts: ProviderTimeoutConfig = field(default_factory=ProviderTimeoutConfig)
    rate_limits: ProviderRateLimitConfig = field(default_factory=ProviderRateLimitConfig)
    temperature_unsupported_models: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BudgetConfig:
    session_usd_cap: float
    daily_usd_cap: float
    monthly_usd_cap: float
    usage_file: str


@dataclass(slots=True)
class DataProtectionConfig:
    encrypt_at_rest: bool = True
    key_provider: str = "os_keyring"
    passphrase_env: str = "MMO_MASTER_PASSPHRASE"


@dataclass(slots=True)
class SecurityConfig:
    block_on_secrets: bool
    redact_logs: bool
    tool_allowlist: list[str]
    retrieval_domain_allowlist: list[str]
    retrieval_domain_denylist: list[str]
    data_protection: DataProtectionConfig = field(default_factory=DataProtectionConfig)


@dataclass(slots=True)
class CritiqueRoutingConfig:
    drafter_provider: str
    critic_provider: str
    refiner_provider: str


@dataclass(slots=True)
class RoutingConfig:
    critique: CritiqueRoutingConfig


@dataclass(slots=True)
class RetrievalConfig:
    search_provider: str
    max_results: int
    max_fetch_bytes: int
    timeout_seconds: float


@dataclass(slots=True)
class LocalRoutingConfig:
    enabled: bool = True
    local_provider_name: str = "local"
    quality_threshold: float = 0.65


@dataclass(slots=True)
class ServerConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8100
    api_key_env: str = "MMO_SERVER_API_KEY"
    cors_origins: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RouterWeightsConfig:
    enabled: bool = True
    learning_rate: float = 0.2
    weights_file: str = "~/.mmo/router_weights.json"
    domain_provider_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ArtifactsConfig:
    enabled: bool = True
    directory: str = "~/.mmo/artifacts"
    retention_days: int = 30


@dataclass(slots=True)
class PromptsConfig:
    directory: str = "prompts"
    selection: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DiscordIntegrationConfig:
    enabled: bool = False
    bot_token_env: str = "DISCORD_BOT_TOKEN"
    allowed_channels: list[str] = field(default_factory=list)
    per_user_daily_cap: int = 50


@dataclass(slots=True)
class IntegrationsConfig:
    discord: DiscordIntegrationConfig = field(default_factory=DiscordIntegrationConfig)


@dataclass(slots=True)
class SkillsConfig:
    require_signature: bool = False
    trusted_public_keys: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    default_mode: str
    providers: dict[str, ProviderConfig]
    budgets: BudgetConfig
    security: SecurityConfig
    routing: RoutingConfig
    retrieval: RetrievalConfig
    local_routing: LocalRoutingConfig = field(default_factory=LocalRoutingConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    router_weights: RouterWeightsConfig = field(default_factory=RouterWeightsConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _require_keys(data: dict[str, Any], keys: list[str], section: str) -> None:
    for key in keys:
        if key not in data:
            raise ConfigError(f"Missing key '{key}' in section '{section}'")


def _expand_path(path: str) -> str:
    return str(Path(path).expanduser())


def load_config(config_path: str) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config not found: {config_path}")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    _require_keys(raw, ["default_mode", "providers", "budgets", "security", "routing"], "root")

    providers = {}
    if not isinstance(raw["providers"], dict) or not raw["providers"]:
        raise ConfigError("providers must be a non-empty mapping")
    for name, pdata in raw["providers"].items():
        _require_keys(pdata, ["enabled", "api_key_env", "models", "pricing_usd_per_1m_tokens"], f"providers.{name}")
        _require_keys(pdata["models"], ["fast", "deep"], f"providers.{name}.models")
        if not isinstance(pdata["pricing_usd_per_1m_tokens"], dict) or not pdata["pricing_usd_per_1m_tokens"]:
            raise ConfigError(f"providers.{name}.pricing_usd_per_1m_tokens must be a non-empty mapping")
        pricing = {}
        for model, price in pdata["pricing_usd_per_1m_tokens"].items():
            _require_keys(price, ["input", "output"], f"providers.{name}.pricing_usd_per_1m_tokens.{model}")
            pricing[model] = ProviderPricing(input=float(price["input"]), output=float(price["output"]))
        timeout_cfg = pdata.get("timeouts", {})
        rate_limit_cfg = pdata.get("rate_limits", {})
        timeouts = ProviderTimeoutConfig(
            standard_seconds=float(timeout_cfg.get("standard_seconds", 60.0)),
            deep_seconds=float(timeout_cfg.get("deep_seconds", 120.0)),
        )
        providers[name] = ProviderConfig(
            enabled=bool(pdata["enabled"]),
            api_key_env=str(pdata["api_key_env"]),
            models=ProviderModelConfig(
                fast=str(pdata["models"]["fast"]),
                deep=str(pdata["models"]["deep"]),
            ),
            pricing_usd_per_1m_tokens=pricing,
            timeouts=timeouts,
            rate_limits=ProviderRateLimitConfig(
                rpm=int(rate_limit_cfg.get("rpm", 60)),
                tpm=int(rate_limit_cfg.get("tpm", 120_000)),
                max_wait_seconds=float(rate_limit_cfg.get("max_wait_seconds", 5.0)),
            ),
            temperature_unsupported_models=[str(m) for m in pdata.get("temperature_unsupported_models", [])],
        )

    budgets_raw = raw["budgets"]
    _require_keys(budgets_raw, ["session_usd_cap", "daily_usd_cap", "monthly_usd_cap", "usage_file"], "budgets")
    budgets = BudgetConfig(
        session_usd_cap=float(budgets_raw["session_usd_cap"]),
        daily_usd_cap=float(budgets_raw["daily_usd_cap"]),
        monthly_usd_cap=float(budgets_raw["monthly_usd_cap"]),
        usage_file=_expand_path(str(budgets_raw["usage_file"])),
    )

    security_raw = raw["security"]
    _require_keys(
        security_raw,
        ["block_on_secrets", "redact_logs", "tool_allowlist", "retrieval_domain_allowlist", "retrieval_domain_denylist"],
        "security",
    )
    security = SecurityConfig(
        block_on_secrets=bool(security_raw["block_on_secrets"]),
        redact_logs=bool(security_raw["redact_logs"]),
        tool_allowlist=list(security_raw["tool_allowlist"]),
        retrieval_domain_allowlist=list(security_raw["retrieval_domain_allowlist"]),
        retrieval_domain_denylist=list(security_raw["retrieval_domain_denylist"]),
        data_protection=DataProtectionConfig(
            encrypt_at_rest=bool((security_raw.get("data_protection") or {}).get("encrypt_at_rest", True)),
            key_provider=str((security_raw.get("data_protection") or {}).get("key_provider", "os_keyring")),
            passphrase_env=str((security_raw.get("data_protection") or {}).get("passphrase_env", "MMO_MASTER_PASSPHRASE")),
        ),
    )

    if not isinstance(raw["routing"], dict) or "critique" not in raw["routing"]:
        raise ConfigError("routing.critique section is required")
    routing_raw = raw["routing"]["critique"]
    _require_keys(routing_raw, ["drafter_provider", "critic_provider", "refiner_provider"], "routing.critique")
    routing = RoutingConfig(
        critique=CritiqueRoutingConfig(
            drafter_provider=str(routing_raw["drafter_provider"]),
            critic_provider=str(routing_raw["critic_provider"]),
            refiner_provider=str(routing_raw["refiner_provider"]),
        )
    )

    retrieval_raw = raw.get("retrieval", {})
    retrieval = RetrievalConfig(
        search_provider=str(retrieval_raw.get("search_provider", "auto")),
        max_results=int(retrieval_raw.get("max_results", 5)),
        max_fetch_bytes=int(retrieval_raw.get("max_fetch_bytes", 200_000)),
        timeout_seconds=float(retrieval_raw.get("timeout_seconds", 10.0)),
    )
    local_routing_raw = raw.get("local_routing", {}) or {}
    local_routing = LocalRoutingConfig(
        enabled=bool(local_routing_raw.get("enabled", True)),
        local_provider_name=str(local_routing_raw.get("local_provider_name", "local")),
        quality_threshold=float(local_routing_raw.get("quality_threshold", 0.65)),
    )
    server_raw = raw.get("server", {}) or {}
    server = ServerConfig(
        enabled=bool(server_raw.get("enabled", False)),
        host=str(server_raw.get("host", "127.0.0.1")),
        port=int(server_raw.get("port", 8100)),
        api_key_env=str(server_raw.get("api_key_env", "MMO_SERVER_API_KEY")),
        cors_origins=[str(item) for item in list(server_raw.get("cors_origins", []))],
    )
    router_weights_raw = raw.get("router_weights", {}) or {}
    router_weights = RouterWeightsConfig(
        enabled=bool(router_weights_raw.get("enabled", True)),
        learning_rate=float(router_weights_raw.get("learning_rate", 0.2)),
        weights_file=_expand_path(str(router_weights_raw.get("weights_file", "~/.mmo/router_weights.json"))),
        domain_provider_overrides={
            str(k): str(v) for k, v in dict(router_weights_raw.get("domain_provider_overrides", {}) or {}).items()
        },
    )
    artifacts_raw = raw.get("artifacts", {}) or {}
    artifacts = ArtifactsConfig(
        enabled=bool(artifacts_raw.get("enabled", True)),
        directory=_expand_path(str(artifacts_raw.get("directory", "~/.mmo/artifacts"))),
        retention_days=int(artifacts_raw.get("retention_days", 30)),
    )
    prompts_raw = raw.get("prompts", {}) or {}
    prompts = PromptsConfig(
        directory=str(prompts_raw.get("directory", "prompts")),
        selection={str(k): str(v) for k, v in dict(prompts_raw.get("selection", {}) or {}).items()},
    )
    integrations_raw = raw.get("integrations", {}) or {}
    discord_raw = dict(integrations_raw.get("discord", {}) or {})
    integrations = IntegrationsConfig(
        discord=DiscordIntegrationConfig(
            enabled=bool(discord_raw.get("enabled", False)),
            bot_token_env=str(discord_raw.get("bot_token_env", "DISCORD_BOT_TOKEN")),
            allowed_channels=[str(item) for item in list(discord_raw.get("allowed_channels", []))],
            per_user_daily_cap=int(discord_raw.get("per_user_daily_cap", 50)),
        )
    )
    skills_raw = raw.get("skills", {}) or {}
    skills = SkillsConfig(
        require_signature=bool(skills_raw.get("require_signature", False)),
        trusted_public_keys=[_expand_path(str(item)) for item in list(skills_raw.get("trusted_public_keys", []))],
    )

    config = AppConfig(
        default_mode=str(raw["default_mode"]),
        providers=providers,
        budgets=budgets,
        security=security,
        routing=routing,
        retrieval=retrieval,
        local_routing=local_routing,
        server=server,
        router_weights=router_weights,
        artifacts=artifacts,
        prompts=prompts,
        integrations=integrations,
        skills=skills,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.default_mode not in {"single", "critique", "retrieval", "debate", "consensus", "council", "auto"}:
        raise ConfigError(
            "default_mode must be 'single', 'critique', 'retrieval', 'debate', 'consensus', 'council', or 'auto'"
        )

    enabled_providers = set()
    for provider_name, provider in config.providers.items():
        env_has_key = bool(os.getenv(provider.api_key_env))
        stored_has_key = _provider_key_exists(provider.api_key_env)
        if provider.enabled and not (env_has_key or stored_has_key):
            raise ConfigError(
                f"Provider '{provider_name}' is enabled but no key found in env var '{provider.api_key_env}' or keyring"
            )
        if provider.enabled:
            enabled_providers.add(provider_name)
        model_names = {provider.models.fast, provider.models.deep}
        missing = [m for m in model_names if m not in provider.pricing_usd_per_1m_tokens]
        if missing:
            raise ConfigError(f"Missing pricing entries for provider '{provider_name}': {missing}")
        for model_name, price in provider.pricing_usd_per_1m_tokens.items():
            if price.input < 0 or price.output < 0:
                raise ConfigError(f"Negative pricing is not allowed: provider={provider_name} model={model_name}")
        if provider.timeouts.standard_seconds <= 0 or provider.timeouts.deep_seconds <= 0:
            raise ConfigError(f"Provider timeouts must be > 0 for provider '{provider_name}'")
        if provider.rate_limits.rpm <= 0 or provider.rate_limits.tpm <= 0:
            raise ConfigError(f"Provider rate limits must be > 0 for provider '{provider_name}'")
        if provider.rate_limits.max_wait_seconds < 0:
            raise ConfigError(f"Provider rate_limits.max_wait_seconds must be >= 0 for provider '{provider_name}'")

    if config.routing.critique.drafter_provider not in config.providers:
        raise ConfigError("routing.critique.drafter_provider is not configured")
    if config.routing.critique.critic_provider not in config.providers:
        raise ConfigError("routing.critique.critic_provider is not configured")
    if config.routing.critique.refiner_provider not in config.providers:
        raise ConfigError("routing.critique.refiner_provider is not configured")
    critique_set = {
        config.routing.critique.drafter_provider,
        config.routing.critique.critic_provider,
        config.routing.critique.refiner_provider,
    }
    if enabled_providers:
        disabled_targets = sorted(critique_set.difference(enabled_providers))
        if disabled_targets:
            raise ConfigError(f"Critique routing references disabled providers: {disabled_targets}")

    if min(
        config.budgets.session_usd_cap,
        config.budgets.daily_usd_cap,
        config.budgets.monthly_usd_cap,
    ) <= 0:
        raise ConfigError("All budget caps must be > 0")
    if config.retrieval.max_results <= 0:
        raise ConfigError("retrieval.max_results must be > 0")
    if config.retrieval.max_fetch_bytes <= 0:
        raise ConfigError("retrieval.max_fetch_bytes must be > 0")
    if config.retrieval.timeout_seconds <= 0:
        raise ConfigError("retrieval.timeout_seconds must be > 0")
    allowed_search_providers = {
        "auto",
        "duckduckgo_html",
        "browser_brave",
        "browser_duckduckgo",
        "brave_api",
        "tavily_api",
        "exa_api",
        "serpapi",
    }
    if config.retrieval.search_provider not in allowed_search_providers:
        raise ConfigError(
            f"retrieval.search_provider must be one of {sorted(allowed_search_providers)}"
        )
    if config.local_routing.quality_threshold < 0 or config.local_routing.quality_threshold > 1:
        raise ConfigError("local_routing.quality_threshold must be between 0 and 1")
    if config.server.port <= 0 or config.server.port > 65535:
        raise ConfigError("server.port must be in 1..65535")
    if config.router_weights.learning_rate <= 0 or config.router_weights.learning_rate > 1:
        raise ConfigError("router_weights.learning_rate must be in (0, 1]")
    if config.artifacts.retention_days <= 0:
        raise ConfigError("artifacts.retention_days must be > 0")
    if config.integrations.discord.per_user_daily_cap <= 0:
        raise ConfigError("integrations.discord.per_user_daily_cap must be > 0")
    for key_path in config.skills.trusted_public_keys:
        if not key_path.strip():
            raise ConfigError("skills.trusted_public_keys cannot contain empty values")
    for domain, provider in config.router_weights.domain_provider_overrides.items():
        if provider not in config.providers:
            raise ConfigError(
                f"router_weights.domain_provider_overrides references unknown provider: domain={domain} provider={provider}"
            )
    if config.security.data_protection.key_provider not in {"passphrase", "os_keyring"}:
        raise ConfigError("security.data_protection.key_provider must be 'passphrase' or 'os_keyring'")
    if (
        config.security.data_protection.encrypt_at_rest
        and config.security.data_protection.key_provider == "passphrase"
        and not os.getenv(config.security.data_protection.passphrase_env)
    ):
        raise ConfigError(
            f"Encryption enabled but passphrase env '{config.security.data_protection.passphrase_env}' is not set"
        )


def _provider_key_exists(api_key_env: str) -> bool:
    try:
        from orchestrator.security.keyring import has_secret

        return has_secret(api_key_env)
    except Exception:
        return False
