from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

import yaml

from orchestrator.skills.workflow import run_workflow_skill


@dataclass(slots=True)
class SkillTestCaseResult:
    case_id: str
    passed: bool
    expected_error: bool
    actual_error: bool
    error: str
    outputs: dict[str, Any]


def load_skill_adversarial_cases(fixtures_path: str) -> list[dict[str, Any]]:
    path = Path(fixtures_path).expanduser()
    if not path.exists():
        raise ValueError(f"Adversarial fixtures file not found: {fixtures_path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Adversarial fixtures file must be a YAML list")
    cases: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            cases.append(item)
    if not cases:
        raise ValueError("No adversarial cases found")
    return cases


async def run_skill_adversarial_tests(
    orchestrator: Any,
    *,
    skill_path: str,
    fixtures_path: str,
    mode: str = "single",
    provider: str | None = None,
    budget_cap_usd: float | None = None,
) -> dict[str, Any]:
    cases = load_skill_adversarial_cases(fixtures_path)
    results: list[SkillTestCaseResult] = []
    for idx, case in enumerate(cases, start=1):
        case_id = str(case.get("id", f"case-{idx}"))
        inputs = case.get("input", {}) or {}
        if not isinstance(inputs, dict):
            raise ValueError(f"Case '{case_id}' input must be a mapping")
        expected_error = bool(case.get("expect_error", False))
        error_contains = str(case.get("error_contains", "")).strip().lower()
        expected_outputs = case.get("expect_output_contains", {}) or {}
        if not isinstance(expected_outputs, dict):
            raise ValueError(f"Case '{case_id}' expect_output_contains must be a mapping")

        actual_error = False
        error_text = ""
        outputs: dict[str, Any] = {}
        try:
            run = await run_workflow_skill(
                orchestrator,
                skill_path=skill_path,
                input_data={str(k): v for k, v in inputs.items()},
                mode=mode,
                provider=provider,
                budget_cap_usd=budget_cap_usd,
            )
            outputs = run.outputs
        except Exception as exc:
            actual_error = True
            error_text = str(exc)

        passed = (actual_error == expected_error)
        if passed and expected_error and error_contains:
            passed = error_contains in error_text.lower()
        if passed and not expected_error and expected_outputs:
            outputs_json = json.dumps(outputs, sort_keys=True, ensure_ascii=True)
            for key, expected_fragment in expected_outputs.items():
                key_s = str(key)
                fragment = str(expected_fragment)
                if key_s in outputs:
                    value_blob = json.dumps(outputs[key_s], sort_keys=True, ensure_ascii=True)
                else:
                    value_blob = outputs_json
                if fragment not in value_blob:
                    passed = False
                    break

        results.append(
            SkillTestCaseResult(
                case_id=case_id,
                passed=passed,
                expected_error=expected_error,
                actual_error=actual_error,
                error=error_text,
                outputs=outputs,
            )
        )

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [asdict(r) for r in results],
    }
    return summary
