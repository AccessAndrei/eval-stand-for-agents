"""Разница строгого mock-предела и согласованного observed_usage у Luna."""

from dataclasses import replace
from pathlib import Path

import pytest

from services.llm_proxy.budget import BudgetError, Ledger
from services.llm_proxy.profiles import Profile, ProfileError, load_profiles


def observed(tmp_path: Path) -> Ledger:
    profile = load_profiles(Path("config/profiles.json"))["luna"]
    return Ledger(tmp_path / "luna.json", "luna", profile_snapshot=profile.snapshot())


def test_last_observed_request_may_overshoot_without_changing_execution(
    tmp_path: Path,
) -> None:
    ledger = observed(tmp_path)
    first = ledger.reserve_observed()
    ledger.settle(first, {"input_tokens": 29_000, "output_tokens": 500})
    last = ledger.reserve_observed(2048)
    assert ledger.data["reservations"][last] == [None, 2048]
    ledger.settle(last, {"input_tokens": 2_000, "output_tokens": 200})
    assert ledger.tokens() == {
        "mode": "observed_usage",
        "limit": 30_000,
        "output_limit": 2048,
        "actual": 31_700,
        "over_target": True,
        "input": 31_000,
        "output": 700,
        "cached": 0,
        "reserved": 0,
        "unknown_reason": None,
    }
    assert ledger.data["error"] is None
    with pytest.raises(BudgetError, match="budget_exceeded"):
        ledger.reserve_observed()
    assert ledger.data["requests"] == 2
    assert ledger.tokens()["actual"] == 31_700


def test_exact_observed_target_stops_next_request(tmp_path: Path) -> None:
    ledger = observed(tmp_path)
    request = ledger.reserve_observed()
    ledger.settle(request, {"input_tokens": 29_000, "output_tokens": 1000})
    assert ledger.tokens()["over_target"] is False
    with pytest.raises(BudgetError, match="budget_exceeded"):
        ledger.reserve_observed()


def test_natural_completion_above_target_does_not_invent_budget_error(
    tmp_path: Path,
) -> None:
    ledger = observed(tmp_path)
    request = ledger.reserve_observed()
    ledger.settle(request, {"input_tokens": 40_000, "output_tokens": 100})
    ledger.close()
    assert ledger.data["error"] is None
    assert ledger.tokens()["actual"] == 40_100
    assert ledger.tokens()["over_target"] is True


def test_unknown_observed_usage_blocks_after_restart_without_fake_input_bound(
    tmp_path: Path,
) -> None:
    ledger = observed(tmp_path)
    settled = ledger.reserve_observed()
    ledger.settle(settled, {"input_tokens": 100, "output_tokens": 10})
    pending = ledger.reserve_observed()
    restarted = Ledger(tmp_path / "luna.json", "luna")
    assert restarted.data["reservations"][pending] == [None, 2048]
    tokens = restarted.tokens()
    assert tokens["actual"] is None
    assert tokens["over_target"] is None
    assert tokens["reserved"] == 2048  # Только выход, не оценка полного входа.
    assert tokens["input"] == 100 and tokens["output"] == 10
    assert tokens["unknown_reason"] == "upstream_usage_unconfirmed"
    with pytest.raises(BudgetError, match="upstream_usage_unconfirmed"):
        restarted.reserve_observed()
    assert restarted.data["requests"] == 2


def test_hard_output_cap_is_still_enforced_and_actual_violation_not_hidden(
    tmp_path: Path,
) -> None:
    ledger = observed(tmp_path)
    with pytest.raises(ValueError, match="output_bound"):
        ledger.reserve_observed(2049)
    request = ledger.reserve_observed(10)
    with pytest.raises(BudgetError, match="provider_budget_violation"):
        ledger.settle(request, {"input_tokens": 50, "output_tokens": 11})
    assert ledger.tokens()["actual"] == 61
    assert ledger.tokens()["reserved"] == 0
    with pytest.raises(BudgetError, match="provider_budget_violation"):
        ledger.reserve_observed()


def test_invalid_usage_does_not_release_unknown_observed_request(
    tmp_path: Path,
) -> None:
    ledger = observed(tmp_path)
    request = ledger.reserve_observed()
    with pytest.raises(BudgetError, match="provider_budget_violation"):
        ledger.settle(request, {"input_tokens": True, "output_tokens": 10})
    assert ledger.tokens()["actual"] is None
    assert request in ledger.data["reservations"]


def test_modes_cannot_silently_use_each_others_reservations(tmp_path: Path) -> None:
    strict = Ledger(tmp_path / "mock.json", "mock")
    with pytest.raises(ValueError, match="strict_budget_requires_input_bound"):
        strict.reserve_observed()
    live = observed(tmp_path)
    with pytest.raises(ValueError, match="observed_usage_requires_reserve_observed"):
        live.reserve(100, 2048)
    with pytest.raises(ValueError, match="run_conflict"):
        Ledger(tmp_path / "luna.json", "luna", mode="strict")


def test_profile_snapshot_retains_mode_and_output_limit_after_reload(
    tmp_path: Path,
) -> None:
    profile = load_profiles(Path("config/profiles.json"))["luna"]
    snapshot = profile.snapshot()
    ledger = Ledger(tmp_path / "luna.json", "luna", profile_snapshot=snapshot)
    snapshot["budget_mode"] = "strict"
    snapshot["output_limit"] = 1
    restarted = Ledger(tmp_path / "luna.json", "luna", profile_snapshot=snapshot)
    assert restarted.data["profile_snapshot"] == ledger.data["profile_snapshot"]
    assert restarted.tokens()["mode"] == "observed_usage"
    assert restarted.tokens()["output_limit"] == 2048


def test_luna_requires_explicit_runtime_auth_readiness_and_fixed_route() -> None:
    profile = load_profiles(Path("config/profiles.json"))["luna"]
    with pytest.raises(ProfileError, match="live_authorization_not_ready"):
        profile.compatible()
    profile.compatible(live_ready=True)
    assert profile.public()["live_authorization_required"] is True
    assert profile.public()["budget_mode"] == "observed_usage"
    for change in (
        {"model": "different"},
        {"auth": "api_key"},
        {"endpoint": "https://api.openai.com/v1"},
        {"budget_mode": "strict"},
        {"output_limit": 8192},
    ):
        with pytest.raises(ProfileError):
            replace(profile, **change).compatible(live_ready=True)


def test_old_profile_snapshot_remains_readable_without_report_dependency(
    tmp_path: Path,
) -> None:
    profile = load_profiles(Path("config/profiles.json"))["luna"]
    snapshot = {**profile.snapshot(), "output_limit_proof": "results/old/report.json"}
    ledger = Ledger(tmp_path / "old.json", "luna", profile_snapshot=snapshot)
    restored = Profile(**ledger.data["profile_snapshot"])
    restored.compatible(live_ready=True)
    assert ledger.data["profile_snapshot"] == snapshot
    assert "output_limit_proof" not in restored.public()
    assert "output_limit_proof" not in restored.snapshot()
