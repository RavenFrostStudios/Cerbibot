from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any

import yaml

from orchestrator.skills.registry import SkillRecord, discover_skills


_TOKEN_RE = re.compile(r"[a-z0-9_][a-z0-9_\\-]{1,}")
_INPUT_RE = re.compile(r"\$input\.([a-zA-Z0-9_]+)")
_GENERIC_TOKENS = {
    "a",
    "an",
    "and",
    "content",
    "data",
    "docs",
    "for",
    "from",
    "local",
    "none",
    "page",
    "pages",
    "public",
    "read",
    "reads",
    "repo",
    "repository",
    "run",
    "skill",
    "the",
    "tool",
    "tools",
    "using",
    "web",
    "workflow",
    "write",
}
_TOKEN_EQUIVALENTS = {
    "analyse": "analyze",
    "analysis": "analyze",
    "brief": "summary",
    "doc": "document",
    "docs": "document",
    "fetch": "retrieve",
    "fetched": "retrieve",
    "fetching": "retrieve",
    "healthcheck": "health",
    "page": "document",
    "pages": "document",
    "repo": "repository",
    "summarise": "summarize",
    "summary": "summarize",
}


@dataclass(slots=True)
class SkillProfile:
    name: str
    path: str
    enabled: bool
    signature_verified: bool
    risk_level: str
    capabilities: list[str]
    io_inputs: list[str]
    io_outputs: list[str]
    dependencies: list[str]
    checksum: str


@dataclass(slots=True)
class Candidate:
    skill_a: str
    skill_b: str
    score: float
    capability_overlap: float
    io_overlap: float
    dependency_overlap: float
    rationale: str
    recommendation: str


def _tokenize(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {_normalize_token(tok) for tok in _TOKEN_RE.findall(value.lower()) if _normalize_token(tok)}


def _normalize_token(token: str) -> str:
    value = token.strip().lower().replace("-", "_")
    if not value:
        return ""
    return _TOKEN_EQUIVALENTS.get(value, value)


def _normalized_signal_tokens(values: list[str]) -> set[str]:
    normalized = {_normalize_token(value) for value in values}
    return {token for token in normalized if token and token not in _GENERIC_TOKENS}


def _normalize_io_name(value: str) -> str:
    tokens = [_normalize_token(tok) for tok in _TOKEN_RE.findall(value.lower())]
    kept = [tok for tok in tokens if tok and tok not in {"data", "final", "output", "raw", "result", "tmp", "temp", "value"}]
    if not kept:
        kept = [tok for tok in tokens if tok]
    return "_".join(sorted(dict.fromkeys(kept)))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _read_workflow(path: str) -> dict[str, Any] | None:
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return None
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _extract_profile(record: SkillRecord) -> SkillProfile | None:
    raw = _read_workflow(record.path)
    if raw is None:
        return None
    manifest = raw.get("manifest", {}) if isinstance(raw.get("manifest"), dict) else {}
    steps = raw.get("steps", []) if isinstance(raw.get("steps"), list) else []
    name = str(raw.get("name", record.name)).strip() or record.name
    risk_level = str(raw.get("risk_level", "unknown")).strip() or "unknown"

    capabilities: set[str] = set()
    dependencies: set[str] = set()
    inputs: set[str] = set()
    outputs: set[str] = set()

    for key in ("purpose",):
        capabilities |= _tokenize(manifest.get(key))
    for list_key in ("tools", "permissions", "data_scope"):
        items = manifest.get(list_key, [])
        if isinstance(items, list):
            for item in items:
                capabilities |= _tokenize(item)
                dependencies |= _tokenize(item)

    for step in steps:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool", "")).strip()
        model_call = str(step.get("model_call", "")).strip()
        if tool:
            capabilities |= _tokenize(tool)
            dependencies |= _tokenize(tool)
        if model_call:
            capabilities |= _tokenize(model_call)
        output_name = str(step.get("output", "")).strip()
        if output_name:
            normalized_output = _normalize_io_name(output_name)
            if normalized_output:
                outputs.add(normalized_output)
        args = step.get("args", {})
        if isinstance(args, dict):
            for value in args.values():
                if isinstance(value, str):
                    for match in _INPUT_RE.finditer(value):
                        normalized_input = _normalize_io_name(match.group(1))
                        if normalized_input:
                            inputs.add(normalized_input)

    return SkillProfile(
        name=name,
        path=record.path,
        enabled=record.enabled,
        signature_verified=record.signature_verified,
        risk_level=risk_level,
        capabilities=sorted(capabilities),
        io_inputs=sorted(inputs),
        io_outputs=sorted(outputs),
        dependencies=sorted(dependencies),
        checksum=record.checksum,
    )


def build_skill_profiles(*, include_disabled: bool = False) -> list[SkillProfile]:
    profiles: list[SkillProfile] = []
    for name, record in sorted(discover_skills().items()):
        if not include_disabled and not record.enabled:
            continue
        profile = _extract_profile(record)
        if profile is not None:
            profiles.append(profile)
    return profiles


def _pairwise_candidates(
    profiles: list[SkillProfile],
    *,
    merge_threshold: float,
    crossover_min: float,
    crossover_max_io: float,
) -> tuple[list[Candidate], list[Candidate]]:
    merge: list[Candidate] = []
    crossover: list[Candidate] = []
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            a = profiles[i]
            b = profiles[j]
            cap = _jaccard(set(a.capabilities), set(b.capabilities))
            io_a = set(a.io_inputs) | set(a.io_outputs)
            io_b = set(b.io_inputs) | set(b.io_outputs)
            io = _jaccard(io_a, io_b)
            deps = _jaccard(set(a.dependencies), set(b.dependencies))
            shared_signals = _normalized_signal_tokens(a.capabilities) & _normalized_signal_tokens(b.capabilities)
            score = round(0.5 * cap + 0.3 * io + 0.2 * deps, 4)
            base = Candidate(
                skill_a=a.name,
                skill_b=b.name,
                score=score,
                capability_overlap=round(cap, 4),
                io_overlap=round(io, 4),
                dependency_overlap=round(deps, 4),
                rationale=(
                    f"cap={cap:.2f}, io={io:.2f}, deps={deps:.2f}, "
                    f"shared={','.join(sorted(shared_signals)) or 'none'}"
                ),
                recommendation="",
            )
            has_strong_shared_signal = len(shared_signals) >= 2 or io >= 0.5
            if score >= merge_threshold and has_strong_shared_signal:
                base.recommendation = "merge_candidate"
                merge.append(base)
            elif cap >= crossover_min and io <= crossover_max_io and len(shared_signals) >= 2:
                base.recommendation = "crossover_candidate"
                crossover.append(base)

    order_key = lambda c: (-c.score, c.skill_a.lower(), c.skill_b.lower())
    merge.sort(key=order_key)
    crossover.sort(key=order_key)
    return merge, crossover


def _markdown_report(
    profiles: list[SkillProfile],
    merge: list[Candidate],
    crossover: list[Candidate],
) -> str:
    lines: list[str] = []
    lines.append("# Skills Bloat Report")
    lines.append("")
    lines.append(f"- skills_analyzed: {len(profiles)}")
    lines.append(f"- merge_candidates: {len(merge)}")
    lines.append(f"- crossover_candidates: {len(crossover)}")
    lines.append("")
    lines.append("## Merge Candidates")
    if not merge:
        lines.append("- none")
    else:
        for item in merge:
            lines.append(
                f"- `{item.skill_a}` + `{item.skill_b}` score={item.score:.2f}"
                f" ({item.rationale})"
            )
    lines.append("")
    lines.append("## Crossover Candidates")
    if not crossover:
        lines.append("- none")
    else:
        for item in crossover:
            lines.append(
                f"- `{item.skill_a}` x `{item.skill_b}` score={item.score:.2f}"
                f" ({item.rationale})"
            )
    lines.append("")
    lines.append("## Skill Inventory")
    for profile in profiles:
        lines.append(
            f"- `{profile.name}` risk={profile.risk_level} enabled={'yes' if profile.enabled else 'no'}"
            f" signed={'yes' if profile.signature_verified else 'no'}"
        )
    lines.append("")
    return "\n".join(lines)


def _deprecation_plan_markdown(merge: list[Candidate]) -> str:
    lines: list[str] = []
    lines.append("# Deprecation Plan")
    lines.append("")
    lines.append("- policy: `no_auto_merge_or_delete_v1`")
    lines.append("- requirement: human approval + tests before any merge/deprecation action")
    lines.append("")
    if not merge:
        lines.append("No merge candidates identified. No deprecation actions proposed.")
        lines.append("")
        return "\n".join(lines)
    lines.append("## Proposed Phased Actions")
    lines.append("")
    for item in merge:
        lines.append(f"### `{item.skill_a}` + `{item.skill_b}`")
        lines.append("- Phase 1: create merged successor skill and run regression tests.")
        lines.append("- Phase 2: mark legacy skills as deprecated but keep enabled.")
        lines.append("- Phase 3: disable legacy skills after migration window.")
        lines.append("")
    return "\n".join(lines)


def analyze_skill_bloat(
    *,
    out_dir: str,
    include_disabled: bool = False,
    merge_threshold: float = 0.72,
    crossover_min: float = 0.45,
    crossover_max_io: float = 0.34,
) -> dict[str, Any]:
    profiles = build_skill_profiles(include_disabled=include_disabled)
    merge, crossover = _pairwise_candidates(
        profiles,
        merge_threshold=merge_threshold,
        crossover_min=crossover_min,
        crossover_max_io=crossover_max_io,
    )
    out_path = Path(out_dir).expanduser()
    out_path.mkdir(parents=True, exist_ok=True)

    merge_json = [asdict(item) for item in merge]
    crossover_json = [asdict(item) for item in crossover]
    (out_path / "merge_candidates.json").write_text(json.dumps(merge_json, indent=2), encoding="utf-8")
    (out_path / "crossover_candidates.json").write_text(json.dumps(crossover_json, indent=2), encoding="utf-8")
    (out_path / "skills_bloat_report.md").write_text(
        _markdown_report(profiles, merge, crossover),
        encoding="utf-8",
    )
    (out_path / "deprecation_plan.md").write_text(_deprecation_plan_markdown(merge), encoding="utf-8")

    return {
        "skills_analyzed": len(profiles),
        "merge_candidates": len(merge),
        "crossover_candidates": len(crossover),
        "out_dir": str(out_path),
    }
