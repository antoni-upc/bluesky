import copy
import math

import pytest

from tests.research import run_disabled_baseline as baseline


def valid_result():
    temperature = 286.6894179899859
    state = {
        "lat_deg": 41.2, "lon_deg": 2.3, "alt_m": 3000.0,
        "tas_m_s": 0.5 * math.sqrt(1.4 * 287.05287 * temperature),
        "cas_m_s": 150.0,
        "mach": 0.5, "vertical_speed_m_s": 0.0, "heading_deg": 101.0,
        "track_deg": 101.0, "pressure_pa": 70000.0,
        "density_kg_m3": 70000.0 / (287.05287 * temperature),
        "temperature_k": temperature, "mass_kg": 60300.0,
        "thrust_n": 36000.0, "drag_n": 36000.0,
        "fuel_flow_kg_s": 0.3, "phase": 4.0,
    }
    return {
        "inputs": {
            "simulation_utc": baseline.UTC.isoformat(),
            "simdt_s": baseline.SIMDT_S, "steps": baseline.STEPS,
            "duration_s": baseline.SIMDT_S * baseline.STEPS,
            "aircraft": "A320", "research_plugins_loaded": [],
            "performance_model": "OpenAP", "atmosphere": "ISA",
        },
        "state": state,
        "isa_reference": {
            "pressure_pa": state["pressure_pa"],
            "density_kg_m3": state["density_kg_m3"],
            "temperature_k": state["temperature_k"],
        },
    }


@pytest.mark.smoke
def test_validate_result_accepts_fixed_openap_isa_contract():
    result = valid_result()
    assert baseline.validate_result(result) is result


@pytest.mark.parametrize("field,value", [
    ("research_plugins_loaded", ["PYBADATEM"]),
    ("performance_model", "PyBadaTEM"),
    ("atmosphere", "ERA5"),
    ("steps", 239),
])
def test_validate_result_rejects_changed_experiment(field, value):
    result = valid_result()
    result["inputs"][field] = value
    with pytest.raises(ValueError, match="inputs changed"):
        baseline.validate_result(result)


def test_validate_result_rejects_non_isa_traffic_state():
    result = valid_result()
    result["state"]["temperature_k"] += 1.0
    with pytest.raises(ValueError, match="does not exactly match native ISA"):
        baseline.validate_result(result)


def test_validate_result_rejects_inconsistent_airdata():
    result = copy.deepcopy(valid_result())
    result["state"]["mach"] = 0.4
    with pytest.raises(ValueError, match="Mach are inconsistent"):
        baseline.validate_result(result)
