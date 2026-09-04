"""Validation for portable research-run manifests used by integration gates."""

import datetime as dt
import math
import re


SCHEMA_VERSION = "research-run-v2"
PROFILES = {
    "baseline-recorder-free": ("OPENAP", "ISA", False),
    "baseline-recorder": ("OPENAP", "ISA", True),
    "meteo-recorder": ("OPENAP", "NWP", True),
    "pybada-recorder": ("PYBADA", "ISA", True),
    "combined-recorder": ("PYBADA", "NWP", True),
}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    """Raised when a run manifest is incomplete or internally inconsistent."""


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{label} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ManifestError(f"{label} must be finite and greater than zero")
    return float(value)


def validate_manifest(manifest):
    """Return *manifest* after validating the reproducibility contract."""
    root = _mapping(manifest, "manifest")
    required = {"schema_version", "revision", "experiment", "configuration"}
    missing = sorted(required - root.keys())
    if missing:
        raise ManifestError(f"manifest lacks required fields: {', '.join(missing)}")
    if root["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {root['schema_version']!r}"
        )

    revision = _mapping(root["revision"], "revision")
    for field in ("commit", "upstream_base"):
        if not COMMIT_RE.fullmatch(str(revision.get(field, ""))):
            raise ManifestError(f"revision.{field} must be a full lowercase Git hash")
    if not isinstance(revision.get("working_tree_dirty"), bool):
        raise ManifestError("revision.working_tree_dirty must be boolean")

    experiment = _mapping(root["experiment"], "experiment")
    scenario = experiment.get("scenario")
    if not isinstance(scenario, str) or not scenario.endswith(".scn"):
        raise ManifestError("experiment.scenario must name a .scn file")
    try:
        timestamp = dt.datetime.fromisoformat(experiment.get("simulation_utc", ""))
    except (TypeError, ValueError) as exc:
        raise ManifestError("experiment.simulation_utc must be ISO-8601") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ManifestError("experiment.simulation_utc must include a UTC offset")
    timestep = _positive_number(experiment.get("timestep_s"), "experiment.timestep_s")
    duration = _positive_number(experiment.get("duration_s"), "experiment.duration_s")
    steps = duration / timestep
    if not math.isclose(steps, round(steps), rel_tol=0.0, abs_tol=1e-9):
        raise ManifestError("experiment.duration_s must contain a whole number of timesteps")
    if isinstance(experiment.get("random_seed"), bool) or not isinstance(
            experiment.get("random_seed"), int):
        raise ManifestError("experiment.random_seed must be an integer")

    config = _mapping(root["configuration"], "configuration")
    profile = config.get("profile")
    if profile not in PROFILES:
        raise ManifestError(f"configuration.profile must be one of {sorted(PROFILES)}")
    performance = _mapping(config.get("performance"), "configuration.performance")
    atmosphere = _mapping(config.get("atmosphere"), "configuration.atmosphere")
    recorder = _mapping(config.get("recorder"), "configuration.recorder")

    performance_kind = performance.get("provider")
    if performance_kind not in ("OPENAP", "PYBADA"):
        raise ManifestError("performance.provider must be OPENAP or PYBADA")
    if performance_kind == "PYBADA":
        if str(performance.get("family")) not in ("3", "4"):
            raise ManifestError("PyBADA family must be 3 or 4")
        if performance.get("dynamics_mode") not in ("KINEMATIC", "TEM"):
            raise ManifestError("PyBADA dynamics_mode must be KINEMATIC or TEM")
        if performance.get("strict") is not True:
            raise ManifestError("PyBADA result-producing runs require strict=true")
        if not performance.get("dataset_id"):
            raise ManifestError("PyBADA performance requires dataset_id provenance")
        if not performance.get("version"):
            raise ManifestError("PyBADA performance requires dataset version provenance")
        aircraft = performance.get("aircraft")
        if (not isinstance(aircraft, list) or not aircraft or
                any(not isinstance(item, str) or not item for item in aircraft)):
            raise ManifestError("PyBADA performance requires a non-empty aircraft list")

    atmosphere_kind = atmosphere.get("provider")
    atmosphere_class = "ISA" if atmosphere_kind == "ISA" else "NWP"
    if atmosphere_kind not in ("ISA", "ERA5", "GFS"):
        raise ManifestError("atmosphere.provider must be ISA, ERA5, or GFS")
    if atmosphere_class == "NWP":
        if atmosphere.get("strict") is not True:
            raise ManifestError("NWP result-producing runs require strict=true")
        if not isinstance(atmosphere.get("interpolation"), bool):
            raise ManifestError("NWP interpolation policy must be boolean")
        bounds = atmosphere.get("bounds")
        if (not isinstance(bounds, list) or len(bounds) != 4 or
                any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) for value in bounds)):
            raise ManifestError("NWP bounds must contain four finite numbers")
        south, west, north, east = bounds
        if not (-90 <= south < north <= 90 and
                -180 <= west <= 180 and -180 <= east <= 180):
            raise ManifestError("NWP bounds are outside the supported latitude/longitude range")
        if not atmosphere.get("dataset_id"):
            raise ManifestError("NWP atmosphere requires dataset_id provenance")

    enabled = recorder.get("enabled")
    if not isinstance(enabled, bool):
        raise ManifestError("recorder.enabled must be boolean")
    if enabled:
        if recorder.get("schema_version") != "samples-v10":
            raise ManifestError("enabled recorder must declare samples-v10")
        _positive_number(recorder.get("interval_s"), "recorder.interval_s")

    expected_performance, expected_atmosphere, expected_recorder = PROFILES[profile]
    actual = (performance_kind, atmosphere_class, enabled)
    expected = (expected_performance, expected_atmosphere, expected_recorder)
    if actual != expected:
        raise ManifestError(
            f"profile {profile!r} requires performance/atmosphere/recorder "
            f"{expected}, got {actual}"
        )

    resources = _mapping(root.get("external_resources", {}), "external_resources")
    resource_references = []
    if performance_kind == "PYBADA":
        resource_references.append((performance["dataset_id"], "licensed-bada"))
    if atmosphere_class == "NWP":
        resource_references.append((atmosphere["dataset_id"], "weather-cache"))
    for identifier, kind in resource_references:
        resource = _mapping(resources.get(identifier),
                            f"external_resources.{identifier}")
        if resource.get("kind") != kind:
            raise ManifestError(
                f"external resource {identifier!r} must have kind {kind!r}"
            )
        if not isinstance(resource.get("path"), str) or not resource["path"]:
            raise ManifestError(f"external resource {identifier!r} requires a path")

    evidence = _mapping(root.get("evidence", {}), "evidence")
    if evidence:
        if evidence.get("status") not in ("planned", "valid", "invalid"):
            raise ManifestError("evidence.status must be planned, valid, or invalid")
        validators = evidence.get("validators")
        if (not isinstance(validators, list) or
                any(not isinstance(item, str) or not item for item in validators)):
            raise ManifestError("evidence.validators must be a list of paths")
    return manifest
