from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass(slots=True)
class DiscordRuntimeSettings:
    enabled: bool
    bot_token_env: str
    allowed_channels: list[str]
    per_user_daily_cap: int
    daemon_base_url: str
    daemon_api_key_env: str


def load_discord_settings(config_path: str) -> DiscordRuntimeSettings:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raw = {}
    integrations = dict(raw.get("integrations", {}) or {})
    discord = dict(integrations.get("discord", {}) or {})
    server = dict(raw.get("server", {}) or {})
    host = str(server.get("host", "127.0.0.1"))
    port = int(server.get("port", 8100))
    return DiscordRuntimeSettings(
        enabled=bool(discord.get("enabled", False)),
        bot_token_env=str(discord.get("bot_token_env", "DISCORD_BOT_TOKEN")),
        allowed_channels=[str(item) for item in list(discord.get("allowed_channels", []))],
        per_user_daily_cap=max(1, int(discord.get("per_user_daily_cap", 50))),
        daemon_base_url=f"http://{host}:{port}",
        daemon_api_key_env=str(server.get("api_key_env", "MMO_SERVER_API_KEY")),
    )


def is_channel_allowed(channel_id: int | None, allowed_channels: list[str]) -> bool:
    if not allowed_channels:
        return True
    if channel_id is None:
        return False
    return str(channel_id) in set(allowed_channels)


def chunk_message(text: str, limit: int = 1800) -> list[str]:
    body = text or ""
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(body):
        chunks.append(body[cursor : cursor + limit])
        cursor += limit
    return chunks


class DailyUserLimiter:
    def __init__(self, per_user_daily_cap: int) -> None:
        self._cap = max(1, int(per_user_daily_cap))
        self._counts: dict[str, int] = {}
        self._date = datetime.now(timezone.utc).date().isoformat()

    def _rollover(self, now: datetime) -> None:
        current = now.date().isoformat()
        if current != self._date:
            self._counts = {}
            self._date = current

    def allow(self, user_id: str, now: datetime | None = None) -> tuple[bool, int]:
        now = now or datetime.now(timezone.utc)
        self._rollover(now)
        used = self._counts.get(user_id, 0)
        if used >= self._cap:
            return False, 0
        used += 1
        self._counts[user_id] = used
        return True, self._cap - used


class OrchestratorApiClient:
    def __init__(self, *, base_url: str, api_key_env: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = os.getenv(api_key_env, "")

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self._base_url}/v1/health", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def ask(self, *, query: str, mode: str) -> dict[str, Any]:
        payload = {"query": query, "mode": mode, "stream": False}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{self._base_url}/v1/ask", json=payload, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def cost(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self._base_url}/v1/cost", headers=self._headers())
            resp.raise_for_status()
            return resp.json()


async def _run_discord_bot_async(settings: DiscordRuntimeSettings) -> None:
    try:
        import discord
        from discord import app_commands
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("discord.py is required (install optional dependency group 'discord').") from exc

    token = os.getenv(settings.bot_token_env, "")
    if not token:
        raise RuntimeError(f"Discord bot token env is not set: {settings.bot_token_env}")

    api = OrchestratorApiClient(
        base_url=settings.daemon_base_url,
        api_key_env=settings.daemon_api_key_env,
    )
    await api.health()
    limiter = DailyUserLimiter(settings.per_user_daily_cap)

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    sync_done = False

    async def _send_answer(interaction: discord.Interaction, query: str, mode: str) -> None:
        if not is_channel_allowed(getattr(interaction.channel, "id", None), settings.allowed_channels):
            await interaction.response.send_message("This channel is not allowed for bot commands.", ephemeral=True)
            return

        allowed, remaining = limiter.allow(str(interaction.user.id))
        if not allowed:
            await interaction.response.send_message("Daily query cap reached for your user.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        result = await api.ask(query=query, mode=mode)
        answer = str(result.get("answer", "")).strip() or "(empty response)"
        meta = f"mode={result.get('mode')} provider={result.get('provider')} cost=${float(result.get('cost', 0.0)):.6f} remaining_today={remaining}"
        chunks = chunk_message(answer, limit=1800)
        await interaction.followup.send(f"{chunks[0]}\n\n`{meta}`")
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    @tree.command(name="ask", description="Send a single-mode query to the orchestrator daemon")
    async def ask_command(interaction: discord.Interaction, query: str) -> None:
        await _send_answer(interaction, query, "single")

    @tree.command(name="critique", description="Run critique mode via the orchestrator daemon")
    async def critique_command(interaction: discord.Interaction, query: str) -> None:
        await _send_answer(interaction, query, "critique")

    @tree.command(name="debate", description="Run debate mode via the orchestrator daemon")
    async def debate_command(interaction: discord.Interaction, query: str) -> None:
        await _send_answer(interaction, query, "debate")

    @tree.command(name="cost", description="Show orchestrator budget usage")
    async def cost_command(interaction: discord.Interaction) -> None:
        if not is_channel_allowed(getattr(interaction.channel, "id", None), settings.allowed_channels):
            await interaction.response.send_message("This channel is not allowed for bot commands.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        payload = await api.cost()
        remaining = payload.get("remaining", {})
        state = payload.get("state", {})
        lines = [
            "Budget status:",
            f"- session_spend: ${float(state.get('session_spend', 0.0)):.6f}",
            f"- daily_spend: ${float(state.get('daily_spend', 0.0)):.6f}",
            f"- monthly_spend: ${float(state.get('monthly_spend', 0.0)):.6f}",
            f"- remaining_session: ${float(remaining.get('session', 0.0)):.6f}",
            f"- remaining_daily: ${float(remaining.get('daily', 0.0)):.6f}",
            f"- remaining_monthly: ${float(remaining.get('monthly', 0.0)):.6f}",
        ]
        await interaction.followup.send("\n".join(lines))

    @client.event
    async def on_ready() -> None:
        nonlocal sync_done
        if not sync_done:
            await tree.sync()
            sync_done = True
        print(f"Discord bot connected as {client.user}")  # noqa: T201

    try:
        await client.start(token)
    finally:
        await client.close()


def run_discord_bot(config_path: str) -> None:
    settings = load_discord_settings(config_path)
    if not settings.enabled:
        raise RuntimeError("integrations.discord.enabled is false in config.")
    asyncio.run(_run_discord_bot_async(settings))

