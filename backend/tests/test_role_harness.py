from evaluation.role_harness import (
    build_recommended_routes,
    compute_candidate_score,
    rank_role_candidates,
)


def test_compute_candidate_score_rewards_quality_over_cost_in_quality_mode() -> None:
    high_quality = {
        "avg_quality": 0.9,
        "json_valid_rate": 0.95,
        "low_signal_rate": 0.05,
        "placeholder_rate": 0.0,
        "error_rate": 0.0,
        "avg_cost": 0.0030,
    }
    low_quality = {
        "avg_quality": 0.45,
        "json_valid_rate": 0.6,
        "low_signal_rate": 0.25,
        "placeholder_rate": 0.1,
        "error_rate": 0.0,
        "avg_cost": 0.0002,
    }
    assert compute_candidate_score(high_quality, "quality") > compute_candidate_score(low_quality, "quality")


def test_rank_role_candidates_prefers_cheaper_candidate_in_cost_mode_when_quality_is_close() -> None:
    candidates = [
        {
            "provider": "alpha",
            "model": "m1",
            "avg_quality": 0.82,
            "json_valid_rate": 0.95,
            "low_signal_rate": 0.05,
            "placeholder_rate": 0.0,
            "error_rate": 0.0,
            "avg_cost": 0.0040,
        },
        {
            "provider": "beta",
            "model": "m2",
            "avg_quality": 0.80,
            "json_valid_rate": 0.94,
            "low_signal_rate": 0.05,
            "placeholder_rate": 0.0,
            "error_rate": 0.0,
            "avg_cost": 0.0002,
        },
    ]
    ranked = rank_role_candidates(candidates, strategy="cost")
    assert ranked[0]["provider"] == "beta"


def test_build_recommended_routes_updates_only_critique_section() -> None:
    existing = {
        "critique": {
            "drafter_provider": "openai",
            "critic_provider": "google",
            "refiner_provider": "openai",
        },
        "debate": {"judge_provider": "xai"},
    }
    winners = {"drafter": "xai", "critic": "anthropic", "refiner": "openai"}
    updated = build_recommended_routes(existing, winners)
    assert updated["critique"]["drafter_provider"] == "xai"
    assert updated["critique"]["critic_provider"] == "anthropic"
    assert updated["critique"]["refiner_provider"] == "openai"
    assert updated["debate"]["judge_provider"] == "xai"
