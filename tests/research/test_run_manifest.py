import json

import pytest

from tests.research.run_manifest import ManifestError, PROFILES, validate_manifest


def manifest(profile="combined-recorder"):
    performance_kind, atmosphere_class, recorder_enabled = PROFILES[profile]
    performance = {"provider": performance_kind}
    if performance_kind == "PYBADA":
        performance.update({
            "family": "4", "version": "4.2", "dataset_id": "BADA-4.2-local",
            "aircraft": ["A320-232"], "dynamics_mode": "TEM", "strict": True,
        })
    atmosphere = {"provider": "ISA"}
    if atmosphere_class == "NWP":
        atmosphere = {
            "provider": "ERA5", "dataset_id": "ERA5-20250815T12",
            "bounds": [40.0, -5.0, 45.0, 5.0], "strict": True,
            "interpolation": False,
        }
    recorder = {"enabled": recorder_enabled}
    if recorder_enabled:
        recorder.update({"schema_version": "samples-v10", "interval_s": 1.0})
    document = {
        "schema_version": "research-run-v2",
        "revision": {
            "commit": "b89b421da56102134a9d18695aa661de7b56e3f8",
            "upstream_base": "22fdf9e3e77c077e0ddb5d7b14c70d67f9a5c855",
            "working_tree_dirty": False,
        },
        "experiment": {
            "scenario": "scenario/research/example.scn",
            "simulation_utc": "2025-08-15T12:00:00+00:00",
            "timestep_s": 0.5, "duration_s": 120.0, "random_seed": 0,
        },
        "configuration": {
            "profile": profile, "performance": performance,
            "atmosphere": atmosphere, "recorder": recorder,
        },
    }
    resources = {}
    if performance_kind == "PYBADA":
        resources[performance["dataset_id"]] = {
            "kind": "licensed-bada", "path": "/external/bada"}
    if atmosphere_class == "NWP":
        resources[atmosphere["dataset_id"]] = {
            "kind": "weather-cache", "path": "/external/weather"}
    document["external_resources"] = resources
    document["evidence"] = {"status": "planned", "validators": []}
    return document


@pytest.mark.parametrize("profile", PROFILES)
def test_all_five_profiles_are_valid(profile):
    value = manifest(profile)
    assert validate_manifest(value) is value


@pytest.mark.parametrize(("path", "value", "message"), [
    (("schema_version",), "research-run-v1", "schema_version"),
    (("revision", "commit"), "b89b421d", "full lowercase Git hash"),
    (("experiment", "simulation_utc"), "2025-08-15T12:00:00", "UTC offset"),
    (("experiment", "duration_s"), 120.1, "whole number"),
    (("configuration", "performance", "strict"), False, "strict=true"),
    (("configuration", "atmosphere", "bounds"), [45, -5, 40, 5], "outside"),
    (("configuration", "recorder", "schema_version"), "samples-v9", "samples-v10"),
])
def test_invalid_provenance_and_configuration_are_rejected(path, value, message):
    document = manifest()
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ManifestError, match=message):
        validate_manifest(document)


def test_profile_rejects_an_incoherent_plugin_combination():
    document = manifest("baseline-recorder")
    document["configuration"]["atmosphere"] = {
        "provider": "GFS", "dataset_id": "GFS-20250815T12",
        "bounds": [40.0, -5.0, 45.0, 5.0], "strict": True,
        "interpolation": False,
    }
    with pytest.raises(ManifestError, match="profile .* requires"):
        validate_manifest(document)


def test_referenced_external_resource_must_exist_and_have_the_right_kind():
    document = manifest()
    identifier = document["configuration"]["performance"]["dataset_id"]
    document["external_resources"][identifier]["kind"] = "weather-cache"
    with pytest.raises(ManifestError, match="must have kind 'licensed-bada'"):
        validate_manifest(document)


def test_tracked_example_is_valid():
    with open("research-run.example.json", encoding="utf-8") as stream:
        validate_manifest(json.load(stream))
