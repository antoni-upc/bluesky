import pytest

from tests.research.run_profile_matrix import (
    atmosphere_evidence, compare_profiles, trajectory_comparison, trajectory_summary,
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


def sample(time, lat=41.0, lon=2.0, alt=1000.0, tas=100.0, mass=60_000.0,
           acid="ONE", source="ISA", reason="", valid=True, temp=280.0,
           pressure=90_000.0, density=1.12, windnorth=0.0, windeast=0.0):
    return {"sim_time_s": time, "acid": acid, "actype": "A320", "lat": lat,
            "lon": lon, "alt": alt, "tas": tas, "mass": mass,
            "atmos_source": source, "atmos_valid": valid,
            "atmos_fallback_reason": reason, "Temp": temp, "p": pressure,
            "rho": density, "windnorth": windnorth, "windeast": windeast}


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
    assert result["unexpected_fallback_samples"] == 1


def test_configured_domain_isa_is_not_invalid_or_unexpected_fallback():
    result = atmosphere_evidence([
        sample(0.5, source="ISA", reason="CONFIGURED_BELOW_ERA5_DOMAIN"),
        sample(1.0, source="ERA5"),
    ])

    assert result["configured_domain_samples"] == 1
    assert result["invalid_atmosphere_samples"] == 0
    assert result["unexpected_fallback_samples"] == 0
    assert result["source_sample_counts"] == {"ERA5": 1, "ISA": 1}


def test_transition_records_before_after_values_and_discontinuities_per_aircraft():
    rows = [
        sample(0.5, acid="ONE", source="ISA", temp=280.0, pressure=90_000.0,
               density=1.1, windnorth=1.0, windeast=2.0),
        sample(0.5, acid="TWO", source="ERA5"),
        sample(1.0, acid="ONE", source="ERA5", alt=1100.0, temp=278.0,
               pressure=89_000.0, density=1.08, windnorth=4.0, windeast=-1.0),
        sample(1.0, acid="TWO", source="ERA5"),
    ]

    transitions = atmosphere_evidence(rows)["source_transitions"]

    assert len(transitions) == 1
    transition = transitions[0]
    assert transition["acid"] == "ONE"
    assert transition["sim_time_s"] == 1.0
    assert transition["geometric_altitude_m"] == 1100.0
    assert (transition["source_before"], transition["source_after"]) == ("ISA", "ERA5")
    assert transition["before"]["temperature_k"] == 280.0
    assert transition["after"]["temperature_k"] == 278.0
    assert transition["discontinuity"]["temperature_k"] == -2.0
    assert transition["discontinuity"]["wind_north_m_s"] == 3.0


def test_invalid_and_unexpected_fallback_counts_are_independent():
    result = atmosphere_evidence([
        sample(0.5, valid=False, reason="TIME_SLOT_UNAVAILABLE"),
        sample(1.0, valid=False),
    ])

    assert result["configured_domain_samples"] == 0
    assert result["invalid_atmosphere_samples"] == 2
    assert result["unexpected_fallback_samples"] == 1
