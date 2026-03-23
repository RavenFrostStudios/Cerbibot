from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "live_acceptance_check.py"
    spec = importlib.util.spec_from_file_location("live_acceptance_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_single_provider_optimization_warning_is_treated_as_valid_fallback() -> None:
    mod = _load_module()

    def _http_json(_method: str, _url: str, _token: str | None, _payload=None, *, timeout_seconds: float = 30.0):
        return 200, {
            "result": {
                "answer": "ok",
                "warnings": ["Critique optimized to single-pass because all critique roles resolved to the same provider/model."],
            }
        }, 12

    mod._http_json = _http_json
    result = mod._mode_check("http://test", "token", "critique", "xai", timeout_seconds=30.0)
    assert result.status == "PASS"
    assert result.detail == "single-provider optimization acknowledged"


def test_missing_structure_without_fallback_warning_stays_failed() -> None:
    mod = _load_module()

    def _http_json(_method: str, _url: str, _token: str | None, _payload=None, *, timeout_seconds: float = 30.0):
        return 200, {"result": {"answer": "ok", "warnings": ["other warning"]}}, 12

    mod._http_json = _http_json
    result = mod._mode_check("http://test", "token", "debate", "xai", timeout_seconds=30.0)
    assert result.status == "FAIL"
    assert result.detail == "missing debate structure"


def test_empty_answer_never_passes_even_with_single_provider_warning() -> None:
    mod = _load_module()

    def _http_json(_method: str, _url: str, _token: str | None, _payload=None, *, timeout_seconds: float = 30.0):
        return 200, {
            "result": {
                "answer": "   ",
                "warnings": ["Council optimized to single-pass because all specialist/synthesizer roles resolved to the same provider/model."],
            }
        }, 12

    mod._http_json = _http_json
    result = mod._mode_check("http://test", "token", "council", "xai", timeout_seconds=30.0)
    assert result.status == "FAIL"
    assert result.detail == "empty response"
