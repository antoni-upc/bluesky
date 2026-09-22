#!/usr/bin/env python3
"""Run one reproducible research-plugin profile and emit external evidence."""

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky.core import simtime
from bluesky.core.entity import getproxied
from bluesky.core.plugin import Plugin
from bluesky.tools.aero import ft, kts
from bluesky.traffic.windsim import WindSim


ROOT = Path(__file__).resolve().parents[2]
UTC = "2025-08-15T12:00:00+00:00"
SIMDT_S = 0.5
STEPS = 240
BOUNDS = (40.0, -5.0, 45.0, 5.0)
ATMOSPHERE_PLUGINS = {"ERA5": "WINDECMWF", "GFS": "WINDGFS"}
SAMPLE_FIELDS = (
    "lat", "lon", "alt", "pressure_alt", "tas", "cas", "M", "vs",
    "hdg", "trk", "p", "rho", "Temp", "windnorth", "windeast",
    "atmos_source", "atmos_valid", "atmos_dataset_time",
    "atmos_fallback_reason",
)
PERFORMANCE_FIELDS = ("mass", "thrust", "drag", "fuelflow", "phase")
RECORDER_SAMPLE_FIELDS = {
    "lat_deg": "lat", "lon_deg": "lon", "geometric_alt_m": "alt",
    "pressure_alt_m": "pressure_alt", "tas_m_s": "tas", "cas_m_s": "cas",
    "mach": "M", "vertical_speed_m_s": "vs", "heading_deg": "hdg",
    "track_deg": "trk", "temperature_k": "Temp", "pressure_pa": "p",
    "density_kg_m3": "rho", "wind_north_m_s": "windnorth",
    "wind_east_m_s": "windeast", "mass_kg": "mass", "thrust_n": "thrust",
    "drag_n": "drag", "fuel_flow_kg_s": "fuelflow",
}


def scalar(values):
    value = np.asarray(values)[0]
    return value.item() if hasattr(value, "item") else value


def external_sample(step):
    sample = {"step": step, "sim_time_s": step * SIMDT_S}
    sample.update({name: scalar(getattr(bs.traf, name)) for name in SAMPLE_FIELDS})
    sample.update({name: scalar(getattr(bs.traf.perf, name))
                   for name in PERFORMANCE_FIELDS})
    return sample


def checksum(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_plugin(name):
    success, message = Plugin.load(name)
    if not success:
        raise RuntimeError(message)


def load_atmosphere(provider):
    bs.settings.meteo_strict = True
    bs.settings.meteo_time_autoupdate = True
    bs.settings.meteo_time_interpolation = False
    bs.settings.era5_cache_path = str(ROOT / "cache" / "weather" / "era5")
    bs.settings.gfs_cache_path = str(ROOT / "cache" / "weather" / "gfs")
    require_plugin(ATMOSPHERE_PLUGINS[provider])
    implementation = getproxied(WindSim.instance())
    implementation.strict = True
    success, message = implementation.load(*BOUNDS)
    if not success:
        raise RuntimeError(message)
    return implementation


def validate_recorder(csv_path, provider, expected_rows):
    with csv_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != expected_rows:
        raise RuntimeError(f"Recorder wrote {len(rows)} rows; expected {expected_rows}")
    sources = {row["atmosphere_source"] for row in rows}
    if sources != {provider}:
        raise RuntimeError(f"Recorder atmosphere sources {sources!r}; expected {provider!r}")
    if any(row["atmosphere_valid"] != "True" for row in rows):
        raise RuntimeError("Recorder contains an invalid atmosphere sample")
    if any(row["fallback_reason"] for row in rows):
        raise RuntimeError("Recorder contains an atmosphere fallback")
    if {row["performance_model"] for row in rows} != {"OpenAP"}:
        raise RuntimeError("Recorder did not use OpenAP exclusively")
    return rows


def compare_common_samples(rows, samples):
    """Require exact recorder/external agreement at shared simulation times."""
    samples_by_time = {sample["sim_time_s"]: sample for sample in samples}
    for index, row in enumerate(rows):
        sim_time = float(row["sim_time_s"])
        sample = samples_by_time.get(sim_time)
        if sample is None:
            raise RuntimeError(f"Recorder row {index} has no external sample at {sim_time}")
        for recorder_field, sample_field in RECORDER_SAMPLE_FIELDS.items():
            recorded = float(row[recorder_field])
            external = float(sample[sample_field])
            if recorded != external:
                raise RuntimeError(
                    f"Recorder row {index} field {recorder_field} differs from external "
                    f"sample: {recorded!r} != {external!r}"
                )


def run(provider, workdir, output):
    workdir.mkdir(parents=True, exist_ok=True)
    bs.init(mode="sim", detached=True, workdir=workdir)
    simtime.setdt(SIMDT_S)
    bs.sim.simdt = SIMDT_S
    bs.sim.utc = dt.datetime.fromisoformat(UTC)

    atmosphere = load_atmosphere(provider)
    require_plugin("RESEARCHRECORDER")
    from bluesky.plugins import research_recorder

    success, message = research_recorder.record("INTERVAL", str(SIMDT_S))
    if not success:
        raise RuntimeError(message)
    csv_name = f"openap-{provider.lower()}-recorder.csv"
    success, message = research_recorder.record("START", csv_name)
    if not success:
        raise RuntimeError(message)

    created = bs.traf.cre(
        "MATRIX1", "A320", 41.30, 2.10, 73.0, 10_000.0 * ft, 250.0 * kts
    )
    if created is not True and (not created or created[0] is not True):
        raise RuntimeError(f"Aircraft creation failed: {created}")
    bs.traf.ap.selspdcmd(0, 280.0 * kts)
    bs.traf.ap.selhdgcmd(0, 101.0)
    bs.traf.ap.selaltcmd(0, 12_000.0 * ft, 1_500.0 * ft / 60.0)

    samples = [external_sample(0)]
    for step in range(1, STEPS + 1):
        bs.sim.step()
        samples.append(external_sample(step))

    paths = research_recorder.recorder.stop()
    if paths is None:
        raise RuntimeError("Recorder did not finalize evidence")
    rows = validate_recorder(paths[0], provider, STEPS)
    compare_common_samples(rows, samples)
    if {sample["atmos_source"] for sample in samples} != {provider}:
        raise RuntimeError("External samples do not exclusively use the selected atmosphere")
    if not all(sample["atmos_valid"] for sample in samples):
        raise RuntimeError("External samples include invalid atmosphere data")
    if any(sample["atmos_fallback_reason"] for sample in samples):
        raise RuntimeError("External samples include an atmosphere fallback")

    evidence = {
        "profile": "meteo-recorder",
        "configuration": {"performance": "OPENAP", "atmosphere": provider,
                          "recorder": True},
        "experiment": {"simulation_utc": UTC, "timestep_s": SIMDT_S,
                       "duration_s": STEPS * SIMDT_S, "steps": STEPS,
                       "bounds": list(BOUNDS)},
        "weather": {"dataset_time": atmosphere.active_slot,
                    "cache": str(atmosphere.cache), "fallback_counts": atmosphere.failure_counts},
        "external_samples": {"count": len(samples), "sha256": checksum(samples),
                             "samples": samples},
        "recorder": {"rows": len(rows), "common_samples_exact": True,
                     "csv": str(paths[0]),
                     "metadata": str(paths[1]),
                     "events": str(research_recorder.recorder.event_path)},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{provider} PROFILE PASSED: {len(samples)} external samples, {len(rows)} recorder rows")
    print(f"EXTERNAL SHA-256: {evidence['external_samples']['sha256']}")
    print(f"EVIDENCE: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--atmosphere", choices=tuple(ATMOSPHERE_PLUGINS), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.atmosphere, args.workdir.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
