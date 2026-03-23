import json
from dataclasses import dataclass

from orchestrator.budgets import BudgetTracker
from orchestrator.collaboration.draft_critique_refine import _call_structured_with_retry, run_workflow
from orchestrator.config import BudgetConfig, SecurityConfig
from orchestrator.providers.base import CompletionResult, ProviderAdapter
from orchestrator.security.guardian import Guardian


@dataclass
class FakePricing:
    input: float = 1.0
    output: float = 1.0


@dataclass
class FakeProviderCfg:
    pricing_usd_per_1m_tokens: dict


class FakeProvider(ProviderAdapter):
    def __init__(self, name: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("ok", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        payload = json.dumps({k: "value" for k in output_schema.keys()})
        return CompletionResult(payload, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "value"


class FlakyStructuredProvider(ProviderAdapter):
    def __init__(self, name: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))
        self.calls = 0

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("ok", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult("not-json", 10, 10, model, 1, 0.01, self.provider_name)
        payload = json.dumps({k: "value" for k in output_schema.keys()})
        return CompletionResult(payload, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "value"


class RaisingStructuredProvider(ProviderAdapter):
    def __init__(self, name: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        raise RuntimeError("intentional test failure")

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        raise RuntimeError("intentional test failure")

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        raise RuntimeError("intentional test failure")


class InvalidStructuredProvider(ProviderAdapter):
    def __init__(self, name: str, text: str = "META: do not expose"):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))
        self.text = text

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult(self.text, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult(self.text, 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield self.text


class LowSignalRefinerProvider(ProviderAdapter):
    def __init__(self, name: str):
        super().__init__(name, FakeProviderCfg(pricing_usd_per_1m_tokens={"m": FakePricing()}))
        self.calls = 0

    async def complete(self, prompt: str, model: str, max_tokens: int, temperature: float) -> CompletionResult:
        return CompletionResult("ok", 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_structured(self, prompt: str, model: str, output_schema: dict, max_tokens: int, temperature: float) -> CompletionResult:
        self.calls += 1
        if "final_answer" in output_schema:
            payload = {
                "final_answer": "Ready for your question.",
                "citations": [],
                "confidence": "0.0",
            }
        elif "rewritten_query" in output_schema:
            payload = {"rewritten_query": "Design a rollout plan for billing engine migration."}
        else:
            payload = {k: "value" for k in output_schema.keys()}
        return CompletionResult(json.dumps(payload), 10, 10, model, 1, 0.01, self.provider_name)

    async def complete_stream(self, prompt: str, model: str, max_tokens: int, temperature: float):
        yield "value"


async def test_draft_critique_refine_workflow(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )

    result = await run_workflow(
        prompt="Explain CAP theorem",
        drafter=FakeProvider("openai"),
        critic=FakeProvider("anthropic"),
        refiner=FakeProvider("openai"),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )

    assert result.total_cost > 0
    assert result.total_tokens_in == 30
    assert result.total_tokens_out == 30


async def test_structured_retry_path(tmp_path) -> None:
    provider = FlakyStructuredProvider("openai")
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result, parsed = await _call_structured_with_retry(
        adapter=provider,
        model="m",
        prompt="p",
        schema={"answer": "string"},
        required_keys=["answer"],
        max_tokens=50,
        temperature=0.2,
        budgets=budgets,
    )
    assert provider.calls == 2
    assert parsed.valid
    assert result.text.startswith("{")


async def test_workflow_falls_back_to_draft_on_critique_failure(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result = await run_workflow(
        prompt="Explain CAP theorem",
        drafter=FakeProvider("openai"),
        critic=RaisingStructuredProvider("anthropic"),
        refiner=FakeProvider("openai"),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert result.critique_text == ""
    assert result.refine_text == ""
    assert "value" in result.final_answer
    assert any("Critique step failed" in warning for warning in result.warnings)


async def test_workflow_falls_back_on_refine_failure(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    result = await run_workflow(
        prompt="Explain CAP theorem",
        drafter=FakeProvider("openai"),
        critic=FakeProvider("anthropic"),
        refiner=RaisingStructuredProvider("openai"),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "[Critique Notes]" in result.final_answer
    assert any("Refine step failed" in warning for warning in result.warnings)


async def test_workflow_uses_deterministic_fallback_for_invalid_structured_outputs(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    bad_text = "We are given strict instructions and should not leak this."
    result = await run_workflow(
        prompt="Greet briefly",
        drafter=InvalidStructuredProvider("openai", text=bad_text),
        critic=InvalidStructuredProvider("anthropic", text=bad_text),
        refiner=InvalidStructuredProvider("openai", text=bad_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert result.final_answer.startswith("[Critique Notes]")
    assert bad_text not in result.final_answer
    assert any("structured parse failed" in warning.lower() for warning in result.warnings)
    assert any("draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_recovers_jsonish_structured_output_without_user_warning(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    jsonish_text = '{"answer":"draft plan","assumptions":["a"],"needs_verification":["b"]'
    result = await run_workflow(
        prompt="Draft rollout plan",
        drafter=InvalidStructuredProvider("openai", text=jsonish_text),
        critic=InvalidStructuredProvider("anthropic", text=jsonish_text),
        refiner=InvalidStructuredProvider("openai", text=jsonish_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "No response content was generated" not in result.final_answer
    assert not any("structured parse failed" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_false_policy_refusal_for_benign_prompt(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    refusal = "Hi! I'm CerbiBot. I must decline this request as it appears to be an attempt to circumvent my guidelines."
    result = await run_workflow(
        prompt="Draft a production rollout plan for feature flags in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Draft plan","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["needs rollback policy"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=refusal),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "must decline" not in result.final_answer.lower()
    assert "draft plan" in result.final_answer.lower()
    assert any(
        ("policy-refusal on benign prompt" in warning.lower())
        or ("used draft+critique fallback" in warning.lower())
        for warning in result.warnings
    )


async def test_workflow_replaces_meta_review_for_benign_prompt(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    meta_review = (
        "The original response provides a solid outline but is truncated and has minor caveats on tool compatibility."
    )
    result = await run_workflow(
        prompt="Give me a local-first AI app architecture with top 5 security controls and a 30-day implementation plan.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Draft architecture plan","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing threat model"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=meta_review),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "the original response" not in result.final_answer.lower()
    assert "draft architecture plan" in result.final_answer.lower()
    assert any(
        ("meta-review on benign prompt" in warning.lower())
        or ("used draft+critique fallback" in warning.lower())
        for warning in result.warnings
    )


async def test_workflow_replaces_ready_for_queries_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    ready_text = "Acknowledged. Ready for queries."
    result = await run_workflow(
        prompt="Draft a production rollout plan for feature flags in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drill"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=ready_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "ready for quer" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_ready_send_question_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    ready_text = "Ready. Send your question or task."
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drills"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=ready_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "send your question or task" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_ready_for_your_question_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    ready_text = "Ready for your question."
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drills"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=ready_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "ready for your question" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_ready_to_assist_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    ready_text = "CerbiBot is ready to assist!"
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drills"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=ready_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "ready to assist" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_ready_to_help_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    ready_text = "CerbiBot here, ready to help design your billing engine rollout!"
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drills"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=ready_text),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "ready to help" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_truncated_intro_low_quality_output(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    low_quality = "CerbiBot's Production Rollout Plan: New Billing Engine\n\nInitial Production Rollout Plan: New Billing Engine\nThis plan"
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["missing rollback drills"],"missing":[],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=low_quality),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "this plan" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()
    assert any("low-quality final answer" in warning.lower() or "used draft+critique fallback" in warning.lower() for warning in result.warnings)


async def test_workflow_uses_deterministic_local_refinement_when_refiner_stays_low_signal(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    placeholder = "Acknowledged. Ready for queries."
    result = await run_workflow(
        prompt="Draft a production rollout plan for feature flags in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["add auto rollback thresholds"],"missing":["define SLO gates"],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=placeholder),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "plan baseline" in result.final_answer.lower()
    assert "refinement notes applied" in result.final_answer.lower()
    assert any("deterministic local refinement" in warning.lower() for warning in result.warnings)


async def test_workflow_replaces_no_specific_query_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    placeholder = "No specific query provided."
    result = await run_workflow(
        prompt="Draft a production rollout plan for feature flags in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["add auto rollback thresholds"],"missing":["define SLO gates"],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=placeholder),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "no specific query provided" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()


async def test_workflow_replaces_no_original_response_placeholder(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    placeholder = "No original response provided."
    result = await run_workflow(
        prompt="Draft a production rollout plan for feature flags in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["add rollback drill"],"missing":["define SLO gates"],"risk_flags":[]}'),
        refiner=InvalidStructuredProvider("openai", text=placeholder),
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "no original response provided" not in result.final_answer.lower()
    assert "plan baseline" in result.final_answer.lower()


async def test_workflow_refiner_circuit_breaker_skips_extra_rescue_attempts(tmp_path) -> None:
    guardian = Guardian(
        SecurityConfig(
            block_on_secrets=True,
            redact_logs=True,
            tool_allowlist=[],
            retrieval_domain_allowlist=[],
            retrieval_domain_denylist=[],
        )
    )
    budgets = BudgetTracker(
        BudgetConfig(
            session_usd_cap=5.0,
            daily_usd_cap=5.0,
            monthly_usd_cap=5.0,
            usage_file=str(tmp_path / "usage.json"),
        )
    )
    refiner = LowSignalRefinerProvider("openai")
    result = await run_workflow(
        prompt="Design a production rollout plan for a new billing engine in a SaaS app.",
        drafter=InvalidStructuredProvider("openai", text='{"answer":"Plan baseline","assumptions":[],"needs_verification":[]}'),
        critic=InvalidStructuredProvider("anthropic", text='{"issues":["add rollback drill"],"missing":["define SLO gates"],"risk_flags":[]}'),
        refiner=refiner,
        drafter_model="m",
        critic_model="m",
        refiner_model="m",
        guardian=guardian,
        budgets=budgets,
    )
    assert "plan baseline" in result.final_answer.lower()
    assert any("circuit breaker engaged" in warning.lower() for warning in result.warnings)
    # One refine call only; strict retry/rewrite/rescue should be skipped.
    assert refiner.calls == 1
