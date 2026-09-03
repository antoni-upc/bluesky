import hashlib
import json
from pathlib import Path

import pytest

from tests.research.scenario_profiles import (
    ProfileConfigError,
    load_profile_config,
    render_scenario,
    render_scenario_text,
    validate_profile_config,
)


ROOT = Path(__file__).resolve().parents[2]


def config():
    return {
        "schema_version": "research-profiles-v1",
        "aircraft_roles": {
            "narrowbody": {"OPENAP": "A320", "PYBADA3": "A320__",
                           "PYBADA4": "A320-232"},
        },
        "profiles": {
            "openap": {
                "performance": {"provider": "OPENAP"},
                "atmosphere": {"provider": "ISA"},
                "recorder": {"enabled": False},
            },
            "bada3": {
                "performance": {"provider": "PYBADA", "family": "3"},
                "atmosphere": {"provider": "ISA"},
                "recorder": {"enabled": True},
            },
        },
    }


def test_render_only_changes_create_aircraft_types():
    source = (
        "# generated scenario\n"
        "00:00:00.00>CRE ONE, {role:narrowbody},41.3,2.1,73,FL100,250 # first\n"
        "00:00:00.10>CREATE TWO,{role:narrowbody},41.4,2.2,74,FL110,260\n"
        "00:00:01.00>ALT ONE,12000,1500\n"
    )

    rendered, replacements = render_scenario_text(
        source, config()["aircraft_roles"], "PYBADA3"
    )

    assert "CRE ONE, A320__,41.3" in rendered
    assert "CREATE TWO,A320__,41.4" in rendered
    assert "# generated scenario" in rendered
    assert "# first" in rendered
    assert "00:00:01.00>ALT ONE,12000,1500" in rendered
    assert replacements == [
        {"line": 2, "role": "narrowbody", "aircraft_type": "A320__"},
        {"line": 3, "role": "narrowbody", "aircraft_type": "A320__"},
    ]


def test_unknown_role_fails_before_simulation():
    with pytest.raises(ProfileConfigError, match="B738.*not configured"):
        render_scenario_text(
            "00:00:00.00>CRE ONE,{role:B738},0,0,0,FL100,250\n",
            config()["aircraft_roles"], "OPENAP",
        )


def test_missing_implementation_mapping_fails_before_simulation():
    with pytest.raises(ProfileConfigError, match="no PYBADA4 mapping"):
        render_scenario_text(
            "00:00:00.00>CRE ONE,{role:narrowbody},0,0,0,FL100,250\n",
            {"narrowbody": {"OPENAP": "A320"}}, "PYBADA4",
        )


def test_config_rejects_unknown_mapping_implementation():
    value = config()
    value["aircraft_roles"]["narrowbody"]["UNKNOWN"] = "A320"
    with pytest.raises(ProfileConfigError, match="unknown implementations"):
        validate_profile_config(value)


def test_canonical_profile_name_must_match_declared_plugins():
    value = config()
    value["profiles"]["baseline-recorder-free"] = {
        "performance": {"provider": "OPENAP"},
        "atmosphere": {
            "provider": "ERA5", "region": "western-europe",
            "pressure_levels_hpa": [100, 1000],
        },
        "recorder": {"enabled": False},
    }
    with pytest.raises(ProfileConfigError, match="baseline-recorder-free requires"):
        validate_profile_config(value)


def test_render_records_original_and_rendered_checksums(tmp_path):
    source = tmp_path / "source.scn"
    output = tmp_path / "run" / "rendered.scn"
    source.write_text(
        "00:00:00.00>CRE ONE,{role:narrowbody},0,0,0,FL100,250\n", encoding="utf-8"
    )

    metadata = render_scenario(source, output, config(), "bada3")

    assert source.read_text(encoding="utf-8").split(",")[1] == "{role:narrowbody}"
    assert output.read_text(encoding="utf-8").split(",")[1] == "A320__"
    assert metadata["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert metadata["rendered_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert metadata["implementation"] == "PYBADA3"


def test_example_contract_is_json_serializable_and_valid():
    value = json.loads(json.dumps(config()))
    assert validate_profile_config(value) is value


def test_concrete_aircraft_types_are_not_rewritten():
    source = "00:00:00.00>CRE ONE,A320,0,0,0,FL100,250\n"

    rendered, replacements = render_scenario_text(
        source, config()["aircraft_roles"], "PYBADA3"
    )

    assert rendered == source
    assert replacements == []


def test_repository_configuration_has_canonical_profiles_and_roles():
    value = load_profile_config(ROOT / "experiments" / "profiles.json")

    assert set(value["profiles"]) == {
        "baseline-recorder-free", "baseline-recorder", "meteo-recorder",
        "pybada-recorder", "combined-recorder",
    }
    assert set(value["aircraft_roles"]) == {"narrowbody", "widebody", "jumbo"}
