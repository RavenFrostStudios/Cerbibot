from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock


DOMAINS = ("coding", "factual", "creative", "security", "math", "general")
logger = logging.getLogger(__name__)


def classify_domain(query: str) -> str:
    text = query.lower()
    if any(term in text for term in ("vulnerability", "threat", "secure", "security", "exploit", "cve", "auth")):
        return "security"
    if any(term in text for term in ("python", "javascript", "java", "rust", "code", "function", "api", "debug")):
        return "coding"
    if any(term in text for term in ("solve", "equation", "integral", "derivative", "algebra", "probability")):
        return "math"
    if any(term in text for term in ("latest", "today", "current", "fact", "citation", "source", "regulation", "law")):
        return "factual"
    if any(term in text for term in ("story", "poem", "creative", "brainstorm", "slogan", "fiction")):
        return "creative"
    return "general"


@dataclass(slots=True)
class DomainWeight:
    score: float = 0.5
    quality_ema: float = 0.5
    cost_ema: float = 0.01
    latency_ema: float = 1000.0
    error_ema: float = 0.0
    count: int = 0
    latencies_ms: list[int] = field(default_factory=list)


class RouterWeights:
    def __init__(self, *, providers: list[str], weights_file: str, learning_rate: float = 0.2):
        self.providers = list(dict.fromkeys(providers))
        self.weights_path = Path(weights_file).expanduser()
        self.learning_rate = max(0.01, min(1.0, learning_rate))
        self._lock = RLock()
        self._table: dict[str, dict[str, DomainWeight]] = {}
        self._init_defaults()
        self._load()

    def _init_defaults(self) -> None:
        self._table = {
            provider: {domain: DomainWeight() for domain in DOMAINS}
            for provider in self.providers
        }

    def _load(self) -> None:
        with self._lock:
            if not self.weights_path.exists():
                return
            try:
                raw = json.loads(self.weights_path.read_text(encoding="utf-8"))
            except OSError as exc:
                logger.warning("router_weights_load_failed", extra={"error": str(exc), "path": str(self.weights_path)})
                return
            if not isinstance(raw, dict):
                return
            table = raw.get("table", {})
            if not isinstance(table, dict):
                return
            for provider, per_domain in table.items():
                if provider not in self._table or not isinstance(per_domain, dict):
                    continue
                for domain, payload in per_domain.items():
                    if domain not in self._table[provider] or not isinstance(payload, dict):
                        continue
                    item = self._table[provider][domain]
                    item.score = float(payload.get("score", item.score))
                    item.quality_ema = float(payload.get("quality_ema", item.quality_ema))
                    item.cost_ema = float(payload.get("cost_ema", item.cost_ema))
                    item.latency_ema = float(payload.get("latency_ema", item.latency_ema))
                    item.error_ema = float(payload.get("error_ema", item.error_ema))
                    item.count = int(payload.get("count", item.count))
                    latencies = payload.get("latencies_ms", [])
                    if isinstance(latencies, list):
                        item.latencies_ms = [int(v) for v in latencies if isinstance(v, (int, float))][-200:]

    def _save(self) -> None:
        with self._lock:
            try:
                self.weights_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "learning_rate": self.learning_rate,
                    "table": {
                        provider: {domain: asdict(weight) for domain, weight in per_domain.items()}
                        for provider, per_domain in self._table.items()
                    },
                }
                self.weights_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("router_weights_save_failed", extra={"error": str(exc), "path": str(self.weights_path)})

    def score(self, provider: str, domain: str) -> float:
        with self._lock:
            return float(self._table.get(provider, {}).get(domain, DomainWeight()).score)

    def rank(self, providers: list[str], domain: str) -> list[str]:
        return sorted(providers, key=lambda p: self.score(p, domain), reverse=True)

    def record_success(
        self,
        *,
        provider: str,
        domain: str,
        quality: float,
        cost: float,
        latency_ms: int,
        had_warning: bool = False,
    ) -> None:
        with self._lock:
            weight = self._table.get(provider, {}).get(domain)
            if weight is None:
                return
            lr = self.learning_rate
            weight.quality_ema = self._ema(weight.quality_ema, self._clamp01(quality), lr)
            weight.cost_ema = self._ema(weight.cost_ema, max(0.0, cost), lr)
            weight.latency_ema = self._ema(weight.latency_ema, float(max(1, latency_ms)), lr)
            weight.error_ema = self._ema(weight.error_ema, 1.0 if had_warning else 0.0, lr)
            weight.count += 1
            weight.latencies_ms.append(int(max(1, latency_ms)))
            weight.latencies_ms = weight.latencies_ms[-200:]
            self._recompute_score(weight)
            self._save()

    def record_failure(self, *, provider: str, domain: str) -> None:
        with self._lock:
            weight = self._table.get(provider, {}).get(domain)
            if weight is None:
                return
            lr = self.learning_rate
            weight.error_ema = self._ema(weight.error_ema, 1.0, lr)
            weight.count += 1
            self._recompute_score(weight)
            self._save()

    def snapshot(self) -> dict[str, dict[str, dict[str, float | int]]]:
        out: dict[str, dict[str, dict[str, float | int]]] = {}
        with self._lock:
            for provider, per_domain in self._table.items():
                out[provider] = {}
                for domain, weight in per_domain.items():
                    out[provider][domain] = {
                        "score": round(weight.score, 4),
                        "quality_ema": round(weight.quality_ema, 4),
                        "cost_ema": round(weight.cost_ema, 6),
                        "latency_ema": round(weight.latency_ema, 2),
                        "error_ema": round(weight.error_ema, 4),
                        "count": weight.count,
                        "p50_latency_ms": self._percentile(weight.latencies_ms, 50),
                        "p95_latency_ms": self._percentile(weight.latencies_ms, 95),
                    }
        return out

    def reset(self) -> None:
        with self._lock:
            self._init_defaults()
            self._save()

    @staticmethod
    def _ema(prev: float, value: float, lr: float) -> float:
        return (1.0 - lr) * prev + (lr * value)

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _percentile(values: list[int], pct: int) -> int:
        if not values:
            return 0
        sorted_vals = sorted(values)
        idx = int((len(sorted_vals) - 1) * (pct / 100.0))
        return int(sorted_vals[idx])

    def _recompute_score(self, weight: DomainWeight) -> None:
        quality = self._clamp01(weight.quality_ema)
        # Quality-per-dollar proxy; cheap+high quality converges near 1.0.
        quality_per_dollar = self._clamp01((quality / max(0.001, weight.cost_ema)) * 0.01)
        latency_factor = self._clamp01(1.0 - min(weight.latency_ema / 10000.0, 1.0))
        reliability = self._clamp01(1.0 - weight.error_ema)
        weight.score = self._clamp01(
            (0.45 * quality) + (0.25 * quality_per_dollar) + (0.15 * latency_factor) + (0.15 * reliability)
        )
