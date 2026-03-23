from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import BudgetConfig
from orchestrator.security.encryption import EnvelopeCipher

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None


@dataclass(slots=True)
class BudgetState:
    session_spend: float
    daily_spend: float
    monthly_spend: float


class BudgetExceededError(Exception):
    """Raised when an API call would exceed configured spend caps."""


class BudgetTracker:
    """Tracks and enforces session/daily/monthly budget limits."""

    def __init__(self, config: BudgetConfig, cipher: EnvelopeCipher | None = None):
        self.config = config
        self.cipher = cipher
        self._session_spend = 0.0
        self.usage_path = Path(config.usage_file)
        self.lock_path = self.usage_path.with_suffix(self.usage_path.suffix + ".lock")
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self._usage = self._load_usage()
        self._rollover_if_needed(self._usage)
        self._atomic_save_usage(self._usage)

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _empty_usage(self) -> dict[str, Any]:
        return {
            "last_reset_date": self._today(),
            "last_reset_month": self._month(),
            "daily_totals": {"cost": 0.0, "requests": 0, "providers": {}},
            "monthly_totals": {"cost": 0.0, "requests": 0, "providers": {}},
            "history": {"daily": {}, "monthly": {}},
        }

    @contextmanager
    def _locked_file(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lockfile:
            if fcntl is not None:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)

    def _load_usage(self) -> dict[str, Any]:
        if not self.usage_path.exists():
            return self._empty_usage()
        text = self.usage_path.read_text()
        raw = json.loads(text)
        if isinstance(raw, dict) and raw.get("encrypted") is True:
            if self.cipher is None:
                raise RuntimeError("Usage file is encrypted but encryption is not configured")
            payload = raw.get("payload")
            if not isinstance(payload, str):
                raise RuntimeError("Invalid encrypted usage payload")
            raw = self.cipher.maybe_decrypt_json(payload)
        return self._migrate_usage(raw)

    def _migrate_usage(self, raw: dict[str, Any]) -> dict[str, Any]:
        if "daily_totals" in raw and "monthly_totals" in raw:
            raw.setdefault("history", {"daily": {}, "monthly": {}})
            raw.setdefault("last_reset_date", self._today())
            raw.setdefault("last_reset_month", self._month())
            for bucket_key in ("daily_totals", "monthly_totals"):
                bucket = raw.get(bucket_key)
                if not isinstance(bucket, dict):
                    raw[bucket_key] = {"cost": 0.0, "requests": 0, "providers": {}}
                    continue
                bucket.setdefault("cost", 0.0)
                bucket.setdefault("requests", 0)
                providers = bucket.get("providers")
                if not isinstance(providers, dict):
                    bucket["providers"] = {}
                    providers = {}
                for provider_name, entry in list(providers.items()):
                    if not isinstance(entry, dict):
                        providers[provider_name] = {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "requests": 0}
                        continue
                    entry.setdefault("cost", 0.0)
                    entry.setdefault("tokens_in", 0)
                    entry.setdefault("tokens_out", 0)
                    entry.setdefault("requests", 0)
            return raw

        migrated = self._empty_usage()
        day = self._today()
        month = self._month()

        old_daily = raw.get("daily", {})
        old_monthly = raw.get("monthly", {})
        if isinstance(old_daily, dict):
            migrated["history"]["daily"] = old_daily
            today_entry = old_daily.get(day, {"cost": 0.0, "providers": {}})
            migrated["daily_totals"] = {
                "cost": float(today_entry.get("cost", 0.0)),
                "requests": int(today_entry.get("requests", 0)),
                "providers": today_entry.get("providers", {}),
            }
        if isinstance(old_monthly, dict):
            migrated["history"]["monthly"] = old_monthly
            month_entry = old_monthly.get(month, {"cost": 0.0, "providers": {}})
            migrated["monthly_totals"] = {
                "cost": float(month_entry.get("cost", 0.0)),
                "requests": int(month_entry.get("requests", 0)),
                "providers": month_entry.get("providers", {}),
            }
        return migrated

    def _rollover_if_needed(self, usage: dict[str, Any]) -> None:
        today = self._today()
        month = self._month()

        if usage.get("last_reset_date") != today:
            prev_day = usage.get("last_reset_date")
            if prev_day:
                usage.setdefault("history", {}).setdefault("daily", {})[prev_day] = usage.get(
                    "daily_totals", {"cost": 0.0, "requests": 0, "providers": {}}
                )
            usage["daily_totals"] = {"cost": 0.0, "requests": 0, "providers": {}}
            usage["last_reset_date"] = today

        if usage.get("last_reset_month") != month:
            prev_month = usage.get("last_reset_month")
            if prev_month:
                usage.setdefault("history", {}).setdefault("monthly", {})[prev_month] = usage.get(
                    "monthly_totals", {"cost": 0.0, "requests": 0, "providers": {}}
                )
            usage["monthly_totals"] = {"cost": 0.0, "requests": 0, "providers": {}}
            usage["last_reset_month"] = month

    def _atomic_save_usage(self, usage: dict[str, Any]) -> None:
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="usage_", suffix=".json", dir=str(self.usage_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                if self.cipher is None:
                    json.dump(usage, tmp_file, indent=2, sort_keys=True)
                else:
                    encrypted = self.cipher.maybe_encrypt_json(
                        usage,
                        aad={"record_type": "usage", "orchestrator_version": "0.1.0"},
                    )
                    json.dump({"encrypted": True, "payload": encrypted}, tmp_file, indent=2, sort_keys=True)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self.usage_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _sync_and_get_usage(self) -> dict[str, Any]:
        with self._locked_file():
            self._usage = self._load_usage()
            self._rollover_if_needed(self._usage)
            self._atomic_save_usage(self._usage)
            return self._usage

    def state(self) -> BudgetState:
        usage = self._sync_and_get_usage()
        return BudgetState(
            session_spend=self._session_spend,
            daily_spend=float(usage["daily_totals"].get("cost", 0.0)),
            monthly_spend=float(usage["monthly_totals"].get("cost", 0.0)),
        )

    def check_would_fit(self, estimated_cost: float) -> None:
        current = self.state()
        if current.session_spend + estimated_cost > self.config.session_usd_cap:
            raise BudgetExceededError("Session budget cap exceeded")
        if current.daily_spend + estimated_cost > self.config.daily_usd_cap:
            raise BudgetExceededError("Daily budget cap exceeded")
        if current.monthly_spend + estimated_cost > self.config.monthly_usd_cap:
            raise BudgetExceededError("Monthly budget cap exceeded")

    def _increment_bucket(self, bucket: dict[str, Any], provider: str, cost: float, tokens_in: int, tokens_out: int) -> None:
        bucket["cost"] = float(bucket.get("cost", 0.0)) + cost
        bucket["requests"] = int(bucket.get("requests", 0)) + 1
        providers = bucket.setdefault("providers", {})
        entry = providers.setdefault(provider, {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "requests": 0})
        entry["cost"] += cost
        entry["tokens_in"] += tokens_in
        entry["tokens_out"] += tokens_out
        entry["requests"] = int(entry.get("requests", 0)) + 1

    def record_cost(self, provider: str, cost: float, tokens_in: int, tokens_out: int) -> None:
        self._session_spend += cost
        with self._locked_file():
            self._usage = self._load_usage()
            self._rollover_if_needed(self._usage)
            self._increment_bucket(self._usage["daily_totals"], provider, cost, tokens_in, tokens_out)
            self._increment_bucket(self._usage["monthly_totals"], provider, cost, tokens_in, tokens_out)
            self._atomic_save_usage(self._usage)

    def remaining(self) -> dict[str, float]:
        current = self.state()
        return {
            "session": max(0.0, self.config.session_usd_cap - current.session_spend),
            "daily": max(0.0, self.config.daily_usd_cap - current.daily_spend),
            "monthly": max(0.0, self.config.monthly_usd_cap - current.monthly_spend),
        }

    def usage_totals(self) -> dict[str, dict[str, Any]]:
        usage = self._sync_and_get_usage()
        daily = usage.get("daily_totals", {})
        monthly = usage.get("monthly_totals", {})
        return {
            "daily_totals": daily if isinstance(daily, dict) else {"cost": 0.0, "requests": 0, "providers": {}},
            "monthly_totals": monthly if isinstance(monthly, dict) else {"cost": 0.0, "requests": 0, "providers": {}},
        }
