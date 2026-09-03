import pytest

from tests.research.run_profile_matrix import compare_profiles


def evidence(samples, duration, wall):
    return {
        "external_samples": {"samples": samples},
        "simulation": {"simulated_duration_s": duration},
        "timing": {"simulation_wall_s": wall},
    }


def test_comparison_reports_exact_non_interference_and_runtime_overhead():
    sample = [{"sim_time_s": 0.5, "acid": "ONE", "lat": 41.0}]
    result = compare_profiles({
        "baseline-recorder-free": evidence(sample, 100.0, 10.0),
        "baseline-recorder": evidence(sample, 100.0, 12.0),
    })

    comparison = result["recorder_non_interference"]
    assert comparison["status"] == "pass"
    assert comparison["exact"] is True
    assert comparison["recorder_simulation_overhead_s"] == 2.0
    assert comparison["recorder_simulation_overhead_percent"] == pytest.approx(20.0)


def test_comparison_fails_when_recorder_changes_state():
    result = compare_profiles({
        "baseline-recorder-free": evidence([{"lat": 1.0}], 1.0, 1.0),
        "baseline-recorder": evidence([{"lat": 2.0}], 1.0, 1.0),
    })

    assert result["recorder_non_interference"]["status"] == "fail"
