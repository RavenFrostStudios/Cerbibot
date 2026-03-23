from __future__ import annotations

from datetime import datetime, timezone

from integrations.discord_bot import DailyUserLimiter, chunk_message, is_channel_allowed, load_discord_settings


def test_discord_settings_defaults(tmp_path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}", encoding="utf-8")
    settings = load_discord_settings(str(cfg))
    assert settings.enabled is False
    assert settings.bot_token_env == "DISCORD_BOT_TOKEN"
    assert settings.per_user_daily_cap == 50
    assert settings.daemon_base_url == "http://127.0.0.1:8100"


def test_discord_channel_allowlist() -> None:
    assert is_channel_allowed(123, []) is True
    assert is_channel_allowed(123, ["123"]) is True
    assert is_channel_allowed(123, ["456"]) is False
    assert is_channel_allowed(None, ["456"]) is False


def test_discord_message_chunking() -> None:
    chunks = chunk_message("a" * 4001, limit=1800)
    assert len(chunks) == 3
    assert len(chunks[0]) == 1800
    assert len(chunks[1]) == 1800
    assert len(chunks[2]) == 401


def test_daily_user_limiter_rollover() -> None:
    limiter = DailyUserLimiter(per_user_daily_cap=2)
    now = datetime(2026, 2, 10, 10, 0, tzinfo=timezone.utc)
    allowed, remaining = limiter.allow("u1", now=now)
    assert allowed is True and remaining == 1
    allowed, remaining = limiter.allow("u1", now=now)
    assert allowed is True and remaining == 0
    allowed, remaining = limiter.allow("u1", now=now)
    assert allowed is False and remaining == 0

    tomorrow = datetime(2026, 2, 11, 10, 0, tzinfo=timezone.utc)
    allowed, remaining = limiter.allow("u1", now=tomorrow)
    assert allowed is True and remaining == 1

