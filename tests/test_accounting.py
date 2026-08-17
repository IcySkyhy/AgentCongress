import pytest

from agentcongress.accounting import Budget, BudgetExceeded, BudgetGovernor, Usage


def test_usage_and_api_equivalent_cost_are_accounted_once_per_completed_turn() -> None:
    governor = BudgetGovernor(Budget(2, 60), "gpt-5.6-luna")
    assert governor.observe_event({"type": "item.completed", "usage": {"input_tokens": 99}}) is None
    observed = governor.observe_event({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 500, "reasoning_tokens": 100}})
    assert observed == Usage(1000, 200, 500, 100)
    assert governor.estimated_cost_usd == pytest.approx((800 * 1 + 200 * 0.1 + 500 * 6) / 1_000_000)
    assert governor.snapshot()["usage_events"] == 1


def test_session_budget_is_a_hard_start_boundary() -> None:
    governor = BudgetGovernor(Budget(1, 60), "gpt-5.6-sol")
    assert governor.start_session() > 0
    governor.finish_session()
    with pytest.raises(BudgetExceeded, match="session"):
        governor.start_session()


def test_session_cap_leaves_remainder_for_a_later_session() -> None:
    governor = BudgetGovernor(Budget(2, 60), "gpt-5.6-sol")
    assert governor.start_session(max_seconds=10) == 10
    governor.finish_session()
    assert governor.start_session() > 49
    governor.finish_session()
    assert len(governor.snapshot()["session_elapsed_seconds"]) == 2


def test_mixed_model_usage_uses_each_model_rate() -> None:
    governor = BudgetGovernor(Budget(2, 60), "gpt-5.6-sol")
    governor.observe_event({"type": "turn.completed", "usage": {"input_tokens": 1_000_000}}, model="gpt-5.6-luna")
    governor.observe_event({"type": "turn.completed", "usage": {"output_tokens": 1_000_000}}, model="gpt-5.6-sol")
    assert governor.estimated_cost_usd == pytest.approx(31.0)
    assert governor.snapshot()["usage_by_model"]["gpt-5.6-luna"]["input_tokens"] == 1_000_000


def test_cost_limit_is_checked_after_the_final_observed_turn() -> None:
    governor = BudgetGovernor(Budget(1, 60, 0.001), "gpt-5.6-luna")
    governor.observe_event({"type": "turn.completed", "usage": {"output_tokens": 1000}})
    with pytest.raises(BudgetExceeded, match="cost"):
        governor.assert_cost_within_limit()
