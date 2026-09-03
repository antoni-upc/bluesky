import pytest

from tests.research.run_profile_matrix import (
    compare_profiles, trajectory_comparison, trajectory_summary,
)


def evidence(samples, duration, wall):
    return {
        "external_samples": {"samples": samples},
        "simulation": {"simulated_duration_s": duration,
                       "termination_reason": "destination_reached"},
        "timing": {"simulation_wall_s": wall},
    }


def test_comparison_reports_exact_non_interference_and_runtime_overhead():
    rows = [sample(0.5)]
    result = compare_profiles({
        "baseline-recorder-free": evidence(rows, 100.0, 10.0),
        "baseline-recorder": evidence(rows, 100.0, 12.0),
    })

    comparison = result["recorder_non_interference"]
    assert comparison["status"] == "pass"
    assert comparison["exact"] is True
    assert comparison["recorder_simulation_overhead_s"] == 2.0
    assert comparison["recorder_simulation_overhead_percent"] == pytest.approx(20.0)


def test_comparison_fails_when_recorder_changes_state():
    result = compare_profiles({
        "baseline-recorder-free": evidence([sample(0.5, lat=1.0)], 1.0, 1.0),
        "baseline-recorder": evidence([sample(0.5, lat=2.0)], 1.0, 1.0),
    })

    assert result["recorder_non_interference"]["status"] == "fail"


def sample(time, lat=41.0, lon=2.0, alt=1000.0, tas=100.0, mass=60_000.0):
    return {"sim_time_s": time, "acid": "ONE", "actype": "A320", "lat": lat,
            "lon": lon, "alt": alt, "tas": tas, "mass": mass,
            "atmos_valid": True, "atmos_fallback_reason": ""}


def test_trajectory_comparison_uses_only_common_timestamps():
    baseline = evidence([sample(0.5), sample(1.0)], 1.0, 1.0)
    candidate = evidence([sample(0.5, lat=41.001, alt=1010, tas=102),
                          sample(1.5)], 1.5, 2.0)

    result = trajectory_comparison(baseline, candidate)

    assert result["common_samples"] == 1
    assert result["maximum_horizontal_difference_m"] > 100
    assert result["maximum_altitude_difference_m"] == 10
    assert result["maximum_tas_difference_m_s"] == 2


def test_trajectory_summary_reports_fuel_and_quality_counts():
    rows = [sample(0.5), sample(1.0, mass=59_900.0)]
    rows[1]["atmos_valid"] = False
    rows[1]["atmos_fallback_reason"] = "OUTSIDE_DOMAIN"

    result = trajectory_summary(evidence(rows, 1.0, 1.0))["aircraft"]["ONE"]

    assert result["fuel_or_mass_change_kg"] == 100.0
    assert result["invalid_atmosphere_samples"] == 1
    assert result["fallback_samples"] == 1
